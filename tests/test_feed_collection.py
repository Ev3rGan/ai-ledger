from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pg0 import Pg0
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ai_intel_agent.collection import (
    ApprovedFeedSourceDefinition,
    CollectionRunStatus,
    FeedFetcher,
    FeedFetchError,
    HttpFeedFetcher,
    SourceDefinitionApprovalError,
    collect_feed_sources,
    load_approved_feed_source_definitions,
)
from ai_intel_agent.persistence import (
    CandidateRecord,
    CollectionDiscoveryRecord,
    CollectionRunRecord,
    CollectionSourceResultRecord,
    DocumentVersionRecord,
    SourceDefinitionRecord,
    create_database_engine,
    upgrade_database,
)

FIXTURES = Path(__file__).parent / "fixtures" / "feeds"
FIXED_NOW = datetime.fromisoformat("2026-08-12T10:00:00+08:00")


@dataclass(frozen=True)
class FixedClock:
    current: datetime = FIXED_NOW

    def now(self) -> datetime:
        return self.current


class FixtureFeedFetcher(FeedFetcher):
    def __init__(self, fixtures_by_url: dict[str, Path]) -> None:
        self._fixtures_by_url = fixtures_by_url

    def fetch(self, source: ApprovedFeedSourceDefinition) -> bytes:
        return self._fixtures_by_url[source.entry_point].read_bytes()


class UnexpectedFeedFetcher(FeedFetcher):
    def fetch(self, source: ApprovedFeedSourceDefinition) -> bytes:
        raise AssertionError(f"Unapproved source was fetched: {source.entry_point}")


def test_http_feed_fetcher_turns_http_failure_into_a_bounded_adapter_error() -> None:
    source = load_approved_feed_source_definitions()[0]
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(503, request=request, text="private upstream body")
        )
    )
    try:
        fetcher = HttpFeedFetcher(client)
        with pytest.raises(FeedFetchError, match="HTTP 503") as caught:
            fetcher.fetch(source)
    finally:
        client.close()

    assert "private upstream body" not in str(caught.value)


def test_collection_rejects_a_source_that_does_not_match_the_approved_audit() -> None:
    approved = load_approved_feed_source_definitions()[0]
    unapproved = replace(
        approved,
        entry_point="https://developer.nvidia.com/blog/tag/generative-ai/feed/",
    )

    with pytest.raises(SourceDefinitionApprovalError, match="not approved"):
        collect_feed_sources(
            "database-must-not-be-opened",
            sources=(unapproved,),
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
def test_collection_persists_rss_and_atom_while_feed_failure_stays_partial_and_retryable(
    collection_database_url: str,
) -> None:
    approved = {
        definition.name: definition
        for definition in load_approved_feed_source_definitions()
    }
    sources = (
        approved["Google AI"],
        approved["Hugging Face Blog"],
        approved["GitHub AI and ML"],
    )
    fetcher = FixtureFeedFetcher(
        {
            sources[0].entry_point: FIXTURES / "google-ai.rss",
            sources[1].entry_point: FIXTURES / "hugging-face.atom",
            sources[2].entry_point: FIXTURES / "malformed.xml",
        }
    )

    first = collect_feed_sources(
        collection_database_url,
        sources=sources,
        fetcher=fetcher,
        clock=FixedClock(),
    )
    retry = collect_feed_sources(
        collection_database_url,
        sources=sources,
        fetcher=fetcher,
        clock=FixedClock(),
        retry_of_run_id=first.id,
    )

    assert first.status is CollectionRunStatus.PARTIAL
    assert retry.status is CollectionRunStatus.PARTIAL
    assert retry.retry_of_run_id == first.id
    assert [result.status.value for result in first.source_results] == [
        "succeeded",
        "succeeded",
        "failed",
    ]
    assert first.source_results[-1].error_code == "invalid_feed"

    engine = create_database_engine(collection_database_url)
    try:
        with Session(engine) as session:
            counts = {
                record_type.__tablename__: session.scalar(
                    select(func.count()).select_from(record_type)
                )
                for record_type in (
                    SourceDefinitionRecord,
                    CollectionRunRecord,
                    CollectionSourceResultRecord,
                    CollectionDiscoveryRecord,
                    CandidateRecord,
                    DocumentVersionRecord,
                )
            }
            persisted_documents = session.execute(
                select(
                    CandidateRecord.canonical_url,
                    DocumentVersionRecord.title,
                    DocumentVersionRecord.body,
                ).join(
                    DocumentVersionRecord,
                    DocumentVersionRecord.candidate_id == CandidateRecord.id,
                )
            ).all()
    finally:
        engine.dispose()

    assert counts == {
        "source_definitions": 3,
        "collection_runs": 2,
        "collection_source_results": 6,
        "collection_discoveries": 4,
        "candidates": 2,
        "document_versions": 2,
    }
    assert set(persisted_documents) == {
        (
            "https://example.com/ai/gemini-agent-traces",
            "Gemini agents add reproducible task traces",
            "Gemini agents now attach reproducible task traces to results.",
        ),
        (
            "https://example.com/ai/open-model-evaluation",
            "Open model evaluation gains evidence links",
            "The evaluation report links every score to its source evidence.",
        ),
    }
