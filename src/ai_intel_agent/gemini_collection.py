from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from hashlib import sha256
from html.parser import HTMLParser
from importlib.resources import files
from string import Formatter
from time import perf_counter, sleep
from typing import Protocol
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx

from ai_intel_agent.domain import (
    ApprovedFeedSourceDefinition,
    Candidate,
    Claim,
    CollectionDiscovery,
    CollectionRun,
    CollectionRunStatus,
    DocumentVersion,
    EvidenceRelation,
    EvidenceRole,
    EvidenceSpan,
    SourceDefinitionCollectionResult,
    SourceDefinitionCollectionStatus,
    Story,
    StoryReviewState,
    StructuredTrace,
    Topic,
)
from ai_intel_agent.feed_acquisition import (
    BoundedPublicHttpsError,
    BoundedPublicHttpsFetcher,
    BoundedPublicHttpsSecurityError,
    HostResolver,
)
from ai_intel_agent.model_routing_evaluation import (
    ModelCandidate,
    ModelEvaluationConfigurationError,
    load_candidate_configuration,
    load_evaluation_corpus,
    load_protocol_configuration,
)
from ai_intel_agent.persistence import (
    FeedCollectionRepository,
    GeminiDraftRepository,
    create_database_engine,
)

RELEASE_NOTES_URL = "https://ai.google.dev/gemini-api/docs/changelog"
SOURCE_CONTRACT_VERSION = "gemini-api-release-notes-2026-08-14.v1"
SOURCE_DEFINITION = ApprovedFeedSourceDefinition(
    id=uuid5(
        NAMESPACE_URL,
        f"ai-intel-agent:source-definition:{SOURCE_CONTRACT_VERSION}:{RELEASE_NOTES_URL}",
    ),
    name="Gemini API Release Notes",
    publisher="Google",
    entry_point=RELEASE_NOTES_URL,
    audit_version=SOURCE_CONTRACT_VERSION,
    collection_schedule="manual via collect-gemini; scheduler is outside M1",
    discovery_method="Concrete dated-section extraction from the official HTML release-notes page",
    language="English",
    topic_scope=(Topic.MODELS, Topic.PRODUCTS_AND_TOOLS),
    access_constraints=(
        "Fetch only the fixed official HTTPS release-notes page",
        "Stop on access challenge, rate limiting, or page-contract drift",
    ),
    extraction_adapter="Gemini API Release Notes dated H2 HTML adapter v1",
    health_policy="Require HTML and at least one parseable dated H2 on the source page",
    cursor="Dated H2 sections; default ten-day backfill",
    storage_policy="Persist each dated section as an immutable Document Version",
    public_excerpt_policy="Exact Claim Evidence only; no full-page public mirror",
    public_excerpt_max_characters=1000,
    pause_conditions=("HTTP 403/429", "access challenge", "dated-section schema drift"),
    canonical_url_prefixes=(f"{RELEASE_NOTES_URL}#",),
)
DATE_HEADING = re.compile(r"^[A-Z][a-z]+ \d{1,2}, \d{4}$")
CHINESE_CHARACTER = re.compile(r"[\u3400-\u9fff]")
BLOCKED_HTML_TAGS = frozenset({"iframe", "math", "object", "script", "style", "svg"})


class GeminiCollectionError(ValueError):
    pass


class GeminiSourceError(GeminiCollectionError):
    pass


class DraftPreparationError(GeminiCollectionError):
    pass


class Clock(Protocol):
    def now(self) -> datetime: ...


class GeminiReleaseNotesFetcher(Protocol):
    def fetch(self) -> bytes: ...


class GeminiDraftProvider(Protocol):
    def prepare(self, document: DocumentVersion) -> PreparedDraft: ...


class MeteredProviderBudget(Protocol):
    def reserve(self) -> bool: ...


@dataclass(frozen=True)
class DatedReleaseSection:
    heading: str
    anchor: str
    published_date: date
    body: str


@dataclass(frozen=True)
class PreparedClaim:
    text: str
    evidence: str


@dataclass(frozen=True)
class PreparedDraft:
    headline: str
    claims: tuple[PreparedClaim, ...]
    route_identifier: str
    candidate_configuration_version: str
    routing_evaluation_version: str
    routing_evaluation_cases_sha256: str
    protocol_version: str
    protocol_content_sha256: str
    prompt_version: str
    model_id: str
    model_version: str
    returned_model_id: str
    attempts: int
    latency_ms: int
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class GeminiDraftProtocol:
    version: str
    prompt_version: str
    route_identifier: str
    candidate_configuration_version: str
    routing_evaluation_version: str
    routing_evaluation_cases_sha256: str
    maximum_claims: int
    maximum_output_tokens: int
    system_prompt: str
    user_prompt_template: str
    content_sha256: str


@dataclass(frozen=True)
class GeminiCollectionSummary:
    collection_run_id: UUID
    sections_collected: int
    document_versions_created: int
    drafts_created: int


class HttpGeminiReleaseNotesFetcher:
    def __init__(
        self,
        client: httpx.Client,
        *,
        resolver: HostResolver | None = None,
        timeout_seconds: float = 30.0,
        maximum_bytes: int = 5_000_000,
        maximum_redirects: int = 3,
    ) -> None:
        self._fetcher = BoundedPublicHttpsFetcher(
            client,
            resolver=resolver,
            timeout_seconds=timeout_seconds,
            max_response_bytes=maximum_bytes,
            max_redirects=maximum_redirects,
        )

    def fetch(self) -> bytes:
        try:
            payload, final_location = self._fetcher.fetch(
                RELEASE_NOTES_URL,
                allowed_mime_types=frozenset({"text/html"}),
                user_agent="ai-intel-agent/0.1 Gemini-release-notes-collector",
                location_validator=_validate_release_notes_location,
            )
        except BoundedPublicHttpsError as error:
            raise GeminiSourceError(f"Gemini release-notes {error}") from error
        final_url = urlparse(final_location)
        if (
            final_url.scheme != "https"
            or final_url.hostname != "ai.google.dev"
            or final_url.path.rstrip("/") != "/gemini-api/docs/changelog"
        ):
            raise GeminiSourceError("Gemini release-notes request left the fixed official page")
        return payload


def _validate_release_notes_location(location: str) -> None:
    parsed = urlparse(location)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "ai.google.dev"
        or parsed.username
        or parsed.password
        or parsed.path.rstrip("/") != "/gemini-api/docs/changelog"
    ):
        raise BoundedPublicHttpsSecurityError(
            "request left the fixed official Gemini release-notes page"
        )


class _DatedSectionParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._blocked_tags: list[str] = []
        self._capturing_heading = False
        self._heading_parts: list[str] = []
        self._heading_anchor = ""
        self._content_parts: list[str] = []
        self.sections: list[DatedReleaseSection] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        normalized_tag = tag.casefold()
        if normalized_tag in BLOCKED_HTML_TAGS:
            self._blocked_tags.append(normalized_tag)
            return
        if self._blocked_tags:
            return
        if normalized_tag == "h2":
            self._finish_section()
            attributes = dict(attrs)
            self._heading_anchor = attributes.get("id") or ""
            self._heading_parts = []
            self._content_parts = []
            self._capturing_heading = True
        elif self._capturing_heading and not self._heading_anchor:
            attributes = dict(attrs)
            self._heading_anchor = attributes.get("id") or ""

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        return

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.casefold()
        if self._blocked_tags:
            if normalized_tag == self._blocked_tags[-1]:
                self._blocked_tags.pop()
            return
        if normalized_tag == "h2":
            self._capturing_heading = False

    def handle_data(self, data: str) -> None:
        if self._blocked_tags:
            return
        normalized = " ".join(data.split())
        if not normalized:
            return
        if self._capturing_heading:
            self._heading_parts.append(normalized)
        elif self._heading_parts:
            self._content_parts.append(normalized)

    def close(self) -> None:
        super().close()
        self._finish_section()

    def _finish_section(self) -> None:
        heading = " ".join(self._heading_parts)
        if not DATE_HEADING.fullmatch(heading):
            return
        try:
            published_date = datetime.strptime(heading, "%B %d, %Y").replace(tzinfo=UTC).date()
        except ValueError:
            return
        content = " ".join(self._content_parts)
        if not content:
            return
        anchor = self._heading_anchor or heading.casefold().replace(",", "").replace(" ", "-")
        self.sections.append(
            DatedReleaseSection(
                heading=heading,
                anchor=anchor,
                published_date=published_date,
                body=f"{heading}\n{content}",
            )
        )


def parse_gemini_release_notes(payload: bytes) -> tuple[DatedReleaseSection, ...]:
    try:
        html = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise GeminiSourceError("Gemini release-notes HTML is not UTF-8") from error
    parser = _DatedSectionParser()
    try:
        parser.feed(html)
        parser.close()
    except (RecursionError, ValueError) as error:
        raise GeminiSourceError("Gemini release-notes HTML could not be parsed") from error
    if not parser.sections:
        raise GeminiSourceError("Gemini release-notes page has no dated sections")
    return tuple(parser.sections)


def load_gemini_draft_protocol() -> GeminiDraftProtocol:
    resource = files("ai_intel_agent").joinpath("data/gemini_draft_protocol.v1.json")
    raw = resource.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    expected_keys = {
        "version",
        "prompt_version",
        "route_identifier",
        "candidate_configuration_version",
        "routing_evaluation_version",
        "maximum_claims",
        "maximum_output_tokens",
        "system_prompt",
        "user_prompt_template",
    }
    if set(payload) != expected_keys:
        raise DraftPreparationError("Gemini draft protocol keys do not match v1")
    if payload["route_identifier"] != "deepseek:v4-pro":
        raise DraftPreparationError("Gemini draft protocol is not the approved DeepSeek route")
    try:
        evaluation_corpus = load_evaluation_corpus()
    except ModelEvaluationConfigurationError as error:
        raise DraftPreparationError(
            "Gemini draft routing evaluation approval is invalid"
        ) from error
    if (
        evaluation_corpus.review_state != "human-approved"
        or evaluation_corpus.approved_cases_sha256
        != evaluation_corpus.cases_sha256
    ):
        raise DraftPreparationError(
            "Gemini draft routing evaluation is not human-approved for the exact cases SHA-256"
        )
    if payload["routing_evaluation_version"] != evaluation_corpus.version:
        raise DraftPreparationError("Gemini draft routing evaluation version drifted")
    fields = {
        name
        for _, name, _, _ in Formatter().parse(payload["user_prompt_template"])
        if name is not None
    }
    if fields != {"document_version_id", "published_at", "body"}:
        raise DraftPreparationError("Gemini draft prompt placeholders do not match v1")
    maximum_claims = int(payload["maximum_claims"])
    maximum_output_tokens = int(payload["maximum_output_tokens"])
    if not 1 <= maximum_claims <= 10 or maximum_output_tokens <= 0:
        raise DraftPreparationError("Gemini draft protocol budgets are invalid")
    return GeminiDraftProtocol(
        **{
            **payload,
            "maximum_claims": maximum_claims,
            "maximum_output_tokens": maximum_output_tokens,
        },
        content_sha256=sha256(raw).hexdigest(),
        routing_evaluation_cases_sha256=evaluation_corpus.cases_sha256,
    )


def deepseek_api_key_from_environment() -> str:
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key and os.getenv("DEEPSEEK_API_KEY_FILE"):
        from ai_intel_agent.runtime import injected_secret_from_environment

        api_key = injected_secret_from_environment(os.environ, "DEEPSEEK_API_KEY")
    if not api_key:
        raise DraftPreparationError("Set DEEPSEEK_API_KEY for collect-gemini")
    return api_key


class DeepSeekGeminiDraftProvider:
    def __init__(
        self,
        client: httpx.Client,
        *,
        api_key: str,
        budget: MeteredProviderBudget | None = None,
        sleeper: Callable[[float], None] = sleep,
    ) -> None:
        self._client = client
        self._api_key = api_key
        self._budget = budget
        self._sleeper = sleeper
        self._draft_protocol = load_gemini_draft_protocol()
        self._routing_protocol = load_protocol_configuration()
        configuration = load_candidate_configuration()
        if configuration.version != self._draft_protocol.candidate_configuration_version:
            raise DraftPreparationError("Gemini draft candidate configuration version drifted")
        self._candidate = _selected_candidate(
            configuration.candidates,
            self._draft_protocol.route_identifier,
        )

    def prepare(self, document: DocumentVersion) -> PreparedDraft:
        protocol = self._draft_protocol
        payload = {
            "model": self._candidate.model_id,
            "messages": [
                {"role": "system", "content": protocol.system_prompt},
                {
                    "role": "user",
                    "content": protocol.user_prompt_template.format(
                        document_version_id=document.id,
                        published_at=(
                            document.published_at.isoformat()
                            if document.published_at is not None
                            else "unknown"
                        ),
                        body=document.body,
                    ),
                },
            ],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "max_tokens": min(
                self._candidate.maximum_output_tokens,
                protocol.maximum_output_tokens,
            ),
            "stream": False,
        }
        started = perf_counter()
        attempts = 0
        while attempts < self._routing_protocol.retry_policy.max_attempts:
            attempts += 1
            if self._budget is not None and not self._budget.reserve():
                raise DraftPreparationError(
                    "Aggregate monthly Provider budget is exhausted"
                )
            try:
                response = self._client.post(
                    f"{self._candidate.base_url.rstrip('/')}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            except httpx.RequestError as error:
                if attempts >= self._routing_protocol.retry_policy.max_attempts:
                    raise DraftPreparationError("DeepSeek draft request failed") from error
                self._sleeper(
                    self._routing_protocol.retry_policy.backoff_seconds[attempts - 1]
                )
                continue
            if (
                response.status_code
                not in self._routing_protocol.retry_policy.retry_status_codes
                or attempts >= self._routing_protocol.retry_policy.max_attempts
            ):
                break
            self._sleeper(
                self._routing_protocol.retry_policy.backoff_seconds[attempts - 1]
            )
        latency_ms = round((perf_counter() - started) * 1000)
        if not response.is_success:
            raise DraftPreparationError(
                f"DeepSeek draft request returned HTTP {response.status_code}"
            )
        try:
            response_body = response.json()
            choice = response_body["choices"][0]
            content = choice["message"]["content"]
            finish_reason = choice["finish_reason"]
            returned_model_id = response_body.get("model")
            usage = response_body.get("usage") or {}
        except (IndexError, KeyError, TypeError, ValueError) as error:
            raise DraftPreparationError("DeepSeek draft response shape is invalid") from error
        if finish_reason != "stop" or not isinstance(content, str):
            raise DraftPreparationError("DeepSeek draft response did not finish completely")
        if (
            not isinstance(returned_model_id, str)
            or not returned_model_id.strip()
            or returned_model_id != self._candidate.model_id
        ):
            raise DraftPreparationError("DeepSeek returned model does not match approved route")
        headline, claims = _parse_prepared_draft(
            content,
            document.body,
            maximum_claims=protocol.maximum_claims,
        )
        return PreparedDraft(
            headline=headline,
            claims=claims,
            route_identifier=protocol.route_identifier,
            candidate_configuration_version=protocol.candidate_configuration_version,
            routing_evaluation_version=protocol.routing_evaluation_version,
            routing_evaluation_cases_sha256=(
                protocol.routing_evaluation_cases_sha256
            ),
            protocol_version=protocol.version,
            protocol_content_sha256=protocol.content_sha256,
            prompt_version=protocol.prompt_version,
            model_id=self._candidate.model_id,
            model_version=self._candidate.model_version,
            returned_model_id=returned_model_id,
            attempts=attempts,
            latency_ms=latency_ms,
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
        )


def _selected_candidate(
    candidates: tuple[ModelCandidate, ...],
    identifier: str,
) -> ModelCandidate:
    matches = [candidate for candidate in candidates if candidate.identifier == identifier]
    if len(matches) != 1 or matches[0].provider != "deepseek":
        raise DraftPreparationError("Approved DeepSeek draft route is unavailable")
    return matches[0]


def _parse_prepared_draft(
    raw_content: str,
    document_body: str,
    *,
    maximum_claims: int,
) -> tuple[str, tuple[PreparedClaim, ...]]:
    try:
        payload = json.loads(raw_content)
    except json.JSONDecodeError as error:
        raise DraftPreparationError("DeepSeek draft output is not valid JSON") from error
    if not isinstance(payload, dict) or set(payload) != {"headline", "claims"}:
        raise DraftPreparationError("DeepSeek draft output keys do not match v1")
    headline = payload["headline"]
    claims_payload = payload["claims"]
    if (
        not isinstance(headline, str)
        or not headline.strip()
        or CHINESE_CHARACTER.search(headline) is None
        or not isinstance(claims_payload, list)
        or not 1 <= len(claims_payload) <= maximum_claims
    ):
        raise DraftPreparationError("DeepSeek draft output has invalid Chinese content")
    claims: list[PreparedClaim] = []
    for item in claims_payload:
        if not isinstance(item, dict) or set(item) != {"text", "evidence"}:
            raise DraftPreparationError("DeepSeek draft Claim keys do not match v1")
        text_value = item["text"]
        evidence_value = item["evidence"]
        if (
            not isinstance(text_value, str)
            or not text_value.strip()
            or CHINESE_CHARACTER.search(text_value) is None
            or not isinstance(evidence_value, str)
            or not evidence_value.strip()
        ):
            raise DraftPreparationError("DeepSeek draft Claim content is invalid")
        if document_body.count(evidence_value) != 1:
            raise DraftPreparationError(
                "DeepSeek draft Evidence is not one unique exact source substring"
            )
        claims.append(PreparedClaim(text=text_value.strip(), evidence=evidence_value))
    return headline.strip(), tuple(claims)


def collect_gemini_release_notes(
    database_url: str,
    *,
    fetcher: GeminiReleaseNotesFetcher,
    provider: GeminiDraftProvider,
    clock: Clock,
    backfill_days: int = 10,
) -> GeminiCollectionSummary:
    if not 1 <= backfill_days <= 3650:
        raise ValueError("Gemini backfill days must be between 1 and 3650")
    observed_at = clock.now()
    sections = select_release_sections_for_backfill(
        parse_gemini_release_notes(fetcher.fetch()),
        observed_at=observed_at,
        backfill_days=backfill_days,
    )
    discoveries = tuple(_build_discovery(section, observed_at) for section in sections)
    run = CollectionRun(
        id=uuid4(),
        retry_of_run_id=None,
        status=CollectionRunStatus.COMPLETE,
        started_at=observed_at,
        completed_at=clock.now(),
        source_definition_results=(
            SourceDefinitionCollectionResult(
                source_definition_id=SOURCE_DEFINITION.id,
                status=SourceDefinitionCollectionStatus.SUCCEEDED,
                candidate_count=len(discoveries),
            ),
        ),
    )
    engine = create_database_engine(database_url)
    try:
        draft_repository = GeminiDraftRepository(engine)
        document_version_ids = {item.document_version.id for item in discoveries}
        known_before = draft_repository.known_document_version_ids(
            document_version_ids
        )
        FeedCollectionRepository(engine).persist(
            run,
            (SOURCE_DEFINITION,),
            discoveries,
        )
        drafts_created = 0
        for discovery in discoveries:
            document = discovery.document_version
            if draft_repository.has_draft_for_candidate(discovery.candidate.id):
                continue
            prepared = provider.prepare(document)
            story, claims, evidence_spans, traces = _build_draft_records(
                document,
                prepared,
                occurred_at=observed_at,
            )
            if draft_repository.persist(story, claims, evidence_spans, traces):
                drafts_created += 1
        return GeminiCollectionSummary(
            collection_run_id=run.id,
            sections_collected=len(discoveries),
            document_versions_created=len(document_version_ids - known_before),
            drafts_created=drafts_created,
        )
    finally:
        engine.dispose()


def select_release_sections_for_backfill(
    sections: tuple[DatedReleaseSection, ...],
    *,
    observed_at: datetime,
    backfill_days: int,
) -> tuple[DatedReleaseSection, ...]:
    if observed_at.tzinfo is None:
        raise ValueError("Gemini collection clock must be timezone-aware")
    if not 1 <= backfill_days <= 3650:
        raise ValueError("Gemini backfill days must be between 1 and 3650")
    observed_date = observed_at.astimezone(UTC).date()
    cutoff = observed_date - timedelta(days=backfill_days)
    return tuple(
        section
        for section in sections
        if cutoff <= section.published_date <= observed_date
    )


def _build_discovery(
    section: DatedReleaseSection,
    observed_at: datetime,
) -> CollectionDiscovery:
    canonical_url = f"{RELEASE_NOTES_URL}#{section.anchor}"
    candidate_id = uuid5(
        NAMESPACE_URL,
        f"ai-intel-agent:gemini-release-candidate:{canonical_url}",
    )
    content_hash = sha256(section.body.encode("utf-8")).hexdigest()
    document_version_id = uuid5(
        NAMESPACE_URL,
        f"ai-intel-agent:gemini-release-document:{candidate_id}:{content_hash}",
    )
    return CollectionDiscovery(
        source_definition_id=SOURCE_DEFINITION.id,
        candidate=Candidate(
            id=candidate_id,
            title=f"Gemini API Release Notes — {section.heading}",
            canonical_url=canonical_url,
            publisher=SOURCE_DEFINITION.publisher,
            discovered_at=observed_at,
        ),
        document_version=DocumentVersion(
            id=document_version_id,
            candidate_id=candidate_id,
            source_url=canonical_url,
            title=f"Gemini API Release Notes — {section.heading}",
            body=section.body,
            content_hash=content_hash,
            observed_at=observed_at,
            published_at=datetime.combine(section.published_date, time.min, UTC),
            published_at_raw=section.heading,
        ),
    )


def _build_draft_records(
    document: DocumentVersion,
    prepared: PreparedDraft,
    *,
    occurred_at: datetime,
) -> tuple[
    Story,
    tuple[Claim, ...],
    tuple[EvidenceSpan, ...],
    tuple[StructuredTrace, ...],
]:
    return build_draft_records(
        document,
        prepared,
        occurred_at=occurred_at,
        namespace="gemini",
        stable_key=f"gemini-release:{document.candidate_id}",
    )


def build_draft_records(
    document: DocumentVersion,
    prepared: PreparedDraft,
    *,
    occurred_at: datetime,
    namespace: str,
    stable_key: str,
    identity_key: str | None = None,
) -> tuple[
    Story,
    tuple[Claim, ...],
    tuple[EvidenceSpan, ...],
    tuple[StructuredTrace, ...],
]:
    if not namespace or not stable_key:
        raise ValueError("Draft identity requires a namespace and stable key")
    draft_identity = identity_key or str(document.candidate_id)
    story_id = uuid5(
        NAMESPACE_URL,
        f"ai-intel-agent:{namespace}-draft-story:{draft_identity}",
    )
    story = Story(
        id=story_id,
        primary_document_version_id=document.id,
        stable_key=stable_key,
        headline=prepared.headline,
        occurred_at=document.published_at or occurred_at,
        review_state=StoryReviewState.UNREVIEWED,
    )
    claims: list[Claim] = []
    evidence_spans: list[EvidenceSpan] = []
    traces: list[StructuredTrace] = []
    for position, prepared_claim in enumerate(prepared.claims):
        claim_id = uuid5(
            NAMESPACE_URL,
            f"ai-intel-agent:{namespace}-draft-claim:{story_id}:{position}",
        )
        start_offset = document.body.index(prepared_claim.evidence)
        evidence_span_id = uuid5(
            NAMESPACE_URL,
            f"ai-intel-agent:{namespace}-draft-evidence:{claim_id}:{start_offset}:"
            f"{prepared_claim.evidence}",
        )
        claims.append(
            Claim(
                id=claim_id,
                story_id=story_id,
                position=position,
                text=prepared_claim.text,
            )
        )
        evidence_spans.append(
            EvidenceSpan(
                id=evidence_span_id,
                claim_id=claim_id,
                document_version_id=document.id,
                exact_text=prepared_claim.evidence,
                start_offset=start_offset,
                end_offset=start_offset + len(prepared_claim.evidence),
                text_hash=sha256(prepared_claim.evidence.encode("utf-8")).hexdigest(),
                role=EvidenceRole.PRIMARY,
                relation=EvidenceRelation.SUPPORTS,
            )
        )
        traces.append(
            StructuredTrace(
                id=uuid5(
                    NAMESPACE_URL,
                    f"ai-intel-agent:{namespace}-draft-trace:{evidence_span_id}",
                ),
                operation_key=f"{namespace}-draft:{story_id}:claim:{position}",
                evidence_span_id=evidence_span_id,
                occurred_at=occurred_at,
                attributes={
                    "candidate_configuration_version": (
                        prepared.candidate_configuration_version
                    ),
                    "document_version_id": str(document.id),
                    "input_tokens": prepared.input_tokens,
                    "latency_ms": prepared.latency_ms,
                    "model_id": prepared.model_id,
                    "model_version": prepared.model_version,
                    "output_tokens": prepared.output_tokens,
                    "prompt_version": prepared.prompt_version,
                    "protocol_content_sha256": prepared.protocol_content_sha256,
                    "protocol_version": prepared.protocol_version,
                    "provider_attempts": prepared.attempts,
                    "returned_model_id": prepared.returned_model_id,
                    "routing_evaluation_cases_sha256": (
                        prepared.routing_evaluation_cases_sha256
                    ),
                    "routing_evaluation_version": prepared.routing_evaluation_version,
                    "route_identifier": prepared.route_identifier,
                },
            )
        )
    return story, tuple(claims), tuple(evidence_spans), tuple(traces)
