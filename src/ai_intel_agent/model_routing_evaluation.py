from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from importlib.resources import files
from pathlib import Path
from statistics import median
from string import Formatter
from time import perf_counter, sleep
from typing import Literal, Protocol

import httpx
from dotenv import load_dotenv

TaskClass = Literal[
    "classification",
    "chinese_summarization",
    "claim_verification",
    "simple_question",
    "complex_reasoning",
]
CriticalGate = Literal["structure", "factual", "citation", "abstention"]
ProviderName = Literal["deepseek", "kimi"]

TASK_CLASSES: tuple[TaskClass, ...] = (
    "classification",
    "chinese_summarization",
    "claim_verification",
    "simple_question",
    "complex_reasoning",
)
EVALUATION_VERSION = "model-routing-evaluation-2026-08-12.v1"
CRITICAL_GATES: tuple[CriticalGate, ...] = (
    "structure",
    "factual",
    "citation",
    "abstention",
)


@dataclass(frozen=True)
class EvidenceFixture:
    identifier: str
    source_url: str
    text: str


@dataclass(frozen=True)
class GoldCriteria:
    expected_label: str | None
    expected_verdict: str | None
    expected_abstained: bool
    expected_facts: dict[str, str]
    required_fact_groups: tuple[tuple[str, ...], ...]
    rejection_patterns: tuple[str, ...]
    required_citations: tuple[str, ...]
    allowed_citations: tuple[str, ...]
    require_chinese: bool
    minimum_answer_characters: int
    maximum_answer_characters: int


@dataclass(frozen=True)
class EvaluationCase:
    identifier: str
    task_class: TaskClass
    instruction: str
    context: tuple[EvidenceFixture, ...]
    question: str
    critical_gates: tuple[CriticalGate, ...]
    gold: GoldCriteria


@dataclass(frozen=True)
class EvaluationCorpus:
    version: str
    review_state: str
    description: str
    approved_by: str | None
    approved_at: str | None
    approved_cases_sha256: str | None
    approval_method: str
    approval_sources: tuple[ApprovalSource, ...]
    content_sha256: str
    cases_sha256: str
    cases: tuple[EvaluationCase, ...]


@dataclass(frozen=True)
class ApprovalSource:
    url: str
    updated_at: str


@dataclass(frozen=True)
class EvaluationOutput:
    answer: str
    label: str | None
    verdict: Literal["supported", "contradicted", "insufficient"] | None
    abstained: bool
    citations: list[str]
    facts: dict[str, str]


@dataclass(frozen=True)
class GateResult:
    passed: bool
    detail: str


@dataclass(frozen=True)
class CaseScore:
    case_identifier: str
    task_class: TaskClass
    gates: dict[CriticalGate, GateResult]
    quality_score: float
    passed_all_critical_gates: bool
    output: EvaluationOutput | None


@dataclass(frozen=True)
class CandidateTaskSummary:
    candidate_identifier: str
    task_class: TaskClass
    passed_all_critical_gates: bool
    quality_score: float
    median_latency_ms: int
    estimated_cost_usd: float
    cases_passed: int
    cases_total: int


class ModelEvaluationConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class ModelCandidate:
    identifier: str
    provider: ProviderName
    model_id: str
    model_version: str
    base_url: str
    api_key_environment_variable: str
    native_currency: Literal["USD", "CNY"]
    native_units_per_usd: float
    cache_hit_per_million: float
    cache_miss_per_million: float
    output_per_million: float
    pricing_source: str
    thinking_task_classes: tuple[TaskClass, ...]
    maximum_output_tokens: int


@dataclass(frozen=True)
class CandidateConfiguration:
    version: str
    pricing_checked_at: str
    conversion_note: str
    candidates: tuple[ModelCandidate, ...]


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    backoff_seconds: tuple[float, ...]
    retry_status_codes: tuple[int, ...]


@dataclass(frozen=True)
class BudgetPolicy:
    maximum_evaluation_cost_usd: float
    monthly_external_api_cap_usd: float


@dataclass(frozen=True)
class ModelRoutingProtocol:
    version: str
    content_sha256: str
    prompt_version: str
    schema_version: str
    system_prompt_template: str
    user_prompt_template: str
    output_schema: dict[str, object]
    retry_policy: RetryPolicy
    budget_policy: BudgetPolicy
    route_eligibility: str
    route_rank_by: tuple[str, ...]
    task_maximum_output_tokens: dict[TaskClass, int]


@dataclass(frozen=True, repr=False)
class ModelEvaluationCredentials:
    values: Mapping[str, str]

    @classmethod
    def from_environment(
        cls,
        configuration: CandidateConfiguration | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> ModelEvaluationCredentials:
        configuration = configuration or load_candidate_configuration()
        if environment is None:
            load_dotenv(".env")
            environment = os.environ
        variable_names = sorted(
            {candidate.api_key_environment_variable for candidate in configuration.candidates}
        )
        values = {name: environment.get(name, "").strip() for name in variable_names}
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise ModelEvaluationConfigurationError(
                "live model routing evaluation requires: " + ", ".join(missing)
            )
        return cls(values=values)

    def for_candidate(self, candidate: ModelCandidate) -> str:
        try:
            return self.values[candidate.api_key_environment_variable]
        except KeyError as error:
            raise ModelEvaluationConfigurationError(
                "missing credential mapping for "
                f"{candidate.api_key_environment_variable}"
            ) from error


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class CostEstimate:
    native_currency: str
    cache_hit_native: float
    cache_miss_native: float
    output_native: float
    total_native: float
    total_usd: float


@dataclass(frozen=True)
class ProviderResponse:
    content: str
    returned_model_id: str
    finish_reason: str
    usage: TokenUsage
    latency_ms: int
    attempts: int = 1


class ModelEvaluationClient(Protocol):
    def complete(
        self,
        candidate: ModelCandidate,
        case: EvaluationCase,
    ) -> ProviderResponse: ...


@dataclass(frozen=True)
class CaseMeasurement:
    candidate_identifier: str
    provider: ProviderName
    model_id: str
    returned_model_id: str
    case_identifier: str
    task_class: TaskClass
    score: CaseScore
    latency_ms: int
    usage: TokenUsage
    cost: CostEstimate
    finish_reason: str
    attempts: int
    error: str | None


@dataclass(frozen=True)
class ModelRoutingEvaluation:
    evaluation_version: str
    candidate_configuration_version: str
    pricing_checked_at: str
    corpus_review_state: str
    corpus_content_sha256: str
    corpus_approved_by: str
    corpus_approved_at: str
    corpus_cases_sha256: str
    corpus_approval_method: str
    corpus_approval_sources: tuple[ApprovalSource, ...]
    protocol_version: str
    protocol_content_sha256: str
    prompt_version: str
    schema_version: str
    maximum_evaluation_cost_usd: float
    worst_case_reserved_usd: float
    estimated_actual_cost_usd: float
    run_at: datetime
    measurements: tuple[CaseMeasurement, ...]
    summaries: tuple[CandidateTaskSummary, ...]
    recommendations: dict[TaskClass, CandidateTaskSummary | None]


ProgressCallback = Callable[[int, int, str], None]


def load_evaluation_corpus() -> EvaluationCorpus:
    resource = files("ai_intel_agent").joinpath(
        "data/model_routing_evaluation.v1.json"
    )
    raw = resource.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    cases = tuple(
        EvaluationCase(
            identifier=item["identifier"],
            task_class=item["task_class"],
            instruction=item["instruction"],
            context=tuple(EvidenceFixture(**evidence) for evidence in item["context"]),
            question=item["question"],
            critical_gates=tuple(item["critical_gates"]),
            gold=GoldCriteria(
                **{
                    **item["gold"],
                    "required_fact_groups": tuple(
                        tuple(group) for group in item["gold"]["required_fact_groups"]
                    ),
                    "rejection_patterns": tuple(item["gold"]["rejection_patterns"]),
                    "required_citations": tuple(item["gold"]["required_citations"]),
                    "allowed_citations": tuple(item["gold"]["allowed_citations"]),
                }
            ),
        )
        for item in payload["cases"]
    )
    invalid_gates = [
        case.identifier
        for case in cases
        if tuple(case.critical_gates) != CRITICAL_GATES
    ]
    if invalid_gates:
        raise ModelEvaluationConfigurationError(
            "every evaluation case must apply all critical gates: "
            + ", ".join(invalid_gates)
        )
    approval_record = payload["approval_record"]
    cases_sha256 = sha256(
        json.dumps(
            payload["cases"],
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    review_state = payload["review_state"]
    approved_by = approval_record["approved_by"]
    approved_at = approval_record["approved_at"]
    approved_cases_sha256 = approval_record["approved_cases_sha256"]
    if review_state == "human-approved" and (
        not isinstance(approved_by, str)
        or not approved_by.strip()
        or not isinstance(approved_at, str)
        or not approved_at.strip()
        or approved_cases_sha256 != cases_sha256
    ):
        raise ModelEvaluationConfigurationError(
            "human-approved corpus metadata must identify the approver, time, and exact cases SHA-256"
        )
    if review_state not in {"awaiting-human-approval", "human-approved"}:
        raise ModelEvaluationConfigurationError("unsupported corpus review state")
    return EvaluationCorpus(
        version=payload["version"],
        review_state=review_state,
        description=payload["description"],
        approved_by=approved_by,
        approved_at=approved_at,
        approved_cases_sha256=approved_cases_sha256,
        approval_method=approval_record["method"],
        approval_sources=tuple(
            ApprovalSource(**item) for item in approval_record["sources"]
        ),
        content_sha256=sha256(raw).hexdigest(),
        cases_sha256=cases_sha256,
        cases=cases,
    )


def load_candidate_configuration() -> CandidateConfiguration:
    resource = files("ai_intel_agent").joinpath(
        "data/model_routing_candidates.v1.json"
    )
    payload = json.loads(resource.read_text(encoding="utf-8"))
    conversions = payload["usd_conversion"]
    candidates = tuple(
        ModelCandidate(
            **{
                **item,
                "native_units_per_usd": conversions[item["native_currency"]],
                "thinking_task_classes": tuple(item["thinking_task_classes"]),
            }
        )
        for item in payload["candidates"]
    )
    return CandidateConfiguration(
        version=payload["version"],
        pricing_checked_at=payload["pricing_checked_at"],
        conversion_note=conversions["note"],
        candidates=candidates,
    )


def _validate_protocol_output_schema(schema: object) -> None:
    if not isinstance(schema, dict):
        raise ModelEvaluationConfigurationError("output schema must be an object")
    required_keys = schema.get("required_keys")
    fields = schema.get("fields")
    if (
        not isinstance(required_keys, list)
        or not all(isinstance(item, str) for item in required_keys)
        or not isinstance(fields, dict)
        or set(required_keys) != set(fields)
        or set(required_keys) != set(EvaluationOutput.__dataclass_fields__)
        or schema.get("additional_properties") is not False
    ):
        raise ModelEvaluationConfigurationError(
            "output schema must define exact required fields"
        )
    supported_types = {"array", "boolean", "null", "object", "string"}
    for field_name, field_schema in fields.items():
        if not isinstance(field_name, str) or not isinstance(field_schema, dict):
            raise ModelEvaluationConfigurationError("invalid output field schema")
        types = field_schema.get("types")
        if (
            not isinstance(types, list)
            or not types
            or not set(types) <= supported_types
        ):
            raise ModelEvaluationConfigurationError(
                f"unsupported output types for {field_name}"
            )
        if not isinstance(field_schema.get("description"), str):
            raise ModelEvaluationConfigurationError(
                f"output field {field_name} must have a versioned description"
            )


def _validate_prompt_template(
    template: object,
    expected_fields: set[str],
    label: str,
) -> None:
    if not isinstance(template, str):
        raise ModelEvaluationConfigurationError(
            f"{label} prompt template must be a string"
        )
    actual_fields = {
        field_name
        for _, field_name, _, _ in Formatter().parse(template)
        if field_name is not None
    }
    if actual_fields != expected_fields:
        raise ModelEvaluationConfigurationError(
            f"{label} prompt template placeholders must be {sorted(expected_fields)}"
        )


def _parse_evaluation_output(
    raw_content: str,
    schema: dict[str, object],
) -> EvaluationOutput:
    payload = json.loads(raw_content)
    if not isinstance(payload, dict):
        raise TypeError("structured output must be an object")
    required_keys = schema["required_keys"]
    fields = schema["fields"]
    if not isinstance(required_keys, list) or not isinstance(fields, dict):
        raise TypeError("invalid versioned output schema")
    if set(payload) != set(required_keys):
        raise ValueError("structured output keys do not match the versioned schema")
    for field_name, field_schema in fields.items():
        if not isinstance(field_schema, dict):
            raise TypeError("invalid versioned field schema")
        value = payload[field_name]
        types = field_schema["types"]
        if not isinstance(types, list) or not _value_matches_types(value, types):
            raise TypeError(f"{field_name} has the wrong type")
        allowed_values = field_schema.get("allowed_values")
        if isinstance(allowed_values, list) and value not in allowed_values:
            raise ValueError(f"{field_name} is outside the allowed values")
        item_types = field_schema.get("item_types")
        if isinstance(item_types, list) and (
            not isinstance(value, list)
            or not all(_value_matches_types(item, item_types) for item in value)
        ):
            raise TypeError(f"{field_name} contains an item with the wrong type")
        key_types = field_schema.get("key_types")
        value_types = field_schema.get("value_types")
        if isinstance(key_types, list) and isinstance(value_types, list) and (
            not isinstance(value, dict)
            or not all(
                _value_matches_types(key, key_types)
                and _value_matches_types(item, value_types)
                for key, item in value.items()
            )
        ):
            raise TypeError(f"{field_name} contains an invalid entry")
    return EvaluationOutput(**payload)


def _value_matches_types(value: object, allowed_types: list[object]) -> bool:
    actual_type = (
        "null"
        if value is None
        else "boolean"
        if isinstance(value, bool)
        else "string"
        if isinstance(value, str)
        else "array"
        if isinstance(value, list)
        else "object"
        if isinstance(value, dict)
        else "unsupported"
    )
    return actual_type in allowed_types


def load_protocol_configuration() -> ModelRoutingProtocol:
    resource = files("ai_intel_agent").joinpath(
        "data/model_routing_protocol.v1.json"
    )
    raw = resource.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    retry = payload["retry_policy"]
    budget = payload["budget_policy"]
    route_policy = payload["route_selection_policy"]
    prompt_templates = payload["prompt_templates"]
    _validate_protocol_output_schema(payload["output_schema"])
    _validate_prompt_template(
        prompt_templates["system"],
        {"prompt_version", "schema_version", "output_schema"},
        "system",
    )
    _validate_prompt_template(
        prompt_templates["user"],
        {"task", "evidence", "question", "fact_keys"},
        "user",
    )
    task_maximum_output_tokens = {
        task_class: int(maximum_tokens)
        for task_class, maximum_tokens in payload[
            "task_maximum_output_tokens"
        ].items()
    }
    if set(task_maximum_output_tokens) != set(TASK_CLASSES):
        raise ModelEvaluationConfigurationError(
            "protocol must define an output-token budget for every task class"
        )
    max_attempts = int(retry["max_attempts"])
    backoff_seconds = tuple(float(value) for value in retry["backoff_seconds"])
    if max_attempts < 1 or len(backoff_seconds) != max_attempts - 1:
        raise ModelEvaluationConfigurationError(
            "retry backoff count must equal max_attempts minus one"
        )
    maximum_cost = float(budget["maximum_evaluation_cost_usd"])
    monthly_cap = float(budget["monthly_external_api_cap_usd"])
    if maximum_cost <= 0 or maximum_cost > monthly_cap:
        raise ModelEvaluationConfigurationError(
            "evaluation cost budget must be positive and within the monthly API cap"
        )
    expected_rank_by = (
        "quality_score descending",
        "estimated_cost_usd ascending",
        "median_latency_ms ascending",
        "candidate_identifier ascending",
    )
    route_rank_by = tuple(route_policy["rank_by"])
    if route_rank_by != expected_rank_by:
        raise ModelEvaluationConfigurationError(
            "unsupported route selection ranking policy"
        )
    return ModelRoutingProtocol(
        version=payload["version"],
        content_sha256=sha256(raw).hexdigest(),
        prompt_version=payload["prompt_version"],
        schema_version=payload["schema_version"],
        system_prompt_template=prompt_templates["system"],
        user_prompt_template=prompt_templates["user"],
        output_schema=payload["output_schema"],
        retry_policy=RetryPolicy(
            max_attempts=max_attempts,
            backoff_seconds=backoff_seconds,
            retry_status_codes=tuple(int(value) for value in retry["retry_status_codes"]),
        ),
        budget_policy=BudgetPolicy(
            maximum_evaluation_cost_usd=maximum_cost,
            monthly_external_api_cap_usd=monthly_cap,
        ),
        route_eligibility=route_policy["eligibility"],
        route_rank_by=route_rank_by,
        task_maximum_output_tokens=task_maximum_output_tokens,
    )


def estimate_cost_usd(candidate: ModelCandidate, usage: TokenUsage) -> CostEstimate:
    cached_tokens = min(usage.input_tokens, usage.cached_input_tokens)
    uncached_tokens = max(0, usage.input_tokens - cached_tokens)
    cache_hit_native = cached_tokens * candidate.cache_hit_per_million / 1_000_000
    cache_miss_native = uncached_tokens * candidate.cache_miss_per_million / 1_000_000
    output_native = usage.output_tokens * candidate.output_per_million / 1_000_000
    total_native = cache_hit_native + cache_miss_native + output_native
    return CostEstimate(
        native_currency=candidate.native_currency,
        cache_hit_native=cache_hit_native,
        cache_miss_native=cache_miss_native,
        output_native=output_native,
        total_native=total_native,
        total_usd=total_native / candidate.native_units_per_usd,
    )


def maximum_request_cost_usd(
    candidate: ModelCandidate,
    case: EvaluationCase,
    protocol: ModelRoutingProtocol,
) -> float:
    messages = _messages_for_case(case, protocol)
    input_token_upper_bound = len(
        json.dumps(messages, ensure_ascii=False).encode("utf-8")
    )
    output_token_limit = min(
        candidate.maximum_output_tokens,
        protocol.task_maximum_output_tokens[case.task_class],
    )
    native_cost = (
        input_token_upper_bound * candidate.cache_miss_per_million
        + output_token_limit * candidate.output_per_million
    ) / 1_000_000
    return native_cost / candidate.native_units_per_usd


class HttpModelEvaluationClient:
    def __init__(
        self,
        *,
        credentials: ModelEvaluationCredentials,
        protocol: ModelRoutingProtocol | None = None,
        http_client: httpx.Client | None = None,
        sleeper: Callable[[float], None] = sleep,
    ) -> None:
        self._credentials = credentials
        self._protocol = protocol or load_protocol_configuration()
        self._http_client = http_client or httpx.Client(timeout=180)
        self._sleeper = sleeper

    def complete(
        self,
        candidate: ModelCandidate,
        case: EvaluationCase,
    ) -> ProviderResponse:
        thinking_enabled = case.task_class in candidate.thinking_task_classes
        payload = {
            "model": candidate.model_id,
            "messages": _messages_for_case(case, self._protocol),
            "response_format": {"type": "json_object"},
            "thinking": {"type": "enabled" if thinking_enabled else "disabled"},
            "max_tokens": min(
                candidate.maximum_output_tokens,
                self._protocol.task_maximum_output_tokens[case.task_class],
            ),
            "stream": False,
        }
        started = perf_counter()
        attempts = 0
        while attempts < self._protocol.retry_policy.max_attempts:
            attempts += 1
            try:
                response = self._http_client.post(
                    f"{candidate.base_url.rstrip('/')}/chat/completions",
                    headers={
                        "Authorization": (
                            "Bearer " + self._credentials.for_candidate(candidate)
                        ),
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            except httpx.RequestError:
                if attempts >= self._protocol.retry_policy.max_attempts:
                    raise
                self._sleeper(
                    self._protocol.retry_policy.backoff_seconds[attempts - 1]
                )
                continue
            if (
                response.status_code
                not in self._protocol.retry_policy.retry_status_codes
                or attempts >= self._protocol.retry_policy.max_attempts
            ):
                break
            self._sleeper(
                self._protocol.retry_policy.backoff_seconds[attempts - 1]
            )
        latency_ms = round((perf_counter() - started) * 1000)
        response.raise_for_status()
        body = response.json()
        usage = body.get("usage") or {}
        input_tokens = int(usage.get("prompt_tokens", 0))
        cached_input_tokens = int(
            usage.get(
                "prompt_cache_hit_tokens",
                usage.get(
                    "cached_tokens",
                    (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
                ),
            )
        )
        choice = body["choices"][0]
        content = choice["message"].get("content")
        if not isinstance(content, str):
            content = ""
        return ProviderResponse(
            content=content,
            returned_model_id=str(body.get("model", candidate.model_id)),
            finish_reason=str(choice.get("finish_reason", "unknown")),
            usage=TokenUsage(
                input_tokens=input_tokens,
                cached_input_tokens=cached_input_tokens,
                output_tokens=int(usage.get("completion_tokens", 0)),
            ),
            latency_ms=latency_ms,
            attempts=attempts,
        )


def run_model_routing_evaluation(
    output: Path,
    *,
    client: ModelEvaluationClient | None = None,
    corpus: EvaluationCorpus | None = None,
    configuration: CandidateConfiguration | None = None,
    protocol: ModelRoutingProtocol | None = None,
    now: datetime | None = None,
    progress: ProgressCallback | None = None,
) -> ModelRoutingEvaluation:
    corpus = corpus or load_evaluation_corpus()
    if (
        corpus.review_state != "human-approved"
        or corpus.approved_by is None
        or corpus.approved_at is None
        or corpus.approved_cases_sha256 != corpus.cases_sha256
    ):
        raise ModelEvaluationConfigurationError(
            "model routing evaluation requires administrator approval of the exact frozen corpus"
        )
    configuration = configuration or load_candidate_configuration()
    protocol = protocol or load_protocol_configuration()
    client = client or HttpModelEvaluationClient(
        credentials=ModelEvaluationCredentials.from_environment(
            configuration=configuration
        ),
        protocol=protocol,
    )
    run_at = now or datetime.now(UTC)
    total = len(configuration.candidates) * len(corpus.cases)
    worst_case_reserved_usd = sum(
        maximum_request_cost_usd(candidate, case, protocol)
        * protocol.retry_policy.max_attempts
        for candidate in configuration.candidates
        for case in corpus.cases
    )
    if worst_case_reserved_usd > protocol.budget_policy.maximum_evaluation_cost_usd:
        raise ModelEvaluationConfigurationError(
            "worst-case evaluation cost "
            f"${worst_case_reserved_usd:.6f} exceeds the versioned per-run budget "
            f"${protocol.budget_policy.maximum_evaluation_cost_usd:.6f}"
        )
    measurements: list[CaseMeasurement] = []

    for candidate in configuration.candidates:
        for case in corpus.cases:
            try:
                response = client.complete(candidate, case)
                score = score_response(case, response.content, protocol=protocol)
                if response.finish_reason != "stop":
                    score = _fail_structure_for_finish_reason(
                        score, response.finish_reason
                    )
                cost = estimate_cost_usd(candidate, response.usage)
                measurement = CaseMeasurement(
                    candidate_identifier=candidate.identifier,
                    provider=candidate.provider,
                    model_id=candidate.model_id,
                    returned_model_id=response.returned_model_id,
                    case_identifier=case.identifier,
                    task_class=case.task_class,
                    score=score,
                    latency_ms=response.latency_ms,
                    usage=response.usage,
                    cost=cost,
                    finish_reason=response.finish_reason,
                    attempts=response.attempts,
                    error=None,
                )
            except (
                httpx.HTTPError,
                KeyError,
                RuntimeError,
                TypeError,
                ValueError,
            ) as error:
                measurement = CaseMeasurement(
                    candidate_identifier=candidate.identifier,
                    provider=candidate.provider,
                    model_id=candidate.model_id,
                    returned_model_id="unavailable",
                    case_identifier=case.identifier,
                    task_class=case.task_class,
                    score=score_response(case, "", protocol=protocol),
                    latency_ms=0,
                    usage=TokenUsage(0, 0, 0),
                    cost=CostEstimate(
                        native_currency=candidate.native_currency,
                        cache_hit_native=0.0,
                        cache_miss_native=0.0,
                        output_native=0.0,
                        total_native=0.0,
                        total_usd=0.0,
                    ),
                    finish_reason="error",
                    attempts=0,
                    error=_safe_error_label(error),
                )
            measurements.append(measurement)
            if progress is not None:
                progress(
                    len(measurements),
                    total,
                    f"{candidate.identifier}/{case.identifier}",
                )

    summaries = _summarize_candidate_tasks(configuration, corpus, measurements)
    recommendations = select_task_routes(summaries)
    evaluation = ModelRoutingEvaluation(
        evaluation_version=corpus.version,
        candidate_configuration_version=configuration.version,
        pricing_checked_at=configuration.pricing_checked_at,
        corpus_review_state=corpus.review_state,
        corpus_content_sha256=corpus.content_sha256,
        corpus_approved_by=corpus.approved_by,
        corpus_approved_at=corpus.approved_at,
        corpus_cases_sha256=corpus.cases_sha256,
        corpus_approval_method=corpus.approval_method,
        corpus_approval_sources=corpus.approval_sources,
        protocol_version=protocol.version,
        protocol_content_sha256=protocol.content_sha256,
        prompt_version=protocol.prompt_version,
        schema_version=protocol.schema_version,
        maximum_evaluation_cost_usd=(
            protocol.budget_policy.maximum_evaluation_cost_usd
        ),
        worst_case_reserved_usd=worst_case_reserved_usd,
        estimated_actual_cost_usd=sum(
            measurement.cost.total_usd for measurement in measurements
        ),
        run_at=run_at,
        measurements=tuple(measurements),
        summaries=summaries,
        recommendations=recommendations,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        render_model_routing_report(evaluation, configuration),
        encoding="utf-8",
    )
    return evaluation


def render_model_routing_report(
    evaluation: ModelRoutingEvaluation,
    configuration: CandidateConfiguration,
) -> str:
    lines = [
        "# DeepSeek and Kimi Model Routing Evaluation",
        "",
        f"- Evaluation version: `{evaluation.evaluation_version}`",
        (
            "- Candidate configuration version: "
            f"`{evaluation.candidate_configuration_version}`"
        ),
        f"- Corpus review state: `{evaluation.corpus_review_state}`",
        f"- Corpus SHA-256: `{evaluation.corpus_content_sha256}`",
        f"- Approved cases SHA-256: `{evaluation.corpus_cases_sha256}`",
        f"- Corpus approved by: `{evaluation.corpus_approved_by}`",
        f"- Corpus approved at: `{evaluation.corpus_approved_at}`",
        f"- Approval method: {evaluation.corpus_approval_method}",
        f"- Evaluation protocol version: `{evaluation.protocol_version}`",
        f"- Evaluation protocol SHA-256: `{evaluation.protocol_content_sha256}`",
        f"- Prompt version: `{evaluation.prompt_version}`",
        f"- Output schema version: `{evaluation.schema_version}`",
        (
            "- Route ranking: quality descending, cost ascending, latency "
            "ascending, stable candidate identifier"
        ),
        f"- Run at: `{evaluation.run_at.isoformat()}`",
        f"- Provider pricing checked at: `{evaluation.pricing_checked_at}`",
        (
            "- Worst-case reserved cost: "
            f"`${evaluation.worst_case_reserved_usd:.6f}` USD of "
            f"`${evaluation.maximum_evaluation_cost_usd:.6f}` per-run budget"
        ),
        f"- Estimated actual cost: `${evaluation.estimated_actual_cost_usd:.6f}` USD",
        "- Approval sources: "
        + ", ".join(
            f"[{source.url}]({source.url}) at `{source.updated_at}`"
            for source in evaluation.corpus_approval_sources
        ),
        "",
        "## Critical gates",
        "",
        (
            "Candidates are eligible only when every gate passes. Quality, "
            "latency, or cost never compensates for a failed gate."
        ),
        "",
        "- `structure`: strict JSON object with exactly the approved fields.",
        "- `factual`: bounded canonical facts, verdicts, labels, language, and contradiction patterns.",
        "- `citation`: every required Evidence identifier is present and no invented identifier appears.",
        "- `abstention`: the model answers or abstains exactly when the frozen case requires it.",
        "",
        "## Route recommendations",
        "",
        "| Task class | Selected candidate | Quality | Median latency | Estimated USD | Eligibility |",
        "| --- | --- | ---: | ---: | ---: | --- |",
    ]
    for task_class in TASK_CLASSES:
        recommendation = evaluation.recommendations[task_class]
        if recommendation is None:
            lines.append(
                f"| `{task_class}` | No eligible candidate | — | — | — | FAIL |"
            )
        else:
            lines.append(
                f"| `{task_class}` | `{recommendation.candidate_identifier}` | "
                f"{recommendation.quality_score:.1f} | "
                f"{recommendation.median_latency_ms} ms | "
                f"${recommendation.estimated_cost_usd:.6f} | PASS |"
            )

    lines.extend(
        [
            "",
            "## Candidate task comparison",
            "",
            "| Candidate | Task class | Critical gates | Cases | Quality | Median latency | Estimated USD |",
            "| --- | --- | --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for summary in evaluation.summaries:
        lines.append(
            f"| `{summary.candidate_identifier}` | `{summary.task_class}` | "
            f"{'PASS' if summary.passed_all_critical_gates else 'FAIL'} | "
            f"{summary.cases_passed}/{summary.cases_total} | "
            f"{summary.quality_score:.1f} | {summary.median_latency_ms} ms | "
            f"${summary.estimated_cost_usd:.6f} |"
        )

    lines.extend(
        [
            "",
            "## Case results",
            "",
            "| Candidate | Returned model | Case | Gates | Failure details | Quality | Latency | Attempts | Tokens in/cached/out | Cost | Finish |",
            "| --- | --- | --- | --- | --- | ---: | ---: | ---: | --- | ---: | --- |",
        ]
    )
    for measurement in evaluation.measurements:
        gate_text = ", ".join(
            f"{name}={'PASS' if result.passed else 'FAIL'}"
            for name, result in measurement.score.gates.items()
        )
        failed_details = "; ".join(
            result.detail.replace("|", "/")
            for result in measurement.score.gates.values()
            if not result.passed
        ) or "none"
        cost_text = (
            f"{measurement.cost.total_native:.6f} {measurement.cost.native_currency} "
            f"(${measurement.cost.total_usd:.6f})"
        )
        finish = measurement.finish_reason
        if measurement.error:
            finish = f"{finish}: {measurement.error}"
        lines.append(
            f"| `{measurement.candidate_identifier}` | `{measurement.returned_model_id}` | "
            f"`{measurement.case_identifier}` | {gate_text} | {failed_details} | "
            f"{measurement.score.quality_score:.1f} | "
            f"{measurement.latency_ms} ms | {measurement.attempts} | "
            f"{measurement.usage.input_tokens}/"
            f"{measurement.usage.cached_input_tokens}/{measurement.usage.output_tokens} | "
            f"{cost_text} | {finish} |"
        )

    lines.extend(
        [
            "",
            "## Versioned candidate configuration",
            "",
            "| Candidate | Provider model | Thinking tasks | Native prices per 1M tokens (hit/miss/output) | Source |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for candidate in configuration.candidates:
        thinking = ", ".join(candidate.thinking_task_classes) or "none"
        prices = (
            f"{candidate.cache_hit_per_million:g}/"
            f"{candidate.cache_miss_per_million:g}/"
            f"{candidate.output_per_million:g} {candidate.native_currency}"
        )
        lines.append(
            f"| `{candidate.identifier}` | `{candidate.model_id}` "
            f"(`{candidate.model_version}`) | {thinking} | {prices} | "
            f"[official pricing]({candidate.pricing_source}) |"
        )

    lines.extend(
        [
            "",
            "## Interpretation limits",
            "",
            "- This v1 corpus is an initial project-specific route smoke evaluation, not a general model leaderboard.",
            (
                "- Classification, Chinese summarization, Claim verification, and complex "
                "reasoning each have one case; simple questions have two. One failure therefore "
                "makes that candidate's whole task route ineligible."
            ),
            (
                "- The complex-reasoning case measures application of the approved routing "
                "policy, not general complex-reasoning ability."
            ),
            "- Recommendations apply only to the frozen corpus and versioned prompts/configuration above.",
            f"- {configuration.conversion_note}",
            "- Provider invoices, returned usage, model availability, and prices must be rechecked before reruns.",
            "- No evaluated model is connected to the production application by this command.",
            "",
        ]
    )
    return "\n".join(lines)


def score_response(
    case: EvaluationCase,
    raw_content: str,
    *,
    protocol: ModelRoutingProtocol | None = None,
) -> CaseScore:
    protocol = protocol or load_protocol_configuration()
    try:
        output = _parse_evaluation_output(raw_content, protocol.output_schema)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        gates = {
            gate: GateResult(
                passed=False,
                detail=(
                    f"invalid structured output: {type(error).__name__}"
                    if gate == "structure"
                    else "not scored because structured output was invalid"
                ),
            )
            for gate in case.critical_gates
        }
        return CaseScore(
            case_identifier=case.identifier,
            task_class=case.task_class,
            gates=gates,
            quality_score=0.0,
            passed_all_critical_gates=False,
            output=None,
        )

    factual_checks: list[tuple[bool, str]] = []
    gold = case.gold
    if gold.expected_label is not None:
        factual_checks.append(
            (output.label == gold.expected_label, f"label must be {gold.expected_label}")
        )
    if gold.expected_verdict is not None:
        factual_checks.append(
            (
                output.verdict == gold.expected_verdict,
                f"verdict must be {gold.expected_verdict}",
            )
        )
    facts_passed = set(output.facts) == set(gold.expected_facts) and all(
        _fact_value_matches(expected_value, output.facts[fact_name])
        for fact_name, expected_value in gold.expected_facts.items()
    )
    factual_checks.append(
        (
            facts_passed,
            (
                "canonical fact keys and bounded values must match: "
                f"expected={gold.expected_facts}, actual={output.facts}"
            ),
        )
    )

    answer_folded = _normalized_text(output.answer)
    for group in gold.required_fact_groups:
        factual_checks.append(
            (
                any(_normalized_text(term) in answer_folded for term in group),
                "required point: " + " | ".join(group),
            )
        )
    for pattern in gold.rejection_patterns:
        factual_checks.append(
            (
                re.search(pattern, output.answer, flags=re.IGNORECASE) is None,
                f"answer matched rejection pattern: {pattern}",
            )
        )
    factual_checks.append(
        (
            gold.minimum_answer_characters
            <= len(output.answer)
            <= gold.maximum_answer_characters,
            "answer length outside approved bounds",
        )
    )
    if gold.require_chinese:
        factual_checks.append(
            (_chinese_character_ratio(output.answer) >= 0.10, "answer must be Chinese")
        )

    factual_passed = all(passed for passed, _ in factual_checks)
    factual_failures = [detail for passed, detail in factual_checks if not passed]

    actual_citations = set(output.citations)
    required_citations = set(gold.required_citations)
    allowed_citations = set(gold.allowed_citations)
    citation_passed = required_citations <= actual_citations <= allowed_citations
    abstention_passed = output.abstained is gold.expected_abstained

    all_checks = [True]
    all_checks.extend(passed for passed, _ in factual_checks)
    all_checks.append(citation_passed)
    all_checks.append(abstention_passed)
    quality_score = round(100 * sum(all_checks) / len(all_checks), 1)

    possible_gates: dict[CriticalGate, GateResult] = {
        "structure": GateResult(True, "valid strict JSON output"),
        "factual": GateResult(
            factual_passed,
            "all approved facts satisfied"
            if factual_passed
            else "; ".join(factual_failures),
        ),
        "citation": GateResult(
            citation_passed,
            "citations match approved Evidence identifiers"
            if citation_passed
            else (
                f"required={sorted(required_citations)}, actual={sorted(actual_citations)}, "
                f"allowed={sorted(allowed_citations)}"
            ),
        ),
        "abstention": GateResult(
            abstention_passed,
            f"expected abstained={gold.expected_abstained}, actual={output.abstained}",
        ),
    }
    gates = possible_gates
    return CaseScore(
        case_identifier=case.identifier,
        task_class=case.task_class,
        gates=gates,
        quality_score=quality_score,
        passed_all_critical_gates=all(result.passed for result in gates.values()),
        output=output,
    )


def _fail_structure_for_finish_reason(
    score: CaseScore,
    finish_reason: str,
) -> CaseScore:
    gates = dict(score.gates)
    gates["structure"] = GateResult(
        passed=False,
        detail=f"provider finish reason must be stop, got {finish_reason}",
    )
    return replace(
        score,
        gates=gates,
        passed_all_critical_gates=False,
    )


def select_task_routes(
    summaries: tuple[CandidateTaskSummary, ...],
) -> dict[TaskClass, CandidateTaskSummary | None]:
    recommendations: dict[TaskClass, CandidateTaskSummary | None] = {}
    for task_class in TASK_CLASSES:
        eligible = [
            summary
            for summary in summaries
            if summary.task_class == task_class and summary.passed_all_critical_gates
        ]
        eligible.sort(
            key=lambda item: (
                -item.quality_score,
                item.estimated_cost_usd,
                item.median_latency_ms,
                item.candidate_identifier,
            )
        )
        recommendations[task_class] = eligible[0] if eligible else None
    return recommendations


def _summarize_candidate_tasks(
    configuration: CandidateConfiguration,
    corpus: EvaluationCorpus,
    measurements: list[CaseMeasurement],
) -> tuple[CandidateTaskSummary, ...]:
    summaries: list[CandidateTaskSummary] = []
    for candidate in configuration.candidates:
        for task_class in TASK_CLASSES:
            task_cases = [case for case in corpus.cases if case.task_class == task_class]
            items = [
                measurement
                for measurement in measurements
                if measurement.candidate_identifier == candidate.identifier
                and measurement.task_class == task_class
            ]
            latencies = [item.latency_ms for item in items if item.latency_ms > 0]
            cases_passed = sum(
                item.score.passed_all_critical_gates for item in items
            )
            summaries.append(
                CandidateTaskSummary(
                    candidate_identifier=candidate.identifier,
                    task_class=task_class,
                    passed_all_critical_gates=(
                        len(items) == len(task_cases)
                        and all(item.score.passed_all_critical_gates for item in items)
                    ),
                    quality_score=(
                        round(sum(item.score.quality_score for item in items) / len(items), 1)
                        if items
                        else 0.0
                    ),
                    median_latency_ms=round(median(latencies)) if latencies else 0,
                    estimated_cost_usd=sum(item.cost.total_usd for item in items),
                    cases_passed=cases_passed,
                    cases_total=len(task_cases),
                )
            )
    return tuple(summaries)


def _safe_error_label(error: Exception) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        return f"HTTP {error.response.status_code}"
    if isinstance(error, httpx.RequestError):
        return type(error).__name__
    return type(error).__name__


def _chinese_character_ratio(value: str) -> float:
    meaningful = [character for character in value if not character.isspace()]
    if not meaningful:
        return 0.0
    chinese = re.findall(r"[\u3400-\u9fff]", value)
    return len(chinese) / len(meaningful)


def _normalized_text(value: str) -> str:
    return re.sub(r"\s+", "", value.casefold())


def _fact_value_matches(expected: str, actual: str) -> bool:
    expected_folded = expected.casefold().strip()
    actual_folded = actual.casefold().strip()
    missing_values = {
        "",
        "missing",
        "not specified",
        "not provided",
        "insufficient evidence",
        "unknown",
    }
    if expected_folded in missing_values:
        return actual_folded in missing_values
    if expected_folded == actual_folded:
        return True
    expected_tokens = set(re.findall(r"[a-z0-9:.+-]+", expected_folded))
    actual_tokens = set(re.findall(r"[a-z0-9:.+-]+", actual_folded))
    if not expected_tokens or not actual_tokens:
        return _normalized_text(expected) == _normalized_text(actual)
    permitted_qualifiers = {
        "and",
        "at",
        "be",
        "can",
        "candidate",
        "candidates",
        "for",
        "must",
        "on",
        "only",
        "public",
        "requests",
        "research",
        "restricted",
        "select",
        "selected",
        "selecting",
        "that",
        "the",
        "to",
    }
    expected_core = expected_tokens - permitted_qualifiers
    actual_core = actual_tokens - permitted_qualifiers
    return bool(expected_core) and expected_core == actual_core


def _messages_for_case(
    case: EvaluationCase,
    protocol: ModelRoutingProtocol,
) -> list[dict[str, str]]:
    evidence = "\n".join(
        f"[{item.identifier}] {item.text}" for item in case.context
    )
    return [
        {
            "role": "system",
            "content": protocol.system_prompt_template.format(
                prompt_version=protocol.prompt_version,
                schema_version=protocol.schema_version,
                output_schema=json.dumps(
                    protocol.output_schema,
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            ),
        },
        {
            "role": "user",
            "content": protocol.user_prompt_template.format(
                task=case.instruction,
                evidence=evidence,
                question=case.question,
                fact_keys=json.dumps(
                    sorted(case.gold.expected_facts),
                    ensure_ascii=False,
                ),
            ),
        },
    ]
