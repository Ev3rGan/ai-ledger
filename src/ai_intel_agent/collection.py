from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5
from xml.etree import ElementTree

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ai_intel_agent.domain import Candidate, DocumentVersion
from ai_intel_agent.persistence import (
    CandidateRecord,
    CollectionDiscoveryRecord,
    CollectionRunRecord,
    CollectionSourceResultRecord,
    DocumentVersionRecord,
    SourceDefinitionRecord,
    create_database_engine,
)
from ai_intel_agent.source_audit import (
    AuditConclusion,
    load_first_wave_source_definition_audit,
)


class CollectionRunStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


class CollectionSourceStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class ApprovedFeedSourceDefinition:
    id: UUID
    name: str
    entry_point: str
    audit_version: str
    storage_policy: str


@dataclass(frozen=True)
class CollectionSourceResult:
    source_definition_id: UUID
    status: CollectionSourceStatus
    candidate_count: int
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class CollectionRun:
    id: UUID
    retry_of_run_id: UUID | None
    status: CollectionRunStatus
    started_at: datetime
    completed_at: datetime
    source_results: tuple[CollectionSourceResult, ...]


@dataclass(frozen=True)
class FeedEntry:
    title: str
    canonical_url: str
    summary: str


@dataclass(frozen=True)
class FeedDiscovery:
    source_definition_id: UUID
    candidate: Candidate
    document_version: DocumentVersion


class Clock(Protocol):
    def now(self) -> datetime: ...


class FeedFetcher(Protocol):
    def fetch(self, source: ApprovedFeedSourceDefinition) -> bytes: ...


class FeedCollectionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class FeedFormatError(FeedCollectionError):
    def __init__(self, message: str) -> None:
        super().__init__("invalid_feed", message)


class FeedFetchError(FeedCollectionError):
    def __init__(self, message: str) -> None:
        super().__init__("fetch_failed", message)


class SourceDefinitionApprovalError(ValueError):
    pass


class HttpFeedFetcher:
    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def fetch(self, source: ApprovedFeedSourceDefinition) -> bytes:
        try:
            response = self._client.get(source.entry_point)
        except httpx.HTTPError as error:
            raise FeedFetchError("Feed request failed") from error
        if not response.is_success:
            raise FeedFetchError(f"Feed request returned HTTP {response.status_code}")
        return response.content


class _SummaryTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def text(self) -> str:
        return " ".join(" ".join(self._parts).split())


def load_approved_feed_source_definitions() -> tuple[ApprovedFeedSourceDefinition, ...]:
    audit = load_first_wave_source_definition_audit()
    definitions: list[ApprovedFeedSourceDefinition] = []
    for source in audit.source_definitions:
        adapter = source.extraction_adapter.casefold()
        if source.conclusion is not AuditConclusion.APPROVED:
            continue
        if "rss" not in adapter or "atom" not in adapter:
            continue
        definitions.append(
            ApprovedFeedSourceDefinition(
                id=uuid5(
                    NAMESPACE_URL,
                    f"ai-intel-agent:source-definition:{audit.version}:"
                    f"{source.name}:{source.entry_point}",
                ),
                name=source.name,
                entry_point=source.entry_point,
                audit_version=audit.version,
                storage_policy=source.storage_policy,
            )
        )
    return tuple(definitions)


def collect_feed_sources(
    database_url: str,
    *,
    sources: tuple[ApprovedFeedSourceDefinition, ...],
    fetcher: FeedFetcher,
    clock: Clock,
    retry_of_run_id: UUID | None = None,
) -> CollectionRun:
    if not sources:
        raise ValueError("A Collection Run requires at least one Feed Source Definition")
    approved_sources = set(load_approved_feed_source_definitions())
    unapproved_sources = [source for source in sources if source not in approved_sources]
    if unapproved_sources:
        names = ", ".join(source.name for source in unapproved_sources)
        raise SourceDefinitionApprovalError(
            f"Feed Source Definitions are not approved by the current audit: {names}"
        )
    if len({source.id for source in sources}) != len(sources):
        raise ValueError("A Collection Run cannot contain duplicate Source Definitions")

    started_at = clock.now()
    results: list[CollectionSourceResult] = []
    discoveries: list[FeedDiscovery] = []
    for source in sources:
        try:
            entries = parse_feed(fetcher.fetch(source))
        except FeedCollectionError as error:
            results.append(
                CollectionSourceResult(
                    source_definition_id=source.id,
                    status=CollectionSourceStatus.FAILED,
                    candidate_count=0,
                    error_code=error.code,
                    error_message=str(error),
                )
            )
            continue

        source_discoveries = _build_discoveries(source, entries, clock.now())
        discoveries.extend(source_discoveries)
        results.append(
            CollectionSourceResult(
                source_definition_id=source.id,
                status=CollectionSourceStatus.SUCCEEDED,
                candidate_count=len(source_discoveries),
            )
        )

    succeeded = sum(result.status is CollectionSourceStatus.SUCCEEDED for result in results)
    failed = len(results) - succeeded
    status = (
        CollectionRunStatus.COMPLETE
        if failed == 0
        else CollectionRunStatus.FAILED
        if succeeded == 0
        else CollectionRunStatus.PARTIAL
    )
    run = CollectionRun(
        id=uuid4(),
        retry_of_run_id=retry_of_run_id,
        status=status,
        started_at=started_at,
        completed_at=clock.now(),
        source_results=tuple(results),
    )

    engine = create_database_engine(database_url)
    try:
        FeedCollectionRepository(engine).persist(run, sources, tuple(discoveries))
    finally:
        engine.dispose()
    return run


def parse_feed(payload: bytes) -> tuple[FeedEntry, ...]:
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as error:
        raise FeedFormatError(f"Feed XML is malformed: {error}") from error

    root_name = _local_name(root.tag)
    if root_name == "rss":
        return _parse_rss(root)
    if root_name == "feed":
        return _parse_atom(root)
    raise FeedFormatError(f"Unsupported Feed root element: {root_name}")


def _parse_rss(root: ElementTree.Element) -> tuple[FeedEntry, ...]:
    channel = _first_child(root, "channel")
    if channel is None:
        raise FeedFormatError("RSS Feed has no channel")
    return tuple(
        _feed_entry(
            title=_child_text(item, "title"),
            canonical_url=_child_text(item, "link"),
            summary=_child_text(item, "description") or _child_text(item, "encoded"),
        )
        for item in _children(channel, "item")
    )


def _parse_atom(root: ElementTree.Element) -> tuple[FeedEntry, ...]:
    entries: list[FeedEntry] = []
    for item in _children(root, "entry"):
        link = next(
            (
                element.attrib.get("href", "").strip()
                for element in _children(item, "link")
                if element.attrib.get("rel", "alternate") in ("", "alternate")
                and element.attrib.get("href", "").strip()
            ),
            "",
        )
        entries.append(
            _feed_entry(
                title=_child_text(item, "title"),
                canonical_url=link,
                summary=_child_text(item, "summary") or _child_text(item, "content"),
            )
        )
    return tuple(entries)


def _feed_entry(*, title: str, canonical_url: str, summary: str) -> FeedEntry:
    if not title:
        raise FeedFormatError("Feed entry has no title")
    parsed_url = urlparse(canonical_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise FeedFormatError("Feed entry has no canonical HTTP URL")
    return FeedEntry(
        title=" ".join(title.split()),
        canonical_url=canonical_url,
        summary=_plain_text(summary),
    )


def _plain_text(value: str) -> str:
    extractor = _SummaryTextExtractor()
    extractor.feed(value)
    extractor.close()
    return extractor.text()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(element: ElementTree.Element, name: str) -> tuple[ElementTree.Element, ...]:
    return tuple(child for child in element if _local_name(child.tag) == name)


def _first_child(
    element: ElementTree.Element,
    name: str,
) -> ElementTree.Element | None:
    return next(iter(_children(element, name)), None)


def _child_text(element: ElementTree.Element, name: str) -> str:
    child = _first_child(element, name)
    if child is None:
        return ""
    return "".join(child.itertext()).strip()


def _build_discoveries(
    source: ApprovedFeedSourceDefinition,
    entries: tuple[FeedEntry, ...],
    observed_at: datetime,
) -> tuple[FeedDiscovery, ...]:
    discoveries: dict[tuple[UUID, UUID], FeedDiscovery] = {}
    for entry in entries:
        candidate_id = uuid5(
            NAMESPACE_URL,
            f"ai-intel-agent:feed-candidate:{entry.canonical_url}",
        )
        version_payload = json.dumps(
            {
                "body": entry.summary,
                "title": entry.title,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        content_hash = sha256(version_payload.encode("utf-8")).hexdigest()
        document_version_id = uuid5(
            NAMESPACE_URL,
            f"ai-intel-agent:feed-document-version:{candidate_id}:{content_hash}",
        )
        discovery = FeedDiscovery(
            source_definition_id=source.id,
            candidate=Candidate(
                id=candidate_id,
                title=entry.title,
                canonical_url=entry.canonical_url,
                publisher=source.name,
                discovered_at=observed_at,
            ),
            document_version=DocumentVersion(
                id=document_version_id,
                candidate_id=candidate_id,
                source_url=entry.canonical_url,
                title=entry.title,
                body=entry.summary,
                content_hash=content_hash,
                observed_at=observed_at,
            ),
        )
        discoveries[(candidate_id, document_version_id)] = discovery
    return tuple(discoveries.values())


class FeedCollectionRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def persist(
        self,
        run: CollectionRun,
        sources: tuple[ApprovedFeedSourceDefinition, ...],
        discoveries: tuple[FeedDiscovery, ...],
    ) -> None:
        sources_by_id = {source.id: source for source in sources}
        discoveries_by_source: dict[UUID, list[FeedDiscovery]] = {
            source.id: [] for source in sources
        }
        for discovery in discoveries:
            discoveries_by_source[discovery.source_definition_id].append(discovery)

        with Session(self._engine) as session, session.begin():
            if run.retry_of_run_id is not None and session.get(
                CollectionRunRecord, run.retry_of_run_id
            ) is None:
                raise ValueError(f"Retry parent Collection Run {run.retry_of_run_id} does not exist")

            for source in sources:
                session.execute(
                    insert(SourceDefinitionRecord)
                    .values(
                        id=source.id,
                        name=source.name,
                        entry_point=source.entry_point,
                        audit_version=source.audit_version,
                        activation_conclusion=AuditConclusion.APPROVED.value,
                        storage_policy=source.storage_policy,
                    )
                    .on_conflict_do_nothing()
                )

            session.add(
                CollectionRunRecord(
                    id=run.id,
                    retry_of_run_id=run.retry_of_run_id,
                    status=run.status.value,
                    started_at=run.started_at,
                    completed_at=run.completed_at,
                )
            )
            for result in run.source_results:
                source = sources_by_id[result.source_definition_id]
                session.add(
                    CollectionSourceResultRecord(
                        collection_run_id=run.id,
                        source_definition_id=source.id,
                        status=result.status.value,
                        candidate_count=result.candidate_count,
                        error_code=result.error_code,
                        error_message=result.error_message,
                    )
                )
                for discovery in discoveries_by_source[source.id]:
                    self._persist_discovery(session, run.id, discovery)

    @staticmethod
    def _persist_discovery(
        session: Session,
        collection_run_id: UUID,
        discovery: FeedDiscovery,
    ) -> None:
        candidate = discovery.candidate
        document = discovery.document_version
        session.execute(
            insert(CandidateRecord)
            .values(**candidate.__dict__)
            .on_conflict_do_nothing()
        )
        persisted_candidate_id = session.scalar(
            select(CandidateRecord.id).where(
                CandidateRecord.canonical_url == candidate.canonical_url
            )
        )
        if persisted_candidate_id != candidate.id:
            raise ValueError(
                f"Candidate URL {candidate.canonical_url} already belongs to another identity"
            )
        session.execute(
            insert(DocumentVersionRecord)
            .values(**document.__dict__)
            .on_conflict_do_nothing()
        )
        session.execute(
            insert(CollectionDiscoveryRecord)
            .values(
                collection_run_id=collection_run_id,
                source_definition_id=discovery.source_definition_id,
                candidate_id=candidate.id,
                document_version_id=document.id,
            )
            .on_conflict_do_nothing()
        )
