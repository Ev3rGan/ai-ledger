from __future__ import annotations

import hmac
import json
import logging
import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from enum import StrEnum
from hashlib import sha256
from importlib.resources import files
from math import ceil
from string import Formatter
from time import monotonic, sleep
from typing import Protocol
from urllib.parse import quote
from uuid import UUID

import httpx
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, aliased

from ai_intel_agent.accepted_knowledge import (
    AcceptedKnowledgeDeadlineExceeded,
    AcceptedKnowledgeHit,
    AcceptedKnowledgeOperation,
    AcceptedKnowledgeRetrieval,
    RetrievalFault,
    RetrievalFilters,
    RetrievalQuery,
)
from ai_intel_agent.domain import (
    AuditAction,
    AuditSubjectType,
    DigestState,
    EvidenceRelation,
    EvidenceRole,
)
from ai_intel_agent.model_routing_evaluation import (
    ModelCandidate,
    ModelEvaluationConfigurationError,
    load_candidate_configuration,
    load_evaluation_corpus,
    load_protocol_configuration,
)
from ai_intel_agent.persistence import (
    AnonymousResearchAllowanceRepository,
    AuditEventRecord,
    CandidateRecord,
    ClaimRecord,
    DatabaseAcquisitionDeadlineExceeded,
    DigestRecord,
    DigestStoryRecord,
    DocumentVersionRecord,
    EvidenceSpanRecord,
    StoryRecord,
    reserve_database_acquisition_budget,
)

LOGGER = logging.getLogger(__name__)

CHINESE_CHARACTER = re.compile(r"[\u3400-\u9fff]")
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
ACCEPTED_PUBLISHED_SCOPE = "accepted-published-knowledge"
YEAR_RANGE = re.compile(
    r"(?<!\d)(20\d{2})\s*(?:年)?\s*(?:-|–|—|至|到)\s*(20\d{2})\s*年?"
)
SINGLE_YEAR = re.compile(r"(?<!\d)(20\d{2})\s*年")
LATIN_ENTITY = re.compile(
    r"(?<![0-9A-Za-z])"
    r"([A-Z][0-9A-Za-z]*(?:[._/+:-][0-9A-Za-z]+)*"
    r"(?:\s+(?:[A-Z][0-9A-Za-z]*|[0-9]+(?:\.[0-9]+)*)(?:[._/+:-][0-9A-Za-z]+)*){0,3})"
)
INTRODUCED_PRODUCT_ENTITY = re.compile(
    r"(?:推出|发布|上线|公布)(?:了)?\s*"
    r"([A-Z][0-9A-Za-z]*(?:[._/+:-][0-9A-Za-z]+)*"
    r"(?:\s+(?:[A-Z][0-9A-Za-z]*|[0-9]+(?:\.[0-9]+)*)(?:[._/+:-][0-9A-Za-z]+)*){0,3})"
)
QUOTED_ENTITY = re.compile(r"[「『《\"`]([^」』》\"`]{1,80})[」』》\"`]")
COMPARISON_MARKER = re.compile(r"比较|对比|相比|差异|区别|\bversus\b|\bvs\.?\b", re.IGNORECASE)
EXPLICIT_COMPARISON_ENTITIES = re.compile(
    r"(?:比较|对比)\s*([\u3400-\u9fff0-9A-Za-z._/+:-]{1,40})\s*"
    r"(?:和|与|及|versus|vs\.?)\s*"
    r"([\u3400-\u9fff0-9A-Za-z._/+:-]{1,40}?)(?=\s*(?:在|的|有何|相比|$))",
    re.IGNORECASE,
)
TIMELINE_MARKER = re.compile(r"时间线|按时间|先后顺序|发布历程|\btimeline\b|chronolog", re.IGNORECASE)
MULTI_HOP_MARKER = re.compile(
    r"多跳|(?:后|之后).*(?:如何|怎样).*(?:影响|导致|促成)|"
    r"(?:如何|怎样).*(?:影响|导致|促成)|(?:导致|促成).*(?:什么|如何|为何)|"
    r"\bhow\b.*\b(?:affect|lead|cause|influence)\b",
    re.IGNORECASE,
)
SIMPLE_LOOKUP_PRICE_QUESTION = re.compile(
    r"价格|定价|价钱|售价|多少钱|费用|收费|成本|免费|"
    r"\bprice\b|\bpricing\b|\bcosts?\b|\bfees?\b|\bhow\s+much\b|"
    r"\b(?:is|are)\s+(?:[0-9A-Za-z._/+:-]+\s+){0,10}"
    r"(?:currently\s+)?free(?:\s+(?:to\s+use|of\s+charge))?\s*(?:[?？]|$)|"
    r"\bfree\s+(?:tier|plan|version|access)\b",
    re.IGNORECASE,
)
SIMPLE_LOOKUP_PRICE_CUE = re.compile(
    r"价格|定价|价钱|售价|费用|收费|成本|"
    r"\bprice\b|\bpricing\b|\bpriced\b|\bcosts?\b|\bfees?\b",
    re.IGNORECASE,
)
SIMPLE_LOOKUP_MONETARY_VALUE = re.compile(
    r"(?:[$¥￥€£]\s*\d+(?:[.,]\d+)?)|"
    r"(?:\b(?:USD|CNY|RMB|EUR|GBP)\b\s*\d+(?:[.,]\d+)?)|"
    r"(?:\d+(?:[.,]\d+)?\s*(?:美元|人民币|元|美分|"
    r"\b(?:USD|CNY|RMB|EUR|GBP|dollars?|cents?|yuan)\b))",
    re.IGNORECASE,
)
SIMPLE_LOOKUP_AFFIRMATIVE_FREE_VALUE = re.compile(
    r"免费(?:提供|使用|开放|可用)?\s*$|"
    r"(?:提供|开放)(?:了)?免费(?:版本|套餐|层级)?|"
    r"\b(?:is|are|remains?|becomes?|will\s+be)\s+(?:currently\s+)?free\b|"
    r"\bfree\s+(?:tier|plan|version|access)\s+"
    r"(?:is|remains?)\s+(?:available|offered)\b",
    re.IGNORECASE,
)
SIMPLE_LOOKUP_NEGATED_FREE_VALUE = re.compile(
    r"(?:不|并不|不是|并非|不再|从未|没有|无)[^。！？；;]{0,8}免费|"
    r"\b(?:not|never|isn't|wasn't|weren't|aren't|without|no)\b"
    r"[^.!?。！？；;]{0,24}\bfree\b",
    re.IGNORECASE,
)
ENTITY_STOPWORDS = frozenset(
    {
        "AI",
        "Research",
        "Timeline",
        "How",
        "What",
        "Does",
        "Do",
        "Is",
        "Are",
        "When",
        "Where",
        "Why",
        "Which",
        "Can",
    }
)
ENTITY_STOPWORDS_CASEFOLDED = frozenset(
    value.casefold() for value in ENTITY_STOPWORDS
)
ENTITY_SCOPED_COMPARISON_DIMENSIONS = frozenset(
    {
        "主要差异",
        "产品形态",
        "开发工具产品形态",
        "公开进展",
        "具体进展",
    }
)


class ResearchError(ValueError):
    pass


class ResearchProviderOutputRejected(ResearchError):
    pass


class ResearchBudgetExceeded(ResearchError):
    def __init__(self, limit: str) -> None:
        super().__init__(f"Research execution budget exceeded: {limit}")
        self.limit = limit


class ResearchTaskType(StrEnum):
    SIMPLE_LOOKUP = "simple-lookup"
    COMPARISON = "comparison"
    TIMELINE = "timeline"
    MULTI_HOP = "multi-hop"


class ResearchTimeSemantic(StrEnum):
    EVENT = "event"
    SOURCE_PUBLICATION = "source-publication"
    DISCOVERY = "discovery"
    EDITORIAL = "editorial"
    DIGEST_PUBLICATION = "digest-publication"
    ALL = "all"


EVIDENCE_TIME_SEMANTICS = tuple(
    semantic
    for semantic in ResearchTimeSemantic
    if semantic is not ResearchTimeSemantic.ALL
)


@dataclass(frozen=True)
class QueryTimeRange:
    start: datetime | None = None
    end: datetime | None = None

    def __post_init__(self) -> None:
        for label, value in (("start", self.start), ("end", self.end)):
            if value is not None and value.utcoffset() is None:
                raise ValueError(f"Query Intent {label} must include a timezone")
        if self.start is not None and self.end is not None and self.start >= self.end:
            raise ValueError("Query Intent start must be earlier than end")


@dataclass(frozen=True)
class ResearchExecutionBudget:
    maximum_iterations: int
    maximum_retrieval_calls: int
    maximum_evidence_items: int
    maximum_output_tokens: int
    maximum_provider_output_characters: int
    maximum_elapsed_seconds: float

    def __post_init__(self) -> None:
        values = (
            self.maximum_iterations,
            self.maximum_retrieval_calls,
            self.maximum_evidence_items,
            self.maximum_output_tokens,
            self.maximum_provider_output_characters,
            self.maximum_elapsed_seconds,
        )
        if any(value <= 0 for value in values):
            raise ValueError("Research execution budget values must be positive")


@dataclass(frozen=True)
class QueryIntent:
    question: str
    task_type: ResearchTaskType
    entities: tuple[str, ...]
    time_range: QueryTimeRange
    time_semantic: ResearchTimeSemantic
    scope: str
    dimensions: tuple[str, ...]
    budget: ResearchExecutionBudget


class ResearchProvider(Protocol):
    def stream(self, evidence_set: ResearchEvidenceSet) -> Iterator[str]: ...


class MeteredProviderBudget(Protocol):
    def reserve(self) -> bool: ...


class ResearchAllowance(Protocol):
    def reserve(self, anonymous_client_id: str) -> bool: ...


class ResearchEvidenceMetadataLoader(Protocol):
    def load(
        self,
        evidence_span_ids: tuple[UUID, ...],
        *,
        timeout_seconds: float | None = None,
    ) -> Mapping[UUID, ResearchEvidenceMetadata]: ...


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
    maximum_iterations: int
    maximum_retrieval_calls: int
    maximum_elapsed_seconds: float
    maximum_evidence_items: int
    maximum_output_tokens: int
    maximum_provider_output_characters: int
    execution_budgets: dict[str, dict[str, int]]
    system_prompt: str
    simple_lookup_system_prompt: str
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
    evidence_role: EvidenceRole | None = None
    evidence_relation: EvidenceRelation | None = None
    evidence_publisher: str | None = None
    times: ResearchEvidenceTimes = field(default_factory=lambda: ResearchEvidenceTimes())

    @property
    def citation_key(self) -> tuple[UUID, UUID, UUID]:
        return self.story_id, self.claim_id, self.evidence_span_id


@dataclass(frozen=True)
class ResearchEvidenceTimes:
    event: datetime | None = None
    source_publication: datetime | None = None
    discovery: datetime | None = None
    editorial: datetime | None = None
    digest_publication: datetime | None = None

    def as_labeled_values(self) -> tuple[tuple[str, datetime | None], ...]:
        return (
            ("event", self.event),
            ("source-publication", self.source_publication),
            ("discovery", self.discovery),
            ("editorial", self.editorial),
            ("digest-publication", self.digest_publication),
        )


@dataclass(frozen=True)
class ResearchEvidenceMetadata:
    evidence_role: EvidenceRole | None = None
    evidence_relation: EvidenceRelation | None = None
    evidence_publisher: str | None = None
    times: ResearchEvidenceTimes = field(default_factory=ResearchEvidenceTimes)


@dataclass(frozen=True)
class ResearchRequirement:
    identifier: str
    label: str
    evidence_keys: tuple[tuple[UUID, UUID, UUID], ...]
    entity: str | None = None
    dimension: str | None = None


@dataclass(frozen=True)
class ResearchEvidenceSet:
    question: str
    evidence: tuple[ResearchEvidence, ...]
    intent: QueryIntent | None = None
    requirements: tuple[ResearchRequirement, ...] = ()
    missing_requirement_ids: tuple[str, ...] = ()
    retrieval_faults: tuple[RetrievalFault, ...] = ()
    retrieval_calls: int = 0
    iterations: int = 0
    execution_budget_exhausted: bool = False
    execution_limit: str | None = None
    provider_timeout_seconds: float | None = None
    decomposition_failed: bool = False


@dataclass(frozen=True)
class ResearchSupportedStatement:
    text: str
    citations: tuple[ResearchEvidence, ...]
    dimension: str | None = None
    time_semantic: str | None = None


@dataclass(frozen=True)
class ResearchAnswer:
    text: str
    citations: tuple[ResearchEvidence, ...]
    statements: tuple[ResearchSupportedStatement, ...]


def interpret_query_intent(question: str) -> QueryIntent:
    normalized = " ".join(question.split())
    if not normalized:
        raise ResearchError("Research question must not be empty")
    task_type = _research_task_type(normalized)
    protocol = load_research_protocol()
    dimensions = (
        _query_dimensions(normalized)
        if task_type is ResearchTaskType.COMPARISON
        else ()
    )
    if task_type is ResearchTaskType.COMPARISON and not dimensions:
        dimensions = ("主要差异",)
    return QueryIntent(
        question=normalized,
        task_type=task_type,
        entities=_query_entities(normalized, task_type=task_type),
        time_range=_query_time_range(normalized),
        time_semantic=_query_time_semantic(normalized, task_type),
        scope=ACCEPTED_PUBLISHED_SCOPE,
        dimensions=dimensions,
        budget=_execution_budget(task_type, protocol),
    )


class DeepSeekResearchProvider:
    """Stream strict Research JSON through the single M1-approved DeepSeek route."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        api_key: str,
        budget: MeteredProviderBudget | None = None,
        sleeper: Callable[[float], None] = sleep,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if not api_key.strip():
            raise ResearchError("DEEPSEEK_API_KEY is required for Research")
        self._client = client
        self._api_key = api_key
        self._budget = budget
        self._sleeper = sleeper
        self._clock = clock
        self._protocol = load_research_protocol()
        self._routing_protocol = load_protocol_configuration()
        configuration = load_candidate_configuration()
        self._candidate = _selected_research_candidate(
            configuration.candidates,
            self._protocol.route_identifier,
        )

    def stream(self, evidence_set: ResearchEvidenceSet) -> Iterator[str]:
        protocol = self._protocol
        intent = evidence_set.intent or interpret_query_intent(evidence_set.question)
        requirement_ids_by_evidence: dict[
            tuple[UUID, UUID, UUID],
            list[str],
        ] = {}
        for requirement in evidence_set.requirements:
            for key in requirement.evidence_keys:
                requirement_ids_by_evidence.setdefault(key, []).append(
                    requirement.identifier
                )
        evidence_json = json.dumps(
            {
                "requirements": [
                    {
                        "id": requirement.identifier,
                        "label": requirement.label,
                        "entity": requirement.entity,
                        "dimension": requirement.dimension,
                    }
                    for requirement in evidence_set.requirements
                ],
                "evidence": [
                    {
                        "story_id": str(item.story_id),
                        "story_title": item.story_headline,
                        "claim_id": str(item.claim_id),
                        "claim_text": item.claim_text,
                        "evidence_span_id": str(item.evidence_span_id),
                        "evidence_text": item.exact_text,
                        "evidence_role": (
                            item.evidence_role.value
                            if item.evidence_role is not None
                            else None
                        ),
                        "evidence_relation": (
                            item.evidence_relation.value
                            if item.evidence_relation is not None
                            else None
                        ),
                        "evidence_publisher": item.evidence_publisher,
                        "times": {
                            label: value.isoformat() if value is not None else None
                            for label, value in item.times.as_labeled_values()
                        },
                        "requirement_ids": requirement_ids_by_evidence.get(
                            item.citation_key,
                            [],
                        ),
                    }
                    for item in evidence_set.evidence[
                        : intent.budget.maximum_evidence_items
                    ]
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        payload = {
            "model": self._candidate.model_id,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        protocol.simple_lookup_system_prompt
                        if intent.task_type is ResearchTaskType.SIMPLE_LOOKUP
                        else protocol.system_prompt
                    ),
                },
                {
                    "role": "user",
                    "content": protocol.user_prompt_template.format(
                        question=evidence_set.question,
                        query_intent_json=json.dumps(
                            _query_intent_payload(intent),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        evidence_json=evidence_json,
                    ),
                },
            ],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "max_tokens": min(
                self._candidate.maximum_output_tokens,
                intent.budget.maximum_output_tokens,
            ),
            "stream": True,
        }
        provider_timeout_seconds = (
            evidence_set.provider_timeout_seconds
            if evidence_set.provider_timeout_seconds is not None
            else intent.budget.maximum_elapsed_seconds
        )
        provider_deadline = self._clock() + provider_timeout_seconds
        attempts = 0
        while attempts < self._routing_protocol.retry_policy.max_attempts:
            remaining_seconds = provider_deadline - self._clock()
            if remaining_seconds <= 0:
                raise ResearchBudgetExceeded("elapsed-time")
            attempts += 1
            if self._budget is not None and not self._budget.reserve():
                raise ResearchError("Aggregate monthly Provider budget is exhausted")
            try:
                with self._client.stream(
                    "POST",
                    f"{self._candidate.base_url.rstrip('/')}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=remaining_seconds,
                ) as response:
                    if (
                        response.status_code
                        in self._routing_protocol.retry_policy.retry_status_codes
                        and attempts < self._routing_protocol.retry_policy.max_attempts
                    ):
                        self._sleeper(
                            self._routing_protocol.retry_policy.backoff_seconds[attempts - 1]
                        )
                        continue
                    if not response.is_success:
                        raise ResearchError(
                            f"DeepSeek Research request returned HTTP {response.status_code}"
                        )
                    if "text/event-stream" not in response.headers.get("content-type", ""):
                        raise ResearchError("DeepSeek Research response was not an SSE stream")
                    yield from self._stream_content(response)
                    return
            except httpx.RequestError as error:
                if attempts >= self._routing_protocol.retry_policy.max_attempts:
                    raise ResearchError("DeepSeek Research request failed") from error
                self._sleeper(self._routing_protocol.retry_policy.backoff_seconds[attempts - 1])
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


class PostgresResearchEvidenceMetadataLoader:
    """Enrich only Evidence identities already admitted by accepted retrieval."""

    def __init__(
        self,
        engine: Engine,
        *,
        timer: Callable[[], float] = monotonic,
    ) -> None:
        self._engine = engine
        self._timer = timer

    def load(
        self,
        evidence_span_ids: tuple[UUID, ...],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[UUID, ResearchEvidenceMetadata]:
        if not evidence_span_ids:
            return {}
        deadline = (
            self._timer() + timeout_seconds if timeout_seconds is not None else None
        )
        if deadline is not None and timeout_seconds <= 0:
            raise ResearchBudgetExceeded("elapsed-time")
        evidence_document = aliased(DocumentVersionRecord)
        evidence_candidate = aliased(CandidateRecord)
        editorial_at = (
            select(func.max(AuditEventRecord.occurred_at))
            .where(
                AuditEventRecord.subject_type == AuditSubjectType.STORY.value,
                AuditEventRecord.subject_id == StoryRecord.id,
                AuditEventRecord.action == AuditAction.STORY_ACCEPTED.value,
            )
            .correlate(StoryRecord)
            .scalar_subquery()
        )
        digest_published_at = (
            select(func.max(DigestRecord.published_at))
            .select_from(DigestStoryRecord)
            .join(DigestRecord, DigestRecord.id == DigestStoryRecord.digest_id)
            .where(
                DigestStoryRecord.story_id == StoryRecord.id,
                DigestRecord.state == DigestState.PUBLISHED.value,
                DigestRecord.published_at.is_not(None),
            )
            .correlate(StoryRecord)
            .scalar_subquery()
        )
        statement = (
            select(
                EvidenceSpanRecord.id.label("evidence_span_id"),
                EvidenceSpanRecord.role,
                EvidenceSpanRecord.relation,
                evidence_candidate.publisher.label("evidence_publisher"),
                StoryRecord.occurred_at.label("event_at"),
                evidence_document.published_at.label("source_published_at"),
                evidence_candidate.discovered_at,
                editorial_at.label("editorial_at"),
                digest_published_at.label("digest_published_at"),
            )
            .select_from(EvidenceSpanRecord)
            .join(ClaimRecord, ClaimRecord.id == EvidenceSpanRecord.claim_id)
            .join(StoryRecord, StoryRecord.id == ClaimRecord.story_id)
            .join(
                evidence_document,
                evidence_document.id == EvidenceSpanRecord.document_version_id,
            )
            .join(evidence_candidate, evidence_candidate.id == evidence_document.candidate_id)
            .where(EvidenceSpanRecord.id.in_(evidence_span_ids))
        )
        try:
            statement_timeout_seconds = reserve_database_acquisition_budget(
                self._engine,
                deadline - self._timer() if deadline is not None else None,
            )
        except DatabaseAcquisitionDeadlineExceeded as error:
            raise ResearchBudgetExceeded("elapsed-time") from error
        try:
            with Session(self._engine) as session:
                if statement_timeout_seconds is not None:
                    timeout_milliseconds = max(
                        1,
                        ceil(statement_timeout_seconds * 1000),
                    )
                    session.execute(
                        select(
                            func.set_config(
                                "statement_timeout",
                                f"{timeout_milliseconds}ms",
                                True,
                            )
                        )
                    )
                rows = tuple(session.execute(statement))
        except Exception as error:
            if deadline is not None and self._timer() >= deadline:
                raise ResearchBudgetExceeded("elapsed-time") from error
            raise
        if deadline is not None and self._timer() >= deadline:
            raise ResearchBudgetExceeded("elapsed-time")
        return {
            row.evidence_span_id: ResearchEvidenceMetadata(
                evidence_role=EvidenceRole(row.role),
                evidence_relation=EvidenceRelation(row.relation),
                evidence_publisher=row.evidence_publisher,
                times=ResearchEvidenceTimes(
                    event=row.event_at,
                    source_publication=row.source_published_at,
                    discovery=row.discovered_at,
                    editorial=row.editorial_at,
                    digest_publication=row.digest_published_at,
                ),
            )
            for row in rows
        }


class _EmptyResearchEvidenceMetadataLoader:
    def load(
        self,
        _evidence_span_ids: tuple[UUID, ...],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[UUID, ResearchEvidenceMetadata]:
        del timeout_seconds
        return {}


class _ResearchGraphNode(StrEnum):
    PLAN = "plan"
    RETRIEVE = "retrieve"
    COMPLETE = "complete"
    REFUSED = "refused"
    LIMIT = "limit"


_RESEARCH_GRAPH_EDGES = {
    _ResearchGraphNode.PLAN: frozenset(
        {
            _ResearchGraphNode.RETRIEVE,
            _ResearchGraphNode.COMPLETE,
            _ResearchGraphNode.REFUSED,
        }
    ),
    _ResearchGraphNode.RETRIEVE: frozenset(
        {
            _ResearchGraphNode.RETRIEVE,
            _ResearchGraphNode.COMPLETE,
            _ResearchGraphNode.LIMIT,
        }
    ),
    _ResearchGraphNode.COMPLETE: frozenset(),
    _ResearchGraphNode.REFUSED: frozenset(),
    _ResearchGraphNode.LIMIT: frozenset(),
}


@dataclass
class _ResearchGraphState:
    intent: QueryIntent
    specs: tuple[_RetrievalRequirementSpec, ...]
    node: _ResearchGraphNode = _ResearchGraphNode.PLAN
    next_spec_index: int = 0
    iterations: int = 0
    execution_limit: str | None = None
    decomposition_failed: bool = False


class _ResearchStateGraph:
    """Schedule bounded retrieval iterations through explicit validated edges."""

    def __init__(
        self,
        intent: QueryIntent,
        specs: tuple[_RetrievalRequirementSpec, ...],
    ) -> None:
        self._overflow_limit = (
            "retrieval-calls"
            if len(specs) > intent.budget.maximum_retrieval_calls
            else None
        )
        self.state = _ResearchGraphState(
            intent=intent,
            specs=specs[: intent.budget.maximum_retrieval_calls],
        )

    def run(self) -> Iterator[tuple[_RetrievalRequirementSpec, ...]]:
        state = self.state
        while state.node not in {
            _ResearchGraphNode.COMPLETE,
            _ResearchGraphNode.REFUSED,
            _ResearchGraphNode.LIMIT,
        }:
            if state.node is _ResearchGraphNode.PLAN:
                if (
                    state.intent.task_type is ResearchTaskType.MULTI_HOP
                    and len(state.specs) < 2
                ):
                    state.decomposition_failed = True
                    self._transition(_ResearchGraphNode.REFUSED)
                elif not state.specs:
                    self._transition(_ResearchGraphNode.COMPLETE)
                else:
                    self._transition(_ResearchGraphNode.RETRIEVE)
                continue

            if state.iterations >= state.intent.budget.maximum_iterations:
                state.execution_limit = self._overflow_limit or "iterations"
                self._transition(_ResearchGraphNode.LIMIT)
                continue

            remaining = len(state.specs) - state.next_spec_index
            batch_size = (
                1
                if state.intent.task_type is ResearchTaskType.MULTI_HOP
                else remaining
            )
            end_index = state.next_spec_index + batch_size
            batch = state.specs[state.next_spec_index : end_index]
            state.next_spec_index = end_index
            state.iterations += 1
            yield batch

            if state.next_spec_index < len(state.specs):
                self._transition(_ResearchGraphNode.RETRIEVE)
            elif self._overflow_limit is not None:
                state.execution_limit = self._overflow_limit
                self._transition(_ResearchGraphNode.LIMIT)
            else:
                self._transition(_ResearchGraphNode.COMPLETE)

    def _transition(self, target: _ResearchGraphNode) -> None:
        state = self.state
        if target not in _RESEARCH_GRAPH_EDGES[state.node]:
            raise ResearchError(
                f"Invalid Research graph transition: {state.node.value} -> {target.value}"
            )
        state.node = target


class ResearchRepository:
    """Assemble bounded Evidence Sets through the shared accepted-knowledge seam."""

    def __init__(
        self,
        engine: Engine | None = None,
        *,
        retrieval: AcceptedKnowledgeOperation | None = None,
        metadata_loader: ResearchEvidenceMetadataLoader | None = None,
    ) -> None:
        if retrieval is None:
            if engine is None:
                raise ValueError("ResearchRepository requires an Engine or Retrieval operation")
            retrieval = AcceptedKnowledgeRetrieval(engine)
        self._retrieval = retrieval
        self._metadata_loader = (
            metadata_loader
            or (
                PostgresResearchEvidenceMetadataLoader(engine)
                if engine is not None
                else _EmptyResearchEvidenceMetadataLoader()
            )
        )

    def retrieve(self, question: str, *, limit: int = 5) -> ResearchEvidenceSet:
        if not question.strip() or limit < 1:
            return ResearchEvidenceSet(question=question, evidence=())
        return self.retrieve_intent(
            interpret_query_intent(question),
            evidence_limit=limit,
        )

    def retrieve_intent(
        self,
        intent: QueryIntent,
        *,
        evidence_limit: int | None = None,
        deadline: float | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> ResearchEvidenceSet:
        limit = min(
            evidence_limit or intent.budget.maximum_evidence_items,
            intent.budget.maximum_evidence_items,
        )
        filters = RetrievalFilters(
            time_semantics=_retrieval_time_semantics(intent.time_semantic),
            time_from=intent.time_range.start,
            time_to=intent.time_range.end,
        )
        selected_hits: list[AcceptedKnowledgeHit] = []
        hits_by_key: dict[tuple[UUID, UUID, UUID], AcceptedKnowledgeHit] = {}
        requirements: list[ResearchRequirement] = []
        missing: list[str] = []
        faults: list[RetrievalFault] = []
        seen_faults: set[tuple[str, str]] = set()
        retrieval_calls = 0
        specs = _retrieval_requirement_specs(intent)
        graph = _ResearchStateGraph(intent, specs)
        execution_limit: str | None = None
        ordinal = 0
        stop_retrieval = False
        for batch in graph.run():
            for spec in batch:
                ordinal += 1
                remaining_seconds = deadline - clock() if deadline is not None else None
                if remaining_seconds is not None and remaining_seconds <= 0:
                    execution_limit = "elapsed-time"
                    stop_retrieval = True
                    break
                retrieval_calls += 1
                try:
                    result = self._retrieval.retrieve(
                        RetrievalQuery(
                            text=spec.query,
                            filters=filters,
                            timeout_seconds=remaining_seconds,
                        )
                    )
                except AcceptedKnowledgeDeadlineExceeded:
                    execution_limit = "elapsed-time"
                    stop_retrieval = True
                    break
                if deadline is not None and clock() >= deadline:
                    execution_limit = "elapsed-time"
                    stop_retrieval = True
                    break
                identifier = f"requirement-{ordinal}"
                requirement_keys: list[tuple[UUID, UUID, UUID]] = []
                for hit in _isolated_retrieval_requirement_hits(
                    result.hits,
                    spec,
                    intent,
                ):
                    key = (hit.story_id, hit.claim_id, hit.evidence_span_id)
                    if key not in hits_by_key and len(selected_hits) < limit:
                        hits_by_key[key] = hit
                        selected_hits.append(hit)
                    if key in hits_by_key:
                        requirement_keys.append(key)
                requirements.append(
                    ResearchRequirement(
                        identifier=identifier,
                        label=spec.label,
                        evidence_keys=tuple(dict.fromkeys(requirement_keys)),
                        entity=spec.entity,
                        dimension=spec.dimension,
                    )
                )
                if not requirement_keys:
                    missing.append(identifier)
                for fault in result.trace.faults:
                    key = (fault.stage, fault.code)
                    if key not in seen_faults:
                        seen_faults.add(key)
                        faults.append(fault)
            if stop_retrieval:
                break
        if execution_limit is None:
            execution_limit = graph.state.execution_limit

        if graph.state.decomposition_failed:
            return ResearchEvidenceSet(
                question=intent.question,
                evidence=(),
                intent=intent,
                iterations=graph.state.iterations,
                decomposition_failed=True,
            )

        if execution_limit == "elapsed-time":
            return ResearchEvidenceSet(
                question=intent.question,
                evidence=(),
                intent=intent,
                requirements=tuple(requirements),
                missing_requirement_ids=tuple(missing),
                retrieval_faults=tuple(faults),
                retrieval_calls=retrieval_calls,
                iterations=graph.state.iterations,
                execution_budget_exhausted=True,
                execution_limit=execution_limit,
            )
        metadata_timeout = deadline - clock() if deadline is not None else None
        if metadata_timeout is not None and metadata_timeout <= 0:
            return ResearchEvidenceSet(
                question=intent.question,
                evidence=(),
                intent=intent,
                requirements=tuple(requirements),
                missing_requirement_ids=tuple(missing),
                retrieval_faults=tuple(faults),
                retrieval_calls=retrieval_calls,
                iterations=graph.state.iterations,
                execution_budget_exhausted=True,
                execution_limit="elapsed-time",
            )
        try:
            metadata = self._metadata_loader.load(
                tuple(hit.evidence_span_id for hit in selected_hits),
                timeout_seconds=metadata_timeout,
            )
        except ResearchBudgetExceeded:
            return ResearchEvidenceSet(
                question=intent.question,
                evidence=(),
                intent=intent,
                requirements=tuple(requirements),
                missing_requirement_ids=tuple(missing),
                retrieval_faults=tuple(faults),
                retrieval_calls=retrieval_calls,
                iterations=graph.state.iterations,
                execution_budget_exhausted=True,
                execution_limit="elapsed-time",
            )
        if deadline is not None and clock() >= deadline:
            return ResearchEvidenceSet(
                question=intent.question,
                evidence=(),
                intent=intent,
                requirements=tuple(requirements),
                missing_requirement_ids=tuple(missing),
                retrieval_faults=tuple(faults),
                retrieval_calls=retrieval_calls,
                iterations=graph.state.iterations,
                execution_budget_exhausted=True,
                execution_limit="elapsed-time",
            )
        evidence = tuple(
            ResearchEvidence(
                story_id=hit.story_id,
                story_stable_key=hit.story_stable_key,
                story_headline=hit.story_headline,
                claim_id=hit.claim_id,
                claim_text=hit.claim_text,
                evidence_span_id=hit.evidence_span_id,
                exact_text=hit.exact_text,
                evidence_role=(item_metadata.evidence_role if item_metadata else None),
                evidence_relation=(
                    item_metadata.evidence_relation if item_metadata else None
                ),
                evidence_publisher=(
                    item_metadata.evidence_publisher if item_metadata else None
                ),
                times=(item_metadata.times if item_metadata else ResearchEvidenceTimes()),
            )
            for hit in selected_hits
            for item_metadata in (metadata.get(hit.evidence_span_id),)
        )
        if intent.time_range.start is not None or intent.time_range.end is not None:
            evidence = tuple(
                item for item in evidence if _evidence_matches_intent_time(item, intent)
            )
            retained_keys = {item.citation_key for item in evidence}
            requirements = [
                replace(
                    requirement,
                    evidence_keys=tuple(
                        key for key in requirement.evidence_keys if key in retained_keys
                    ),
                )
                for requirement in requirements
            ]
            missing = list(
                dict.fromkeys(
                    [*missing]
                    + [
                        requirement.identifier
                        for requirement in requirements
                        if not requirement.evidence_keys
                    ]
                )
            )
        return ResearchEvidenceSet(
            question=intent.question,
            evidence=evidence,
            intent=intent,
            requirements=tuple(requirements),
            missing_requirement_ids=tuple(missing),
            retrieval_faults=tuple(faults),
            retrieval_calls=retrieval_calls,
            iterations=graph.state.iterations,
            execution_budget_exhausted=(
                execution_limit is not None
            ),
            execution_limit=execution_limit,
        )


class PersistentAnonymousResearchAllowance:
    """Persist a privacy-preserving daily Provider-call allowance in PostgreSQL."""

    def __init__(
        self,
        engine: Engine,
        *,
        daily_limit: int,
        identity_salt: bytes,
        today: Callable[[], date] | None = None,
    ) -> None:
        if daily_limit < 1:
            raise ValueError("Anonymous Research daily limit must be positive")
        if not identity_salt:
            raise ValueError("Anonymous Research identity salt must not be empty")
        self._repository = AnonymousResearchAllowanceRepository(engine)
        self._daily_limit = daily_limit
        self._identity_salt = identity_salt
        self._today = today or (lambda: datetime.now(UTC).date())

    def reserve(self, anonymous_client_id: str) -> bool:
        normalized_id = anonymous_client_id.strip()
        if not normalized_id:
            return False
        client_hash = hmac.new(
            self._identity_salt,
            normalized_id.encode("utf-8"),
            sha256,
        ).hexdigest()
        return self._repository.reserve(
            usage_date=self._today(),
            client_hash=client_hash,
            daily_limit=self._daily_limit,
        )


def _advanced_answer_lacks_required_story_coverage(
    intent: QueryIntent,
    story_ids: set[UUID],
) -> bool:
    requires_multiple_stories = (
        intent.task_type is ResearchTaskType.MULTI_HOP
        or (
            intent.task_type is ResearchTaskType.COMPARISON
            and len(intent.entities) >= 2
        )
    )
    return requires_multiple_stories and len(story_ids) < 2


def stream_research_events(
    question: str,
    *,
    repository: ResearchRepository,
    provider: ResearchProvider | None,
    allowance: ResearchAllowance | None = None,
    anonymous_client_id: str | None = None,
    clock: Callable[[], float] = monotonic,
) -> Iterator[tuple[str, dict[str, object]]]:
    protocol = load_research_protocol()
    version = protocol.sse_contract_version
    intent = interpret_query_intent(question)
    deadline = clock() + intent.budget.maximum_elapsed_seconds
    yield (
        "status",
        {
            "version": version,
            "state": "retrieving",
            "intent": _query_intent_payload(intent),
        },
    )
    try:
        evidence_set = repository.retrieve_intent(
            intent,
            deadline=deadline,
            clock=clock,
        )
    except Exception:  # noqa: BLE001 - retrieval failures must terminate as SSE.
        yield (
            "error",
            {
                "version": version,
                "code": "retrieval-failed",
                "message": "Research 检索失败，未生成答案。",
            },
        )
        yield "done", {"version": version, "status": "failed"}
        return
    if evidence_set.execution_budget_exhausted:
        yield (
            "error",
            {
                "version": version,
                "code": "execution-budget-exceeded",
                "message": (
                    "Research 已达到执行时间上限，未生成答案。"
                    if evidence_set.execution_limit == "elapsed-time"
                    else "Research 已达到有界检索调用上限，未生成答案。"
                ),
                "limit": evidence_set.execution_limit,
                "iterations": evidence_set.iterations,
            },
        )
        yield "done", {"version": version, "status": "failed"}
        return
    if evidence_set.decomposition_failed:
        yield (
            "refusal",
            {
                "version": version,
                "reason": "unsupported-decomposition",
                "message": "无法在有界执行预算内将问题分解为至少两个可验证步骤。",
            },
        )
        yield "done", {"version": version, "status": "refused"}
        return
    if (
        evidence_set.missing_requirement_ids
        and intent.task_type is not ResearchTaskType.SIMPLE_LOOKUP
    ):
        reason = (
            "missing-intermediate-evidence"
            if intent.task_type is ResearchTaskType.MULTI_HOP
            else "insufficient-evidence"
        )
        message = (
            "证据不足：回答所需的中间证据缺失。"
            if intent.task_type is ResearchTaskType.MULTI_HOP
            else "证据不足：请求范围内缺少必要支持。"
        )
        yield (
            "refusal",
            {
                "version": version,
                "reason": reason,
                "message": message,
                "missing_requirements": list(evidence_set.missing_requirement_ids),
            },
        )
        yield "done", {"version": version, "status": "refused"}
        return
    if not evidence_set.evidence:
        yield from _insufficient_evidence_events(version)
        return
    if _advanced_answer_lacks_required_story_coverage(
        intent,
        {item.story_id for item in evidence_set.evidence},
    ):
        yield from _insufficient_evidence_events(version)
        return
    fault_payloads = [
        {"stage": fault.stage, "code": fault.code}
        for fault in evidence_set.retrieval_faults
    ]
    if (
        fault_payloads
        and intent.task_type is not ResearchTaskType.SIMPLE_LOOKUP
    ):
        yield (
            "status",
            {
                "version": version,
                "state": "retrieval-degraded",
                "fallback": _retrieval_fallback_name(evidence_set.retrieval_faults),
                "faults": fault_payloads,
            },
        )
    if intent.task_type is not ResearchTaskType.SIMPLE_LOOKUP:
        yield (
            "status",
            {
                "version": version,
                "state": "evidence-assembled",
                "retrieval_calls": evidence_set.retrieval_calls,
                "evidence_items": len(evidence_set.evidence),
                "iterations": evidence_set.iterations,
            },
        )
    if provider is None:
        yield (
            "error",
            {
                "version": version,
                "code": "provider-unavailable",
                "message": "Research Provider 当前不可用。",
            },
        )
        yield "done", {"version": version, "status": "failed"}
        return

    if allowance is not None and not allowance.reserve(anonymous_client_id or ""):
        yield (
            "refusal",
            {
                "version": version,
                "code": "anonymous-allowance-exhausted",
                "message": "匿名 Research 今日额度已用尽，请明日再试。",
            },
        )
        yield "done", {"version": version, "status": "refused"}
        return

    yield (
        "status",
        {
            "version": version,
            "state": "generating",
            "retrieval_degraded": bool(fault_payloads),
            "retrieval_fallback": (
                _retrieval_fallback_name(evidence_set.retrieval_faults)
                if fault_payloads
                else None
            ),
            "retrieval_faults": fault_payloads,
        },
    )
    try:
        remaining_seconds = deadline - clock()
        if remaining_seconds <= 0:
            raise ResearchBudgetExceeded("elapsed-time")
        provider_evidence_set = replace(
            evidence_set,
            provider_timeout_seconds=remaining_seconds,
        )
        answer = _validated_provider_answer(
            provider,
            provider_evidence_set,
            protocol,
            deadline=deadline,
            clock=clock,
        )
    except ResearchBudgetExceeded as error:
        yield (
            "error",
            {
                "version": version,
                "code": "execution-budget-exceeded",
                "message": "Research 已达到执行时间上限，未生成答案。",
                "limit": error.limit,
            },
        )
        yield "done", {"version": version, "status": "failed"}
        return
    except ResearchProviderOutputRejected as error:
        LOGGER.warning("Research Provider output rejected: %s", error)
        yield (
            "error",
            {
                "version": version,
                "code": "provider-output-rejected",
                "message": "Research Provider 输出未通过验证。",
            },
        )
        yield "done", {"version": version, "status": "failed"}
        return
    except Exception as error:  # noqa: BLE001 - external failures must fail closed.
        LOGGER.warning(
            "Research Provider failed with unexpected error type: %s",
            type(error).__name__,
        )
        yield (
            "error",
            {
                "version": version,
                "code": "provider-failed",
                "message": "Research Provider 当前不可用。",
            },
        )
        yield "done", {"version": version, "status": "failed"}
        return

    if answer is None:
        yield (
            "refusal",
            {
                "version": version,
                "reason": "provider-abstained",
                "message": "Provider 未能根据已检索证据生成受支持的答案。",
            },
        )
        yield "done", {"version": version, "status": "refused"}
        return

    if intent.task_type is not ResearchTaskType.SIMPLE_LOOKUP:
        yield (
            "status",
            {
                "version": version,
                "state": "verifying-citations",
            },
        )

    for start in range(0, len(answer.text), ANSWER_DELTA_CHARACTERS):
        yield (
            "answer.delta",
            {"version": version, "text": answer.text[start : start + ANSWER_DELTA_CHARACTERS]},
        )
    for citation in answer.citations:
        story_url = f"/stories/{quote(citation.story_stable_key, safe='')}"
        yield (
            "citation",
            {
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
                "statement_indexes": [
                    index
                    for index, statement in enumerate(answer.statements, start=1)
                    if citation.citation_key
                    in {item.citation_key for item in statement.citations}
                ],
                "statement_support": [
                    {
                        "statement_index": index,
                        "dimension": statement.dimension,
                        "time_semantic": statement.time_semantic,
                    }
                    for index, statement in enumerate(answer.statements, start=1)
                    if citation.citation_key
                    in {item.citation_key for item in statement.citations}
                ],
                "evidence_role": (
                    citation.evidence_role.value
                    if citation.evidence_role is not None
                    else None
                ),
                "evidence_relation": (
                    citation.evidence_relation.value
                    if citation.evidence_relation is not None
                    else None
                ),
                "evidence_publisher": citation.evidence_publisher,
                "times": {
                    label: value.isoformat() if value is not None else None
                    for label, value in citation.times.as_labeled_values()
                },
            },
        )
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
        "maximum_iterations",
        "maximum_retrieval_calls",
        "maximum_elapsed_seconds",
        "maximum_evidence_items",
        "maximum_output_tokens",
        "maximum_provider_output_characters",
        "execution_budgets",
        "system_prompt",
        "simple_lookup_system_prompt",
        "user_prompt_template",
        "output_contract",
        "sse_events",
    }
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise ResearchError("Research protocol keys do not match v2")
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
    if fields != {"question", "query_intent_json", "evidence_json"}:
        raise ResearchError("Research prompt placeholders do not match v2")
    expected_output_contract = {
        "v2_required_keys": ["answer", "support"],
        "legacy_simple_required_keys": ["answer", "citations"],
        "support_required_keys": [
            "statement",
            "citations",
            "requirement_ids",
            "dimension",
            "time_semantic",
        ],
        "citation_required_keys": [
            "story_id",
            "claim_id",
            "evidence_span_id",
        ],
        "v2_abstention_shape": {"answer": None, "support": []},
        "legacy_simple_abstention_shape": {"answer": None, "citations": []},
        "additional_properties": False,
        "support_additional_properties": False,
        "citation_additional_properties": False,
    }
    if payload["output_contract"] != expected_output_contract:
        raise ResearchError("Research output contract does not match v2")
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

    maximum_iterations = int(payload["maximum_iterations"])
    maximum_retrieval_calls = int(payload["maximum_retrieval_calls"])
    maximum_elapsed_seconds = float(payload["maximum_elapsed_seconds"])
    maximum_evidence_items = int(payload["maximum_evidence_items"])
    maximum_output_tokens = int(payload["maximum_output_tokens"])
    maximum_provider_output_characters = int(payload["maximum_provider_output_characters"])
    if (
        not 1 <= maximum_iterations <= 4
        or not 1 <= maximum_retrieval_calls <= 8
        or not 0 < maximum_elapsed_seconds <= 60
        or not 1 <= maximum_evidence_items <= 20
        or maximum_output_tokens <= 0
        or maximum_provider_output_characters <= 0
    ):
        raise ResearchError("Research protocol budgets are invalid")
    execution_budgets = _validated_execution_budgets(
        payload["execution_budgets"],
        maximum_iterations=maximum_iterations,
        maximum_retrieval_calls=maximum_retrieval_calls,
        maximum_evidence_items=maximum_evidence_items,
    )
    return ResearchProtocol(
        **{
            **payload,
            "maximum_iterations": maximum_iterations,
            "maximum_retrieval_calls": maximum_retrieval_calls,
            "maximum_elapsed_seconds": maximum_elapsed_seconds,
            "maximum_evidence_items": maximum_evidence_items,
            "maximum_output_tokens": maximum_output_tokens,
            "maximum_provider_output_characters": maximum_provider_output_characters,
            "execution_budgets": execution_budgets,
            "sse_events": expected_sse_events,
        },
        routing_evaluation_cases_sha256=evaluation.cases_sha256,
        content_sha256=sha256(raw).hexdigest(),
    )


def _validated_provider_answer(
    provider: ResearchProvider,
    evidence_set: ResearchEvidenceSet,
    protocol: ResearchProtocol,
    *,
    deadline: float | None = None,
    clock: Callable[[], float] = monotonic,
) -> ResearchAnswer | None:
    intent = evidence_set.intent or interpret_query_intent(evidence_set.question)
    parts: list[str] = []
    characters = 0
    for part in provider.stream(evidence_set):
        if deadline is not None and clock() >= deadline:
            raise ResearchBudgetExceeded("elapsed-time")
        if not isinstance(part, str):
            raise ResearchProviderOutputRejected("Provider stream chunks must be text")
        characters += len(part)
        if characters > intent.budget.maximum_provider_output_characters:
            raise ResearchProviderOutputRejected(
                "Provider output exceeded its bounded size"
            )
        parts.append(part)
    try:
        payload = json.loads("".join(parts))
    except (json.JSONDecodeError, TypeError) as error:
        raise ResearchProviderOutputRejected(
            "Provider output is not valid JSON"
        ) from error
    if not isinstance(payload, dict):
        raise ResearchProviderOutputRejected(
            "Provider output keys do not match the Research contract"
        )

    keys = set(payload)
    try:
        if keys == set(protocol.output_contract["v2_required_keys"]):
            return _validated_v2_provider_answer(payload, evidence_set, intent, protocol)
        if (
            intent.task_type is ResearchTaskType.SIMPLE_LOOKUP
            and keys == set(protocol.output_contract["legacy_simple_required_keys"])
        ):
            return _validated_legacy_simple_answer(payload, evidence_set, protocol)
        raise ResearchError("Provider output keys do not match the Research contract")
    except ResearchError as error:
        raise ResearchProviderOutputRejected(str(error)) from error


def _validated_legacy_simple_answer(
    payload: dict[str, object],
    evidence_set: ResearchEvidenceSet,
    protocol: ResearchProtocol,
) -> ResearchAnswer | None:
    answer = payload["answer"]
    raw_citations = payload["citations"]
    if answer is None:
        if payload != protocol.output_contract["legacy_simple_abstention_shape"]:
            raise ResearchError("Provider abstention shape is invalid")
        return None
    text = _validated_answer_text(answer)
    citations = _validated_citations(raw_citations, evidence_set, protocol)
    if not citations:
        raise ResearchError("Provider answer must cite retrieved Evidence")
    return ResearchAnswer(
        text=text,
        citations=citations,
        statements=(ResearchSupportedStatement(text=text, citations=citations),),
    )


def _validated_v2_provider_answer(
    payload: dict[str, object],
    evidence_set: ResearchEvidenceSet,
    intent: QueryIntent,
    protocol: ResearchProtocol,
) -> ResearchAnswer | None:
    answer = payload["answer"]
    raw_support = payload["support"]
    if answer is None:
        if payload != protocol.output_contract["v2_abstention_shape"]:
            raise ResearchError("Provider abstention shape is invalid")
        return None
    answer_text = _validated_answer_text(answer)
    if not isinstance(raw_support, list) or not raw_support:
        raise ResearchError("Provider answer must map every statement to Evidence")

    requirements = {
        requirement.identifier: requirement
        for requirement in evidence_set.requirements
        if requirement.evidence_keys
    }
    required_support_keys = set(protocol.output_contract["support_required_keys"])
    statements: list[str] = []
    supported_statements: list[ResearchSupportedStatement] = []
    citations: list[ResearchEvidence] = []
    seen_citations: set[tuple[UUID, UUID, UUID]] = set()
    covered_requirements: set[str] = set()
    covered_dimensions: set[str] = set()
    cited_stories: set[UUID] = set()
    for raw_item in raw_support:
        if not isinstance(raw_item, dict) or set(raw_item) != required_support_keys:
            raise ResearchError("Provider support item shape is invalid")
        statement = _validated_answer_text(raw_item["statement"])
        item_citations = _validated_citations(
            raw_item["citations"],
            evidence_set,
            protocol,
        )
        if not item_citations:
            raise ResearchError("Every material statement must cite Evidence")
        raw_requirement_ids = raw_item["requirement_ids"]
        dimension = raw_item["dimension"]
        time_semantic = raw_item["time_semantic"]
        if (
            not isinstance(raw_requirement_ids, list)
            or not raw_requirement_ids
            or any(not isinstance(value, str) or not value for value in raw_requirement_ids)
            or len(set(raw_requirement_ids)) != len(raw_requirement_ids)
        ):
            raise ResearchError("Provider support requirement identities are invalid")
        citation_keys = {item.citation_key for item in item_citations}
        requirement_evidence_keys: set[tuple[UUID, UUID, UUID]] = set()
        for identifier in raw_requirement_ids:
            requirement = requirements.get(identifier)
            if requirement is None or not citation_keys.intersection(
                requirement.evidence_keys
            ):
                raise ResearchError("Provider support does not cover its retrieval requirement")
            if (
                intent.task_type is ResearchTaskType.COMPARISON
                and requirement.dimension != dimension
            ):
                raise ResearchError(
                    "Comparison support crossed an entity-dimension requirement"
                )
            requirement_evidence_keys.update(requirement.evidence_keys)
            covered_requirements.add(identifier)
        if (
            intent.task_type is ResearchTaskType.COMPARISON
            and not citation_keys.issubset(requirement_evidence_keys)
        ):
            raise ResearchError(
                "Comparison support cited Evidence from another entity-dimension requirement"
            )

        if intent.task_type is ResearchTaskType.COMPARISON:
            if not isinstance(dimension, str) or dimension not in intent.dimensions:
                raise ResearchError("Comparison output lost a requested dimension")
            covered_dimensions.add(dimension)
            if time_semantic is not None:
                raise ResearchError("Comparison output mixed in a timeline semantic")
        elif dimension is not None:
            raise ResearchError("Non-comparison output returned a comparison dimension")

        if intent.task_type is ResearchTaskType.TIMELINE:
            if not isinstance(time_semantic, str) or time_semantic not in {
                label
                for evidence in item_citations
                for label, value in evidence.times.as_labeled_values()
                if value is not None
            }:
                raise ResearchError("Timeline output conflated or invented a time semantic")
            if (
                intent.time_semantic is not ResearchTimeSemantic.ALL
                and time_semantic != intent.time_semantic.value
            ):
                raise ResearchError("Timeline output ignored the requested time semantic")
        elif time_semantic is not None:
            raise ResearchError("Non-timeline output returned a timeline semantic")

        statements.append(statement)
        supported_statements.append(
            ResearchSupportedStatement(
                text=statement,
                citations=item_citations,
                dimension=dimension,
                time_semantic=time_semantic,
            )
        )
        cited_stories.update(item.story_id for item in item_citations)
        for citation in item_citations:
            if citation.citation_key not in seen_citations:
                seen_citations.add(citation.citation_key)
                citations.append(citation)

    canonical_answer = _normalized_supported_answer("\n".join(statements))
    if _normalized_supported_answer(answer_text) != canonical_answer:
        raise ResearchError("Provider answer does not match its supported statements")
    if set(requirements) != covered_requirements:
        raise ResearchError("Provider answer omitted a required evidence step")
    if (
        intent.task_type is ResearchTaskType.COMPARISON
        and set(intent.dimensions) != covered_dimensions
    ):
        raise ResearchError("Comparison output omitted a requested dimension")
    if _advanced_answer_lacks_required_story_coverage(intent, cited_stories):
        raise ResearchError("Advanced Research collapsed distinct Stories")
    return ResearchAnswer(
        text=answer_text,
        citations=tuple(citations),
        statements=tuple(supported_statements),
    )


def _normalized_supported_answer(value: str) -> str:
    return "\n".join(line.strip() for line in value.splitlines() if line.strip())


def _validated_answer_text(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or CHINESE_CHARACTER.search(value) is None
        or FORBIDDEN_ANSWER_URL.search(value) is not None
        or FORBIDDEN_REASONING.search(value) is not None
    ):
        raise ResearchError("Provider answer content is invalid")
    return value.strip()


def _validated_citations(
    raw_citations: object,
    evidence_set: ResearchEvidenceSet,
    protocol: ResearchProtocol,
) -> tuple[ResearchEvidence, ...]:
    if not isinstance(raw_citations, list):
        raise ResearchError("Provider citations must be a list")
    retrieved = {item.citation_key: item for item in evidence_set.evidence}
    citations: list[ResearchEvidence] = []
    seen: set[tuple[UUID, UUID, UUID]] = set()
    for raw_citation in raw_citations:
        citation_required_keys = set(protocol.output_contract["citation_required_keys"])
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
    return tuple(citations)


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


def _retrieval_fallback_name(faults: tuple[RetrievalFault, ...]) -> str:
    if any(fault.stage == "embedding" for fault in faults):
        return "fts-exact-entity-fusion"
    if any(fault.stage == "reranker" for fault in faults):
        return "deterministic-fusion"
    return "accepted-knowledge-fallback"


def _evidence_matches_intent_time(
    evidence: ResearchEvidence,
    intent: QueryIntent,
) -> bool:
    values = dict(evidence.times.as_labeled_values())
    timestamps = (
        tuple(value for value in values.values() if value is not None)
        if intent.time_semantic is ResearchTimeSemantic.ALL
        else tuple(
            value
            for value in (values[intent.time_semantic.value],)
            if value is not None
        )
    )
    return any(
        (intent.time_range.start is None or value >= intent.time_range.start)
        and (intent.time_range.end is None or value < intent.time_range.end)
        for value in timestamps
    )


def _selected_research_candidate(
    candidates: tuple[ModelCandidate, ...],
    identifier: str,
) -> ModelCandidate:
    matches = tuple(candidate for candidate in candidates if candidate.identifier == identifier)
    if len(matches) != 1 or matches[0].provider != "deepseek":
        raise ResearchError("Approved Research DeepSeek route is unavailable")
    return matches[0]


def _research_task_type(question: str) -> ResearchTaskType:
    if TIMELINE_MARKER.search(question) is not None:
        return ResearchTaskType.TIMELINE
    if COMPARISON_MARKER.search(question) is not None:
        return ResearchTaskType.COMPARISON
    if MULTI_HOP_MARKER.search(question) is not None:
        return ResearchTaskType.MULTI_HOP
    return ResearchTaskType.SIMPLE_LOOKUP


def _query_intent_payload(intent: QueryIntent) -> dict[str, object]:
    return {
        "task_type": intent.task_type.value,
        "entities": list(intent.entities),
        "time_range": {
            "start": (
                intent.time_range.start.isoformat()
                if intent.time_range.start is not None
                else None
            ),
            "end": (
                intent.time_range.end.isoformat()
                if intent.time_range.end is not None
                else None
            ),
        },
        "retrieval_time_semantics": list(
            _retrieval_time_semantics(intent.time_semantic)
        ),
        "answer_time_semantic": (
            intent.time_semantic.value
            if intent.task_type is ResearchTaskType.TIMELINE
            else None
        ),
        "scope": intent.scope,
        "dimensions": list(intent.dimensions),
        "budget": {
            "maximum_iterations": intent.budget.maximum_iterations,
            "maximum_retrieval_calls": intent.budget.maximum_retrieval_calls,
            "maximum_evidence_items": intent.budget.maximum_evidence_items,
            "maximum_output_tokens": intent.budget.maximum_output_tokens,
            "maximum_provider_output_characters": (
                intent.budget.maximum_provider_output_characters
            ),
            "maximum_elapsed_seconds": intent.budget.maximum_elapsed_seconds,
        },
    }


@dataclass(frozen=True)
class _RetrievalRequirementSpec:
    label: str
    query: str
    entity: str | None = None
    dimension: str | None = None
    match_terms: tuple[str, ...] = ()
    exclude_terms: tuple[str, ...] = ()


def _hit_matches_retrieval_requirement(
    hit: AcceptedKnowledgeHit,
    spec: _RetrievalRequirementSpec,
    intent: QueryIntent,
) -> bool:
    searchable = _retrieval_hit_searchable(hit)
    if intent.task_type is ResearchTaskType.SIMPLE_LOOKUP:
        return _simple_lookup_hit_supports_requested_attribute(hit, intent)
    if intent.task_type is ResearchTaskType.MULTI_HOP:
        return bool(spec.match_terms) and any(
            term in searchable for term in spec.match_terms
        )
    if intent.task_type is not ResearchTaskType.COMPARISON:
        return True
    if spec.entity is None or spec.entity.casefold() not in searchable:
        return False
    if spec.dimension is None or spec.dimension in ENTITY_SCOPED_COMPARISON_DIMENSIONS:
        return True
    terms = _semantic_match_terms(spec.dimension)
    minimum_matches = max(1, ceil(len(terms) / 2))
    return sum(term in searchable for term in terms) >= minimum_matches


def _simple_lookup_hit_supports_requested_attribute(
    hit: AcceptedKnowledgeHit,
    intent: QueryIntent,
) -> bool:
    if SIMPLE_LOOKUP_PRICE_QUESTION.search(intent.question) is None:
        return True
    subject_entities = tuple(
        normalized
        for entity in intent.entities
        for normalized in (
            re.sub(
                r"^(?:how|what|is|are|does|do)\s+",
                "",
                entity,
                flags=re.IGNORECASE,
            ).strip(),
        )
        if normalized and normalized.casefold() not in {"how", "what", "is", "are"}
    )
    if not subject_entities:
        return False
    claim_evidence_text = f"{hit.claim_text}。{hit.exact_text}"
    for statement in re.split(
        r"(?<!\d)\.|\.(?!\d)|[!?。！？；;]+",
        claim_evidence_text,
    ):
        searchable_statement = statement.casefold()
        if not any(
            entity.casefold() in searchable_statement for entity in subject_entities
        ):
            continue
        if (
            SIMPLE_LOOKUP_PRICE_CUE.search(statement) is not None
            and SIMPLE_LOOKUP_MONETARY_VALUE.search(statement) is not None
        ):
            return True
        if (
            SIMPLE_LOOKUP_AFFIRMATIVE_FREE_VALUE.search(statement) is not None
            and SIMPLE_LOOKUP_NEGATED_FREE_VALUE.search(statement) is None
        ):
            return True
    return False


def _isolated_retrieval_requirement_hits(
    hits: tuple[AcceptedKnowledgeHit, ...],
    spec: _RetrievalRequirementSpec,
    intent: QueryIntent,
) -> tuple[AcceptedKnowledgeHit, ...]:
    matched = tuple(
        hit for hit in hits if _hit_matches_retrieval_requirement(hit, spec, intent)
    )
    if not matched:
        return ()
    if intent.task_type is ResearchTaskType.COMPARISON and spec.entity is not None:
        other_entities = tuple(
            entity.casefold()
            for entity in intent.entities
            if entity.casefold() != spec.entity.casefold()
        )
        entity_exclusive = tuple(
            hit
            for hit in matched
            if not any(
                entity in _retrieval_hit_searchable(hit) for entity in other_entities
            )
        )
        if entity_exclusive:
            return entity_exclusive
    if intent.task_type is ResearchTaskType.MULTI_HOP and spec.match_terms:
        clause_exclusive = tuple(
            hit
            for hit in matched
            if not any(
                term in _retrieval_hit_searchable(hit) for term in spec.exclude_terms
            )
        )
        if clause_exclusive:
            matched = clause_exclusive
        scores = tuple(
            sum(term in _retrieval_hit_searchable(hit) for term in spec.match_terms)
            for hit in matched
        )
        strongest_score = max(scores)
        return tuple(
            hit
            for hit, score in zip(matched, scores, strict=True)
            if score == strongest_score
        )
    return matched


def _retrieval_hit_searchable(hit: AcceptedKnowledgeHit) -> str:
    return (
        f"{hit.story_stable_key} {hit.story_headline} "
        f"{hit.claim_text} {hit.exact_text}"
    ).casefold()


def _semantic_match_terms(value: str) -> tuple[str, ...]:
    latin_terms = tuple(
        match.casefold()
        for match in re.findall(r"[0-9A-Za-z][0-9A-Za-z._/+:-]*", value)
    )
    chinese_terms = tuple(
        chunk if len(chunk) == 2 else chunk[index : index + 2]
        for chunk in re.findall(r"[\u3400-\u9fff]{2,}", value)
        for index in range(max(1, len(chunk) - 1))
    )
    return tuple(dict.fromkeys((*latin_terms, *chinese_terms)))


def _multi_hop_match_terms(value: str) -> tuple[str, ...]:
    latin_terms = tuple(
        match.casefold()
        for match in re.findall(r"[A-Za-z][0-9A-Za-z._/+:-]*", value)
    )
    chinese_source = re.sub(
        r"如何|怎样|影响|导致|促成|之后|然后|随后|并且|以及|什么|为何|的|了|会|后",
        " ",
        value,
    )
    chinese_terms = tuple(
        term
        for chunk in re.findall(r"[\u3400-\u9fff]{2,}", chinese_source)
        for term in (
            chunk,
            *(chunk[index : index + 2] for index in range(len(chunk) - 1)),
        )
    )
    return tuple(dict.fromkeys((*latin_terms, *chinese_terms)))


def _multi_hop_requirement_specs(
    queries: tuple[str, ...],
) -> tuple[_RetrievalRequirementSpec, ...]:
    terms_by_query = tuple(_multi_hop_match_terms(query) for query in queries)
    specs: list[_RetrievalRequirementSpec] = []
    for index, query in enumerate(queries):
        own_terms = terms_by_query[index]
        other_terms = tuple(
            dict.fromkeys(
                term
                for other_index, terms in enumerate(terms_by_query)
                if other_index != index
                for term in terms
            )
        )
        own_unique_terms = tuple(term for term in own_terms if term not in other_terms)
        foreign_unique_terms = tuple(term for term in other_terms if term not in own_terms)
        specs.append(
            _RetrievalRequirementSpec(
                label=query if len(query) <= 48 else f"{query[:47].rstrip()}…",
                query=query,
                match_terms=own_unique_terms or own_terms,
                exclude_terms=foreign_unique_terms,
            )
        )
    return tuple(specs)


def _normalized_multi_hop_clause(value: str) -> str:
    normalized = value.strip(" ，,。！？?：:")
    normalized = re.sub(
        r"^(?:请\s*)?多跳(?:检索|研究)\s*",
        "",
        normalized,
    )
    normalized = re.sub(
        r"(?:分别)?(?:体现|说明|反映)(?:了)?(?:哪些|什么|怎样的).+$",
        "",
        normalized,
    )
    return normalized.strip(" ，,。！？?：:")


def _retrieval_requirement_specs(
    intent: QueryIntent,
) -> tuple[_RetrievalRequirementSpec, ...]:
    if intent.task_type is ResearchTaskType.COMPARISON and intent.entities:
        return tuple(
            _RetrievalRequirementSpec(
                label=f"{entity} × {dimension}",
                query=f"{entity} {dimension}",
                entity=entity,
                dimension=dimension,
            )
            for entity in intent.entities
            for dimension in intent.dimensions
        )
    if intent.task_type is ResearchTaskType.TIMELINE and intent.entities:
        return tuple(
            _RetrievalRequirementSpec(label=entity, query=entity)
            for entity in intent.entities
        )
    if intent.task_type is ResearchTaskType.MULTI_HOP:
        clauses = tuple(
            normalized
            for clause in re.split(
                r"[；;。！？!?]+|(?:，|,)?(?:然后|随后|并且|以及)(?:，|,)?",
                intent.question,
            )
            for normalized in (_normalized_multi_hop_clause(clause),)
            if normalized
        )
        if len(clauses) >= 2:
            return _multi_hop_requirement_specs(clauses)
        causal = re.fullmatch(
            r"(.+?)(?:后|之后)(?:会)?(?:如何|怎样)(?:影响|导致|促成)(.+)",
            intent.question.strip(" ，,。！？?：:"),
        )
        if causal is not None:
            return _multi_hop_requirement_specs(
                tuple(
                    value.strip(" ，,。！？?：:") for value in causal.groups()
                )
            )
        generic_causal = re.fullmatch(
            r"(.+?)(?:会)?(?:如何|怎样)(?:影响|导致|促成)(.+)",
            intent.question.strip(" ，,。！？?：:"),
        )
        if generic_causal is not None:
            return _multi_hop_requirement_specs(
                tuple(
                    value.strip(" ，,。！？?：:")
                    for value in generic_causal.groups()
                )
            )
        return ()
    return (
        _RetrievalRequirementSpec(
            label=intent.task_type.value,
            query=intent.question,
        ),
    )


def _query_entities(
    question: str,
    *,
    task_type: ResearchTaskType,
) -> tuple[str, ...]:
    entities: list[str] = []
    introduced_products = (
        {
            match.group(1).strip().casefold()
            for match in INTRODUCED_PRODUCT_ENTITY.finditer(question)
        }
        if task_type is ResearchTaskType.MULTI_HOP
        else set()
    )
    for value in (
        *(
            value
            for match in EXPLICIT_COMPARISON_ENTITIES.finditer(question)
            for value in match.groups()
        ),
        *(match.group(1) for match in QUOTED_ENTITY.finditer(question)),
        *(match.group(1) for match in LATIN_ENTITY.finditer(question)),
    ):
        normalized = value.strip(" ，,。！？?：:；;")
        if (
            normalized
            and normalized.casefold() not in ENTITY_STOPWORDS_CASEFOLDED
            and normalized.casefold() not in introduced_products
            and normalized.casefold() not in {item.casefold() for item in entities}
        ):
            entities.append(normalized)
        if len(entities) == 4:
            break
    return tuple(entities)


def _query_time_range(question: str) -> QueryTimeRange:
    if match := YEAR_RANGE.search(question):
        start_year, end_year = (int(value) for value in match.groups())
        if start_year > end_year:
            raise ResearchError("Research time range must be in ascending order")
        return QueryTimeRange(
            start=datetime(start_year, 1, 1, tzinfo=UTC),
            end=datetime(end_year + 1, 1, 1, tzinfo=UTC),
        )
    if match := SINGLE_YEAR.search(question):
        year = int(match.group(1))
        return QueryTimeRange(
            start=datetime(year, 1, 1, tzinfo=UTC),
            end=datetime(year + 1, 1, 1, tzinfo=UTC),
        )
    return QueryTimeRange()


def _query_time_semantic(
    question: str,
    task_type: ResearchTaskType,
) -> ResearchTimeSemantic:
    markers = (
        (
            ResearchTimeSemantic.DIGEST_PUBLICATION,
            r"Digest\s*发布|日报发布|摘要发布|digest publication",
        ),
        (
            ResearchTimeSemantic.SOURCE_PUBLICATION,
            r"来源发布|来源发表|原文发布|source publication",
        ),
        (
            ResearchTimeSemantic.DISCOVERY,
            r"发现时间|发现日期|采集发现|discovery",
        ),
        (
            ResearchTimeSemantic.EDITORIAL,
            r"编辑时间|编辑接受|审核接受|editorial",
        ),
        (ResearchTimeSemantic.EVENT, r"事件时间|发生时间|event time"),
    )
    for semantic, marker in markers:
        if re.search(marker, question, re.IGNORECASE) is not None:
            return semantic
    if task_type is ResearchTaskType.TIMELINE:
        return ResearchTimeSemantic.ALL
    return ResearchTimeSemantic.EVENT


def _retrieval_time_semantics(
    semantic: ResearchTimeSemantic,
) -> tuple[str, ...]:
    if semantic is ResearchTimeSemantic.ALL:
        return tuple(value.value for value in EVIDENCE_TIME_SEMANTICS)
    return (semantic.value,)


def _query_dimensions(question: str) -> tuple[str, ...]:
    match = re.search(r"年的\s*([^，。！？?]{1,80}?)方面", question)
    if match is None:
        match = re.search(r"在\s*([^，。！？?]{1,80}?)方面", question)
    if match is None:
        return ()
    dimensions = tuple(
        value.strip()
        for value in re.split(r"[、,，/]|(?:和|与|及)", match.group(1))
        if value.strip()
    )
    return tuple(dict.fromkeys(dimensions))[:4]


def _execution_budget(
    task_type: ResearchTaskType,
    protocol: ResearchProtocol,
) -> ResearchExecutionBudget:
    values = protocol.execution_budgets[task_type.value]
    return ResearchExecutionBudget(
        maximum_iterations=values["maximum_iterations"],
        maximum_retrieval_calls=values["maximum_retrieval_calls"],
        maximum_evidence_items=values["maximum_evidence_items"],
        maximum_output_tokens=protocol.maximum_output_tokens,
        maximum_provider_output_characters=protocol.maximum_provider_output_characters,
        maximum_elapsed_seconds=protocol.maximum_elapsed_seconds,
    )


def _validated_execution_budgets(
    value: object,
    *,
    maximum_iterations: int,
    maximum_retrieval_calls: int,
    maximum_evidence_items: int,
) -> dict[str, dict[str, int]]:
    if not isinstance(value, dict) or set(value) != {item.value for item in ResearchTaskType}:
        raise ResearchError("Research task execution budgets do not match v2")
    result: dict[str, dict[str, int]] = {}
    expected_keys = {
        "maximum_iterations",
        "maximum_retrieval_calls",
        "maximum_evidence_items",
    }
    for task_type in ResearchTaskType:
        raw = value[task_type.value]
        if not isinstance(raw, dict) or set(raw) != expected_keys:
            raise ResearchError("Research task execution budget shape is invalid")
        converted = {key: int(raw[key]) for key in expected_keys}
        if (
            not 1 <= converted["maximum_iterations"] <= maximum_iterations
            or not 1 <= converted["maximum_retrieval_calls"] <= maximum_retrieval_calls
            or not 1 <= converted["maximum_evidence_items"] <= maximum_evidence_items
        ):
            raise ResearchError("Research task execution budget exceeds protocol limits")
        result[task_type.value] = converted
    return result
