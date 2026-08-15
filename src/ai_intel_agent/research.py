from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files
from string import Formatter
from time import sleep
from typing import Protocol
from urllib.parse import quote
from uuid import UUID

import httpx
from sqlalchemy import exists, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ai_intel_agent.domain import DigestState, EvidenceRelation, EvidenceRole, StoryReviewState
from ai_intel_agent.model_routing_evaluation import (
    ModelCandidate,
    ModelEvaluationConfigurationError,
    load_candidate_configuration,
    load_evaluation_corpus,
    load_protocol_configuration,
)
from ai_intel_agent.persistence import (
    ClaimRecord,
    DigestRecord,
    DigestStoryRecord,
    EvidenceSpanRecord,
    StoryRecord,
)
from ai_intel_agent.publication import bounded_public_evidence_excerpt

QUERY_TERM = re.compile(r"[0-9A-Za-z][0-9A-Za-z._+-]*|[\u3400-\u9fff]{2,}")
CHINESE_CHARACTER = re.compile(r"[\u3400-\u9fff]")
GENERIC_QUESTION_TERMS = frozenset(
    {
        "发生了什么",
        "是什么",
        "怎么样",
        "有什么新消息",
        "有什么更新",
        "有哪些变化",
        "如何",
    }
)
QUESTION_PREFIXES = ("请问", "关于", "的")
QUESTION_SUFFIXES = ("是多少", "是什么", "怎么样", "如何", "多少", "了吗", "吗", "呢")
FORBIDDEN_ANSWER_URL = re.compile(
    r"https?://|www\.|"
    r"(?:[a-z][a-z0-9+.-]*):(?:[^\s]|$)|"
    r"(?:[a-z0-9-]+\.)+[a-z]{2,63}\b|"
    r"\[[^\]]+\]\([^)]+\)|"
    r"(?:^|[\s(（])(?:/|\.{1,2}/)[a-z0-9]",
    flags=re.IGNORECASE,
)
FORBIDDEN_REASONING = re.compile(
    r"</?think>|chain[- ]of[- ]thought|思考过程|推理过程",
    flags=re.IGNORECASE,
)
ANSWER_DELTA_CHARACTERS = 12
APPROVED_RESEARCH_ROUTE = "deepseek:v4-pro"


class ResearchError(ValueError):
    pass


class ResearchProvider(Protocol):
    def stream(self, evidence_set: ResearchEvidenceSet) -> Iterator[str]: ...


@dataclass(frozen=True)
class ResearchProtocol:
    version: str
    prompt_version: str
    output_schema_version: str
    sse_contract_version: str
    route_identifier: str
    candidate_configuration_version: str
    routing_evaluation_version: str
    routing_evaluation_cases_sha256: str
    maximum_evidence_items: int
    maximum_output_tokens: int
    maximum_provider_output_characters: int
    system_prompt: str
    user_prompt_template: str
    output_contract: dict[str, object]
    sse_events: tuple[str, ...]
    content_sha256: str


@dataclass(frozen=True)
class ResearchEvidence:
    story_id: UUID
    story_stable_key: str
    story_headline: str
    claim_id: UUID
    claim_text: str
    evidence_span_id: UUID
    exact_text: str

    @property
    def citation_key(self) -> tuple[UUID, UUID, UUID]:
        return self.story_id, self.claim_id, self.evidence_span_id


@dataclass(frozen=True)
class ResearchEvidenceSet:
    question: str
    evidence: tuple[ResearchEvidence, ...]


@dataclass(frozen=True)
class ResearchAnswer:
    text: str
    citations: tuple[ResearchEvidence, ...]


class DeepSeekResearchProvider:
    """Stream strict Research JSON through the single M1-approved DeepSeek route."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        api_key: str,
        sleeper: Callable[[float], None] = sleep,
    ) -> None:
        if not api_key.strip():
            raise ResearchError("DEEPSEEK_API_KEY is required for Research")
        self._client = client
        self._api_key = api_key
        self._sleeper = sleeper
        self._protocol = load_research_protocol()
        self._routing_protocol = load_protocol_configuration()
        configuration = load_candidate_configuration()
        self._candidate = _selected_research_candidate(
            configuration.candidates,
            self._protocol.route_identifier,
        )

    def stream(self, evidence_set: ResearchEvidenceSet) -> Iterator[str]:
        protocol = self._protocol
        evidence_json = json.dumps(
            [
                {
                    "story_id": str(item.story_id),
                    "story_title": item.story_headline,
                    "claim_id": str(item.claim_id),
                    "claim_text": item.claim_text,
                    "evidence_span_id": str(item.evidence_span_id),
                    "evidence_text": item.exact_text,
                }
                for item in evidence_set.evidence[: protocol.maximum_evidence_items]
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        payload = {
            "model": self._candidate.model_id,
            "messages": [
                {"role": "system", "content": protocol.system_prompt},
                {
                    "role": "user",
                    "content": protocol.user_prompt_template.format(
                        question=evidence_set.question,
                        evidence_json=evidence_json,
                    ),
                },
            ],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "max_tokens": min(
                self._candidate.maximum_output_tokens,
                protocol.maximum_output_tokens,
            ),
            "stream": True,
        }
        attempts = 0
        while attempts < self._routing_protocol.retry_policy.max_attempts:
            attempts += 1
            try:
                with self._client.stream(
                    "POST",
                    f"{self._candidate.base_url.rstrip('/')}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                ) as response:
                    if (
                        response.status_code
                        in self._routing_protocol.retry_policy.retry_status_codes
                        and attempts < self._routing_protocol.retry_policy.max_attempts
                    ):
                        self._sleeper(
                            self._routing_protocol.retry_policy.backoff_seconds[
                                attempts - 1
                            ]
                        )
                        continue
                    if not response.is_success:
                        raise ResearchError(
                            "DeepSeek Research request returned "
                            f"HTTP {response.status_code}"
                        )
                    if "text/event-stream" not in response.headers.get(
                        "content-type", ""
                    ):
                        raise ResearchError(
                            "DeepSeek Research response was not an SSE stream"
                        )
                    yield from self._stream_content(response)
                    return
            except httpx.RequestError as error:
                if attempts >= self._routing_protocol.retry_policy.max_attempts:
                    raise ResearchError("DeepSeek Research request failed") from error
                self._sleeper(
                    self._routing_protocol.retry_policy.backoff_seconds[attempts - 1]
                )
        raise ResearchError("DeepSeek Research request did not complete")

    def _stream_content(self, response: httpx.Response) -> Iterator[str]:
        returned_models: set[str] = set()
        finish_reason: object = None
        saw_done = False
        for line in response.iter_lines():
            if not line:
                continue
            if not line.startswith("data:"):
                raise ResearchError("DeepSeek Research SSE frame is invalid")
            data = line.removeprefix("data:").strip()
            if data == "[DONE]":
                saw_done = True
                break
            try:
                chunk = json.loads(data)
                returned_model = chunk.get("model")
                choice = chunk["choices"][0]
                delta = choice["delta"]
            except (AttributeError, IndexError, KeyError, TypeError, json.JSONDecodeError) as error:
                raise ResearchError("DeepSeek Research SSE payload is invalid") from error
            if not isinstance(returned_model, str) or not returned_model:
                raise ResearchError("DeepSeek Research stream omitted its returned model")
            returned_models.add(returned_model)
            if delta.get("reasoning_content"):
                raise ResearchError("DeepSeek Research returned prohibited reasoning")
            unexpected_delta_keys = set(delta) - {"content", "role", "reasoning_content"}
            if unexpected_delta_keys:
                raise ResearchError("DeepSeek Research returned an unexpected delta")
            content = delta.get("content")
            if content is not None:
                if not isinstance(content, str):
                    raise ResearchError("DeepSeek Research content delta is invalid")
                yield content
            if choice.get("finish_reason") is not None:
                finish_reason = choice["finish_reason"]
        if not saw_done or finish_reason != "stop":
            raise ResearchError("DeepSeek Research stream did not finish completely")
        if returned_models != {self._candidate.model_id}:
            raise ResearchError("DeepSeek returned model does not match approved route")


class ResearchRepository:
    """Retrieve only accepted Evidence published through a Digest."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def retrieve(self, question: str, *, limit: int = 5) -> ResearchEvidenceSet:
        query_text = _fts_query_text(question)
        if not query_text:
            return ResearchEvidenceSet(question=question, evidence=())

        searchable_text = func.concat_ws(
            " ",
            StoryRecord.headline,
            ClaimRecord.text,
            EvidenceSpanRecord.exact_text,
        )
        search_vector = func.to_tsvector("simple", searchable_text)
        query = func.websearch_to_tsquery("simple", query_text)
        is_published = exists(
            select(DigestStoryRecord.story_id)
            .join(DigestRecord, DigestRecord.id == DigestStoryRecord.digest_id)
            .where(
                DigestStoryRecord.story_id == StoryRecord.id,
                DigestRecord.state == DigestState.PUBLISHED.value,
            )
        )
        statement = (
            select(
                StoryRecord.id.label("story_id"),
                StoryRecord.stable_key,
                StoryRecord.headline,
                ClaimRecord.id.label("claim_id"),
                ClaimRecord.text.label("claim_text"),
                EvidenceSpanRecord.id.label("evidence_span_id"),
                EvidenceSpanRecord.exact_text,
                func.ts_rank_cd(search_vector, query).label("rank"),
            )
            .join(ClaimRecord, ClaimRecord.story_id == StoryRecord.id)
            .join(EvidenceSpanRecord, EvidenceSpanRecord.claim_id == ClaimRecord.id)
            .where(
                StoryRecord.review_state == StoryReviewState.ACCEPTED.value,
                EvidenceSpanRecord.relation == EvidenceRelation.SUPPORTS.value,
                EvidenceSpanRecord.role != EvidenceRole.COMMUNITY.value,
                is_published,
                search_vector.op("@@")(query),
            )
            .order_by(
                func.ts_rank_cd(search_vector, query).desc(),
                StoryRecord.occurred_at.desc(),
                ClaimRecord.position,
                EvidenceSpanRecord.start_offset,
            )
            .limit(limit)
        )
        with Session(self._engine) as session:
            evidence = tuple(
                ResearchEvidence(
                    story_id=row.story_id,
                    story_stable_key=row.stable_key,
                    story_headline=row.headline,
                    claim_id=row.claim_id,
                    claim_text=row.claim_text,
                    evidence_span_id=row.evidence_span_id,
                    exact_text=bounded_public_evidence_excerpt(row.exact_text),
                )
                for row in session.execute(statement)
            )
        return ResearchEvidenceSet(question=question, evidence=evidence)


def stream_research_events(
    question: str,
    *,
    repository: ResearchRepository,
    provider: ResearchProvider | None,
) -> Iterator[tuple[str, dict[str, object]]]:
    protocol = load_research_protocol()
    version = protocol.sse_contract_version
    yield "status", {"version": version, "state": "retrieving"}
    evidence_set = repository.retrieve(
        question,
        limit=protocol.maximum_evidence_items,
    )
    if not evidence_set.evidence:
        yield from _insufficient_evidence_events(version)
        return
    if provider is None:
        yield "error", {
            "version": version,
            "code": "provider-unavailable",
            "message": "Research Provider 当前不可用。",
        }
        yield "done", {"version": version, "status": "failed"}
        return

    yield "status", {"version": version, "state": "generating"}
    try:
        answer = _validated_provider_answer(provider, evidence_set, protocol)
    except Exception:  # noqa: BLE001 - external Provider failures must fail closed.
        yield "error", {
            "version": version,
            "code": "provider-failed",
            "message": "Research Provider 输出未通过验证。",
        }
        yield "done", {"version": version, "status": "failed"}
        return

    if answer is None:
        yield from _insufficient_evidence_events(version)
        return

    for start in range(0, len(answer.text), ANSWER_DELTA_CHARACTERS):
        yield "answer.delta", {
            "version": version,
            "text": answer.text[start : start + ANSWER_DELTA_CHARACTERS]
        }
    for citation in answer.citations:
        story_url = f"/stories/{quote(citation.story_stable_key, safe='')}"
        yield "citation", {
            "version": version,
            "story_id": str(citation.story_id),
            "story_title": citation.story_headline,
            "story_url": story_url,
            "claim_id": str(citation.claim_id),
            "claim_text": citation.claim_text,
            "claim_url": f"{story_url}#claim-{citation.claim_id}",
            "evidence_span_id": str(citation.evidence_span_id),
            "evidence_text": citation.exact_text,
            "evidence_url": f"{story_url}#evidence-{citation.evidence_span_id}",
        }
    yield "done", {"version": version, "status": "answered"}


def load_research_protocol() -> ResearchProtocol:
    resource = files("ai_intel_agent").joinpath("data/research_protocol.v1.json")
    raw = resource.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    expected_keys = {
        "version",
        "prompt_version",
        "output_schema_version",
        "sse_contract_version",
        "route_identifier",
        "candidate_configuration_version",
        "routing_evaluation_version",
        "maximum_evidence_items",
        "maximum_output_tokens",
        "maximum_provider_output_characters",
        "system_prompt",
        "user_prompt_template",
        "output_contract",
        "sse_events",
    }
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise ResearchError("Research protocol keys do not match v1")
    if payload["route_identifier"] != APPROVED_RESEARCH_ROUTE:
        raise ResearchError("Research protocol is not the approved M1 DeepSeek route")

    try:
        evaluation = load_evaluation_corpus()
    except ModelEvaluationConfigurationError as error:
        raise ResearchError("Research routing evaluation approval is invalid") from error
    if (
        evaluation.review_state != "human-approved"
        or evaluation.approved_cases_sha256 != evaluation.cases_sha256
    ):
        raise ResearchError(
            "Research route is not human-approved for the exact evaluation cases SHA-256"
        )
    if payload["routing_evaluation_version"] != evaluation.version:
        raise ResearchError("Research routing evaluation version drifted")

    candidates = load_candidate_configuration()
    if payload["candidate_configuration_version"] != candidates.version:
        raise ResearchError("Research candidate configuration version drifted")
    _selected_research_candidate(
        candidates.candidates,
        payload["route_identifier"],
    )

    fields = {
        name
        for _, name, _, _ in Formatter().parse(payload["user_prompt_template"])
        if name is not None
    }
    if fields != {"question", "evidence_json"}:
        raise ResearchError("Research prompt placeholders do not match v1")
    expected_output_contract = {
        "required_keys": ["answer", "citations"],
        "citation_required_keys": [
            "story_id",
            "claim_id",
            "evidence_span_id",
        ],
        "abstention_shape": {"answer": None, "citations": []},
        "additional_properties": False,
        "citation_additional_properties": False,
    }
    if payload["output_contract"] != expected_output_contract:
        raise ResearchError("Research output contract does not match v1")
    expected_sse_events = (
        "status",
        "answer.delta",
        "citation",
        "refusal",
        "error",
        "done",
    )
    if tuple(payload["sse_events"]) != expected_sse_events:
        raise ResearchError("Research SSE contract does not match v1")

    maximum_evidence_items = int(payload["maximum_evidence_items"])
    maximum_output_tokens = int(payload["maximum_output_tokens"])
    maximum_provider_output_characters = int(
        payload["maximum_provider_output_characters"]
    )
    if (
        not 1 <= maximum_evidence_items <= 10
        or maximum_output_tokens <= 0
        or maximum_provider_output_characters <= 0
    ):
        raise ResearchError("Research protocol budgets are invalid")
    return ResearchProtocol(
        **{
            **payload,
            "maximum_evidence_items": maximum_evidence_items,
            "maximum_output_tokens": maximum_output_tokens,
            "maximum_provider_output_characters": maximum_provider_output_characters,
            "sse_events": expected_sse_events,
        },
        routing_evaluation_cases_sha256=evaluation.cases_sha256,
        content_sha256=sha256(raw).hexdigest(),
    )


def _validated_provider_answer(
    provider: ResearchProvider,
    evidence_set: ResearchEvidenceSet,
    protocol: ResearchProtocol,
) -> ResearchAnswer | None:
    parts: list[str] = []
    characters = 0
    for part in provider.stream(evidence_set):
        if not isinstance(part, str):
            raise ResearchError("Provider stream chunks must be text")
        characters += len(part)
        if characters > protocol.maximum_provider_output_characters:
            raise ResearchError("Provider output exceeded its bounded size")
        parts.append(part)
    try:
        payload = json.loads("".join(parts))
    except (json.JSONDecodeError, TypeError) as error:
        raise ResearchError("Provider output is not valid JSON") from error
    required_keys = set(protocol.output_contract["required_keys"])
    if not isinstance(payload, dict) or set(payload) != required_keys:
        raise ResearchError("Provider output keys do not match the Research contract")

    answer = payload["answer"]
    raw_citations = payload["citations"]
    if answer is None:
        if {"answer": answer, "citations": raw_citations} != protocol.output_contract[
            "abstention_shape"
        ]:
            raise ResearchError("Provider abstention shape is invalid")
        return None
    if (
        not isinstance(answer, str)
        or not answer.strip()
        or CHINESE_CHARACTER.search(answer) is None
        or FORBIDDEN_ANSWER_URL.search(answer) is not None
        or FORBIDDEN_REASONING.search(answer) is not None
    ):
        raise ResearchError("Provider answer content is invalid")
    if not isinstance(raw_citations, list) or not raw_citations:
        raise ResearchError("Provider answer must cite retrieved Evidence")

    retrieved = {item.citation_key: item for item in evidence_set.evidence}
    citations: list[ResearchEvidence] = []
    seen: set[tuple[UUID, UUID, UUID]] = set()
    for raw_citation in raw_citations:
        citation_required_keys = set(
            protocol.output_contract["citation_required_keys"]
        )
        if not isinstance(raw_citation, dict) or set(raw_citation) != citation_required_keys:
            raise ResearchError("Provider citation shape is invalid")
        try:
            key = (
                UUID(raw_citation["story_id"]),
                UUID(raw_citation["claim_id"]),
                UUID(raw_citation["evidence_span_id"]),
            )
        except (AttributeError, TypeError, ValueError) as error:
            raise ResearchError("Provider citation identity is invalid") from error
        if key not in retrieved or key in seen:
            raise ResearchError("Provider citation is outside the retrieved Evidence Set")
        seen.add(key)
        citations.append(retrieved[key])
    return ResearchAnswer(text=answer.strip(), citations=tuple(citations))


def _fts_query_text(question: str) -> str:
    terms = tuple(
        normalized
        for term in QUERY_TERM.findall(question)
        if (normalized := _normalized_query_term(term)) is not None
    )
    return " ".join(dict.fromkeys(terms))


def _normalized_query_term(term: str) -> str | None:
    if term in GENERIC_QUESTION_TERMS:
        return None
    if CHINESE_CHARACTER.search(term) is None:
        return term
    normalized = term
    for prefix in QUESTION_PREFIXES:
        if normalized.startswith(prefix) and len(normalized) > len(prefix):
            normalized = normalized.removeprefix(prefix)
            break
    for suffix in QUESTION_SUFFIXES:
        if normalized.endswith(suffix) and len(normalized) > len(suffix):
            normalized = normalized.removesuffix(suffix)
            break
    if not normalized or normalized in GENERIC_QUESTION_TERMS:
        return None
    return normalized


def _insufficient_evidence_events(
    version: str,
) -> tuple[tuple[str, dict[str, object]], ...]:
    return (
        (
            "refusal",
            {
                "version": version,
                "reason": "insufficient-evidence",
                "message": "证据不足：已发布知识中没有足够证据回答这个问题。",
            },
        ),
        ("done", {"version": version, "status": "refused"}),
    )


def _selected_research_candidate(
    candidates: tuple[ModelCandidate, ...],
    identifier: str,
) -> ModelCandidate:
    matches = tuple(
        candidate for candidate in candidates if candidate.identifier == identifier
    )
    if len(matches) != 1 or matches[0].provider != "deepseek":
        raise ResearchError("Approved Research DeepSeek route is unavailable")
    return matches[0]
