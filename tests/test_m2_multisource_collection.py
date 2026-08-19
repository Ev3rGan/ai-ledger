from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from ipaddress import IPv4Address
from pathlib import Path
from threading import Event
from uuid import uuid4

import httpx
import pytest
from alembic.config import Config
from pg0 import Pg0
from sqlalchemy import func, select, update
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from ai_intel_agent.cli import app
from ai_intel_agent.domain import StoryReviewState
from ai_intel_agent.feed_acquisition import FeedEntry
from ai_intel_agent.gemini_collection import (
    DraftPreparationError,
    PreparedClaim,
    PreparedDraft,
)
from ai_intel_agent.multisource_collection import (
    ArticleAccessBlockedError,
    ArticleBodyInvalidError,
    ArticleDocument,
    ArticleSecurityError,
    ArticleTemporaryFailureError,
    FeedDiscoveryInvalidFormatError,
    FeedDiscoveryTemporaryFailureError,
    HttpArticleAdapter,
    HttpFeedDiscoveryAdapter,
    collect_source_profiles,
    load_source_profiles,
    scheduled_operation_key,
)
from ai_intel_agent.persistence import (
    CandidateRecord,
    ClaimRecord,
    CollectionRunRecord,
    DocumentVersionRecord,
    EvidenceSpanRecord,
    SourceCandidateResultRecord,
    SourceDefinitionCollectionResultRecord,
    SourceDefinitionRecord,
    SourceProfileStateRecord,
    StoryRecord,
    TraceRecord,
    create_database_engine,
    database_url_for_alembic_config,
    upgrade_database,
)
from ai_intel_agent.runtime import PostgresCollectionLease
from alembic import command

PUBLIC_ADDRESS = IPv4Address("93.184.216.34")
runner = CliRunner()


@dataclass(frozen=True)
class StaticResolver:
    addresses: tuple[IPv4Address, ...] = (PUBLIC_ADDRESS,)

    def resolve(self, hostname: str) -> tuple[IPv4Address, ...]:
        return self.addresses


@dataclass(frozen=True)
class FixedClock:
    current: datetime = datetime(2026, 8, 17, 2, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.current


def _prepared_fixture_draft(document) -> PreparedDraft:
    evidence = document.body.split(" Context", 1)[0]
    return PreparedDraft(
        headline=f"{document.title} 的中文草稿",
        claims=(PreparedClaim(text="这是可核查的中文事实。", evidence=evidence),),
        route_identifier="deepseek:v4-pro",
        candidate_configuration_version="fixture-candidates.v1",
        routing_evaluation_version="fixture-evaluation.v1",
        routing_evaluation_cases_sha256="a" * 64,
        protocol_version="fixture-draft.v1",
        protocol_content_sha256="b" * 64,
        prompt_version="fixture-prompt.v1",
        model_id="deepseek-v4-pro",
        model_version="fixture-model-version",
        returned_model_id="deepseek-v4-pro",
        attempts=1,
        latency_ms=1,
        input_tokens=10,
        output_tokens=5,
    )


@pytest.fixture
def m2_database_url():
    name = f"ai_intel_m2_{uuid4().hex}"
    data_root = os.getenv("PG0_TEST_DATA_ROOT")
    data_dir = str(Path(data_root) / name) if data_root else None
    server = Pg0(name=name, data_dir=data_dir)
    server.start()
    try:
        upgrade_database(server.uri)
        yield server.uri
    finally:
        server.drop()


def test_source_profiles_are_exactly_the_current_four_host_whitelist() -> None:
    profiles = load_source_profiles()

    assert {profile.host for profile in profiles} == {
        "the-decoder.com",
        "techcrunch.com",
        "huggingface.co",
        "qbitai.com",
    }
    assert len(profiles) == 4
    assert len({profile.id for profile in profiles}) == 4
    assert {profile.profile_version for profile in profiles} == {
        "mvp-v2-m2-source-profiles-2026-08-17.v1"
    }
    techcrunch = next(profile for profile in profiles if profile.host == "techcrunch.com")
    assert techcrunch.feed_url == (
        "https://techcrunch.com/category/artificial-intelligence/feed/"
    )
    assert all(profile.feed_url.startswith("https://") for profile in profiles)
    assert all(profile.allowed_hosts for profile in profiles)
    assert all(profile.allowed_path_prefixes for profile in profiles)
    assert all(profile.language for profile in profiles)
    assert all(profile.topic_scope for profile in profiles)
    assert all(profile.cursor_policy for profile in profiles)
    assert all(profile.health_policy for profile in profiles)


@pytest.mark.parametrize(
    ("instant", "expected"),
    (
        (
            datetime(2026, 8, 16, 22, 0, tzinfo=UTC),
            (
                "m2-incremental:2026-08-17T06:00+08:00:"
                "mvp-v2-1-m1-active-source-profiles-2026-08-19.v1"
            ),
        ),
        (
            datetime(2026, 8, 17, 10, 0, tzinfo=UTC),
            (
                "m2-incremental:2026-08-17T18:00+08:00:"
                "mvp-v2-1-m1-active-source-profiles-2026-08-19.v1"
            ),
        ),
        (
            datetime(2026, 8, 17, 20, 0, tzinfo=UTC),
            (
                "m2-incremental:2026-08-17T18:00+08:00:"
                "mvp-v2-1-m1-active-source-profiles-2026-08-19.v1"
            ),
        ),
    ),
)
def test_scheduled_operation_key_is_stable_for_each_shanghai_slot(
    instant: datetime,
    expected: str,
) -> None:
    assert scheduled_operation_key(instant) == expected


def test_article_adapter_extracts_sanitized_body_and_validates_canonical_url() -> None:
    profile = next(
        profile for profile in load_source_profiles() if profile.host == "the-decoder.com"
    )
    entry = FeedEntry(
        title="A bounded article",
        canonical_url="https://the-decoder.com/a-bounded-article/",
        summary="Discovery metadata must not become the body.",
        published_at=None,
        published_at_raw=None,
        updated_at=None,
        updated_at_raw=None,
    )
    lead = (
        "This independently written article contains enough concrete words to pass the "
        "production quality gate while analyzing authentication policy. "
    ) * 8
    discussion = (
        "Checking your browser compatibility is essential before running web-based AI models. "
        "Login to continue messages are a common dark pattern in consumer software. "
        "The article also explains access denied errors without making this page an "
        "access-control shell. "
    )
    conclusion = (
        "The remaining analysis describes recovery, observability, and safe operator action. "
    ) * 8
    html = f"""<!doctype html>
    <html><head>
      <title>A bounded article</title>
      <link rel="canonical" href="{entry.canonical_url}">
      <script>Feed summary and hostile instructions must disappear.</script>
    </head><body><main><article>
      <h1>A bounded article</h1>
      <p>{discussion}</p><p>{lead}</p><p>{conclusion}</p>
      <style>private {{ display: block }}</style>
    </article></main></body></html>"""

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.host == str(PUBLIC_ADDRESS)
        assert request.headers["host"] == "the-decoder.com"
        assert request.extensions["sni_hostname"] == "the-decoder.com"
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/html; charset=utf-8"},
            text=html,
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        article = HttpArticleAdapter(client, resolver=StaticResolver()).fetch(
            profile,
            entry,
        )

    assert article.canonical_url == entry.canonical_url.rstrip("/")
    assert article.title == "A bounded article"
    assert "production quality gate" in article.body
    assert entry.summary not in article.body
    assert "hostile instructions" not in article.body
    assert "private" not in article.body


@pytest.mark.parametrize(
    ("response_html", "error_type"),
    [
        (
            (
                "<html><head><title>Checking your browser</title></head>"
                "<body>Cloudflare verification. Enable JavaScript and cookies to continue."
                " CAPTCHA challenge.</body></html>"
            ),
            ArticleAccessBlockedError,
        ),
        (
            (
                "<html><head><title>Consent required</title></head>"
                "<body>Accept cookies to continue.</body></html>"
            ),
            ArticleAccessBlockedError,
        ),
        (
            (
                "<html><head><title>Short article</title>"
                '<link rel="canonical" href="https://the-decoder.com/short">'
                "</head><body><article><p>Too short.</p></article></body></html>"
            ),
            ArticleBodyInvalidError,
        ),
        (
            "<html><head><title>Wrong canonical</title>"
            '<link rel="canonical" href="https://the-decoder.com/different">'
            "</head><body><article><p>" + ("substantive article words " * 100) + "</p>"
            "</article></body></html>",
            ArticleBodyInvalidError,
        ),
    ],
    ids=("challenge", "consent", "short-body", "canonical-mismatch"),
)
def test_article_adapter_fails_closed_for_blocked_or_invalid_pages(
    response_html: str,
    error_type: type[Exception],
) -> None:
    profile = next(
        profile for profile in load_source_profiles() if profile.host == "the-decoder.com"
    )
    entry = FeedEntry(
        title="Short article",
        canonical_url="https://the-decoder.com/short",
        summary="This summary is discovery metadata only.",
        published_at=None,
        published_at_raw=None,
        updated_at=None,
        updated_at_raw=None,
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/html"},
            text=response_html,
        )
    )

    with httpx.Client(transport=transport) as client, pytest.raises(error_type):
        HttpArticleAdapter(client, resolver=StaticResolver()).fetch(profile, entry)


@pytest.mark.parametrize(
    "shell_text",
    (
        "This article is for subscribers.",
        "Login to continue.",
        "Login to continue reading.",
        "Log in to continue.",
        "Sign in to continue.",
        "Subscribe to continue.",
        "Subscribe to continue reading.",
        "Sign in to continue reading.",
        "Log in to continue reading.",
        "Register to continue.",
        "Register to continue reading.",
        "Create an account to continue.",
        "Create an account to continue reading.",
        "Accept cookies to continue.",
    ),
)
def test_long_single_signal_access_shell_is_blocked_after_substantive_copy(
    shell_text: str,
) -> None:
    profile = next(
        profile for profile in load_source_profiles() if profile.host == "the-decoder.com"
    )
    entry = FeedEntry(
        title="Preview shell",
        canonical_url="https://the-decoder.com/preview-shell/",
        summary="Discovery only.",
        published_at=None,
        published_at_raw=None,
        updated_at=None,
        updated_at_raw=None,
    )
    html = (
        "<html><head><title>Preview shell</title>"
        f'<link rel="canonical" href="{entry.canonical_url}"></head>'
        "<body><article><p>"
        + ("Substantive preview context with many ordinary article words. " * 40)
        + f"</p><p>{shell_text}</p><p>"
        + ("Related footer context with many ordinary navigation words. " * 40)
        + "</p></article></body></html>"
    )

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/html"},
            text=html,
        )

    with httpx.Client(
        transport=httpx.MockTransport(respond)
    ) as client, pytest.raises(ArticleAccessBlockedError):
        HttpArticleAdapter(client, resolver=StaticResolver()).fetch(profile, entry)


def test_article_adapter_rejects_cross_whitelist_redirect_before_following_it() -> None:
    profile = next(
        profile for profile in load_source_profiles() if profile.host == "huggingface.co"
    )
    entry = FeedEntry(
        title="Trusted article",
        canonical_url="https://huggingface.co/blog/trusted-article",
        summary="",
        published_at=None,
        published_at_raw=None,
        updated_at=None,
        updated_at_raw=None,
    )
    requests: list[httpx.Request] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            302,
            request=request,
            headers={"location": "https://attacker.example/internal"},
        )

    with httpx.Client(
        transport=httpx.MockTransport(redirect)
    ) as client, pytest.raises(ArticleSecurityError, match="Source Profile scope"):
        HttpArticleAdapter(client, resolver=StaticResolver()).fetch(profile, entry)

    assert len(requests) == 1


def test_article_adapter_rejects_private_dns_before_any_request() -> None:
    profile = next(
        profile for profile in load_source_profiles() if profile.host == "the-decoder.com"
    )
    entry = FeedEntry(
        title="Private target",
        canonical_url="https://the-decoder.com/private-target/",
        summary="Discovery only.",
        published_at=None,
        published_at_raw=None,
        updated_at=None,
        updated_at_raw=None,
    )
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, request=request, text="must not be fetched")

    resolver = StaticResolver(addresses=(IPv4Address("127.0.0.1"),))
    with httpx.Client(
        transport=httpx.MockTransport(respond)
    ) as client, pytest.raises(ArticleSecurityError, match="public network"):
        HttpArticleAdapter(client, resolver=resolver).fetch(profile, entry)

    assert requests == []


def test_article_adapter_enforces_the_response_size_limit() -> None:
    profile = next(
        profile for profile in load_source_profiles() if profile.host == "the-decoder.com"
    )
    entry = FeedEntry(
        title="Oversized target",
        canonical_url="https://the-decoder.com/oversized-target/",
        summary="Discovery only.",
        published_at=None,
        published_at_raw=None,
        updated_at=None,
        updated_at_raw=None,
    )

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/html", "content-length": "1000"},
            content=b"small transport fixture",
        )

    with httpx.Client(
        transport=httpx.MockTransport(respond)
    ) as client, pytest.raises(ArticleBodyInvalidError, match="response policy"):
        HttpArticleAdapter(
            client,
            resolver=StaticResolver(),
            max_response_bytes=128,
        ).fetch(profile, entry)


@pytest.mark.parametrize("status_code", (429, 503))
@pytest.mark.parametrize(
    ("adapter_kind", "error_type"),
    (
        ("feed", FeedDiscoveryTemporaryFailureError),
        ("article", ArticleTemporaryFailureError),
    ),
)
def test_transient_http_status_is_temporary_and_retryable(
    adapter_kind: str,
    error_type: type[Exception],
    status_code: int,
) -> None:
    profile = next(
        profile for profile in load_source_profiles() if profile.host == "techcrunch.com"
    )
    entry = FeedEntry(
        title="Rate-limited article",
        canonical_url="https://techcrunch.com/2026/08/17/rate-limited-article/",
        summary="Discovery only.",
        published_at=None,
        published_at_raw=None,
        updated_at=None,
        updated_at_raw=None,
    )

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, request=request, text="temporary failure")

    with httpx.Client(
        transport=httpx.MockTransport(respond)
    ) as client, pytest.raises(error_type):
        if adapter_kind == "feed":
            HttpFeedDiscoveryAdapter(client, resolver=StaticResolver()).discover(profile)
        else:
            HttpArticleAdapter(client, resolver=StaticResolver()).fetch(profile, entry)


@pytest.mark.parametrize("case", ("not-found", "oversized"))
def test_feed_terminal_response_policy_failure_is_invalid_format(case: str) -> None:
    profile = next(
        profile for profile in load_source_profiles() if profile.host == "the-decoder.com"
    )

    def respond(request: httpx.Request) -> httpx.Response:
        if case == "not-found":
            return httpx.Response(404, request=request, text="missing")
        return httpx.Response(
            200,
            request=request,
            headers={
                "content-type": "application/rss+xml",
                "content-length": "1000",
            },
            content=b"small transport fixture",
        )

    with httpx.Client(
        transport=httpx.MockTransport(respond)
    ) as client, pytest.raises(FeedDiscoveryInvalidFormatError, match="response policy"):
        HttpFeedDiscoveryAdapter(
            client,
            resolver=StaticResolver(),
            max_response_bytes=128,
        ).discover(profile)


def test_shared_feed_adapter_parses_metadata_without_promoting_the_summary() -> None:
    profile = next(
        profile for profile in load_source_profiles() if profile.host == "techcrunch.com"
    )
    feed = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel><item>
      <title>Scoped AI article</title>
      <link>https://techcrunch.com/2026/08/17/scoped-ai-article/</link>
      <description>Discovery metadata only, never an article body.</description>
      <pubDate>Mon, 17 Aug 2026 01:00:00 +0000</pubDate>
    </item></channel></rss>"""

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["host"] == "techcrunch.com"
        assert request.url.host == str(PUBLIC_ADDRESS)
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "application/rss+xml"},
            content=feed,
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        entries = HttpFeedDiscoveryAdapter(client, resolver=StaticResolver()).discover(
            profile
        )

    assert len(entries) == 1
    assert entries[0].canonical_url.endswith("/scoped-ai-article/")
    assert entries[0].summary == "Discovery metadata only, never an article body."


def test_techcrunch_feed_adapter_rejects_redirect_to_a_general_feed() -> None:
    profile = next(
        profile for profile in load_source_profiles() if profile.host == "techcrunch.com"
    )
    requests: list[httpx.Request] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            302,
            request=request,
            headers={"location": "https://techcrunch.com/feed/"},
        )

    with httpx.Client(
        transport=httpx.MockTransport(redirect)
    ) as client, pytest.raises(FeedDiscoveryInvalidFormatError, match="Feed URL"):
        HttpFeedDiscoveryAdapter(client, resolver=StaticResolver()).discover(profile)

    assert len(requests) == 1


@pytest.mark.postgres
def test_four_source_profile_collection_is_idempotent_blocked_safe_and_traceable(
    m2_database_url: str,
) -> None:
    profiles = load_source_profiles()
    empty_status_result = runner.invoke(
        app,
        ["operator", "source-status"],
        env={"AI_INTEL_DATABASE_URL": m2_database_url},
    )
    assert empty_status_result.exit_code == 0, empty_status_result.output
    empty_status = json.loads(empty_status_result.output)["sources"]
    assert {item["host"] for item in empty_status} == {
        profile.host for profile in profiles
    }
    assert all(item["recent_result"] is None for item in empty_status)
    assert all(item["health"] == "unknown" for item in empty_status)

    feed_calls: list[str] = []
    article_calls: list[str] = []
    provider_calls: list[str] = []
    summaries = {
        profile.host: f"FEED SUMMARY FOR {profile.host} MUST NEVER BECOME A BODY"
        for profile in profiles
    }

    class FakeFeedAdapter:
        def discover(self, profile):
            feed_calls.append(profile.host)
            return (
                FeedEntry(
                    title=f"{profile.publisher} source article",
                    canonical_url=(
                        "https://www.qbitai.com/2026/08/400001.html"
                        if profile.host == "qbitai.com"
                        else f"https://{profile.host}/articles/source-article"
                        if profile.host != "huggingface.co"
                        else "https://huggingface.co/blog/source-article"
                    ),
                    summary=summaries[profile.host],
                    published_at=datetime(2026, 8, 16, 12, 0, tzinfo=UTC),
                    published_at_raw="2026-08-16T12:00:00Z",
                    updated_at=None,
                    updated_at_raw=None,
                ),
            )

    class FakeArticleAdapter:
        def fetch(self, profile, entry):
            article_calls.append(profile.host)
            if profile.host == "huggingface.co":
                raise ArticleAccessBlockedError("fixture access block")
            evidence = f"Exact source evidence for {profile.host}."
            return ArticleDocument(
                title=entry.title,
                canonical_url=entry.canonical_url,
                body=evidence + " " + ("Independent article context. " * 30),
            )

    class FakeDraftProvider:
        def prepare(self, document):
            provider_calls.append(document.source_url)
            evidence = document.body.split(" Independent", 1)[0]
            return PreparedDraft(
                headline=f"{document.title} 的中文草稿",
                claims=(PreparedClaim(text="这是可核查的中文事实。", evidence=evidence),),
                route_identifier="deepseek:v4-pro",
                candidate_configuration_version="fixture-candidates.v1",
                routing_evaluation_version="fixture-evaluation.v1",
                routing_evaluation_cases_sha256="a" * 64,
                protocol_version="fixture-draft.v1",
                protocol_content_sha256="b" * 64,
                prompt_version="fixture-prompt.v1",
                model_id="deepseek-v4-pro",
                model_version="fixture-model-version",
                returned_model_id="deepseek-v4-pro",
                attempts=1,
                latency_ms=1,
                input_tokens=10,
                output_tokens=5,
            )

    first = collect_source_profiles(
        m2_database_url,
        profiles=profiles,
        feed_adapter=FakeFeedAdapter(),
        article_adapter=FakeArticleAdapter(),
        provider=FakeDraftProvider(),
        clock=FixedClock(),
        operation_key="m2-backfill:fixture-2026-08-17",
        backfill_limit=5,
    )
    replay = collect_source_profiles(
        m2_database_url,
        profiles=profiles,
        feed_adapter=FakeFeedAdapter(),
        article_adapter=FakeArticleAdapter(),
        provider=FakeDraftProvider(),
        clock=FixedClock(),
        operation_key="m2-backfill:fixture-2026-08-17",
        backfill_limit=5,
    )

    assert first.replayed is False
    assert first.status.value == "partial"
    assert first.source_results == {
        "the-decoder.com": "success",
        "techcrunch.com": "success",
        "huggingface.co": "access-blocked",
        "qbitai.com": "success",
    }
    assert first.document_versions_created == 3
    assert first.drafts_created == 3
    assert replay.collection_run_id == first.collection_run_id
    assert replay.replayed is True
    assert feed_calls == [profile.host for profile in profiles]
    assert article_calls == [profile.host for profile in profiles]
    assert len(provider_calls) == 3

    engine = create_database_engine(m2_database_url)
    try:
        with Session(engine) as session:
            counts = {
                record.__tablename__: session.scalar(
                    select(func.count()).select_from(record)
                )
                for record in (
                    SourceDefinitionRecord,
                    SourceProfileStateRecord,
                    CollectionRunRecord,
                    SourceDefinitionCollectionResultRecord,
                    SourceCandidateResultRecord,
                    CandidateRecord,
                    DocumentVersionRecord,
                    StoryRecord,
                    ClaimRecord,
                    EvidenceSpanRecord,
                    TraceRecord,
                )
            }
            documents = session.scalars(select(DocumentVersionRecord)).all()
            stories = session.scalars(select(StoryRecord)).all()
            claims = session.scalars(select(ClaimRecord)).all()
            evidence = session.scalars(select(EvidenceSpanRecord)).all()
            traces = session.scalars(select(TraceRecord)).all()
            states = {
                record.source_definition_id: record
                for record in session.scalars(select(SourceProfileStateRecord))
            }
            definitions = {
                record.id: record
                for record in session.scalars(select(SourceDefinitionRecord))
            }
        with Session(engine) as session, pytest.raises(
            ProgrammingError,
            match="Source candidate collection result is immutable",
        ):
            session.execute(
                update(SourceCandidateResultRecord)
                .where(SourceCandidateResultRecord.article_status == "body-valid")
                .values(article_status="body-valid")
            )
        with Session(engine) as session, pytest.raises(
            ProgrammingError,
            match="Candidate is immutable",
        ):
            session.execute(
                update(CandidateRecord)
                .where(CandidateRecord.id == next(iter(documents)).candidate_id)
                .values(title="silently rewritten")
            )
    finally:
        engine.dispose()

    assert counts == {
        "source_definitions": 4,
        "source_profile_states": 4,
        "collection_runs": 1,
        "source_definition_collection_results": 4,
        "source_candidate_results": 4,
        "candidates": 4,
        "document_versions": 3,
        "stories": 3,
        "claims": 3,
        "evidence_spans": 3,
        "structured_traces": 3,
    }
    assert all(
        summaries[definitions[state.source_definition_id].entry_point.split("/")[2]]
        not in document.body
        for state in states.values()
        if definitions[state.source_definition_id].entry_point.split("/")[2] in summaries
        for document in documents
    )
    assert {story.review_state for story in stories} == {
        StoryReviewState.UNREVIEWED.value
    }
    documents_by_id = {document.id: document for document in documents}
    claims_by_id = {claim.id: claim for claim in claims}
    stories_by_id = {story.id: story for story in stories}
    for span in evidence:
        claim = claims_by_id[span.claim_id]
        story = stories_by_id[claim.story_id]
        document = documents_by_id[span.document_version_id]
        assert story.primary_document_version_id == document.id
        assert span.exact_text == document.body[span.start_offset : span.end_offset]
        assert span.text_hash == sha256(span.exact_text.encode("utf-8")).hexdigest()
    assert {trace.operation_key for trace in traces} == {
        f"multisource-draft:{story.id}:claim:0" for story in stories
    }
    blocked_state = next(
        state
        for source_id, state in states.items()
        if definitions[source_id].publisher == "Hugging Face"
    )
    assert blocked_state.recent_result == "access-blocked"
    assert blocked_state.health == "blocked"
    assert blocked_state.cursor_value is not None

    status_result = runner.invoke(
        app,
        ["operator", "source-status"],
        env={"AI_INTEL_DATABASE_URL": m2_database_url},
    )
    assert status_result.exit_code == 0, status_result.output
    status_payload = json.loads(status_result.output)
    assert len(status_payload["sources"]) == 4
    status_by_host = {item["host"]: item for item in status_payload["sources"]}
    assert set(status_by_host) == {
        "the-decoder.com",
        "techcrunch.com",
        "huggingface.co",
        "qbitai.com",
    }
    assert status_by_host["huggingface.co"]["recent_result"] == "access-blocked"
    assert status_by_host["huggingface.co"]["health"] == "blocked"
    assert status_by_host["huggingface.co"]["pending_drafts"] == 0
    assert status_by_host["techcrunch.com"]["pending_drafts"] == 0
    assert status_by_host["techcrunch.com"]["cursor"] is not None

    operational_result = runner.invoke(
        app,
        ["operator", "status"],
        env={"AI_INTEL_DATABASE_URL": m2_database_url},
    )
    assert operational_result.exit_code == 0, operational_result.output
    operational = json.loads(operational_result.output)
    assert operational["recent_collection"] == {
        "id": str(first.collection_run_id),
        "operation_key": "m2-backfill:fixture-2026-08-17",
        "status": "partial",
        "started_at": "2026-08-17T02:00:00+00:00",
        "completed_at": "2026-08-17T02:00:00+00:00",
        "candidates_processed": 4,
    }
    assert operational["pending_reviews"] == 3
    assert {
        source["host"]: source["health"] for source in operational["sources"]
    } == {
        "the-decoder.com": "healthy",
        "techcrunch.com": "healthy",
        "huggingface.co": "blocked",
        "qbitai.com": "healthy",
    }


@pytest.mark.postgres
def test_one_source_profile_failure_preserves_other_source_profile_results(
    m2_database_url: str,
) -> None:
    profiles = load_source_profiles()

    class PartiallyFailingFeedAdapter:
        def discover(self, profile):
            if profile.host == "the-decoder.com":
                raise FeedDiscoveryTemporaryFailureError("fixture timeout")
            return (
                FeedEntry(
                    title=f"{profile.publisher} source article",
                    canonical_url=(
                        "https://www.qbitai.com/2026/08/400002.html"
                        if profile.host == "qbitai.com"
                        else "https://huggingface.co/blog/partial-source"
                        if profile.host == "huggingface.co"
                        else f"https://{profile.host}/articles/partial-source"
                    ),
                    summary="Discovery only.",
                    published_at=datetime(2026, 8, 16, 13, 0, tzinfo=UTC),
                    published_at_raw="2026-08-16T13:00:00Z",
                    updated_at=None,
                    updated_at_raw=None,
                ),
            )

    class BodyAdapter:
        def fetch(self, profile, entry):
            evidence = f"Exact source evidence for {profile.host}."
            return ArticleDocument(
                title=entry.title,
                canonical_url=entry.canonical_url,
                body=evidence + " " + ("Independent article context. " * 30),
            )

    class DraftProvider:
        def prepare(self, document):
            evidence = document.body.split(" Independent", 1)[0]
            return PreparedDraft(
                headline=f"{document.title} 的中文草稿",
                claims=(PreparedClaim(text="这是可核查的中文事实。", evidence=evidence),),
                route_identifier="deepseek:v4-pro",
                candidate_configuration_version="fixture-candidates.v1",
                routing_evaluation_version="fixture-evaluation.v1",
                routing_evaluation_cases_sha256="a" * 64,
                protocol_version="fixture-draft.v1",
                protocol_content_sha256="b" * 64,
                prompt_version="fixture-prompt.v1",
                model_id="deepseek-v4-pro",
                model_version="fixture-model-version",
                returned_model_id="deepseek-v4-pro",
                attempts=1,
                latency_ms=1,
                input_tokens=10,
                output_tokens=5,
            )

    summary = collect_source_profiles(
        m2_database_url,
        profiles=profiles,
        feed_adapter=PartiallyFailingFeedAdapter(),
        article_adapter=BodyAdapter(),
        provider=DraftProvider(),
        clock=FixedClock(),
        operation_key="m2-partial:fixture-2026-08-17",
    )

    assert summary.status.value == "partial"
    assert summary.source_results == {
        "the-decoder.com": "temporary-failure",
        "techcrunch.com": "success",
        "huggingface.co": "success",
        "qbitai.com": "success",
    }
    assert summary.document_versions_created == 3
    assert summary.drafts_created == 3

    engine = create_database_engine(m2_database_url)
    try:
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(CandidateRecord)) == 3
            assert (
                session.scalar(select(func.count()).select_from(DocumentVersionRecord))
                == 3
            )
            assert session.scalar(select(func.count()).select_from(StoryRecord)) == 3
            states = {
                definition.name: state
                for state, definition in session.execute(
                    select(SourceProfileStateRecord, SourceDefinitionRecord).join(
                        SourceDefinitionRecord,
                        SourceDefinitionRecord.id
                        == SourceProfileStateRecord.source_definition_id,
                    )
                )
            }
    finally:
        engine.dispose()

    assert states["the-decoder.com"].health == "degraded"
    assert states["the-decoder.com"].cursor_value is None
    assert set(states) == {
        "the-decoder.com",
        "techcrunch.com",
        "huggingface.co",
        "qbitai.com",
    }
    assert states["techcrunch.com"].health == "healthy"


@pytest.mark.postgres
def test_persisted_cursor_makes_a_new_incremental_operation_logically_idempotent(
    m2_database_url: str,
) -> None:
    profiles = load_source_profiles()
    article_calls: list[str] = []
    provider_calls: list[str] = []

    class StableFeedAdapter:
        def discover(self, profile):
            return (
                FeedEntry(
                    title=f"{profile.publisher} cursor article",
                    canonical_url=(
                        "https://www.qbitai.com/2026/08/400003.html"
                        if profile.host == "qbitai.com"
                        else "https://huggingface.co/blog/cursor-source"
                        if profile.host == "huggingface.co"
                        else f"https://{profile.host}/articles/cursor-source"
                    ),
                    summary="Discovery only.",
                    published_at=datetime(2026, 8, 16, 14, 0, tzinfo=UTC),
                    published_at_raw="2026-08-16T14:00:00Z",
                    updated_at=None,
                    updated_at_raw=None,
                ),
            )

    class CountingArticleAdapter:
        def fetch(self, profile, entry):
            article_calls.append(profile.host)
            evidence = f"Exact source evidence for {profile.host}."
            return ArticleDocument(
                title=entry.title,
                canonical_url=entry.canonical_url,
                body=evidence + " " + ("Independent article context. " * 30),
            )

    class CountingDraftProvider:
        def prepare(self, document):
            provider_calls.append(document.source_url)
            evidence = document.body.split(" Independent", 1)[0]
            return PreparedDraft(
                headline=f"{document.title} 的中文草稿",
                claims=(PreparedClaim(text="这是可核查的中文事实。", evidence=evidence),),
                route_identifier="deepseek:v4-pro",
                candidate_configuration_version="fixture-candidates.v1",
                routing_evaluation_version="fixture-evaluation.v1",
                routing_evaluation_cases_sha256="a" * 64,
                protocol_version="fixture-draft.v1",
                protocol_content_sha256="b" * 64,
                prompt_version="fixture-prompt.v1",
                model_id="deepseek-v4-pro",
                model_version="fixture-model-version",
                returned_model_id="deepseek-v4-pro",
                attempts=1,
                latency_ms=1,
                input_tokens=10,
                output_tokens=5,
            )

    article_adapter = CountingArticleAdapter()
    provider = CountingDraftProvider()
    first = collect_source_profiles(
        m2_database_url,
        profiles=profiles,
        feed_adapter=StableFeedAdapter(),
        article_adapter=article_adapter,
        provider=provider,
        clock=FixedClock(),
        operation_key="m2-cursor:first",
    )
    incremental = collect_source_profiles(
        m2_database_url,
        profiles=profiles,
        feed_adapter=StableFeedAdapter(),
        article_adapter=article_adapter,
        provider=provider,
        clock=FixedClock(),
        operation_key="m2-cursor:incremental",
    )

    assert first.document_versions_created == 4
    assert first.drafts_created == 4
    assert incremental.replayed is False
    assert set(incremental.source_results.values()) == {"empty"}
    assert incremental.candidates_processed == 0
    assert incremental.document_versions_created == 0
    assert incremental.drafts_created == 0
    assert article_calls == [profile.host for profile in profiles]
    assert len(provider_calls) == 4

    engine = create_database_engine(m2_database_url)
    try:
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(CollectionRunRecord)) == 2
            assert (
                session.scalar(
                    select(func.count()).select_from(
                        SourceDefinitionCollectionResultRecord
                    )
                )
                == 8
            )
            assert session.scalar(select(func.count()).select_from(CandidateRecord)) == 4
            assert (
                session.scalar(select(func.count()).select_from(DocumentVersionRecord))
                == 4
            )
            assert session.scalar(select(func.count()).select_from(StoryRecord)) == 4
            active_state_count = session.scalar(
                select(func.count()).select_from(SourceProfileStateRecord)
            )
    finally:
        engine.dispose()

    assert active_state_count == 4


@pytest.mark.postgres
def test_multisource_collection_lease_prevents_overlapping_operations(
    m2_database_url: str,
) -> None:
    first_engine = create_database_engine(m2_database_url)
    second_engine = create_database_engine(m2_database_url)
    try:
        with PostgresCollectionLease(first_engine), pytest.raises(
            RuntimeError,
            match="already active",
        ), PostgresCollectionLease(second_engine):
            pass
        with PostgresCollectionLease(second_engine):
            pass
    finally:
        first_engine.dispose()
        second_engine.dispose()


@pytest.mark.postgres
def test_multisource_collection_lease_monitors_loss_during_io(
    m2_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_database_engine(m2_database_url)
    lease = PostgresCollectionLease(engine)
    lease_lost = Event()

    def fail_lease_check() -> None:
        raise RuntimeError("Multi-source Collection lease was lost")

    try:
        with lease:
            monkeypatch.setattr(lease, "assert_held", fail_lease_check)
            with lease.monitor(lease_lost.set, check_interval_seconds=0.01):
                assert lease_lost.wait(timeout=1)
    finally:
        engine.dispose()


@pytest.mark.postgres
def test_collection_reacquisition_waits_for_the_lost_owner_to_stop(
    m2_database_url: str,
) -> None:
    first_engine = create_database_engine(m2_database_url)
    replacement_engine = create_database_engine(m2_database_url)
    lease_lost = Event()
    lease_options = {
        "monitor_check_interval_seconds": 0.01,
        "activation_grace_seconds": 0.03,
    }
    first = PostgresCollectionLease(first_engine, **lease_options)
    replacement = PostgresCollectionLease(replacement_engine, **lease_options)
    try:
        with first, first.monitor(lease_lost.set):
            assert first._connection is not None
            first._connection.invalidate()
            first._connection.close()
            with replacement:
                assert lease_lost.is_set()
    finally:
        first_engine.dispose()
        replacement_engine.dispose()


@pytest.mark.postgres
def test_changed_article_body_creates_a_new_document_version_and_draft(
    m2_database_url: str,
) -> None:
    profiles = load_source_profiles()
    revision = 1

    class VersionedFeedAdapter:
        def discover(self, profile):
            if profile.host != "techcrunch.com":
                return ()
            return (
                FeedEntry(
                    title="Versioned AI article",
                    canonical_url="https://techcrunch.com/2026/08/17/versioned-ai/",
                    summary="Discovery only.",
                    published_at=datetime(2026, 8, 17, 1, 0, tzinfo=UTC),
                    published_at_raw="2026-08-17T01:00:00Z",
                    updated_at=datetime(2026, 8, 17, revision, 0, tzinfo=UTC),
                    updated_at_raw=f"2026-08-17T{revision:02d}:00:00Z",
                ),
            )

    class VersionedArticleAdapter:
        def fetch(self, _profile, entry):
            evidence = f"Exact version {revision} evidence."
            return ArticleDocument(
                title=entry.title,
                canonical_url=entry.canonical_url,
                body=evidence + " " + (f"Version {revision} context. " * 40),
            )

    class DraftProvider:
        def prepare(self, document):
            evidence = document.body.split(" Version", 1)[0]
            return PreparedDraft(
                headline="版本化草稿",
                claims=(PreparedClaim(text="可核查的版本事实。", evidence=evidence),),
                route_identifier="deepseek:v4-pro",
                candidate_configuration_version="fixture-candidates.v1",
                routing_evaluation_version="fixture-evaluation.v1",
                routing_evaluation_cases_sha256="a" * 64,
                protocol_version="fixture-draft.v1",
                protocol_content_sha256="b" * 64,
                prompt_version="fixture-prompt.v1",
                model_id="deepseek-v4-pro",
                model_version="fixture-model-version",
                returned_model_id="deepseek-v4-pro",
                attempts=1,
                latency_ms=1,
                input_tokens=10,
                output_tokens=5,
            )

    first = collect_source_profiles(
        m2_database_url,
        profiles=profiles,
        feed_adapter=VersionedFeedAdapter(),
        article_adapter=VersionedArticleAdapter(),
        provider=DraftProvider(),
        clock=FixedClock(),
        operation_key="m2-versioned:first",
    )
    revision = 2
    second = collect_source_profiles(
        m2_database_url,
        profiles=profiles,
        feed_adapter=VersionedFeedAdapter(),
        article_adapter=VersionedArticleAdapter(),
        provider=DraftProvider(),
        clock=FixedClock(),
        operation_key="m2-versioned:second",
    )

    assert first.document_versions_created == 1
    assert first.drafts_created == 1
    assert second.document_versions_created == 1
    assert second.drafts_created == 1

    engine = create_database_engine(m2_database_url)
    try:
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(CandidateRecord)) == 1
            assert (
                session.scalar(select(func.count()).select_from(DocumentVersionRecord))
                == 2
            )
            assert session.scalar(select(func.count()).select_from(StoryRecord)) == 2
            assert session.scalar(select(func.count()).select_from(EvidenceSpanRecord)) == 2
    finally:
        engine.dispose()


@pytest.mark.postgres
def test_provider_failure_leaves_a_visible_pending_draft_and_retries_it(
    m2_database_url: str,
) -> None:
    profiles = load_source_profiles()

    class OneArticleFeedAdapter:
        def discover(self, profile):
            if profile.host != "huggingface.co":
                return ()
            return (
                FeedEntry(
                    title="Retryable draft source",
                    canonical_url="https://huggingface.co/blog/retryable-draft-source",
                    summary="Discovery only.",
                    published_at=datetime(2026, 8, 17, 3, 0, tzinfo=UTC),
                    published_at_raw="2026-08-17T03:00:00Z",
                    updated_at=None,
                    updated_at_raw=None,
                ),
            )

    class BodyAdapter:
        def fetch(self, _profile, entry):
            return ArticleDocument(
                title=entry.title,
                canonical_url=entry.canonical_url,
                body="Exact pending evidence. " + ("Retryable context. " * 40),
            )

    class RecoveringDraftProvider:
        attempts = 0

        def prepare(self, document):
            self.attempts += 1
            if self.attempts == 1:
                raise DraftPreparationError("fixture provider outage")
            return PreparedDraft(
                headline="恢复后的草稿",
                claims=(
                    PreparedClaim(
                        text="这是恢复后生成的事实。",
                        evidence="Exact pending evidence.",
                    ),
                ),
                route_identifier="deepseek:v4-pro",
                candidate_configuration_version="fixture-candidates.v1",
                routing_evaluation_version="fixture-evaluation.v1",
                routing_evaluation_cases_sha256="a" * 64,
                protocol_version="fixture-draft.v1",
                protocol_content_sha256="b" * 64,
                prompt_version="fixture-prompt.v1",
                model_id="deepseek-v4-pro",
                model_version="fixture-model-version",
                returned_model_id="deepseek-v4-pro",
                attempts=1,
                latency_ms=1,
                input_tokens=10,
                output_tokens=5,
            )

    provider = RecoveringDraftProvider()
    first = collect_source_profiles(
        m2_database_url,
        profiles=profiles,
        feed_adapter=OneArticleFeedAdapter(),
        article_adapter=BodyAdapter(),
        provider=provider,
        clock=FixedClock(),
        operation_key="m2-pending:first",
    )
    first_status = runner.invoke(
        app,
        ["operator", "source-status"],
        env={"AI_INTEL_DATABASE_URL": m2_database_url},
    )
    assert first_status.exit_code == 0, first_status.output
    first_by_host = {
        item["host"]: item for item in json.loads(first_status.output)["sources"]
    }
    first_operational = runner.invoke(
        app,
        ["operator", "status"],
        env={"AI_INTEL_DATABASE_URL": m2_database_url},
    )
    assert first_operational.exit_code == 0, first_operational.output
    first_operational_by_host = {
        item["host"]: item
        for item in json.loads(first_operational.output)["sources"]
    }

    second = collect_source_profiles(
        m2_database_url,
        profiles=profiles,
        feed_adapter=OneArticleFeedAdapter(),
        article_adapter=BodyAdapter(),
        provider=provider,
        clock=FixedClock(),
        operation_key="m2-pending:second",
    )
    second_status = runner.invoke(
        app,
        ["operator", "source-status"],
        env={"AI_INTEL_DATABASE_URL": m2_database_url},
    )
    assert second_status.exit_code == 0, second_status.output
    second_by_host = {
        item["host"]: item for item in json.loads(second_status.output)["sources"]
    }

    assert first.document_versions_created == 1
    assert first.drafts_created == 0
    assert first_by_host["huggingface.co"]["pending_drafts"] == 1
    assert first_operational_by_host["huggingface.co"]["health"] == "healthy"
    assert first_operational_by_host["huggingface.co"]["pending_drafts"] == 1
    assert second.candidates_processed == 0
    assert second.drafts_created == 1
    assert second_by_host["huggingface.co"]["pending_drafts"] == 0
    assert provider.attempts == 2


@pytest.mark.postgres
def test_incremental_batches_are_bounded_without_skipping_unseen_entries(
    m2_database_url: str,
) -> None:
    profiles = load_source_profiles()
    available_entries = 1
    article_urls: list[str] = []

    class GrowingFeedAdapter:
        def discover(self, profile):
            if profile.host != "techcrunch.com":
                return ()
            return tuple(
                FeedEntry(
                    title=f"Bounded article {index}",
                    canonical_url=(
                        f"https://techcrunch.com/2026/08/17/bounded-article-{index}/"
                    ),
                    summary="Discovery only.",
                    published_at=datetime(2026, 8, 17, index, 0, tzinfo=UTC),
                    published_at_raw=f"2026-08-17T{index:02d}:00:00Z",
                    updated_at=None,
                    updated_at_raw=None,
                )
                for index in range(available_entries)
            )

    class BodyAdapter:
        def fetch(self, _profile, entry):
            article_urls.append(entry.canonical_url)
            return ArticleDocument(
                title=entry.title,
                canonical_url=entry.canonical_url,
                body=f"Evidence for {entry.canonical_url}. Context " + ("detail " * 80),
            )

    class DraftProvider:
        def prepare(self, document):
            return _prepared_fixture_draft(document)

    first = collect_source_profiles(
        m2_database_url,
        profiles=profiles,
        feed_adapter=GrowingFeedAdapter(),
        article_adapter=BodyAdapter(),
        provider=DraftProvider(),
        clock=FixedClock(),
        operation_key="m2-bounded:first",
        backfill_limit=2,
    )
    available_entries = 6
    second = collect_source_profiles(
        m2_database_url,
        profiles=profiles,
        feed_adapter=GrowingFeedAdapter(),
        article_adapter=BodyAdapter(),
        provider=DraftProvider(),
        clock=FixedClock(),
        operation_key="m2-bounded:second",
        backfill_limit=2,
    )
    third = collect_source_profiles(
        m2_database_url,
        profiles=profiles,
        feed_adapter=GrowingFeedAdapter(),
        article_adapter=BodyAdapter(),
        provider=DraftProvider(),
        clock=FixedClock(),
        operation_key="m2-bounded:third",
        backfill_limit=2,
    )
    fourth = collect_source_profiles(
        m2_database_url,
        profiles=profiles,
        feed_adapter=GrowingFeedAdapter(),
        article_adapter=BodyAdapter(),
        provider=DraftProvider(),
        clock=FixedClock(),
        operation_key="m2-bounded:fourth",
        backfill_limit=2,
    )

    assert [
        first.candidates_processed,
        second.candidates_processed,
        third.candidates_processed,
        fourth.candidates_processed,
    ] == [1, 2, 2, 1]
    assert article_urls == [
        f"https://techcrunch.com/2026/08/17/bounded-article-{index}/"
        for index in range(6)
    ]


@pytest.mark.postgres
def test_qbit_alias_and_tracking_variations_share_one_canonical_candidate(
    m2_database_url: str,
) -> None:
    profiles = load_source_profiles()
    revision = 1

    class TrackingFeedAdapter:
        def discover(self, profile):
            if profile.host != "qbitai.com":
                return ()
            entry_host = "www.qbitai.com" if revision == 1 else "qbitai.com"
            return (
                FeedEntry(
                    title="Canonical tracking article",
                    canonical_url=(
                        f"https://{entry_host}/2026/08/400004.html"
                        f"?utm_source=revision-{revision}"
                    ),
                    summary="Discovery only.",
                    published_at=datetime(2026, 8, 17, 1, 0, tzinfo=UTC),
                    published_at_raw="2026-08-17T01:00:00Z",
                    updated_at=datetime(2026, 8, 17, revision, 0, tzinfo=UTC),
                    updated_at_raw=f"2026-08-17T{revision:02d}:00:00Z",
                ),
            )

    class BodyAdapter:
        def fetch(self, _profile, entry):
            return ArticleDocument(
                title=entry.title,
                canonical_url=entry.canonical_url,
                body="Canonical evidence. Context " + ("detail " * 80),
            )

    class DraftProvider:
        def prepare(self, document):
            return _prepared_fixture_draft(document)

    first = collect_source_profiles(
        m2_database_url,
        profiles=profiles,
        feed_adapter=TrackingFeedAdapter(),
        article_adapter=BodyAdapter(),
        provider=DraftProvider(),
        clock=FixedClock(),
        operation_key="m2-canonical:first",
    )
    revision = 2
    second = collect_source_profiles(
        m2_database_url,
        profiles=profiles,
        feed_adapter=TrackingFeedAdapter(),
        article_adapter=BodyAdapter(),
        provider=DraftProvider(),
        clock=FixedClock(),
        operation_key="m2-canonical:second",
    )

    assert first.document_versions_created == 1
    assert second.candidates_processed == 1
    assert second.document_versions_created == 0
    engine = create_database_engine(m2_database_url)
    try:
        with Session(engine) as session:
            candidate = session.scalar(select(CandidateRecord))
            document = session.scalar(select(DocumentVersionRecord))
            assert candidate is not None
            assert document is not None
            assert candidate.canonical_url == "https://qbitai.com/2026/08/400004.html"
            assert document.source_url == candidate.canonical_url
            assert session.scalar(select(func.count()).select_from(CandidateRecord)) == 1
            assert (
                session.scalar(select(func.count()).select_from(DocumentVersionRecord))
                == 1
            )
    finally:
        engine.dispose()


@pytest.mark.postgres
def test_invalid_feed_metadata_is_source_isolated_before_persistence(
    m2_database_url: str,
) -> None:
    profiles = load_source_profiles()

    class MixedFeedAdapter:
        def discover(self, profile):
            if profile.host == "the-decoder.com":
                return (
                    FeedEntry(
                        title="x" * 501,
                        canonical_url="https://the-decoder.com/overlong-title/",
                        summary="Discovery only.",
                        published_at=None,
                        published_at_raw=None,
                        updated_at=None,
                        updated_at_raw=None,
                    ),
                )
            if profile.host == "huggingface.co":
                return (
                    FeedEntry(
                        title="Valid bounded metadata",
                        canonical_url="https://huggingface.co/blog/valid-bounded-metadata",
                        summary="Discovery only.",
                        published_at=None,
                        published_at_raw=None,
                        updated_at=None,
                        updated_at_raw=None,
                    ),
                )
            return ()

    class BodyAdapter:
        def fetch(self, _profile, entry):
            return ArticleDocument(
                title=entry.title,
                canonical_url=entry.canonical_url,
                body="Bounded evidence. Context " + ("detail " * 80),
            )

    class DraftProvider:
        def prepare(self, document):
            return _prepared_fixture_draft(document)

    summary = collect_source_profiles(
        m2_database_url,
        profiles=profiles,
        feed_adapter=MixedFeedAdapter(),
        article_adapter=BodyAdapter(),
        provider=DraftProvider(),
        clock=FixedClock(),
        operation_key="m2-bounds:mixed",
    )
    incremental = collect_source_profiles(
        m2_database_url,
        profiles=profiles,
        feed_adapter=MixedFeedAdapter(),
        article_adapter=BodyAdapter(),
        provider=DraftProvider(),
        clock=FixedClock(),
        operation_key="m2-bounds:incremental",
    )

    assert summary.source_results["the-decoder.com"] == "invalid-format"
    assert summary.source_results["huggingface.co"] == "success"
    assert summary.document_versions_created == 1
    assert incremental.source_results["the-decoder.com"] == "empty"


@pytest.mark.postgres
def test_temporary_article_failure_retains_cursor_and_retries_the_entry(
    m2_database_url: str,
) -> None:
    profiles = load_source_profiles()
    article_attempts = 0

    class StableFeedAdapter:
        def discover(self, profile):
            if profile.host != "techcrunch.com":
                return ()
            return (
                FeedEntry(
                    title="Retry after rate limit",
                    canonical_url="https://techcrunch.com/2026/08/17/retry-after-limit/",
                    summary="Discovery only.",
                    published_at=datetime(2026, 8, 17, 4, 0, tzinfo=UTC),
                    published_at_raw="2026-08-17T04:00:00Z",
                    updated_at=None,
                    updated_at_raw=None,
                ),
            )

    class RecoveringArticleAdapter:
        def fetch(self, _profile, entry):
            nonlocal article_attempts
            article_attempts += 1
            if article_attempts == 1:
                raise ArticleTemporaryFailureError("fixture rate limit")
            return ArticleDocument(
                title=entry.title,
                canonical_url=entry.canonical_url,
                body="Retry evidence. Context " + ("detail " * 80),
            )

    class DraftProvider:
        def prepare(self, document):
            return _prepared_fixture_draft(document)

    first = collect_source_profiles(
        m2_database_url,
        profiles=profiles,
        feed_adapter=StableFeedAdapter(),
        article_adapter=RecoveringArticleAdapter(),
        provider=DraftProvider(),
        clock=FixedClock(),
        operation_key="m2-rate-limit:first",
    )
    second = collect_source_profiles(
        m2_database_url,
        profiles=profiles,
        feed_adapter=StableFeedAdapter(),
        article_adapter=RecoveringArticleAdapter(),
        provider=DraftProvider(),
        clock=FixedClock(),
        operation_key="m2-rate-limit:second",
    )

    assert first.source_results["techcrunch.com"] == "temporary-failure"
    assert second.source_results["techcrunch.com"] == "success"
    assert second.document_versions_created == 1
    assert article_attempts == 2


@pytest.mark.postgres
def test_terminal_http_404_is_consumed_before_a_later_article_progresses(
    m2_database_url: str,
) -> None:
    profiles = load_source_profiles()
    revision = 0

    class GrowingFeedAdapter:
        def discover(self, profile):
            if profile.host != "techcrunch.com":
                return ()
            slug = "missing-article" if revision == 0 else "later-article"
            return (
                FeedEntry(
                    title=slug.replace("-", " ").title(),
                    canonical_url=f"https://techcrunch.com/2026/08/17/{slug}/",
                    summary="Discovery only.",
                    published_at=datetime(2026, 8, 17, revision + 1, 0, tzinfo=UTC),
                    published_at_raw=f"2026-08-17T{revision + 1:02d}:00:00Z",
                    updated_at=None,
                    updated_at_raw=None,
                ),
            )

    class DraftProvider:
        def prepare(self, document):
            return _prepared_fixture_draft(document)

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/missing-article/"):
            return httpx.Response(404, request=request, text="missing")
        canonical_url = "https://techcrunch.com/2026/08/17/later-article/"
        html = (
            "<html><head><title>Later Article</title>"
            f'<link rel="canonical" href="{canonical_url}"></head>'
            "<body><article><p>"
            + ("Later body-valid article evidence and independent context. " * 100)
            + "</p></article></body></html>"
        )
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/html"},
            text=html,
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        article_adapter = HttpArticleAdapter(client, resolver=StaticResolver())
        first = collect_source_profiles(
            m2_database_url,
            profiles=profiles,
            feed_adapter=GrowingFeedAdapter(),
            article_adapter=article_adapter,
            provider=DraftProvider(),
            clock=FixedClock(),
            operation_key="m2-terminal-http:first",
            backfill_limit=1,
        )
        revision = 1
        second = collect_source_profiles(
            m2_database_url,
            profiles=profiles,
            feed_adapter=GrowingFeedAdapter(),
            article_adapter=article_adapter,
            provider=DraftProvider(),
            clock=FixedClock(),
            operation_key="m2-terminal-http:second",
            backfill_limit=1,
        )

    assert first.source_results["techcrunch.com"] == "invalid-format"
    assert second.source_results["techcrunch.com"] == "success"
    assert second.document_versions_created == 1


@pytest.mark.postgres
def test_m2_migration_downgrade_translates_persisted_result_statuses(
    m2_database_url: str,
) -> None:
    profiles = load_source_profiles()

    class EmptyFeedAdapter:
        def discover(self, _profile):
            return ()

    class UnusedArticleAdapter:
        def fetch(self, _profile, _entry):
            raise AssertionError("No article should be fetched")

    class UnusedDraftProvider:
        def prepare(self, _document):
            raise AssertionError("No draft should be prepared")

    collect_source_profiles(
        m2_database_url,
        profiles=profiles,
        feed_adapter=EmptyFeedAdapter(),
        article_adapter=UnusedArticleAdapter(),
        provider=UnusedDraftProvider(),
        clock=FixedClock(),
        operation_key="m2-downgrade:fixture",
    )

    project_root = Path(__file__).resolve().parents[1]
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option(
        "sqlalchemy.url",
        database_url_for_alembic_config(m2_database_url),
    )
    command.downgrade(config, "0005")
    engine = create_database_engine(m2_database_url)
    try:
        with Session(engine) as session:
            assert session.execute(select(CollectionRunRecord.status)).all()
            statuses = set(
                session.scalars(
                    select(SourceDefinitionCollectionResultRecord.status)
                )
            )
            assert statuses == {"succeeded"}
            assert session.scalar(select(func.count()).select_from(CandidateRecord)) == 0
            assert session.execute(
                select(func.to_regclass("source_candidate_results"))
            ).scalar_one() is None
    finally:
        engine.dispose()
        command.upgrade(config, "head")
