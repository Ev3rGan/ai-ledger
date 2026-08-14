from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from importlib.resources import files
from ipaddress import IPv4Address
from uuid import uuid4

import httpx
import pytest
from pg0 import Pg0
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import OperationalError, ProgrammingError
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
    FeedFormatError,
    FeedSecurityError,
    HostResolver,
    HttpFeedFetcher,
    SampleFeedFetcher,
    load_approved_feed_source_definitions,
    load_sample_feed_source_definitions,
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
        assert request.extensions["sni_hostname"] in {
            "blog.google",
            "feeds.example.com",
        }
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
    assert [request.headers["host"] for request in requests] == [
        "blog.google",
        "feeds.example.com",
    ]
    assert [request.url.host for request in requests] == [
        str(PUBLIC_ADDRESS),
        str(PUBLIC_ADDRESS),
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


@pytest.mark.parametrize(
    "payload",
    [
        b"<!--" + (b"padding" * 700) + b"--><!DOCTYPE rss><rss><channel /></rss>",
        (
            '<?xml version="1.0" encoding="utf-16"?>'
            "<!DOCTYPE rss><rss><channel /></rss>"
        ).encode("utf-16"),
    ],
    ids=("late-declaration", "utf-16"),
)
def test_parse_feed_rejects_dtd_regardless_of_position_or_encoding(
    payload: bytes,
) -> None:
    with pytest.raises(FeedFormatError, match="document type or entity"):
        parse_feed(payload)


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


def test_approved_feed_source_definition_retains_its_audited_operating_policy() -> None:
    google_ai = next(
        definition
        for definition in load_approved_feed_source_definitions()
        if definition.name == "Google AI"
    )

    assert google_ai.collection_schedule == "06:00 and 18:00 Asia/Shanghai"
    assert google_ai.discovery_method.startswith("Official RSS filtered to")
    assert google_ai.language == "English with localized variants possible"
    assert google_ai.topic_scope
    assert google_ai.access_constraints
    assert "AI-section filter" in google_ai.extraction_adapter
    assert google_ai.health_policy
    assert google_ai.cursor
    assert google_ai.public_excerpt_max_characters == 280
    assert google_ai.pause_conditions
    assert google_ai.canonical_url_prefixes == (
        "https://blog.google/innovation-and-ai/technology/ai/",
    )


@pytest.mark.postgres
def test_malformed_entry_is_isolated_to_its_source_definition(
    collection_database_url: str,
) -> None:
    google_ai, hugging_face, _github = load_sample_feed_source_definitions()
    malformed_google_feed = b"""\
        <rss version="2.0">
          <channel>
            <item>
              <title>Malformed port</title>
              <link>https://blog.google:bad/innovation-and-ai/technology/ai/x</link>
              <description>This Source Definition must fail in isolation.</description>
            </item>
          </channel>
        </rss>
    """
    valid_hugging_face_feed = FIXTURES.joinpath("hugging-face.atom").read_bytes()

    class FixtureFetcher:
        def fetch(self, source_definition: ApprovedFeedSourceDefinition) -> bytes:
            if source_definition.id == google_ai.id:
                return malformed_google_feed
            return valid_hugging_face_feed

    run = collect_feed_source_definitions(
        collection_database_url,
        source_definitions=(google_ai, hugging_face),
        fetcher=FixtureFetcher(),
        clock=FixedClock(),
    )

    engine = create_database_engine(collection_database_url)
    try:
        with Session(engine) as session:
            persisted_status = session.scalar(
                select(CollectionRunRecord.status).where(CollectionRunRecord.id == run.id)
            )
            persisted_results = session.execute(
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
                    SourceDefinitionCollectionResultRecord.collection_run_id == run.id
                )
                .order_by(SourceDefinitionRecord.name)
            ).all()
    finally:
        engine.dispose()

    assert persisted_status == "partial"
    assert persisted_results == [
        ("Google AI", "failed", "invalid_feed"),
        ("Hugging Face Blog", "succeeded", None),
    ]


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
def test_child_insert_locks_running_run_until_the_child_commits(
    collection_database_url: str,
) -> None:
    source_definitions = load_sample_feed_source_definitions()
    collect_feed_source_definitions(
        collection_database_url,
        source_definitions=source_definitions,
        fetcher=SampleFeedFetcher(),
        clock=FixedClock(),
    )
    running_run_id = uuid4()
    google_ai = source_definitions[0]
    engine = create_database_engine(collection_database_url)
    child_connection = engine.connect()
    child_transaction = None
    try:
        with engine.begin() as connection:
            connection.execute(
                insert(CollectionRunRecord).values(
                    id=running_run_id,
                    retry_of_run_id=None,
                    status="running",
                    started_at=FIXED_NOW,
                    completed_at=None,
                )
            )

        child_transaction = child_connection.begin()
        child_connection.execute(
            insert(SourceDefinitionCollectionResultRecord).values(
                collection_run_id=running_run_id,
                source_definition_id=google_ai.id,
                status="succeeded",
                candidate_count=0,
                error_code=None,
                error_message=None,
            )
        )

        completion_was_blocked = False
        with engine.connect() as completion_connection:
            completion_transaction = completion_connection.begin()
            try:
                completion_connection.execute(text("SET LOCAL lock_timeout = '250ms'"))
                completion_connection.execute(
                    update(CollectionRunRecord)
                    .where(CollectionRunRecord.id == running_run_id)
                    .values(status="complete", completed_at=FIXED_NOW)
                )
            except OperationalError as error:
                assert getattr(error.orig, "sqlstate", None) == "55P03"
                completion_was_blocked = True
            finally:
                completion_transaction.rollback()

        child_transaction.commit()
        child_transaction = None
        assert completion_was_blocked

        with engine.begin() as connection:
            connection.execute(
                update(CollectionRunRecord)
                .where(CollectionRunRecord.id == running_run_id)
                .values(status="complete", completed_at=FIXED_NOW)
            )
    finally:
        if child_transaction is not None:
            child_transaction.rollback()
        child_connection.close()
        engine.dispose()


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
            google_source_definition_id = session.scalar(
                select(SourceDefinitionRecord.id).where(
                    SourceDefinitionRecord.name == "Google AI"
                )
            )
            google_operating_policy = session.execute(
                select(
                    SourceDefinitionRecord.collection_schedule,
                    SourceDefinitionRecord.discovery_method,
                    SourceDefinitionRecord.topic_scope,
                    SourceDefinitionRecord.canonical_url_prefixes,
                ).where(SourceDefinitionRecord.name == "Google AI")
            ).one()
            document_identities = {
                canonical_url: (candidate_id, document_version_id)
                for canonical_url, candidate_id, document_version_id in session.execute(
                    select(
                        CandidateRecord.canonical_url,
                        CandidateRecord.id,
                        DocumentVersionRecord.id,
                    ).join(
                        DocumentVersionRecord,
                        DocumentVersionRecord.candidate_id == CandidateRecord.id,
                    )
                )
            }
    finally:
        engine.dispose()

    assert first_run_id is not None
    assert google_source_definition_id is not None
    assert google_operating_policy == (
        "06:00 and 18:00 Asia/Shanghai",
        (
            "Official RSS filtered to "
            "https://blog.google/innovation-and-ai/technology/ai/."
        ),
        [
            "Models",
            "Research",
            "Products and Tools",
            "Applications",
            "Policy and Safety",
        ],
        ["https://blog.google/innovation-and-ai/technology/ai/"],
    )
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
            "https://blog.google/innovation-and-ai/technology/ai/gemini-agent-traces/",
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

        hugging_face_candidate_id, hugging_face_document_version_id = (
            document_identities["https://example.com/ai/open-model-evaluation"]
        )
        with Session(immutability_engine) as session, pytest.raises(
            ProgrammingError,
            match="Collection Run result is immutable",
        ):
            session.add(
                SourceDefinitionCollectionResultRecord(
                    collection_run_id=first_run_id,
                    source_definition_id=google_source_definition_id,
                    status="succeeded",
                    candidate_count=0,
                    error_code=None,
                    error_message=None,
                )
            )
            session.flush()

        with Session(immutability_engine) as session, pytest.raises(
            ProgrammingError,
            match="Collection Run discovery is immutable",
        ):
            session.add(
                CollectionDiscoveryRecord(
                    collection_run_id=first_run_id,
                    source_definition_id=google_source_definition_id,
                    candidate_id=hugging_face_candidate_id,
                    document_version_id=hugging_face_document_version_id,
                )
            )
            session.flush()

        with Session(immutability_engine) as session, pytest.raises(
            ProgrammingError,
            match="Document Version is immutable",
        ):
            session.execute(
                update(DocumentVersionRecord)
                .where(DocumentVersionRecord.id == hugging_face_document_version_id)
                .values(title="silently rewritten")
            )
    finally:
        immutability_engine.dispose()
