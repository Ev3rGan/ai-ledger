from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from ai_intel_agent.domain import (
    ApprovedFeedSourceDefinition,
    Candidate,
    CollectionDiscovery,
    CollectionRun,
    CollectionRunStatus,
    DocumentVersion,
    SourceDefinitionCollectionResult,
    SourceDefinitionCollectionStatus,
)
from ai_intel_agent.feed_acquisition import (
    FeedAcquisitionError,
    FeedEntry,
    FeedFetcher,
    load_approved_feed_source_definitions,
    parse_feed,
)
from ai_intel_agent.persistence import FeedCollectionRepository, create_database_engine


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class SourceDefinitionApprovalError(ValueError):
    pass


def collect_feed_source_definitions(
    database_url: str,
    *,
    source_definitions: tuple[ApprovedFeedSourceDefinition, ...],
    fetcher: FeedFetcher,
    clock: Clock,
    retry_of_run_id: UUID | None = None,
) -> CollectionRun:
    """Run one immutable Collection over approved RSS and Atom definitions."""
    if not source_definitions:
        raise ValueError("A Collection Run requires at least one Feed Source Definition")
    approved_definitions = set(load_approved_feed_source_definitions())
    unapproved_definitions = [
        definition
        for definition in source_definitions
        if definition not in approved_definitions
    ]
    if unapproved_definitions:
        names = ", ".join(definition.name for definition in unapproved_definitions)
        raise SourceDefinitionApprovalError(
            f"Feed Source Definitions are not approved by the current audit: {names}"
        )
    if len({definition.id for definition in source_definitions}) != len(
        source_definitions
    ):
        raise ValueError("A Collection Run cannot contain duplicate Source Definitions")

    started_at = clock.now()
    results: list[SourceDefinitionCollectionResult] = []
    discoveries: list[CollectionDiscovery] = []
    for source_definition in source_definitions:
        try:
            entries = parse_feed(fetcher.fetch(source_definition))
        except FeedAcquisitionError as error:
            results.append(
                SourceDefinitionCollectionResult(
                    source_definition_id=source_definition.id,
                    status=SourceDefinitionCollectionStatus.FAILED,
                    candidate_count=0,
                    error_code=error.code,
                    error_message=str(error),
                )
            )
            continue

        definition_discoveries = _build_discoveries(
            source_definition,
            entries,
            observed_at=clock.now(),
        )
        discoveries.extend(definition_discoveries)
        results.append(
            SourceDefinitionCollectionResult(
                source_definition_id=source_definition.id,
                status=SourceDefinitionCollectionStatus.SUCCEEDED,
                candidate_count=len(definition_discoveries),
            )
        )

    succeeded = sum(
        result.status is SourceDefinitionCollectionStatus.SUCCEEDED
        for result in results
    )
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
        source_definition_results=tuple(results),
    )

    engine = create_database_engine(database_url)
    try:
        FeedCollectionRepository(engine).persist(
            run,
            source_definitions,
            tuple(discoveries),
        )
    finally:
        engine.dispose()
    return run


def _build_discoveries(
    source_definition: ApprovedFeedSourceDefinition,
    entries: tuple[FeedEntry, ...],
    *,
    observed_at: datetime,
) -> tuple[CollectionDiscovery, ...]:
    discoveries: dict[tuple[UUID, UUID], CollectionDiscovery] = {}
    for entry in entries:
        candidate_id = uuid5(
            NAMESPACE_URL,
            f"ai-intel-agent:feed-candidate:{entry.canonical_url}",
        )
        version_payload = json.dumps(
            {
                "published_at": entry.published_at_raw,
                "summary": entry.summary,
                "title": entry.title,
                "updated_at": entry.updated_at_raw,
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
        discovery = CollectionDiscovery(
            source_definition_id=source_definition.id,
            candidate=Candidate(
                id=candidate_id,
                title=entry.title,
                canonical_url=entry.canonical_url,
                publisher=source_definition.publisher,
                discovered_at=observed_at,
            ),
            document_version=DocumentVersion(
                id=document_version_id,
                candidate_id=candidate_id,
                source_url=source_definition.entry_point,
                title=entry.title,
                body=entry.summary,
                content_hash=content_hash,
                observed_at=observed_at,
                published_at=entry.published_at,
                published_at_raw=entry.published_at_raw,
                updated_at=entry.updated_at,
                updated_at_raw=entry.updated_at_raw,
            ),
        )
        discoveries[(candidate_id, document_version_id)] = discovery
    return tuple(discoveries.values())
