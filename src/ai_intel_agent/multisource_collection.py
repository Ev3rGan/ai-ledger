from __future__ import annotations

import json
import posixpath
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5
from zoneinfo import ZoneInfo

import httpx
from trafilatura import extract

from ai_intel_agent.domain import (
    ApprovedFeedSourceDefinition,
    ArticleCollectionStatus,
    Candidate,
    CollectionRun,
    CollectionRunStatus,
    DocumentVersion,
    SourceDefinitionCollectionResult,
    SourceDefinitionCollectionStatus,
    SourceProfileHealth,
)
from ai_intel_agent.feed_acquisition import (
    BoundedPublicHttpsError,
    BoundedPublicHttpsFetcher,
    BoundedPublicHttpsSecurityError,
    FeedEntry,
    FeedFormatError,
    HostResolver,
    parse_feed,
)
from ai_intel_agent.gemini_collection import (
    DraftPreparationError,
    PreparedDraft,
    build_draft_records,
)
from ai_intel_agent.persistence import (
    GeminiDraftRepository,
    MultiSourceCollectionRepository,
    create_database_engine,
)
from ai_intel_agent.source_portfolio import (
    CORE_PROFILE_KEYS,
    SourcePortfolioDefinition,
    SourceProfile,
    load_legacy_article_profiles,
    load_source_universe,
)
from ai_intel_agent.source_portfolio_acquisition import (
    SourceAcquisition,
    SourcePortfolioAcquisitionError,
    SourcePortfolioItemResult,
    SourceSpecificRecord,
)

SOURCE_PROFILE_VERSION = "mvp-v2-m2-source-profiles-2026-08-17.v1"
SOURCE_PROFILE_SET_VERSION = "mvp-v2-1-m1-active-source-profiles-2026-08-19.v1"
SHANGHAI = ZoneInfo("Asia/Shanghai")
APPROVED_SOURCE_HOSTS = frozenset(
    {
        "the-decoder.com",
        "techcrunch.com",
        "huggingface.co",
        "qbitai.com",
    }
)
APPROVED_FEED_URLS = {
    "the-decoder.com": "https://the-decoder.com/feed/",
    "techcrunch.com": "https://techcrunch.com/category/artificial-intelligence/feed/",
    "huggingface.co": "https://huggingface.co/blog/feed.xml",
    "qbitai.com": "https://www.qbitai.com/feed/",
}
ALLOWED_ARTICLE_MIME_TYPES = frozenset({"text/html", "application/xhtml+xml"})
AMBIGUOUS_ACCESS_CONTROL_MARKERS = (
    "access denied",
)
DEFINITIVE_ACCESS_SHELL_MARKERS = (
    "checking your browser",
    "verify you are human",
    "enable javascript and cookies to continue",
    "captcha challenge",
    "login to continue",
    "login to continue reading",
    "log in to continue",
    "sign in to continue",
    "subscribe to continue",
    "subscribe to continue reading",
    "sign in to continue reading",
    "log in to continue reading",
    "register to continue",
    "register to continue reading",
    "create an account to continue",
    "create an account to continue reading",
    "article is for subscribers",
    "subscribe now to continue reading",
    "consent required",
    "accept cookies to continue",
)
ACCESS_CONTROL_TITLE_MARKERS = (
    *AMBIGUOUS_ACCESS_CONTROL_MARKERS,
    *DEFINITIVE_ACCESS_SHELL_MARKERS,
)
ACCESS_SHELL_SENTENCE_MAX_WORDS = 20
TRACKING_QUERY_KEYS = frozenset({"fbclid", "gclid", "ref", "source"})


@dataclass(frozen=True)
class ArticleDocument:
    title: str
    canonical_url: str
    body: str


@dataclass(frozen=True)
class PersistableArticleResult:
    source_definition_id: UUID
    candidate: Candidate
    status: ArticleCollectionStatus
    evidence_eligible: bool
    eligibility_kind: str
    document_version: DocumentVersion | None = None
    source_record: SourceSpecificRecord | None = None
    error_code: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        body_valid = self.status is ArticleCollectionStatus.BODY_VALID
        errors_absent = self.error_code is None and self.error_message is None
        errors_present = self.error_code is not None and self.error_message is not None
        if (
            self.evidence_eligible != body_valid
            or (self.document_version is not None) != body_valid
            or self.eligibility_kind
            != ("body-valid" if body_valid else "ineligible")
            or (body_valid and not errors_absent)
            or (not body_valid and not errors_present)
        ):
            raise ValueError("Article result Evidence policy is invalid")


@dataclass(frozen=True)
class MultiSourceCollectionSummary:
    collection_run_id: UUID
    status: CollectionRunStatus
    source_results: dict[str, str]
    candidates_processed: int
    document_versions_created: int
    drafts_created: int
    replayed: bool
    core_results_persisted: int = 0
    core_eligible_contributors: int = 0
    core_acceptance_met: bool = False


@dataclass(frozen=True)
class PortfolioSourceState:
    source_definition_id: UUID
    recent_result: SourceDefinitionCollectionStatus
    cursor_value: str | None
    health: SourceProfileHealth
    consecutive_failures: int
    last_collection_run_id: UUID
    updated_at: datetime
    pause_state: str


class Clock(Protocol):
    def now(self) -> datetime: ...


class FeedDiscoveryAdapter(Protocol):
    def discover(self, profile: SourceProfile) -> tuple[FeedEntry, ...]: ...


class ArticleAdapter(Protocol):
    def fetch(self, profile: SourceProfile, entry: FeedEntry) -> ArticleDocument: ...


class DraftProvider(Protocol):
    def prepare(self, document: DocumentVersion) -> PreparedDraft: ...


class SourcePortfolioAdapter(Protocol):
    def acquire(
        self,
        profile: SourceProfile,
        *,
        observed_at: datetime,
        backfill_limit: int,
        cursor_value: str | None,
        known_paper_identities: frozenset[tuple[str, str]],
        known_signal_targets: frozenset[str],
    ) -> SourceAcquisition: ...


class SourceProfileConfigurationError(ValueError):
    pass


class ArticleAcquisitionError(ValueError):
    code = "temporary-failure"


class ArticleSecurityError(ArticleAcquisitionError):
    code = "invalid-format"


class ArticleAccessBlockedError(ArticleAcquisitionError):
    code = "access-blocked"


class ArticleBodyInvalidError(ArticleAcquisitionError):
    code = "invalid-format"


class ArticleTemporaryFailureError(ArticleAcquisitionError):
    code = "temporary-failure"


class FeedDiscoveryError(ValueError):
    code = "temporary-failure"


class FeedDiscoveryInvalidFormatError(FeedDiscoveryError):
    code = "invalid-format"


class FeedDiscoveryAccessBlockedError(FeedDiscoveryError):
    code = "access-blocked"


class FeedDiscoveryTemporaryFailureError(FeedDiscoveryError):
    code = "temporary-failure"


def scheduled_operation_key(instant: datetime) -> str:
    """Return one stable operation key for the containing Shanghai schedule slot."""
    if instant.tzinfo is None:
        raise ValueError("Scheduled collection instant must be timezone-aware")
    local = instant.astimezone(SHANGHAI)
    if local.hour >= 18:
        slot = local.replace(hour=18, minute=0, second=0, microsecond=0)
    elif local.hour >= 6:
        slot = local.replace(hour=6, minute=0, second=0, microsecond=0)
    else:
        slot = (local - timedelta(days=1)).replace(
            hour=18,
            minute=0,
            second=0,
            microsecond=0,
        )
    return (
        f"m2-incremental:{slot.isoformat(timespec='minutes')}:"
        f"{SOURCE_PROFILE_SET_VERSION}"
    )


class _ArticleMetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.canonical_url: str | None = None
        self.open_graph_url: str | None = None
        self._in_title = False
        self._title_parts: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        normalized_tag = tag.casefold()
        attributes = {
            name.casefold(): value for name, value in attrs if value is not None
        }
        if normalized_tag == "title":
            self._in_title = True
        elif normalized_tag == "link" and "canonical" in attributes.get(
            "rel", ""
        ).casefold().split():
            self.canonical_url = self.canonical_url or attributes.get("href")
        elif (
            normalized_tag == "meta"
            and attributes.get("property", "").casefold() == "og:url"
        ):
            self.open_graph_url = self.open_graph_url or attributes.get("content")

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)

    @property
    def title(self) -> str:
        return " ".join(" ".join(self._title_parts).split())


def load_source_profiles() -> tuple[SourceProfile, ...]:
    profiles = tuple(
        replace(
            profile,
            id=uuid5(
                NAMESPACE_URL,
                f"ai-intel-agent:source-profile:{SOURCE_PROFILE_VERSION}:{profile.host}",
            ),
            profile_version=SOURCE_PROFILE_VERSION,
        )
        for profile in load_legacy_article_profiles()
    )
    if (
        len(profiles) != len(APPROVED_SOURCE_HOSTS)
        or {profile.host for profile in profiles} != APPROVED_SOURCE_HOSTS
        or len({profile.id for profile in profiles}) != len(profiles)
    ):
        raise SourceProfileConfigurationError(
            "Source Profile manifest must contain exactly the current four hosts"
        )
    for profile in profiles:
        _validate_profile(profile)
    return profiles


def _validate_profile(profile: SourceProfile) -> None:
    if (
        not profile.publisher.strip()
        or len(profile.publisher) > 255
        or not profile.language.strip()
        or not profile.topic_scope
        or not profile.cursor_policy.strip()
        or not profile.health_policy.strip()
        or not profile.allowed_hosts
        or not profile.allowed_path_prefixes
        or profile.host not in profile.allowed_hosts
        or profile.feed_url != APPROVED_FEED_URLS.get(profile.host)
    ):
        raise SourceProfileConfigurationError("Source Profile operating policy is incomplete")
    _validate_profile_location(profile, profile.feed_url)


class HttpFeedDiscoveryAdapter:
    """Discover article metadata from the one Feed configured by a Source Profile."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        resolver: HostResolver | None = None,
        timeout_seconds: float = 10.0,
        max_response_bytes: int = 2_000_000,
        max_redirects: int = 3,
    ) -> None:
        self._fetcher = BoundedPublicHttpsFetcher(
            client,
            resolver=resolver,
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
            max_redirects=max_redirects,
        )

    def discover(self, profile: SourceProfile) -> tuple[FeedEntry, ...]:
        try:
            payload, _ = self._fetcher.fetch(
                profile.feed_url,
                allowed_mime_types=frozenset(
                    {
                        "application/atom+xml",
                        "application/rdf+xml",
                        "application/rss+xml",
                        "application/xml",
                        "text/xml",
                    }
                ),
                user_agent="ai-intel-agent/0.1 feed-discovery",
                location_validator=lambda location: _validate_feed_location(
                    profile, location
                ),
            )
        except ArticleSecurityError as error:
            raise FeedDiscoveryInvalidFormatError(str(error)) from error
        except BoundedPublicHttpsSecurityError as error:
            raise FeedDiscoveryInvalidFormatError(str(error)) from error
        except BoundedPublicHttpsError as error:
            message = str(error)
            if any(f"HTTP {status}" in message for status in (401, 403, 407, 451)):
                raise FeedDiscoveryAccessBlockedError("Feed access was blocked") from error
            if "unsupported MIME" in message:
                raise FeedDiscoveryInvalidFormatError("Feed response is not XML") from error
            if _is_terminal_fetch_error(message):
                raise FeedDiscoveryInvalidFormatError(
                    "Feed response policy rejected the response"
                ) from error
            raise FeedDiscoveryTemporaryFailureError("Feed HTTPS request failed") from error
        try:
            entries = parse_feed(payload)
            for entry in entries:
                _validate_profile_location(profile, entry.canonical_url)
        except (FeedFormatError, ArticleSecurityError) as error:
            raise FeedDiscoveryInvalidFormatError("Feed content is outside its profile") from error
        return entries


class HttpArticleAdapter:
    """Fetch and extract one body-valid article inside a Source Profile's scope."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        resolver: HostResolver | None = None,
        timeout_seconds: float = 15.0,
        max_response_bytes: int = 2_000_000,
        max_redirects: int = 3,
        minimum_body_characters: int = 400,
        minimum_body_words: int = 80,
    ) -> None:
        if minimum_body_characters <= 0 or minimum_body_words <= 0:
            raise ValueError("Article body quality limits must be positive")
        self._fetcher = BoundedPublicHttpsFetcher(
            client,
            resolver=resolver,
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
            max_redirects=max_redirects,
        )
        self._minimum_body_characters = minimum_body_characters
        self._minimum_body_words = minimum_body_words

    def fetch(self, profile: SourceProfile, entry: FeedEntry) -> ArticleDocument:
        _validate_profile_location(profile, entry.canonical_url)
        try:
            payload, final_url = self._fetcher.fetch(
                entry.canonical_url,
                allowed_mime_types=ALLOWED_ARTICLE_MIME_TYPES,
                user_agent="ai-intel-agent/0.1 article-collector",
                location_validator=lambda location: _validate_profile_location(
                    profile, location
                ),
            )
        except ArticleSecurityError:
            raise
        except BoundedPublicHttpsSecurityError as error:
            raise ArticleSecurityError(str(error)) from error
        except BoundedPublicHttpsError as error:
            message = str(error)
            if any(f"HTTP {status}" in message for status in (401, 403, 407, 451)):
                raise ArticleAccessBlockedError("Article access was blocked") from error
            if _is_terminal_fetch_error(message):
                raise ArticleBodyInvalidError(
                    "Article response policy rejected the response"
                ) from error
            raise ArticleTemporaryFailureError("Article HTTPS request failed") from error

        html = payload.decode("utf-8", errors="replace")
        parser = _ArticleMetadataParser()
        try:
            parser.feed(html)
            parser.close()
        except Exception as error:
            raise ArticleBodyInvalidError("Article HTML metadata is invalid") from error
        if _is_access_control_title(parser.title):
            raise ArticleAccessBlockedError("Article page is an access-control response")

        declared_canonical = parser.canonical_url or parser.open_graph_url
        canonical_url = (
            urljoin(final_url, declared_canonical) if declared_canonical else final_url
        )
        _validate_profile_location(profile, canonical_url)
        if _canonical_identity(
            canonical_url,
            profile=profile,
        ) != _canonical_identity(entry.canonical_url, profile=profile):
            raise ArticleBodyInvalidError("Article canonical URL does not match the Feed entry")

        try:
            body = extract(
                html,
                url=canonical_url,
                output_format="txt",
                include_comments=False,
                include_tables=False,
                favor_precision=True,
            )
        except Exception as error:
            raise ArticleBodyInvalidError("Article body extraction failed") from error
        normalized_body = "\n".join(
            line.strip() for line in (body or "").splitlines() if line.strip()
        )
        body_passes_quality_gate = _body_passes_quality_gate(
            normalized_body,
            minimum_characters=self._minimum_body_characters,
            minimum_words=self._minimum_body_words,
        )
        if _is_access_control_body(
            normalized_body,
            body_passes_quality_gate=body_passes_quality_gate,
        ):
            raise ArticleAccessBlockedError("Article page is an access-control response")
        if not body_passes_quality_gate:
            raise ArticleBodyInvalidError("Article body did not pass the quality gate")
        return ArticleDocument(
            title=parser.title or entry.title,
            canonical_url=_canonical_identity(canonical_url, profile=profile),
            body=normalized_body,
        )


def _validate_profile_location(profile: SourceProfile, location: str) -> None:
    try:
        parsed = urlparse(location)
        port = parsed.port
    except ValueError as error:
        raise ArticleSecurityError("Article URL is invalid") from error
    hostname = (parsed.hostname or "").casefold()
    path = posixpath.normpath(parsed.path or "/")
    if parsed.path.endswith("/") and path != "/":
        path += "/"
    allowed_path = any(
        path == prefix.rstrip("/") or path.startswith(prefix)
        for prefix in profile.allowed_path_prefixes
    )
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or port not in (None, 443)
        or hostname not in profile.allowed_hosts
        or not allowed_path
    ):
        raise ArticleSecurityError("Article URL is outside the Source Profile scope")


def _validate_feed_location(profile: SourceProfile, location: str) -> None:
    _validate_profile_location(profile, location)
    actual = urlparse(location)
    expected = urlparse(profile.feed_url)
    if (
        (actual.hostname or "").casefold() != (expected.hostname or "").casefold()
        or posixpath.normpath(actual.path or "/")
        != posixpath.normpath(expected.path or "/")
        or actual.query != expected.query
    ):
        raise ArticleSecurityError("Feed URL is outside the Source Profile feed scope")


def _validate_feed_entry_metadata(profile: SourceProfile, entry: FeedEntry) -> None:
    canonical_url = _canonical_identity(entry.canonical_url, profile=profile)
    raw_timestamps = (entry.published_at_raw, entry.updated_at_raw)
    if (
        not entry.title.strip()
        or len(entry.title) > 500
        or len(canonical_url) > 2048
        or any(value is not None and len(value) > 255 for value in raw_timestamps)
    ):
        raise ArticleSecurityError("Feed entry metadata exceeds storage bounds")


def _validate_article_document(
    profile: SourceProfile,
    article: ArticleDocument,
) -> None:
    if (
        not article.title.strip()
        or len(article.title) > 500
        or len(_canonical_identity(article.canonical_url, profile=profile)) > 2048
        or not article.body.strip()
    ):
        raise ArticleBodyInvalidError("Article document exceeds storage bounds")


def _canonical_identity(
    location: str,
    *,
    profile: SourceProfile | None = None,
) -> str:
    parsed = urlparse(location)
    path = posixpath.normpath(parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query = urlencode(
        sorted(
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.casefold().startswith("utm_")
            and key.casefold() not in TRACKING_QUERY_KEYS
        )
    )
    parsed_host = (parsed.hostname or "").casefold()
    canonical_host = (
        profile.host
        if profile is not None and parsed_host in profile.allowed_hosts
        else parsed_host
    )
    return urlunparse(("https", canonical_host, path, "", query, ""))


def _body_passes_quality_gate(
    body: str,
    *,
    minimum_characters: int,
    minimum_words: int,
) -> bool:
    if len(body) < minimum_characters:
        return False
    words = re.findall(r"\b[\w'-]+\b", body, flags=re.UNICODE)
    cjk_characters = re.findall(r"[\u3400-\u9fff]", body)
    return len(words) >= minimum_words or len(cjk_characters) >= minimum_characters // 2


def _is_access_control_body(
    text: str,
    *,
    body_passes_quality_gate: bool,
) -> bool:
    normalized = " ".join(text.casefold().split())
    if not body_passes_quality_gate:
        return any(marker in normalized for marker in ACCESS_CONTROL_TITLE_MARKERS)
    sentences = re.split(r"(?<=[.!?。！？])\s+|\n+", text)
    return any(_is_standalone_access_shell_sentence(sentence) for sentence in sentences)


def _is_standalone_access_shell_sentence(sentence: str) -> bool:
    normalized = " ".join(sentence.casefold().split()).strip(" .!?。！？:-")
    words = re.findall(r"\b[\w'-]+\b", normalized, flags=re.UNICODE)
    if not normalized or len(words) > ACCESS_SHELL_SENTENCE_MAX_WORDS:
        return False
    for marker in DEFINITIVE_ACCESS_SHELL_MARKERS:
        allowed_forms = (marker, f"please {marker}")
        if marker == "article is for subscribers":
            allowed_forms += (f"this {marker}",)
        if normalized in allowed_forms:
            return True
    return False


def _is_access_control_title(title: str) -> bool:
    normalized = " ".join(title.casefold().split()).strip(" .:-")
    return any(
        normalized == marker or normalized.startswith(f"{marker} -")
        for marker in ACCESS_CONTROL_TITLE_MARKERS
    )


def _is_terminal_fetch_error(message: str) -> bool:
    status_match = re.search(r"returned HTTP (?P<status>\d{3})", message)
    if status_match is not None:
        status = int(status_match.group("status"))
        return 400 <= status < 500 and status not in {408, 409, 425, 429}
    return any(
        marker in message
        for marker in (
            "unsupported MIME",
            "redirect limit exceeded",
            "redirect has no Location",
            "invalid Content-Length",
            "exceeds the size limit",
        )
    )


def collect_source_profiles(
    database_url: str,
    *,
    profiles: tuple[SourceProfile, ...],
    feed_adapter: FeedDiscoveryAdapter,
    article_adapter: ArticleAdapter,
    portfolio_adapter: SourcePortfolioAdapter | None = None,
    provider: DraftProvider,
    clock: Clock,
    operation_key: str,
    backfill_limit: int = 5,
) -> MultiSourceCollectionSummary:
    """Collect the approved source universe while isolating every profile."""
    legacy_profiles = load_source_profiles()
    universe_profiles = load_source_universe()
    is_source_universe = profiles == universe_profiles
    if profiles != legacy_profiles and not is_source_universe:
        raise SourceProfileConfigurationError(
            "Collection requires an exact approved ordered Source Profile set"
        )
    if is_source_universe and portfolio_adapter is None:
        raise SourceProfileConfigurationError(
            "The source universe requires the approved portfolio adapters"
        )
    if not operation_key.strip() or len(operation_key) > 255:
        raise ValueError("Collection operation key must be 1-255 characters")
    if not 1 <= backfill_limit <= 100:
        raise ValueError("Initial backfill limit must be between 1 and 100 per source")

    engine = create_database_engine(database_url)
    try:
        repository = MultiSourceCollectionRepository(engine)
        existing = repository.operation(operation_key)
        if existing is not None:
            core_results, core_contributors = repository.acceptance_counts(
                existing.collection_run_id
            )
            return MultiSourceCollectionSummary(
                collection_run_id=existing.collection_run_id,
                status=CollectionRunStatus(existing.status),
                source_results=existing.source_results,
                candidates_processed=existing.candidates_processed,
                document_versions_created=0,
                drafts_created=0,
                replayed=True,
                core_results_persisted=core_results,
                core_eligible_contributors=core_contributors,
                core_acceptance_met=(
                    core_results == len(CORE_PROFILE_KEYS) and core_contributors >= 6
                ),
            )

        source_definitions = tuple(_source_definition(profile) for profile in profiles)
        cursor_values = repository.cursor_values({profile.id for profile in profiles})
        previous_states = {
            state.source_definition_id: state
            for state in repository.source_statuses({profile.id for profile in profiles})
        }
        observed_at = clock.now()
        if observed_at.tzinfo is None:
            raise ValueError("Collection clock must be timezone-aware")
        run_id = uuid4()
        source_results: list[SourceDefinitionCollectionResult] = []
        candidate_results: list[
            PersistableArticleResult | SourcePortfolioItemResult
        ] = []
        states: list[PortfolioSourceState] = []
        valid_documents: list[tuple[Candidate, DocumentVersion]] = []
        known_paper_identities = set(repository.known_paper_identities())
        known_signal_targets = set(repository.known_signal_targets())

        for profile in profiles:
            previous_cursor = cursor_values.get(profile.id)
            previous_state = previous_states.get(profile.id)
            previous_failures = (
                previous_state.consecutive_failures
                if previous_state is not None
                else 0
            )
            if not profile.enabled:
                status = SourceDefinitionCollectionStatus.EMPTY
                source_results.append(_source_result(profile, status, candidate_count=0))
                states.append(
                    _source_state(
                        profile,
                        run_id=run_id,
                        status=status,
                        cursor_value=previous_cursor,
                        updated_at=clock.now(),
                        previous_failures=previous_failures,
                    )
                )
                continue
            if profile.adapter != "shared-feed-html":
                assert portfolio_adapter is not None
                try:
                    acquisition = portfolio_adapter.acquire(
                        profile,
                        observed_at=observed_at,
                        backfill_limit=backfill_limit,
                        cursor_value=previous_cursor,
                        known_paper_identities=frozenset(known_paper_identities),
                        known_signal_targets=frozenset(known_signal_targets),
                    )
                except SourcePortfolioAcquisitionError as error:
                    status = SourceDefinitionCollectionStatus(error.code)
                    source_results.append(
                        _source_result(profile, status, candidate_count=0)
                    )
                    states.append(
                        _source_state(
                            profile,
                            run_id=run_id,
                            status=status,
                            cursor_value=previous_cursor,
                            updated_at=clock.now(),
                            previous_failures=previous_failures,
                        )
                    )
                    continue
                candidate_results.extend(acquisition.items)
                status = (
                    SourceDefinitionCollectionStatus.SUCCESS
                    if acquisition.items
                    else SourceDefinitionCollectionStatus.EMPTY
                )
                source_results.append(
                    _source_result(
                        profile,
                        status,
                        candidate_count=len(acquisition.items),
                    )
                )
                for item in acquisition.items:
                    if item.evidence_eligible and item.document_version is not None:
                        valid_documents.append((item.candidate, item.document_version))
                    record = item.source_record
                    if record.record_kind == "paper":
                        known_paper_identities.add(
                            (record.external_id, record.external_version)
                        )
                    if record.record_kind in {
                        "community-signal",
                        "github-trending",
                    }:
                        known_signal_targets.add(record.canonical_url)
                states.append(
                    _source_state(
                        profile,
                        run_id=run_id,
                        status=status,
                        cursor_value=acquisition.cursor_value or previous_cursor,
                        updated_at=clock.now(),
                        previous_failures=previous_failures,
                    )
                )
                continue
            try:
                entries = feed_adapter.discover(profile)
                for entry in entries:
                    _validate_profile_location(profile, entry.canonical_url)
                selected_entries = _select_entries(
                    profile,
                    entries,
                    cursor_value=previous_cursor,
                    backfill_limit=backfill_limit,
                )
            except FeedDiscoveryError as error:
                status = SourceDefinitionCollectionStatus(error.code)
                source_results.append(
                    _source_result(profile, status, candidate_count=0)
                )
                states.append(
                    _source_state(
                        profile,
                        run_id=run_id,
                        status=status,
                        cursor_value=previous_cursor,
                        updated_at=clock.now(),
                        previous_failures=previous_failures,
                    )
                )
                continue
            except ArticleSecurityError:
                status = SourceDefinitionCollectionStatus.INVALID_FORMAT
                source_results.append(
                    _source_result(profile, status, candidate_count=0)
                )
                states.append(
                    _source_state(
                        profile,
                        run_id=run_id,
                        status=status,
                        cursor_value=previous_cursor,
                        updated_at=clock.now(),
                        previous_failures=previous_failures,
                    )
                )
                continue

            profile_candidate_results: list[PersistableArticleResult] = []
            invalid_metadata_count = 0
            for entry in selected_entries:
                try:
                    _validate_feed_entry_metadata(profile, entry)
                except ArticleSecurityError:
                    invalid_metadata_count += 1
                    continue
                candidate = _candidate(profile, entry, observed_at=observed_at)
                try:
                    article = article_adapter.fetch(profile, entry)
                    _validate_profile_location(profile, article.canonical_url)
                    if _canonical_identity(
                        article.canonical_url,
                        profile=profile,
                    ) != _canonical_identity(
                        entry.canonical_url,
                        profile=profile,
                    ):
                        raise ArticleBodyInvalidError(
                            "Article canonical URL does not match the Feed entry"
                        )
                    _validate_article_document(profile, article)
                    if not article.body.strip():
                        raise ArticleBodyInvalidError(
                            "Article body did not pass the quality gate"
                        )
                    document = _document_version(
                        candidate,
                        entry,
                        article,
                        observed_at=observed_at,
                    )
                    result = PersistableArticleResult(
                        source_definition_id=profile.id,
                        candidate=candidate,
                        status=ArticleCollectionStatus.BODY_VALID,
                        evidence_eligible=True,
                        eligibility_kind="body-valid",
                        document_version=document,
                    )
                    valid_documents.append((candidate, document))
                except ArticleAcquisitionError as error:
                    article_status = ArticleCollectionStatus(error.code)
                    result = PersistableArticleResult(
                        source_definition_id=profile.id,
                        candidate=candidate,
                        status=article_status,
                        evidence_eligible=False,
                        eligibility_kind="ineligible",
                        error_code=article_status.value,
                        error_message=_article_error_message(article_status),
                    )
                profile_candidate_results.append(result)

            status = _profile_result_status(
                profile_candidate_results,
                invalid_metadata_count=invalid_metadata_count,
            )
            source_results.append(
                _source_result(
                    profile,
                    status,
                    candidate_count=len(selected_entries),
                )
            )
            candidate_results.extend(profile_candidate_results)
            next_cursor = previous_cursor
            if selected_entries and status in {
                SourceDefinitionCollectionStatus.SUCCESS,
                SourceDefinitionCollectionStatus.ACCESS_BLOCKED,
                SourceDefinitionCollectionStatus.INVALID_FORMAT,
            }:
                next_cursor = _cursor_value(
                    profile,
                    max(
                        selected_entries,
                        key=lambda entry: _entry_cursor_key(profile, entry),
                    ),
                )
            states.append(
                _source_state(
                    profile,
                    run_id=run_id,
                    status=status,
                    cursor_value=next_cursor,
                    updated_at=clock.now(),
                    previous_failures=previous_failures,
                )
            )

        run_status = _run_status(source_results)
        run = CollectionRun(
            id=run_id,
            retry_of_run_id=None,
            status=run_status,
            started_at=observed_at,
            completed_at=clock.now(),
            source_definition_results=tuple(source_results),
            operation_key=operation_key,
        )
        draft_repository = GeminiDraftRepository(engine)
        document_ids = {document.id for _, document in valid_documents}
        known_before = draft_repository.known_document_version_ids(document_ids)
        persisted = repository.persist(
            run,
            source_definitions,
            tuple(candidate_results),
            tuple(states),
        )
        if not persisted:
            existing = repository.operation(operation_key)
            if existing is None:
                raise RuntimeError("Concurrent Collection Run could not be loaded")
            core_results, core_contributors = repository.acceptance_counts(
                existing.collection_run_id
            )
            return MultiSourceCollectionSummary(
                collection_run_id=existing.collection_run_id,
                status=CollectionRunStatus(existing.status),
                source_results=existing.source_results,
                candidates_processed=existing.candidates_processed,
                document_versions_created=0,
                drafts_created=0,
                replayed=True,
                core_results_persisted=core_results,
                core_eligible_contributors=core_contributors,
                core_acceptance_met=(
                    core_results == len(CORE_PROFILE_KEYS) and core_contributors >= 6
                ),
            )

        drafts_created = 0
        pending_documents = repository.pending_draft_documents(
            {profile.id for profile in profiles}
        )
        for document in pending_documents:
            if draft_repository.has_draft_for_document_version(document.id):
                continue
            try:
                prepared = provider.prepare(document)
            except DraftPreparationError:
                continue
            story, claims, evidence_spans, traces = build_draft_records(
                document,
                prepared,
                occurred_at=observed_at,
                namespace="multisource",
                stable_key=(
                    f"source:{document.candidate_id}:document:{document.id}"
                ),
                identity_key=str(document.id),
            )
            if draft_repository.persist(story, claims, evidence_spans, traces):
                drafts_created += 1

        core_results = sum(
            profile.acceptance_group == "core" for profile in profiles
        )
        core_contributors = len(
            {
                item.source_definition_id
                for item in candidate_results
                if item.evidence_eligible
                and next(
                    profile.acceptance_group
                    for profile in profiles
                    if profile.id == item.source_definition_id
                )
                == "core"
            }
        )
        return MultiSourceCollectionSummary(
            collection_run_id=run.id,
            status=run.status,
            source_results={
                (profile.key if is_source_universe else profile.host): result.status.value
                for profile, result in zip(profiles, source_results, strict=True)
            },
            candidates_processed=len(candidate_results),
            document_versions_created=len(document_ids - known_before),
            drafts_created=drafts_created,
            replayed=False,
            core_results_persisted=core_results,
            core_eligible_contributors=core_contributors,
            core_acceptance_met=(
                core_results == len(CORE_PROFILE_KEYS) and core_contributors >= 6
            ),
        )
    finally:
        engine.dispose()


def _source_definition(profile: SourceProfile) -> ApprovedFeedSourceDefinition:
    is_legacy = profile.profile_version == SOURCE_PROFILE_VERSION
    return SourcePortfolioDefinition(
        id=profile.id,
        name=profile.host if is_legacy else profile.key,
        publisher=profile.publisher,
        entry_point=profile.entry_point,
        audit_version=profile.profile_version,
        collection_schedule=profile.cadence,
        discovery_method=profile.discovery_method,
        language=profile.language,
        topic_scope=profile.topic_scope,
        access_constraints=(
            profile.access_scope,
            "No login, paywall, CAPTCHA, consent, or challenge bypass",
        ),
        extraction_adapter=profile.adapter,
        health_policy=profile.health_policy,
        cursor=profile.cursor_policy,
        storage_policy=profile.body_eligibility,
        public_excerpt_policy=(
            "Only policy-eligible immutable Documents may support Claim Evidence"
        ),
        public_excerpt_max_characters=(
            0 if profile.evidence_eligibility == "never" else 1000
        ),
        pause_conditions=(profile.pause_policy,),
        canonical_url_prefixes=tuple(
            f"https://{host}{path}"
            for host in profile.allowed_hosts
            for path in profile.allowed_path_prefixes
        ),
        activation_conclusion="approved" if profile.enabled else "disabled",
        acceptance_group=profile.acceptance_group,
        contribution_role=profile.contribution_role,
        evidence_eligibility=profile.evidence_eligibility,
        body_eligibility=profile.body_eligibility,
        pause_state=profile.pause_state,
        expected_contribution=profile.expected_contribution,
        overlap_rationale=profile.overlap_rationale,
    )


def _select_entries(
    profile: SourceProfile,
    entries: tuple[FeedEntry, ...],
    *,
    cursor_value: str | None,
    backfill_limit: int,
) -> tuple[FeedEntry, ...]:
    unique_entries = {
        _canonical_identity(entry.canonical_url, profile=profile): entry
        for entry in entries
    }
    ordered = tuple(
        sorted(unique_entries.values(), key=lambda entry: _entry_cursor_key(profile, entry))
    )
    if cursor_value is None:
        return ordered[-backfill_limit:]
    cursor_key = _parse_cursor_value(cursor_value)
    unseen = tuple(
        entry for entry in ordered if _entry_cursor_key(profile, entry) > cursor_key
    )
    return unseen[:backfill_limit]


def _entry_cursor_key(
    profile: SourceProfile,
    entry: FeedEntry,
) -> tuple[str, str]:
    timestamp = entry.updated_at or entry.published_at
    normalized_timestamp = (
        timestamp.astimezone(UTC).isoformat() if timestamp is not None else "0001-01-01T00:00:00+00:00"
    )
    return normalized_timestamp, _canonical_identity(
        entry.canonical_url,
        profile=profile,
    )


def _cursor_value(profile: SourceProfile, entry: FeedEntry) -> str:
    timestamp, canonical_url = _entry_cursor_key(profile, entry)
    return json.dumps(
        {"timestamp": timestamp, "canonical_url": canonical_url},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _parse_cursor_value(cursor_value: str) -> tuple[str, str]:
    try:
        payload = json.loads(cursor_value)
        if set(payload) != {"timestamp", "canonical_url"}:
            raise ValueError
        return str(payload["timestamp"]), str(payload["canonical_url"])
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise SourceProfileConfigurationError("Persisted Source Profile cursor is invalid") from error


def _candidate(
    profile: SourceProfile,
    entry: FeedEntry,
    *,
    observed_at: datetime,
) -> Candidate:
    identity = _canonical_identity(entry.canonical_url, profile=profile)
    return Candidate(
        id=uuid5(NAMESPACE_URL, f"ai-intel-agent:source-candidate:{identity}"),
        title=entry.title,
        canonical_url=identity,
        publisher=profile.publisher,
        discovered_at=observed_at,
    )


def _document_version(
    candidate: Candidate,
    entry: FeedEntry,
    article: ArticleDocument,
    *,
    observed_at: datetime,
) -> DocumentVersion:
    content_hash = sha256(article.body.encode("utf-8")).hexdigest()
    return DocumentVersion(
        id=uuid5(
            NAMESPACE_URL,
            f"ai-intel-agent:source-document:{candidate.id}:{content_hash}",
        ),
        candidate_id=candidate.id,
        source_url=candidate.canonical_url,
        title=article.title,
        body=article.body,
        content_hash=content_hash,
        observed_at=observed_at,
        published_at=entry.published_at,
        published_at_raw=entry.published_at_raw,
        updated_at=entry.updated_at,
        updated_at_raw=entry.updated_at_raw,
    )


def _profile_result_status(
    candidate_results: list[PersistableArticleResult],
    *,
    invalid_metadata_count: int = 0,
) -> SourceDefinitionCollectionStatus:
    if not candidate_results and invalid_metadata_count == 0:
        return SourceDefinitionCollectionStatus.EMPTY
    statuses = {result.status for result in candidate_results}
    if invalid_metadata_count:
        statuses.add(ArticleCollectionStatus.INVALID_FORMAT)
    if ArticleCollectionStatus.TEMPORARY_FAILURE in statuses:
        return SourceDefinitionCollectionStatus.TEMPORARY_FAILURE
    if ArticleCollectionStatus.ACCESS_BLOCKED in statuses:
        return SourceDefinitionCollectionStatus.ACCESS_BLOCKED
    if ArticleCollectionStatus.INVALID_FORMAT in statuses:
        return SourceDefinitionCollectionStatus.INVALID_FORMAT
    return SourceDefinitionCollectionStatus.SUCCESS


def _source_result(
    profile: SourceProfile,
    status: SourceDefinitionCollectionStatus,
    *,
    candidate_count: int,
) -> SourceDefinitionCollectionResult:
    if status in {
        SourceDefinitionCollectionStatus.SUCCESS,
        SourceDefinitionCollectionStatus.EMPTY,
    }:
        error_code = None
        error_message = None
    else:
        error_code = status.value
        error_message = f"Source Profile {profile.host} completed as {status.value}"
    return SourceDefinitionCollectionResult(
        source_definition_id=profile.id,
        status=status,
        candidate_count=candidate_count,
        error_code=error_code,
        error_message=error_message,
    )


def _source_state(
    profile: SourceProfile,
    *,
    run_id: UUID,
    status: SourceDefinitionCollectionStatus,
    cursor_value: str | None,
    updated_at: datetime,
    previous_failures: int,
) -> PortfolioSourceState:
    if status in {
        SourceDefinitionCollectionStatus.SUCCESS,
        SourceDefinitionCollectionStatus.EMPTY,
    }:
        health = SourceProfileHealth.HEALTHY
        consecutive_failures = 0
    elif status is SourceDefinitionCollectionStatus.ACCESS_BLOCKED:
        health = SourceProfileHealth.BLOCKED
        consecutive_failures = previous_failures + 1
    else:
        health = SourceProfileHealth.DEGRADED
        consecutive_failures = previous_failures + 1
    return PortfolioSourceState(
        source_definition_id=profile.id,
        recent_result=status,
        cursor_value=cursor_value,
        health=health,
        consecutive_failures=consecutive_failures,
        last_collection_run_id=run_id,
        updated_at=updated_at,
        pause_state=profile.pause_state,
    )


def _run_status(
    source_results: list[SourceDefinitionCollectionResult],
) -> CollectionRunStatus:
    failures = sum(
        result.status
        in {
            SourceDefinitionCollectionStatus.INVALID_FORMAT,
            SourceDefinitionCollectionStatus.ACCESS_BLOCKED,
            SourceDefinitionCollectionStatus.TEMPORARY_FAILURE,
        }
        for result in source_results
    )
    if failures == 0:
        return CollectionRunStatus.COMPLETE
    if failures == len(source_results):
        return CollectionRunStatus.FAILED
    return CollectionRunStatus.PARTIAL


def _article_error_message(status: ArticleCollectionStatus) -> str:
    return {
        ArticleCollectionStatus.INVALID_FORMAT: "Article body or canonical format is invalid",
        ArticleCollectionStatus.ACCESS_BLOCKED: "Article body access is blocked",
        ArticleCollectionStatus.TEMPORARY_FAILURE: "Article fetch failed temporarily",
        ArticleCollectionStatus.BODY_VALID: "",
    }[status]
