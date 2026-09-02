from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import UUID

from ai_intel_agent.domain import EvidenceRelation, EvidenceRole
from ai_intel_agent.model_routing_evaluation import (
    ModelCandidate,
    load_candidate_configuration,
    load_protocol_configuration,
)
from ai_intel_agent.research import (
    QueryIntent,
    ResearchEvidence,
    ResearchEvidenceSet,
    ResearchProvider,
    load_research_protocol,
    stream_research_events,
)

_REVISION = re.compile(r"[0-9a-f]{40}")
_SOURCE_SHA256 = re.compile(r"[0-9a-f]{64}")


class ResearchProviderQualificationError(ValueError):
    pass


class QualificationAttemptBudget:
    def __init__(self, maximum_attempts: int) -> None:
        if maximum_attempts < 1:
            raise ValueError("Qualification maximum attempts must be positive")
        self._maximum_attempts = maximum_attempts
        self._reserved = 0

    def reserve(self) -> bool:
        if self._reserved >= self._maximum_attempts:
            return False
        self._reserved += 1
        return True


@dataclass(frozen=True)
class ResearchProviderQualificationCase:
    identifier: str
    question: str
    evidence: tuple[ResearchEvidence, ...]
    expected_status: Literal["answered", "refused"]
    required_answer_terms: tuple[str, ...]
    repetitions: int


@dataclass(frozen=True)
class ResearchProviderQualificationCorpus:
    version: str
    content_sha256: str
    maximum_input_tokens_per_request: int
    maximum_cost_usd: float
    qualified_source_paths: tuple[str, ...]
    cases: tuple[ResearchProviderQualificationCase, ...]


@dataclass(frozen=True)
class ResearchProviderQualificationResult:
    case_identifier: str
    repetition: int
    expected_status: str
    observed_status: str
    passed: bool
    failure_code: str | None
    citation_count: int
    validated_returned_model_id: str | None


@dataclass(frozen=True)
class ResearchProviderQualification:
    schema_version: str
    status: Literal["passed", "failed", "non-qualifying"]
    execution_mode: Literal["live-provider", "mocked-provider"]
    commit_sha: str
    qualified_source_sha256: str
    route_identifier: str
    approved_model_id: str
    protocol_version: str
    protocol_sha256: str
    corpus_version: str
    corpus_sha256: str
    generated_at: datetime
    maximum_provider_attempts: int
    worst_case_reserved_cost_usd: float
    results: tuple[ResearchProviderQualificationResult, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "execution_mode": self.execution_mode,
            "commit_sha": self.commit_sha,
            "qualified_source_sha256": self.qualified_source_sha256,
            "route_identifier": self.route_identifier,
            "approved_model_id": self.approved_model_id,
            "protocol_version": self.protocol_version,
            "protocol_sha256": self.protocol_sha256,
            "corpus_version": self.corpus_version,
            "corpus_sha256": self.corpus_sha256,
            "generated_at": self.generated_at.isoformat().replace("+00:00", "Z"),
            "maximum_provider_attempts": self.maximum_provider_attempts,
            "worst_case_reserved_cost_usd": self.worst_case_reserved_cost_usd,
            "results": [
                {
                    "case_identifier": result.case_identifier,
                    "repetition": result.repetition,
                    "expected_status": result.expected_status,
                    "observed_status": result.observed_status,
                    "passed": result.passed,
                    "failure_code": result.failure_code,
                    "citation_count": result.citation_count,
                    "validated_returned_model_id": result.validated_returned_model_id,
                }
                for result in self.results
            ],
        }


def write_research_provider_qualification(
    qualification: ResearchProviderQualification,
    output: Path,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    temporary.write_text(
        json.dumps(qualification.as_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)


def qualified_source_sha256(
    project_root: Path,
    relative_paths: tuple[str, ...],
) -> str:
    """Bind a PR qualification to deployable Provider behavior.

    The stdlib-only deployment preflight intentionally mirrors this small algorithm so
    it can validate a release before pulling or starting application containers.
    """
    if (
        not relative_paths
        or any(not isinstance(path, str) for path in relative_paths)
        or len(set(relative_paths)) != len(relative_paths)
    ):
        raise ResearchProviderQualificationError(
            "Research Provider qualified source paths are invalid"
        )
    digest = sha256()
    for relative_path in sorted(relative_paths):
        normalized = PurePosixPath(relative_path)
        if (
            normalized.is_absolute()
            or ".." in normalized.parts
            or normalized.as_posix() != relative_path
        ):
            raise ResearchProviderQualificationError(
                "Research Provider qualified source path is unsafe"
            )
        source = project_root.joinpath(*normalized.parts)
        if not source.is_file():
            raise ResearchProviderQualificationError(
                f"Research Provider qualified source is missing: {relative_path}"
            )
        path_bytes = relative_path.encode("utf-8")
        content = source.read_bytes().replace(b"\r\n", b"\n")
        content_sha = sha256(content).hexdigest().encode("ascii")
        digest.update(path_bytes)
        digest.update(b"\0")
        digest.update(content_sha)
        digest.update(b"\n")
    return digest.hexdigest()


class _QualificationRepository:
    def __init__(self, case: ResearchProviderQualificationCase) -> None:
        self._case = case

    def retrieve_intent(self, intent: QueryIntent, **_: object) -> ResearchEvidenceSet:
        return ResearchEvidenceSet(
            question=self._case.question,
            evidence=self._case.evidence,
            intent=intent,
            retrieval_calls=1,
            iterations=1,
        )


def load_research_provider_qualification_corpus() -> ResearchProviderQualificationCorpus:
    resource = files("ai_intel_agent").joinpath(
        "data/research_provider_qualification.v1.json"
    )
    raw = resource.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
        evidence_sets = {
            identifier: tuple(_parse_evidence(item) for item in evidence)
            for identifier, evidence in payload["evidence_sets"].items()
        }
        cases = tuple(
            ResearchProviderQualificationCase(
                identifier=item["identifier"],
                question=item["question"],
                evidence=evidence_sets[item["evidence_set"]],
                expected_status=item["expected_status"],
                required_answer_terms=tuple(item["required_answer_terms"]),
                repetitions=int(item["repetitions"]),
            )
            for item in payload["cases"]
        )
        corpus = ResearchProviderQualificationCorpus(
            version=payload["version"],
            content_sha256=sha256(raw).hexdigest(),
            maximum_input_tokens_per_request=int(
                payload["maximum_input_tokens_per_request"]
            ),
            maximum_cost_usd=float(payload["maximum_cost_usd"]),
            qualified_source_paths=tuple(payload["qualified_source_paths"]),
            cases=cases,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ResearchProviderQualificationError(
            "Research Provider qualification corpus is invalid"
        ) from error
    _validate_corpus(corpus)
    return corpus


def run_research_provider_qualification(
    *,
    provider: ResearchProvider,
    revision: str,
    qualified_source_sha256: str,
    execution_mode: Literal["live-provider", "mocked-provider"] = "mocked-provider",
    corpus: ResearchProviderQualificationCorpus | None = None,
    now: datetime | None = None,
) -> ResearchProviderQualification:
    if _REVISION.fullmatch(revision) is None:
        raise ResearchProviderQualificationError(
            "Research Provider qualification requires an exact 40-character commit SHA"
        )
    if _SOURCE_SHA256.fullmatch(qualified_source_sha256) is None:
        raise ResearchProviderQualificationError(
            "Research Provider qualification requires a qualified source SHA-256"
        )
    corpus = corpus or load_research_provider_qualification_corpus()
    protocol = load_research_protocol()
    routing_protocol = load_protocol_configuration()
    candidate = _approved_candidate(protocol.route_identifier)
    maximum_provider_attempts = (
        sum(case.repetitions for case in corpus.cases)
        * routing_protocol.retry_policy.max_attempts
    )
    worst_case_reserved_cost_usd = _worst_case_cost_usd(
        candidate,
        maximum_provider_attempts=maximum_provider_attempts,
        maximum_input_tokens=corpus.maximum_input_tokens_per_request,
        maximum_output_tokens=protocol.maximum_output_tokens,
    )
    if worst_case_reserved_cost_usd > corpus.maximum_cost_usd:
        raise ResearchProviderQualificationError(
            "Research Provider qualification worst-case cost exceeds its corpus limit"
        )
    generated_at = now or datetime.now(UTC)
    if generated_at.tzinfo is None:
        raise ResearchProviderQualificationError(
            "Research Provider qualification timestamp must be timezone-aware"
        )

    results: list[ResearchProviderQualificationResult] = []
    for case in corpus.cases:
        for repetition in range(1, case.repetitions + 1):
            results.append(_run_case(provider, case, repetition, candidate))

    all_cases_passed = all(result.passed for result in results)
    status: Literal["passed", "failed", "non-qualifying"]
    if not all_cases_passed:
        status = "failed"
    elif execution_mode == "live-provider":
        status = "passed"
    else:
        status = "non-qualifying"
    return ResearchProviderQualification(
        schema_version="research-provider-qualification-report.v1",
        status=status,
        execution_mode=execution_mode,
        commit_sha=revision,
        qualified_source_sha256=qualified_source_sha256,
        route_identifier=protocol.route_identifier,
        approved_model_id=candidate.model_id,
        protocol_version=protocol.version,
        protocol_sha256=protocol.content_sha256,
        corpus_version=corpus.version,
        corpus_sha256=corpus.content_sha256,
        generated_at=generated_at,
        maximum_provider_attempts=maximum_provider_attempts,
        worst_case_reserved_cost_usd=worst_case_reserved_cost_usd,
        results=tuple(results),
    )


def maximum_provider_attempts(
    corpus: ResearchProviderQualificationCorpus | None = None,
) -> int:
    corpus = corpus or load_research_provider_qualification_corpus()
    return sum(case.repetitions for case in corpus.cases) * (
        load_protocol_configuration().retry_policy.max_attempts
    )


def _run_case(
    provider: ResearchProvider,
    case: ResearchProviderQualificationCase,
    repetition: int,
    candidate: ModelCandidate,
) -> ResearchProviderQualificationResult:
    events = list(
        stream_research_events(
            case.question,
            repository=_QualificationRepository(case),
            provider=provider,
        )
    )
    done_payload = next(
        (payload for event, payload in reversed(events) if event == "done"),
        {},
    )
    observed_status = str(done_payload.get("status", "missing"))
    answer = "".join(
        str(payload.get("text", ""))
        for event, payload in events
        if event == "answer.delta"
    )
    citations = [payload for event, payload in events if event == "citation"]
    error_code = next(
        (
            str(payload.get("code", "provider-failed"))
            for event, payload in events
            if event == "error"
        ),
        None,
    )
    refusal_reason = next(
        (
            str(payload.get("reason", payload.get("code", "refused")))
            for event, payload in events
            if event == "refusal"
        ),
        None,
    )
    returned_model = getattr(provider, "last_returned_model_id", None)
    failure_code: str | None = None
    if error_code is not None:
        failure_code = error_code
    elif observed_status != case.expected_status:
        failure_code = (
            "abstention-mismatch"
            if case.expected_status == "answered" and refusal_reason == "provider-abstained"
            else "terminal-status-mismatch"
        )
    elif case.expected_status == "answered" and not answer.strip():
        failure_code = "answer-missing"
    elif case.expected_status == "answered" and not citations:
        failure_code = "citation-missing"
    elif any(term.casefold() not in answer.casefold() for term in case.required_answer_terms):
        failure_code = "required-fact-missing"
    elif case.expected_status == "refused" and (answer or citations):
        failure_code = "unexpected-supported-output"
    elif returned_model != candidate.model_id:
        failure_code = "returned-model-unverified"

    return ResearchProviderQualificationResult(
        case_identifier=case.identifier,
        repetition=repetition,
        expected_status=case.expected_status,
        observed_status=observed_status,
        passed=failure_code is None,
        failure_code=failure_code,
        citation_count=len(citations),
        validated_returned_model_id=(
            returned_model if isinstance(returned_model, str) else None
        ),
    )


def _parse_evidence(item: dict[str, object]) -> ResearchEvidence:
    return ResearchEvidence(
        story_id=UUID(str(item["story_id"])),
        story_stable_key=str(item["story_stable_key"]),
        story_headline=str(item["story_headline"]),
        claim_id=UUID(str(item["claim_id"])),
        claim_text=str(item["claim_text"]),
        evidence_span_id=UUID(str(item["evidence_span_id"])),
        exact_text=str(item["evidence_text"]),
        evidence_role=EvidenceRole(str(item["evidence_role"])),
        evidence_relation=EvidenceRelation(str(item["evidence_relation"])),
        evidence_publisher=str(item["evidence_publisher"]),
    )


def _validate_corpus(corpus: ResearchProviderQualificationCorpus) -> None:
    if (
        not corpus.version.strip()
        or corpus.maximum_input_tokens_per_request < 1
        or corpus.maximum_cost_usd <= 0
        or not corpus.qualified_source_paths
        or not corpus.cases
    ):
        raise ResearchProviderQualificationError(
            "Research Provider qualification corpus policy is invalid"
        )
    identifiers: set[str] = set()
    for case in corpus.cases:
        if (
            not case.identifier.strip()
            or case.identifier in identifiers
            or not case.question.strip()
            or not case.evidence
            or case.expected_status not in {"answered", "refused"}
            or case.repetitions < 1
            or (case.expected_status == "answered" and not case.required_answer_terms)
            or (case.expected_status == "refused" and case.required_answer_terms)
        ):
            raise ResearchProviderQualificationError(
                "Research Provider qualification case is invalid"
            )
        identifiers.add(case.identifier)


def _approved_candidate(route_identifier: str) -> ModelCandidate:
    configuration = load_candidate_configuration()
    try:
        return next(
            candidate
            for candidate in configuration.candidates
            if candidate.identifier == route_identifier
        )
    except StopIteration as error:
        raise ResearchProviderQualificationError(
            "Research Provider qualification route is unavailable"
        ) from error


def _worst_case_cost_usd(
    candidate: ModelCandidate,
    *,
    maximum_provider_attempts: int,
    maximum_input_tokens: int,
    maximum_output_tokens: int,
) -> float:
    native_per_attempt = (
        maximum_input_tokens * candidate.cache_miss_per_million
        + maximum_output_tokens * candidate.output_per_million
    ) / 1_000_000
    return native_per_attempt * maximum_provider_attempts / candidate.native_units_per_usd
