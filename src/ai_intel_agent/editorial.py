from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from hashlib import sha256
from importlib.resources import files
from time import sleep
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo

import httpx

from ai_intel_agent.domain import (
    AuditAction,
    AuditEvent,
    AuditSubjectType,
    Digest,
    DigestState,
    EvidenceRelation,
    EvidenceRole,
    SampleDigestPublication,
    SampleStory,
    Story,
    StoryReviewState,
    Topic,
)
from ai_intel_agent.model_routing_evaluation import (
    load_candidate_configuration,
    load_protocol_configuration,
)


@dataclass(frozen=True)
class EvidenceSpanInspection:
    id: UUID
    document_version_id: UUID
    exact_text: str
    start_offset: int
    end_offset: int
    text_hash: str
    role: EvidenceRole
    relation: EvidenceRelation
    publisher: str
    canonical_url: str


@dataclass(frozen=True)
class ClaimInspection:
    id: UUID
    text: str
    evidence_spans: tuple[EvidenceSpanInspection, ...]


@dataclass(frozen=True)
class StoryInspection:
    id: UUID
    stable_key: str
    headline: str
    review_state: StoryReviewState
    claims: tuple[ClaimInspection, ...]
    publisher: str
    canonical_url: str
    original_published_at: datetime | None
    primary_document_version_id: UUID
    primary_document_content_hash: str
    source_definition_id: UUID | None
    source_definition_name: str | None
    summary: str | None
    why_it_matters: str | None
    primary_topic: Topic | None
    secondary_topics: tuple[Topic, ...]


@dataclass(frozen=True)
class DigestPreview:
    publication_date: date
    stories: tuple[StoryInspection, ...]


class Clock(Protocol):
    def now(self) -> datetime: ...


class Administrator(Protocol):
    identifier: str

    def review_state_for(self, story: Story) -> StoryReviewState | None: ...


class EditorialStateError(ValueError):
    pass


class DigestPublicationContract(StrEnum):
    LEGACY_FIXTURE = "legacy-fixture"
    M3_MULTISOURCE = "m3-multisource"
    M3_EDITORIAL_PLAN = "m3-editorial-plan"


class DigestPlanInclusion(StrEnum):
    INCLUDED = "included"
    EXCLUDED = "excluded"
    HELD = "held"


@dataclass(frozen=True)
class SourceHealthInspection:
    source_definition_id: UUID
    name: str
    publisher: str
    recent_result: str
    health: str
    pause_state: str
    consecutive_failures: int
    updated_at: datetime


@dataclass(frozen=True)
class SchedulerHealthInspection:
    state: str
    last_result: str | None
    last_completed_at: datetime | None
    updated_at: datetime


@dataclass(frozen=True)
class EditorialContext:
    publication_date: date
    window_start: datetime
    window_end: datetime
    stories: tuple[StoryInspection, ...]
    source_health: tuple[SourceHealthInspection, ...]
    scheduler_health: SchedulerHealthInspection | None

    @property
    def current_state_hash(self) -> str:
        return _content_hash(_editorial_context_payload(self))


@dataclass(frozen=True)
class EditorialStoryProposal:
    stable_key: str
    inclusion: DigestPlanInclusion
    order: int | None
    summary: str
    why_it_matters: str
    primary_topic: str
    secondary_topics: tuple[str, ...]
    exclusion_reason: str | None


@dataclass(frozen=True)
class EditorialPlanProposal:
    digest_summary: str
    stories: tuple[EditorialStoryProposal, ...]
    provider_identifier: str
    protocol_version: str


class EditorialPlanProvider(Protocol):
    def prepare(self, context: EditorialContext) -> EditorialPlanProposal: ...


class EditorialProviderBudget(Protocol):
    def reserve(self) -> bool: ...


@dataclass(frozen=True)
class EditorialAgentProtocol:
    version: str
    route_identifier: str
    candidate_configuration_version: str
    maximum_pending_stories: int
    maximum_output_tokens: int
    system_prompt: str
    user_prompt_template: str
    content_sha256: str


class DeepSeekEditorialPlanProvider:
    """Prepare a strict proposal while persisted Claims and Evidence stay authoritative."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        api_key: str,
        budget: EditorialProviderBudget | None = None,
        sleeper: Callable[[float], None] = sleep,
    ) -> None:
        if not api_key.strip():
            raise EditorialStateError("Editorial Agent requires a DeepSeek API key")
        self._client = client
        self._api_key = api_key
        self._budget = budget
        self._sleeper = sleeper
        self._protocol = load_editorial_agent_protocol()
        self._routing_protocol = load_protocol_configuration()
        configuration = load_candidate_configuration()
        if configuration.version != self._protocol.candidate_configuration_version:
            raise EditorialStateError("Editorial Agent candidate configuration version drifted")
        candidates = tuple(
            candidate
            for candidate in configuration.candidates
            if candidate.identifier == self._protocol.route_identifier
            and candidate.provider == "deepseek"
        )
        if len(candidates) != 1:
            raise EditorialStateError("Approved Editorial Agent route is unavailable")
        self._candidate = candidates[0]

    def prepare(self, context: EditorialContext) -> EditorialPlanProposal:
        if not 1 <= len(context.stories) <= self._protocol.maximum_pending_stories:
            raise EditorialStateError(
                "Editorial Window pending Story count is outside the Agent protocol"
            )
        context_json = json.dumps(
            _editorial_context_payload(context),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        request_payload = {
            "model": self._candidate.model_id,
            "messages": [
                {"role": "system", "content": self._protocol.system_prompt},
                {
                    "role": "user",
                    "content": self._protocol.user_prompt_template.format(
                        context_json=context_json
                    ),
                },
            ],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "max_tokens": min(
                self._candidate.maximum_output_tokens,
                self._protocol.maximum_output_tokens,
            ),
            "stream": False,
        }
        attempts = 0
        response: httpx.Response | None = None
        while attempts < self._routing_protocol.retry_policy.max_attempts:
            attempts += 1
            if self._budget is not None and not self._budget.reserve():
                raise EditorialStateError("Aggregate monthly Provider budget is exhausted")
            try:
                response = self._client.post(
                    f"{self._candidate.base_url.rstrip('/')}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=request_payload,
                )
            except httpx.RequestError as error:
                if attempts >= self._routing_protocol.retry_policy.max_attempts:
                    raise EditorialStateError("Editorial Agent Provider request failed") from error
                self._sleeper(self._routing_protocol.retry_policy.backoff_seconds[attempts - 1])
                continue
            if (
                response.status_code not in self._routing_protocol.retry_policy.retry_status_codes
                or attempts >= self._routing_protocol.retry_policy.max_attempts
            ):
                break
            self._sleeper(self._routing_protocol.retry_policy.backoff_seconds[attempts - 1])
        if response is None or not response.is_success:
            status = response.status_code if response is not None else "unavailable"
            raise EditorialStateError(f"Editorial Agent Provider returned HTTP {status}")
        try:
            response_body = response.json()
            choice = response_body["choices"][0]
            content = choice["message"]["content"]
            finish_reason = choice["finish_reason"]
            returned_model = response_body["model"]
        except (IndexError, KeyError, TypeError, ValueError) as error:
            raise EditorialStateError(
                "Editorial Agent Provider response shape is invalid"
            ) from error
        if finish_reason != "stop" or not isinstance(content, str):
            raise EditorialStateError("Editorial Agent Provider response did not finish completely")
        if returned_model != self._candidate.model_id:
            raise EditorialStateError("Editorial Agent Provider returned an unapproved model")
        return _parse_editorial_plan_proposal(
            content,
            provider_identifier=(
                f"{self._protocol.route_identifier}@{self._candidate.model_version}"
            ),
            protocol_version=self._protocol.version,
        )


def load_editorial_agent_protocol() -> EditorialAgentProtocol:
    resource = files("ai_intel_agent").joinpath("data/editorial_digest_plan_protocol.v1.json")
    raw = resource.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EditorialStateError("Editorial Agent protocol is invalid JSON") from error
    expected_keys = {
        "version",
        "route_identifier",
        "candidate_configuration_version",
        "maximum_pending_stories",
        "maximum_output_tokens",
        "system_prompt",
        "user_prompt_template",
    }
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise EditorialStateError("Editorial Agent protocol keys do not match v1")
    for key in (
        "version",
        "route_identifier",
        "candidate_configuration_version",
        "system_prompt",
        "user_prompt_template",
    ):
        if not isinstance(payload[key], str) or not payload[key].strip():
            raise EditorialStateError("Editorial Agent protocol text is invalid")
    if payload["route_identifier"] != "deepseek:v4-pro":
        raise EditorialStateError("Editorial Agent protocol route is not approved")
    if payload["user_prompt_template"].count("{context_json}") != 1:
        raise EditorialStateError("Editorial Agent prompt contract is invalid")
    maximum_pending_stories = payload["maximum_pending_stories"]
    maximum_output_tokens = payload["maximum_output_tokens"]
    if (
        isinstance(maximum_pending_stories, bool)
        or not isinstance(maximum_pending_stories, int)
        or not 12 <= maximum_pending_stories <= 100
        or isinstance(maximum_output_tokens, bool)
        or not isinstance(maximum_output_tokens, int)
        or not 1 <= maximum_output_tokens <= 16_384
    ):
        raise EditorialStateError("Editorial Agent protocol budgets are invalid")
    return EditorialAgentProtocol(
        version=payload["version"],
        route_identifier=payload["route_identifier"],
        candidate_configuration_version=payload["candidate_configuration_version"],
        maximum_pending_stories=maximum_pending_stories,
        maximum_output_tokens=maximum_output_tokens,
        system_prompt=payload["system_prompt"],
        user_prompt_template=payload["user_prompt_template"],
        content_sha256=sha256(raw).hexdigest(),
    )


def _parse_editorial_plan_proposal(
    raw_content: str,
    *,
    provider_identifier: str,
    protocol_version: str,
) -> EditorialPlanProposal:
    try:
        payload = json.loads(raw_content)
    except json.JSONDecodeError as error:
        raise EditorialStateError("Editorial Agent output is not valid JSON") from error
    if not isinstance(payload, dict) or set(payload) != {"digest_summary", "stories"}:
        raise EditorialStateError("Editorial Agent output keys do not match v1")
    digest_summary = payload["digest_summary"]
    raw_stories = payload["stories"]
    if not isinstance(digest_summary, str) or not isinstance(raw_stories, list):
        raise EditorialStateError("Editorial Agent output types do not match v1")
    stories: list[EditorialStoryProposal] = []
    required_story_keys = {
        "stable_key",
        "inclusion",
        "order",
        "summary",
        "why_it_matters",
        "primary_topic",
        "secondary_topics",
        "exclusion_reason",
    }
    for raw_story in raw_stories:
        if not isinstance(raw_story, dict) or set(raw_story) != required_story_keys:
            raise EditorialStateError("Editorial Agent Story output keys do not match v1")
        secondary_topics = raw_story["secondary_topics"]
        order = raw_story["order"]
        exclusion_reason = raw_story["exclusion_reason"]
        text_values = (
            raw_story["stable_key"],
            raw_story["inclusion"],
            raw_story["summary"],
            raw_story["why_it_matters"],
            raw_story["primary_topic"],
        )
        if (
            any(not isinstance(value, str) for value in text_values)
            or not isinstance(secondary_topics, list)
            or any(not isinstance(value, str) for value in secondary_topics)
            or (order is not None and (isinstance(order, bool) or not isinstance(order, int)))
            or (exclusion_reason is not None and not isinstance(exclusion_reason, str))
        ):
            raise EditorialStateError("Editorial Agent Story output types do not match v1")
        try:
            inclusion = DigestPlanInclusion(raw_story["inclusion"])
        except ValueError as error:
            raise EditorialStateError("Editorial Agent Story inclusion is invalid") from error
        stories.append(
            EditorialStoryProposal(
                stable_key=raw_story["stable_key"],
                inclusion=inclusion,
                order=order,
                summary=raw_story["summary"],
                why_it_matters=raw_story["why_it_matters"],
                primary_topic=raw_story["primary_topic"],
                secondary_topics=tuple(secondary_topics),
                exclusion_reason=exclusion_reason,
            )
        )
    return EditorialPlanProposal(
        digest_summary=digest_summary,
        stories=tuple(stories),
        provider_identifier=provider_identifier,
        protocol_version=protocol_version,
    )


@dataclass(frozen=True)
class DigestPlanAnomaly:
    code: str
    message: str
    blocking: bool
    story_stable_key: str | None = None


@dataclass(frozen=True)
class DigestPlanStory:
    id: UUID
    stable_key: str
    headline: str
    review_state: StoryReviewState
    claims: tuple[ClaimInspection, ...]
    publisher: str
    canonical_url: str
    original_published_at: datetime | None
    primary_document_version_id: UUID
    primary_document_content_hash: str
    source_definition_id: UUID | None
    source_definition_name: str | None
    inclusion: DigestPlanInclusion
    order: int | None
    summary: str
    why_it_matters: str
    primary_topic: str
    secondary_topics: tuple[str, ...]
    exclusion_reason: str | None


@dataclass(frozen=True)
class DigestPlan:
    id: UUID
    publication_date: date
    window_start: datetime
    window_end: datetime
    version: int
    prepared_at: datetime
    digest_summary: str
    stories: tuple[DigestPlanStory, ...]
    source_health: tuple[SourceHealthInspection, ...]
    scheduler_health: SchedulerHealthInspection | None
    source_coverage: tuple[str, ...]
    topic_coverage: tuple[str, ...]
    anomalies: tuple[DigestPlanAnomaly, ...]
    provider_identifier: str
    protocol_version: str
    current_state_hash: str
    content_hash: str

    @property
    def included_stories(self) -> tuple[DigestPlanStory, ...]:
        return tuple(
            sorted(
                (item for item in self.stories if item.inclusion is DigestPlanInclusion.INCLUDED),
                key=lambda item: item.order if item.order is not None else -1,
            )
        )

    @property
    def excluded_stories(self) -> tuple[DigestPlanStory, ...]:
        return tuple(
            item for item in self.stories if item.inclusion is DigestPlanInclusion.EXCLUDED
        )

    @property
    def held_stories(self) -> tuple[DigestPlanStory, ...]:
        return tuple(item for item in self.stories if item.inclusion is DigestPlanInclusion.HELD)

    def content_payload(self) -> dict[str, object]:
        return _digest_plan_content_payload(self)


def editorial_window_for(publication_date: date) -> tuple[datetime, datetime]:
    timezone = ZoneInfo("Asia/Shanghai")
    end = datetime.combine(publication_date, time(hour=6), tzinfo=timezone)
    return end - timedelta(days=1), end


_PAST_EDITORIAL_WINDOW_EXCLUSION_REASON = (
    "Source time is before the current Editorial Window."
)
_FUTURE_EDITORIAL_WINDOW_HOLD_REASON = (
    "Source time has not entered the current Editorial Window."
)


def _normalize_editorial_window_proposals(
    context: EditorialContext,
    proposals_by_key: dict[str, EditorialStoryProposal],
) -> dict[str, EditorialStoryProposal]:
    normalized = dict(proposals_by_key)
    for story in context.stories:
        source_time = story.original_published_at
        if source_time is not None and source_time < context.window_start:
            normalized[story.stable_key] = replace(
                normalized[story.stable_key],
                inclusion=DigestPlanInclusion.EXCLUDED,
                order=None,
                exclusion_reason=_PAST_EDITORIAL_WINDOW_EXCLUSION_REASON,
            )
        elif source_time is not None and source_time >= context.window_end:
            normalized[story.stable_key] = replace(
                normalized[story.stable_key],
                inclusion=DigestPlanInclusion.HELD,
                order=None,
                exclusion_reason=_FUTURE_EDITORIAL_WINDOW_HOLD_REASON,
            )

    provider_included = tuple(
        item
        for item in proposals_by_key.values()
        if item.inclusion is DigestPlanInclusion.INCLUDED
    )
    provider_orders = tuple(item.order for item in provider_included)
    if (
        any(order is None for order in provider_orders)
        or len(set(provider_orders)) != len(provider_orders)
        or set(provider_orders) != set(range(len(provider_orders)))
    ):
        return normalized

    included_by_provider_order = sorted(
        (
            item
            for item in normalized.values()
            if item.inclusion is DigestPlanInclusion.INCLUDED
        ),
        key=lambda item: item.order if item.order is not None else -1,
    )
    for order, item in enumerate(included_by_provider_order):
        normalized[item.stable_key] = replace(item, order=order)
    return normalized


def prepare_digest_plan(
    context: EditorialContext,
    provider: EditorialPlanProvider,
    *,
    version: int,
    prepared_at: datetime,
) -> DigestPlan:
    if version < 1:
        raise EditorialStateError("Digest Plan version must be positive")
    proposal = provider.prepare(context)
    proposals_by_key: dict[str, EditorialStoryProposal] = {}
    for item in proposal.stories:
        if item.stable_key in proposals_by_key:
            raise EditorialStateError(f"Editorial Provider repeated Story {item.stable_key!r}")
        proposals_by_key[item.stable_key] = item
    expected_keys = {story.stable_key for story in context.stories}
    if set(proposals_by_key) != expected_keys:
        raise EditorialStateError(
            "Editorial Provider must return exactly one proposal for every pending Story"
        )
    proposals_by_key = _normalize_editorial_window_proposals(context, proposals_by_key)
    provider_identifier = proposal.provider_identifier.strip()
    protocol_version = proposal.protocol_version.strip()
    if not provider_identifier or not protocol_version:
        raise EditorialStateError("Editorial Provider identity and protocol are required")

    planned_story_list: list[DigestPlanStory] = []
    for story in context.stories:
        item = proposals_by_key[story.stable_key]
        planned_story_list.append(
            DigestPlanStory(
                id=story.id,
                stable_key=story.stable_key,
                headline=story.headline,
                review_state=story.review_state,
                claims=story.claims,
                publisher=story.publisher,
                canonical_url=story.canonical_url,
                original_published_at=story.original_published_at,
                primary_document_version_id=story.primary_document_version_id,
                primary_document_content_hash=story.primary_document_content_hash,
                source_definition_id=story.source_definition_id,
                source_definition_name=story.source_definition_name,
                inclusion=item.inclusion,
                order=item.order,
                summary=item.summary.strip(),
                why_it_matters=item.why_it_matters.strip(),
                primary_topic=item.primary_topic.strip(),
                secondary_topics=tuple(topic.strip() for topic in item.secondary_topics),
                exclusion_reason=(
                    item.exclusion_reason.strip() if item.exclusion_reason is not None else None
                ),
            )
        )
    planned_stories = tuple(planned_story_list)
    digest_summary = proposal.digest_summary.strip()
    source_coverage = _ordered_unique(
        item.publisher
        for item in sorted(
            (story for story in planned_stories if story.inclusion is DigestPlanInclusion.INCLUDED),
            key=lambda story: story.order if story.order is not None else -1,
        )
    )
    topic_coverage = _ordered_unique(
        topic
        for item in sorted(
            (story for story in planned_stories if story.inclusion is DigestPlanInclusion.INCLUDED),
            key=lambda story: story.order if story.order is not None else -1,
        )
        for topic in (item.primary_topic, *item.secondary_topics)
    )
    anomalies = _digest_plan_anomalies(
        context,
        planned_stories,
        digest_summary=digest_summary,
    )
    current_state_hash = context.current_state_hash
    content = {
        "publication_date": context.publication_date.isoformat(),
        "window_start": context.window_start.isoformat(),
        "window_end": context.window_end.isoformat(),
        "digest_summary": digest_summary,
        "stories": [_digest_plan_story_payload(item) for item in planned_stories],
        "source_health": [_source_health_payload(item) for item in context.source_health],
        "scheduler_health": (
            _scheduler_health_payload(context.scheduler_health)
            if context.scheduler_health is not None
            else None
        ),
        "source_coverage": list(source_coverage),
        "topic_coverage": list(topic_coverage),
        "anomalies": [_anomaly_payload(item) for item in anomalies],
        "provider_identifier": provider_identifier,
        "protocol_version": protocol_version,
        "current_state_hash": current_state_hash,
    }
    content_hash = _content_hash(content)
    plan_id = uuid5(
        NAMESPACE_URL,
        "ai-intel-agent:digest-plan:"
        f"{context.publication_date.isoformat()}:v{version}:{content_hash}",
    )
    return DigestPlan(
        id=plan_id,
        publication_date=context.publication_date,
        window_start=context.window_start,
        window_end=context.window_end,
        version=version,
        prepared_at=prepared_at,
        digest_summary=digest_summary,
        stories=planned_stories,
        source_health=context.source_health,
        scheduler_health=context.scheduler_health,
        source_coverage=source_coverage,
        topic_coverage=topic_coverage,
        anomalies=anomalies,
        provider_identifier=provider_identifier,
        protocol_version=protocol_version,
        current_state_hash=current_state_hash,
        content_hash=content_hash,
    )


def restore_digest_plan(
    *,
    plan_id: UUID,
    version: int,
    prepared_at: datetime,
    content_hash: str,
    payload: Mapping[str, object],
) -> DigestPlan:
    normalized_payload = dict(payload)
    if _content_hash(normalized_payload) != content_hash:
        raise EditorialStateError("Persisted Digest Plan content hash is invalid")
    try:
        source_health = tuple(
            SourceHealthInspection(
                source_definition_id=UUID(str(item["source_definition_id"])),
                name=str(item["name"]),
                publisher=str(item["publisher"]),
                recent_result=str(item["recent_result"]),
                health=str(item["health"]),
                pause_state=str(item["pause_state"]),
                consecutive_failures=int(item["consecutive_failures"]),
                updated_at=datetime.fromisoformat(str(item["updated_at"])),
            )
            for item in _mapping_items(normalized_payload["source_health"])
        )
        raw_scheduler = normalized_payload["scheduler_health"]
        scheduler_health = (
            SchedulerHealthInspection(
                state=str(raw_scheduler["state"]),
                last_result=(
                    str(raw_scheduler["last_result"])
                    if raw_scheduler["last_result"] is not None
                    else None
                ),
                last_completed_at=(
                    datetime.fromisoformat(str(raw_scheduler["last_completed_at"]))
                    if raw_scheduler["last_completed_at"] is not None
                    else None
                ),
                updated_at=datetime.fromisoformat(str(raw_scheduler["updated_at"])),
            )
            if isinstance(raw_scheduler, Mapping)
            else None
        )
        stories = tuple(
            _restore_digest_plan_story(item)
            for item in _mapping_items(normalized_payload["stories"])
        )
        anomalies = tuple(
            DigestPlanAnomaly(
                code=str(item["code"]),
                message=str(item["message"]),
                blocking=bool(item["blocking"]),
                story_stable_key=(
                    str(item["story_stable_key"]) if item["story_stable_key"] is not None else None
                ),
            )
            for item in _mapping_items(normalized_payload["anomalies"])
        )
        plan = DigestPlan(
            id=plan_id,
            publication_date=date.fromisoformat(str(normalized_payload["publication_date"])),
            window_start=datetime.fromisoformat(str(normalized_payload["window_start"])),
            window_end=datetime.fromisoformat(str(normalized_payload["window_end"])),
            version=version,
            prepared_at=prepared_at,
            digest_summary=str(normalized_payload["digest_summary"]),
            stories=stories,
            source_health=source_health,
            scheduler_health=scheduler_health,
            source_coverage=tuple(
                str(item) for item in _sequence(normalized_payload["source_coverage"])
            ),
            topic_coverage=tuple(
                str(item) for item in _sequence(normalized_payload["topic_coverage"])
            ),
            anomalies=anomalies,
            provider_identifier=str(normalized_payload["provider_identifier"]),
            protocol_version=str(normalized_payload["protocol_version"]),
            current_state_hash=str(normalized_payload["current_state_hash"]),
            content_hash=content_hash,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise EditorialStateError("Persisted Digest Plan content is invalid") from error
    expected_id = uuid5(
        NAMESPACE_URL,
        f"ai-intel-agent:digest-plan:{plan.publication_date.isoformat()}:v{version}:{content_hash}",
    )
    if plan.id != expected_id:
        raise EditorialStateError("Persisted Digest Plan identity is invalid")
    return plan


def _restore_digest_plan_story(payload: Mapping[str, object]) -> DigestPlanStory:
    claims = tuple(
        ClaimInspection(
            id=UUID(str(claim["id"])),
            text=str(claim["text"]),
            evidence_spans=tuple(
                EvidenceSpanInspection(
                    id=UUID(str(evidence["id"])),
                    document_version_id=UUID(str(evidence["document_version_id"])),
                    exact_text=str(evidence["exact_text"]),
                    start_offset=int(evidence["start_offset"]),
                    end_offset=int(evidence["end_offset"]),
                    text_hash=str(evidence["text_hash"]),
                    role=EvidenceRole(str(evidence["role"])),
                    relation=EvidenceRelation(str(evidence["relation"])),
                    publisher=str(evidence["publisher"]),
                    canonical_url=str(evidence["canonical_url"]),
                )
                for evidence in _mapping_items(claim["evidence_spans"])
            ),
        )
        for claim in _mapping_items(payload["claims"])
    )
    return DigestPlanStory(
        id=UUID(str(payload["id"])),
        stable_key=str(payload["stable_key"]),
        headline=str(payload["headline"]),
        review_state=StoryReviewState(str(payload["review_state"])),
        claims=claims,
        publisher=str(payload["publisher"]),
        canonical_url=str(payload["canonical_url"]),
        original_published_at=(
            datetime.fromisoformat(str(payload["original_published_at"]))
            if payload["original_published_at"] is not None
            else None
        ),
        primary_document_version_id=UUID(str(payload["primary_document_version_id"])),
        primary_document_content_hash=str(payload["primary_document_content_hash"]),
        source_definition_id=(
            UUID(str(payload["source_definition_id"]))
            if payload["source_definition_id"] is not None
            else None
        ),
        source_definition_name=(
            str(payload["source_definition_name"])
            if payload["source_definition_name"] is not None
            else None
        ),
        inclusion=DigestPlanInclusion(str(payload["inclusion"])),
        order=int(payload["order"]) if payload["order"] is not None else None,
        summary=str(payload["summary"]),
        why_it_matters=str(payload["why_it_matters"]),
        primary_topic=str(payload["primary_topic"]),
        secondary_topics=tuple(str(item) for item in _sequence(payload["secondary_topics"])),
        exclusion_reason=(
            str(payload["exclusion_reason"]) if payload["exclusion_reason"] is not None else None
        ),
    )


def _digest_plan_anomalies(
    context: EditorialContext,
    stories: tuple[DigestPlanStory, ...],
    *,
    digest_summary: str,
) -> tuple[DigestPlanAnomaly, ...]:
    anomalies: list[DigestPlanAnomaly] = []
    included = tuple(item for item in stories if item.inclusion is DigestPlanInclusion.INCLUDED)
    if not 8 <= len(included) <= 12:
        anomalies.append(
            DigestPlanAnomaly(
                code="invalid-selection",
                message="A Digest Plan requires between 8 and 12 included Stories",
                blocking=True,
            )
        )
    expected_orders = set(range(len(included)))
    actual_orders = {item.order for item in included}
    if actual_orders != expected_orders or any(
        item.order is not None
        for item in stories
        if item.inclusion is not DigestPlanInclusion.INCLUDED
    ):
        anomalies.append(
            DigestPlanAnomaly(
                code="invalid-order",
                message="Included Story order must be unique and contiguous",
                blocking=True,
            )
        )
    if len({item.publisher for item in included}) < 3:
        anomalies.append(
            DigestPlanAnomaly(
                code="weak-source-coverage",
                message="A Digest Plan requires at least three Publishers",
                blocking=True,
            )
        )
    if not 20 <= len(digest_summary.strip()) <= 2000:
        anomalies.append(
            DigestPlanAnomaly(
                code="invalid-editorial-fields",
                message="Digest summary must contain between 20 and 2000 characters",
                blocking=True,
            )
        )

    valid_topics = {topic.value for topic in Topic}
    source_health = {item.source_definition_id: item for item in context.source_health}
    for item in stories:
        invalid_fields = (
            not 20 <= len(item.summary.strip()) <= 1000
            or not 20 <= len(item.why_it_matters.strip()) <= 1000
            or item.primary_topic not in valid_topics
            or len(item.secondary_topics) != len(set(item.secondary_topics))
            or item.primary_topic in item.secondary_topics
            or any(topic not in valid_topics for topic in item.secondary_topics)
            or (
                item.inclusion is DigestPlanInclusion.INCLUDED and item.exclusion_reason is not None
            )
            or (
                item.inclusion is not DigestPlanInclusion.INCLUDED
                and not (item.exclusion_reason or "").strip()
            )
        )
        if invalid_fields:
            anomalies.append(
                DigestPlanAnomaly(
                    code="invalid-editorial-fields",
                    message="Story reader-facing fields do not satisfy the Editorial contract",
                    blocking=True,
                    story_stable_key=item.stable_key,
                )
            )

        supported = True
        weak = False
        if not item.claims:
            supported = False
        for claim in item.claims:
            supporting = tuple(
                evidence
                for evidence in claim.evidence_spans
                if evidence.relation is EvidenceRelation.SUPPORTS
                and evidence.role is not EvidenceRole.COMMUNITY
            )
            if not supporting:
                supported = False
            elif all(evidence.role is EvidenceRole.SECONDARY for evidence in supporting):
                weak = True
        if not supported:
            anomalies.append(
                DigestPlanAnomaly(
                    code="missing-evidence",
                    message="Story has a Claim without non-community supporting Evidence",
                    blocking=item.inclusion is DigestPlanInclusion.INCLUDED,
                    story_stable_key=item.stable_key,
                )
            )
        elif weak:
            anomalies.append(
                DigestPlanAnomaly(
                    code="weak-evidence",
                    message="Story support is limited to Secondary Evidence",
                    blocking=False,
                    story_stable_key=item.stable_key,
                )
            )

        observed_time = item.original_published_at
        if observed_time is None or observed_time < context.window_start:
            anomalies.append(
                DigestPlanAnomaly(
                    code="stale-material",
                    message="Story is outside the current Editorial Window",
                    blocking=item.inclusion is DigestPlanInclusion.INCLUDED,
                    story_stable_key=item.stable_key,
                )
            )
        elif observed_time >= context.window_end:
            anomalies.append(
                DigestPlanAnomaly(
                    code="future-material",
                    message="Story source time has not entered the current Editorial Window",
                    blocking=item.inclusion is DigestPlanInclusion.INCLUDED,
                    story_stable_key=item.stable_key,
                )
            )

        health = (
            source_health.get(item.source_definition_id)
            if item.source_definition_id is not None
            else None
        )
        if health is None:
            anomalies.append(
                DigestPlanAnomaly(
                    code="unhealthy-source-state",
                    message="Story has no current persisted Source health",
                    blocking=item.inclusion is DigestPlanInclusion.INCLUDED,
                    story_stable_key=item.stable_key,
                )
            )
        elif (
            health.health != "healthy"
            or health.pause_state != "active"
            or health.recent_result not in {"success", "succeeded", "empty", "complete"}
        ):
            anomalies.append(
                DigestPlanAnomaly(
                    code="extraction-provider-failure",
                    message="Story Source has a failed, paused, or unhealthy current result",
                    blocking=item.inclusion is DigestPlanInclusion.INCLUDED,
                    story_stable_key=item.stable_key,
                )
            )

    normalized_headlines: dict[str, str] = {}
    for item in included:
        normalized = " ".join(item.headline.casefold().split())
        if previous := normalized_headlines.get(normalized):
            anomalies.append(
                DigestPlanAnomaly(
                    code="duplicate-warning",
                    message=f"Story headline duplicates included Story {previous!r}",
                    blocking=False,
                    story_stable_key=item.stable_key,
                )
            )
        else:
            normalized_headlines[normalized] = item.stable_key

    scheduler = context.scheduler_health
    if scheduler is None:
        anomalies.append(
            DigestPlanAnomaly(
                code="unhealthy-scheduler-state",
                message="No current persisted Scheduler health is available",
                blocking=False,
            )
        )
    elif scheduler.state in {"failed", "stopped"} or scheduler.last_result == "failed":
        anomalies.append(
            DigestPlanAnomaly(
                code="unhealthy-scheduler-state",
                message="Current persisted Scheduler health is blocking publication",
                blocking=True,
            )
        )
    return tuple(anomalies)


def _editorial_context_payload(context: EditorialContext) -> dict[str, object]:
    return {
        "publication_date": context.publication_date.isoformat(),
        "window_start": context.window_start.isoformat(),
        "window_end": context.window_end.isoformat(),
        "stories": [
            _story_inspection_payload(item)
            for item in sorted(context.stories, key=lambda story: story.stable_key)
        ],
        "source_health": [
            _source_health_payload(item)
            for item in sorted(
                context.source_health,
                key=lambda source: str(source.source_definition_id),
            )
        ],
        "scheduler_health": (
            _scheduler_health_payload(context.scheduler_health)
            if context.scheduler_health is not None
            else None
        ),
    }


def _story_inspection_payload(story: StoryInspection) -> dict[str, object]:
    return {
        "id": str(story.id),
        "stable_key": story.stable_key,
        "headline": story.headline,
        "review_state": story.review_state.value,
        "claims": [_claim_payload(claim) for claim in story.claims],
        "publisher": story.publisher,
        "canonical_url": story.canonical_url,
        "original_published_at": (
            story.original_published_at.isoformat()
            if story.original_published_at is not None
            else None
        ),
        "primary_document_version_id": str(story.primary_document_version_id),
        "primary_document_content_hash": story.primary_document_content_hash,
        "source_definition_id": (
            str(story.source_definition_id) if story.source_definition_id is not None else None
        ),
        "source_definition_name": story.source_definition_name,
        "summary": story.summary,
        "why_it_matters": story.why_it_matters,
        "primary_topic": (story.primary_topic.value if story.primary_topic is not None else None),
        "secondary_topics": [topic.value for topic in story.secondary_topics],
    }


def _claim_payload(claim: ClaimInspection) -> dict[str, object]:
    return {
        "id": str(claim.id),
        "text": claim.text,
        "evidence_spans": [
            {
                "id": str(evidence.id),
                "document_version_id": str(evidence.document_version_id),
                "exact_text": evidence.exact_text,
                "start_offset": evidence.start_offset,
                "end_offset": evidence.end_offset,
                "text_hash": evidence.text_hash,
                "role": evidence.role.value,
                "relation": evidence.relation.value,
                "publisher": evidence.publisher,
                "canonical_url": evidence.canonical_url,
            }
            for evidence in claim.evidence_spans
        ],
    }


def _digest_plan_story_payload(story: DigestPlanStory) -> dict[str, object]:
    payload = _story_inspection_payload(
        StoryInspection(
            id=story.id,
            stable_key=story.stable_key,
            headline=story.headline,
            review_state=story.review_state,
            claims=story.claims,
            publisher=story.publisher,
            canonical_url=story.canonical_url,
            original_published_at=story.original_published_at,
            primary_document_version_id=story.primary_document_version_id,
            primary_document_content_hash=story.primary_document_content_hash,
            source_definition_id=story.source_definition_id,
            source_definition_name=story.source_definition_name,
            summary=None,
            why_it_matters=None,
            primary_topic=None,
            secondary_topics=(),
        )
    )
    payload.update(
        {
            "inclusion": story.inclusion.value,
            "order": story.order,
            "summary": story.summary,
            "why_it_matters": story.why_it_matters,
            "primary_topic": story.primary_topic,
            "secondary_topics": list(story.secondary_topics),
            "exclusion_reason": story.exclusion_reason,
        }
    )
    return payload


def _source_health_payload(source: SourceHealthInspection) -> dict[str, object]:
    return {
        "source_definition_id": str(source.source_definition_id),
        "name": source.name,
        "publisher": source.publisher,
        "recent_result": source.recent_result,
        "health": source.health,
        "pause_state": source.pause_state,
        "consecutive_failures": source.consecutive_failures,
        "updated_at": source.updated_at.isoformat(),
    }


def _scheduler_health_payload(
    scheduler: SchedulerHealthInspection,
) -> dict[str, object]:
    return {
        "state": scheduler.state,
        "last_result": scheduler.last_result,
        "last_completed_at": (
            scheduler.last_completed_at.isoformat()
            if scheduler.last_completed_at is not None
            else None
        ),
        "updated_at": scheduler.updated_at.isoformat(),
    }


def _anomaly_payload(anomaly: DigestPlanAnomaly) -> dict[str, object]:
    return {
        "code": anomaly.code,
        "message": anomaly.message,
        "blocking": anomaly.blocking,
        "story_stable_key": anomaly.story_stable_key,
    }


def _digest_plan_content_payload(plan: DigestPlan) -> dict[str, object]:
    return {
        "publication_date": plan.publication_date.isoformat(),
        "window_start": plan.window_start.isoformat(),
        "window_end": plan.window_end.isoformat(),
        "digest_summary": plan.digest_summary,
        "stories": [_digest_plan_story_payload(item) for item in plan.stories],
        "source_health": [_source_health_payload(item) for item in plan.source_health],
        "scheduler_health": (
            _scheduler_health_payload(plan.scheduler_health)
            if plan.scheduler_health is not None
            else None
        ),
        "source_coverage": list(plan.source_coverage),
        "topic_coverage": list(plan.topic_coverage),
        "anomalies": [_anomaly_payload(item) for item in plan.anomalies],
        "provider_identifier": plan.provider_identifier,
        "protocol_version": plan.protocol_version,
        "current_state_hash": plan.current_state_hash,
    }


def _content_hash(payload: dict[str, object]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(canonical).hexdigest()


def _ordered_unique(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return tuple(result)


def _sequence(value: object) -> tuple[object, ...]:
    if not isinstance(value, (list, tuple)):
        raise TypeError("Digest Plan value must be a sequence")
    return tuple(value)


def _mapping_items(value: object) -> tuple[Mapping[str, object], ...]:
    items = _sequence(value)
    if not all(isinstance(item, Mapping) for item in items):
        raise TypeError("Digest Plan sequence entries must be objects")
    return tuple(item for item in items if isinstance(item, Mapping))


def _id(name: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"ai-intel-agent:sample-editorial-v1:{name}")


def _mvp_id(name: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"ai-intel-agent:mvp-editorial-v1:{name}")


def _review_story(story: Story, decision: StoryReviewState) -> Story:
    if story.review_state is not StoryReviewState.UNREVIEWED:
        raise EditorialStateError("Only unreviewed Stories may be reviewed")
    if decision not in (StoryReviewState.ACCEPTED, StoryReviewState.REJECTED):
        raise EditorialStateError("A review must accept or reject a Story")
    return replace(story, review_state=decision)


def _compose_digest(stories: tuple[SampleStory, ...], now: datetime) -> Digest:
    accepted_story_ids = tuple(
        sample.story.id
        for sample in stories
        if sample.story.review_state is StoryReviewState.ACCEPTED
    )
    return Digest(
        id=_id("digest:2026-08-12"),
        stable_key="sample-digest:2026-08-12",
        publication_date=now.date(),
        state=DigestState.DRAFT,
        published_at=None,
        story_ids=accepted_story_ids,
    )


def _publish_digest(digest: Digest, now: datetime) -> Digest:
    if digest.state is not DigestState.DRAFT:
        raise EditorialStateError("Only a draft Digest may be published")
    if not digest.story_ids:
        raise EditorialStateError("A Digest must contain at least one accepted Story")
    return replace(digest, state=DigestState.PUBLISHED, published_at=now)


def review_story(
    story: Story,
    decision: StoryReviewState,
    *,
    actor_identifier: str,
    now: datetime,
) -> tuple[Story, AuditEvent]:
    reviewed = _review_story(story, decision)
    action = (
        AuditAction.STORY_ACCEPTED
        if decision is StoryReviewState.ACCEPTED
        else AuditAction.STORY_REJECTED
    )
    return reviewed, AuditEvent(
        id=_mvp_id(f"story:{story.id}:{action.value}"),
        operation_key=f"mvp-editorial-v1:story:{story.id}:{action.value}",
        actor_identifier=actor_identifier,
        action=action,
        subject_type=AuditSubjectType.STORY,
        subject_id=story.id,
        occurred_at=now,
        sequence=0,
        attributes={
            "from_state": StoryReviewState.UNREVIEWED.value,
            "to_state": decision.value,
        },
    )


def compose_digest(publication_date: date, story_ids: tuple[UUID, ...]) -> Digest:
    return Digest(
        id=_mvp_id(f"digest:{publication_date.isoformat()}"),
        stable_key=f"digest:{publication_date.isoformat()}",
        publication_date=publication_date,
        state=DigestState.DRAFT,
        published_at=None,
        story_ids=story_ids,
    )


def publish_digest(
    digest: Digest,
    *,
    actor_identifier: str,
    now: datetime,
) -> tuple[Digest, tuple[AuditEvent, AuditEvent]]:
    published = _publish_digest(digest, now)
    composed_event = AuditEvent(
        id=_mvp_id(f"digest:{digest.id}:{AuditAction.DIGEST_COMPOSED.value}"),
        operation_key=(f"mvp-editorial-v1:digest:{digest.id}:{AuditAction.DIGEST_COMPOSED.value}"),
        actor_identifier=actor_identifier,
        action=AuditAction.DIGEST_COMPOSED,
        subject_type=AuditSubjectType.DIGEST,
        subject_id=digest.id,
        occurred_at=now,
        sequence=0,
        attributes={"included_story_ids": [str(story_id) for story_id in digest.story_ids]},
    )
    published_event = AuditEvent(
        id=_mvp_id(f"digest:{digest.id}:{AuditAction.DIGEST_PUBLISHED.value}"),
        operation_key=(f"mvp-editorial-v1:digest:{digest.id}:{AuditAction.DIGEST_PUBLISHED.value}"),
        actor_identifier=actor_identifier,
        action=AuditAction.DIGEST_PUBLISHED,
        subject_type=AuditSubjectType.DIGEST,
        subject_id=digest.id,
        occurred_at=now,
        sequence=1,
        attributes={
            "from_state": DigestState.DRAFT.value,
            "to_state": DigestState.PUBLISHED.value,
        },
    )
    return published, (composed_event, published_event)


def review_and_publish_digest(
    stories: tuple[SampleStory, ...],
    *,
    administrator: Administrator,
    clock: Clock,
) -> SampleDigestPublication:
    reviewed_stories: list[SampleStory] = []
    audit_events: list[AuditEvent] = []
    now = clock.now()

    for sample in stories:
        decision = administrator.review_state_for(sample.story)
        if decision is None:
            reviewed_stories.append(sample)
            continue
        reviewed_story = _review_story(sample.story, decision)
        reviewed_stories.append(replace(sample, story=reviewed_story))
        action = (
            AuditAction.STORY_ACCEPTED
            if decision is StoryReviewState.ACCEPTED
            else AuditAction.STORY_REJECTED
        )
        audit_events.append(
            AuditEvent(
                id=_id(f"{sample.story.stable_key}:{action.value}"),
                operation_key=(f"sample-editorial-v1:{sample.story.stable_key}:{action.value}"),
                actor_identifier=administrator.identifier,
                action=action,
                subject_type=AuditSubjectType.STORY,
                subject_id=sample.story.id,
                occurred_at=now,
                sequence=len(audit_events),
                attributes={
                    "from_state": StoryReviewState.UNREVIEWED.value,
                    "to_state": decision.value,
                },
            )
        )

    reviewed = tuple(reviewed_stories)
    draft_digest = _compose_digest(reviewed, now)
    audit_events.append(
        AuditEvent(
            id=_id("digest:2026-08-12:composed"),
            operation_key="sample-editorial-v1:digest:2026-08-12:composed",
            actor_identifier=administrator.identifier,
            action=AuditAction.DIGEST_COMPOSED,
            subject_type=AuditSubjectType.DIGEST,
            subject_id=draft_digest.id,
            occurred_at=now,
            sequence=len(audit_events),
            attributes={
                "included_story_ids": [str(story_id) for story_id in draft_digest.story_ids]
            },
        )
    )
    digest = _publish_digest(draft_digest, now)
    audit_events.append(
        AuditEvent(
            id=_id("digest:2026-08-12:published"),
            operation_key="sample-editorial-v1:digest:2026-08-12:published",
            actor_identifier=administrator.identifier,
            action=AuditAction.DIGEST_PUBLISHED,
            subject_type=AuditSubjectType.DIGEST,
            subject_id=digest.id,
            occurred_at=now,
            sequence=len(audit_events),
            attributes={
                "from_state": DigestState.DRAFT.value,
                "to_state": DigestState.PUBLISHED.value,
            },
        )
    )
    return SampleDigestPublication(
        stories=reviewed,
        digest=digest,
        audit_events=tuple(audit_events),
    )
