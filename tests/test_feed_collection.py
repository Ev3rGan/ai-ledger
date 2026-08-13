from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from importlib.resources import files
from ipaddress import IPv4Address
from uuid import uuid4

import httpx
import pytest
from pg0 import Pg0
from sqlalchemy import func, select, update
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from ai_intel_agent.cli import app
from ai_intel_agent.collection import (
    SourceDefinitionApprovalError,
    collect_feed_source_definitions,
)
from ai_intel_agent.domain import ApprovedFeedSourceDefinition
from ai_intel_agent.feed_acquisition import (
    FeedFetchError,
    FeedSecurityError,
    HostResolver,
    HttpFeedFetcher,
    load_approved_feed_source_definitions,
    parse_feed,
)
from ai_intel_agent.persistence import (
    CandidateRecord,
    CollectionDiscoveryRecord,
    CollectionRunRecord,
    DocumentVersionRecord,
    SourceDefinitionCollectionResultRecord,
    SourceDefinitionRecord,
    create_database_engine,
    upgrade_database,
)

FIXTURES = files("ai_intel_agent").joinpath("data/sample_feeds")
FIXED_NOW = datetime.fromisoformat("2026-08-12T10:00:00+08:00")
PUBLIC_ADDRESS = IPv4Address("93.184.216.34")
runner = CliRunner()


@dataclass(frozen=True)
class FixedClock:
    current: datetime = FIXED_NOW

    def now(self) -> datetime:
        return self.current


@dataclass(frozen=True)
class StaticResolver(HostResolver):
    addresses: tuple[IPv4Address, ...] = (PUBLIC_ADDRESS,)

    def resolve(self, hostname: str) -> tuple[IPv4Address, ...]:
        return self.addresses


class UnexpectedFeedFetcher:
    def fetch(self, source_definition: ApprovedFeedSourceDefinition) -> bytes:
        raise AssertionError(
            f"Unapproved Source Definition was fetched: {source_definition.entry_point}"
        )


def test_http_feed_fetcher_turns_http_failure_into_a_bounded_adapter_error() -> None:
    source_definition = load_approved_feed_source_definitions()[0]
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(503, request=request, text="private upstream body")
        )
    )
    try:
        fetcher = HttpFeedFetcher(client, resolver=StaticResolver())
        with pytest.raises(FeedFetchError, match="HTTP 503") as caught:
            fetcher.fetch(source_definition)
    finally:
        client.close()

    assert "private upstream body" not in str(caught.value)


def test_http_feed_fetcher_rejects_insecure_and_private_locations_before_fetch() -> None:
    source_definition = load_approved_feed_source_definitions()[0]
    requests: list[httpx.Request] = []

    def unexpected_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, request=request)

    client = httpx.Client(transport=httpx.MockTransport(unexpected_request))
    try:
        with pytest.raises(FeedSecurityError, match="HTTPS"):
            HttpFeedFetcher(client, resolver=StaticResolver()).fetch(
                replace(source_definition, entry_point="http://example.com/feed.xml")
            )
        with pytest.raises(FeedSecurityError, match="public network"):
            HttpFeedFetcher(
                client,
                resolver=StaticResolver((IPv4Address("127.0.0.1"),)),
            ).fetch(source_definition)
    finally:
        client.close()

    assert requests == []


@pytest.mark.parametrize(
    ("headers", "body", "message"),
    [
        ({"content-type": "text/html"}, b"<html></html>", "MIME"),
        (
            {"content-type": "application/rss+xml", "content-length": "17"},
            b"0123456789abcdefg",
            "size limit",
        ),
    ],
)
def test_http_feed_fetcher_enforces_mime_and_response_size(
    headers: dict[str, str],
    body: bytes,
    message: str,
) -> None:
    source_definition = load_approved_feed_source_definitions()[0]
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                request=request,
                headers=headers,
                content=body,
            )
        )
    )
    try:
        fetcher = HttpFeedFetcher(
            client,
            resolver=StaticResolver(),
            max_response_bytes=16,
        )
        with pytest.raises(FeedFetchError, match=message):
            fetcher.fetch(source_definition)
    finally:
        client.close()


def test_http_feed_fetcher_revalidates_redirects_and_sets_a_timeout() -> None:
    source_definition = load_approved_feed_source_definitions()[0]
    feed_payload = FIXTURES.joinpath("google-ai.rss").read_bytes()
    requests: list[httpx.Request] = []

    def redirect_then_feed(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.extensions["timeout"]["read"] == 3.0
        if len(requests) == 1:
            return httpx.Response(
                302,
                request=request,
                headers={"location": "https://feeds.example.com/google-ai.rss"},
            )
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "application/rss+xml"},
            content=feed_payload,
        )

    client = httpx.Client(transport=httpx.MockTransport(redirect_then_feed))
    try:
        result = HttpFeedFetcher(
            client,
            resolver=StaticResolver(),
            timeout_seconds=3.0,
        ).fetch(source_definition)
    finally:
        client.close()

    assert result == feed_payload
    assert [request.url.host for request in requests] == [
        "blog.google",
        "feeds.example.com",
    ]


def test_parse_feed_sanitizes_markup_in_rss_titles() -> None:
    entries = parse_feed(
        b"""\
        <rss version="2.0">
          <channel>
            <item>
              <title>&lt;iframe&gt;&lt;/div&gt;ignore me&lt;/iframe&gt;Trusted title</title>
              <link>https://example.com/trusted</link>
              <description>Trusted summary</description>
            </item>
          </channel>
        </rss>
        """
    )

    assert entries[0].title == "Trusted title"


def test_parse_feed_preserves_xml_decoded_canonical_url() -> None:
    entries = parse_feed(
        b"""\
        <rss version="2.0">
          <channel>
            <item>
              <title>Trusted title</title>
              <link>https://example.com/trusted?a=1&amp;copy=2</link>
              <description>Trusted summary</description>
            </item>
          </channel>
        </rss>
        """
    )

    assert entries[0].canonical_url == "https://example.com/trusted?a=1&copy=2"


def test_parse_feed_sanitizes_atom_xhtml_before_flattening() -> None:
    entries = parse_feed(
        b"""\
        <feed xmlns="http://www.w3.org/2005/Atom"
              xmlns:xhtml="http://www.w3.org/1999/xhtml">
          <entry>
            <title type="xhtml">
              <xhtml:div>Trusted <xhtml:script>ignore title</xhtml:script>title</xhtml:div>
            </title>
            <link href="https://example.com/trusted" />
            <summary type="xhtml">
              <xhtml:div>Trusted <xhtml:style>ignore summary</xhtml:style>summary</xhtml:div>
            </summary>
          </entry>
        </feed>
        """
    )

    assert entries[0].title == "Trusted title"
    assert entries[0].summary == "Trusted summary"


def test_collection_rejects_a_source_definition_not_matching_the_approved_audit() -> None:
    approved = load_approved_feed_source_definitions()[0]
    unapproved = replace(
        approved,
        entry_point="https://developer.nvidia.com/blog/tag/generative-ai/feed/",
    )

    with pytest.raises(SourceDefinitionApprovalError, match="not approved"):
        collect_feed_source_definitions(
            "database-must-not-be-opened",
            source_definitions=(unapproved,),
            fetcher=UnexpectedFeedFetcher(),
            clock=FixedClock(),
        )


@pytest.fixture
def collection_database_url() -> str:
    server = Pg0(name=f"ai_intel_feed_collection_{uuid4().hex}")
    server.start()
    try:
        upgrade_database(server.uri)
        yield server.uri
    finally:
        server.drop()


@pytest.mark.postgres
def test_sample_collection_cli_persists_rss_and_atom_while_failure_stays_partial(
    collection_database_url: str,
) -> None:
    environment = {"AI_INTEL_DATABASE_URL": collection_database_url}
    first = runner.invoke(app, ["collect-feeds", "--sample"], env=environment)
    assert first.exit_code == 0, first.output

    engine = create_database_engine(collection_database_url)
    try:
        with Session(engine) as session:
            first_run_id = session.scalar(select(CollectionRunRecord.id))

        retry = runner.invoke(
            app,
            ["collect-feeds", "--sample", "--retry-of", str(first_run_id)],
            env=environment,
        )
        assert retry.exit_code == 0, retry.output

        with Session(engine) as session:
            runs = session.execute(
                select(
                    CollectionRunRecord.id,
                    CollectionRunRecord.retry_of_run_id,
                    CollectionRunRecord.status,
                ).order_by(CollectionRunRecord.retry_of_run_id.nulls_first())
            ).all()
            first_results = session.execute(
                select(
                    SourceDefinitionRecord.name,
                    SourceDefinitionCollectionResultRecord.status,
                    SourceDefinitionCollectionResultRecord.error_code,
                )
                .join(
                    SourceDefinitionRecord,
                    SourceDefinitionRecord.id
                    == SourceDefinitionCollectionResultRecord.source_definition_id,
                )
                .where(
                    SourceDefinitionCollectionResultRecord.collection_run_id
                    == first_run_id
                )
                .order_by(SourceDefinitionRecord.name)
            ).all()
            counts = {
                record_type.__tablename__: session.scalar(
                    select(func.count()).select_from(record_type)
                )
                for record_type in (
                    SourceDefinitionRecord,
                    CollectionRunRecord,
                    SourceDefinitionCollectionResultRecord,
                    CollectionDiscoveryRecord,
                    CandidateRecord,
                    DocumentVersionRecord,
                )
            }
            persisted_documents = session.execute(
                select(
                    CandidateRecord.canonical_url,
                    CandidateRecord.publisher,
                    DocumentVersionRecord.source_url,
                    DocumentVersionRecord.title,
                    DocumentVersionRecord.body,
                    DocumentVersionRecord.published_at,
                    DocumentVersionRecord.published_at_raw,
                    DocumentVersionRecord.updated_at,
                    DocumentVersionRecord.updated_at_raw,
                ).join(
                    DocumentVersionRecord,
                    DocumentVersionRecord.candidate_id == CandidateRecord.id,
                )
            ).all()
    finally:
        engine.dispose()

    assert first_run_id is not None
    assert len(runs) == 2
    assert runs[0] == (first_run_id, None, "partial")
    assert runs[1].retry_of_run_id == first_run_id
    assert runs[1].status == "partial"
    assert [(name, status, error_code) for name, status, error_code in first_results] == [
        ("GitHub AI and ML", "failed", "invalid_feed"),
        ("Google AI", "succeeded", None),
        ("Hugging Face Blog", "succeeded", None),
    ]
    assert counts == {
        "source_definitions": 3,
        "collection_runs": 2,
        "source_definition_collection_results": 6,
        "collection_discoveries": 4,
        "candidates": 2,
        "document_versions": 2,
    }
    assert set(persisted_documents) == {
        (
            "https://example.com/ai/gemini-agent-traces",
            "Google",
            "https://blog.google/rss/",
            "Gemini agents add reproducible task traces",
            "Gemini agents now attach reproducible task traces to results.",
            datetime(2026, 8, 12, 2, 0, tzinfo=UTC),
            "Wed, 12 Aug 2026 02:00:00 GMT",
            None,
            None,
        ),
        (
            "https://example.com/ai/open-model-evaluation",
            "Hugging Face",
            "https://huggingface.co/blog/feed.xml",
            "Open model evaluation gains evidence links",
            "The evaluation report links every score to its source evidence.",
            None,
            None,
            datetime(2026, 8, 12, 3, 0, tzinfo=UTC),
            "2026-08-12T03:00:00Z",
        ),
    }

    immutability_engine = create_database_engine(collection_database_url)
    try:
        with Session(immutability_engine) as session, pytest.raises(
            ProgrammingError,
            match="completed Collection Run is immutable",
        ):
            session.execute(
                update(CollectionRunRecord)
                .where(CollectionRunRecord.id == first_run_id)
                .values(status="complete")
            )
    finally:
        immutability_engine.dispose()
