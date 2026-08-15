from __future__ import annotations

import os
from dataclasses import replace
from datetime import date, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import UUID

from alembic.config import Config
from dotenv import load_dotenv
from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from ai_intel_agent.domain import (
    ApprovedFeedSourceDefinition,
    AuditAction,
    AuditEvent,
    AuditSubjectType,
    Claim,
    CollectionDiscovery,
    CollectionRun,
    DigestState,
    EvidenceSpan,
    SampleDigestPublication,
    SampleStory,
    Story,
    StoryReviewState,
    StructuredTrace,
)
from alembic import command


class Base(DeclarativeBase):
    pass


class CandidateRecord(Base):
    __tablename__ = "candidates"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    title: Mapped[str] = mapped_column(String(500))
    canonical_url: Mapped[str] = mapped_column(String(2048), unique=True)
    publisher: Mapped[str] = mapped_column(String(255))
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DocumentVersionRecord(Base):
    __tablename__ = "document_versions"
    __table_args__ = (UniqueConstraint("candidate_id", "content_hash"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    candidate_id: Mapped[UUID] = mapped_column(ForeignKey("candidates.id"))
    source_url: Mapped[str] = mapped_column(String(2048))
    title: Mapped[str] = mapped_column(String(500))
    body: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at_raw: Mapped[str | None] = mapped_column(String(255))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at_raw: Mapped[str | None] = mapped_column(String(255))


class StoryRecord(Base):
    __tablename__ = "stories"
    __table_args__ = (
        CheckConstraint(
            "review_state IN ('unreviewed', 'accepted', 'rejected')",
            name="ck_stories_review_state",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    primary_document_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("document_versions.id")
    )
    stable_key: Mapped[str] = mapped_column(String(255), unique=True)
    headline: Mapped[str] = mapped_column(String(500))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    review_state: Mapped[str] = mapped_column(String(32))


class ClaimRecord(Base):
    __tablename__ = "claims"
    __table_args__ = (UniqueConstraint("story_id", "position"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    story_id: Mapped[UUID] = mapped_column(ForeignKey("stories.id"))
    position: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)


class EvidenceSpanRecord(Base):
    __tablename__ = "evidence_spans"
    __table_args__ = (
        CheckConstraint(
            "start_offset >= 0",
            name="ck_evidence_spans_start_offset_nonnegative",
        ),
        CheckConstraint(
            "end_offset > start_offset",
            name="ck_evidence_spans_end_offset_after_start",
        ),
        CheckConstraint(
            "relation IN ('supports', 'contradicts')",
            name="ck_evidence_spans_relation",
        ),
        UniqueConstraint("claim_id", "document_version_id", "start_offset", "end_offset"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    claim_id: Mapped[UUID] = mapped_column(ForeignKey("claims.id"))
    document_version_id: Mapped[UUID] = mapped_column(ForeignKey("document_versions.id"))
    exact_text: Mapped[str] = mapped_column(Text)
    start_offset: Mapped[int] = mapped_column(Integer)
    end_offset: Mapped[int] = mapped_column(Integer)
    text_hash: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(32))
    relation: Mapped[str] = mapped_column(String(32))


class TraceRecord(Base):
    __tablename__ = "structured_traces"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    operation_key: Mapped[str] = mapped_column(String(255), unique=True)
    evidence_span_id: Mapped[UUID] = mapped_column(ForeignKey("evidence_spans.id"))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON)


class DigestRecord(Base):
    __tablename__ = "digests"
    __table_args__ = (
        CheckConstraint(
            "state IN ('draft', 'published')",
            name="ck_digests_state",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    stable_key: Mapped[str] = mapped_column(String(255), unique=True)
    publication_date: Mapped[date] = mapped_column(Date)
    state: Mapped[str] = mapped_column(String(32))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DigestStoryRecord(Base):
    __tablename__ = "digest_stories"
    __table_args__ = (UniqueConstraint("digest_id", "position"),)

    digest_id: Mapped[UUID] = mapped_column(ForeignKey("digests.id"), primary_key=True)
    story_id: Mapped[UUID] = mapped_column(ForeignKey("stories.id"), primary_key=True)
    position: Mapped[int] = mapped_column(Integer)


class AuditEventRecord(Base):
    __tablename__ = "audit_events"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    operation_key: Mapped[str] = mapped_column(String(255), unique=True)
    actor_identifier: Mapped[str] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(64))
    subject_type: Mapped[str] = mapped_column(String(64))
    subject_id: Mapped[UUID] = mapped_column(Uuid)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sequence: Mapped[int] = mapped_column(Integer)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON)


class SourceDefinitionRecord(Base):
    __tablename__ = "source_definitions"
    __table_args__ = (
        CheckConstraint(
            "activation_conclusion = 'approved'",
            name="ck_source_definitions_approved",
        ),
        CheckConstraint(
            "public_excerpt_max_characters BETWEEN 0 AND 1000",
            name="ck_source_definitions_excerpt_limit",
        ),
        UniqueConstraint("audit_version", "entry_point"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    publisher: Mapped[str] = mapped_column(String(255))
    entry_point: Mapped[str] = mapped_column(String(2048))
    audit_version: Mapped[str] = mapped_column(String(255))
    activation_conclusion: Mapped[str] = mapped_column(String(32))
    collection_schedule: Mapped[str] = mapped_column(String(255))
    discovery_method: Mapped[str] = mapped_column(Text)
    language: Mapped[str] = mapped_column(String(255))
    topic_scope: Mapped[list[str]] = mapped_column(JSON)
    access_constraints: Mapped[list[str]] = mapped_column(JSON)
    extraction_adapter: Mapped[str] = mapped_column(Text)
    health_policy: Mapped[str] = mapped_column(Text)
    cursor: Mapped[str] = mapped_column(Text)
    storage_policy: Mapped[str] = mapped_column(Text)
    public_excerpt_policy: Mapped[str] = mapped_column(Text)
    public_excerpt_max_characters: Mapped[int] = mapped_column(Integer)
    pause_conditions: Mapped[list[str]] = mapped_column(JSON)
    canonical_url_prefixes: Mapped[list[str]] = mapped_column(JSON)


class CollectionRunRecord(Base):
    __tablename__ = "collection_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'complete', 'partial', 'failed')",
            name="ck_collection_runs_status",
        ),
        CheckConstraint(
            "(status = 'running' AND completed_at IS NULL) OR "
            "(status IN ('complete', 'partial', 'failed') "
            "AND completed_at >= started_at)",
            name="ck_collection_runs_lifecycle",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    retry_of_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("collection_runs.id")
    )
    status: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SourceDefinitionCollectionResultRecord(Base):
    __tablename__ = "source_definition_collection_results"
    __table_args__ = (
        CheckConstraint(
            "status IN ('succeeded', 'failed')",
            name="ck_source_definition_collection_results_status",
        ),
        CheckConstraint(
            "candidate_count >= 0",
            name="ck_source_definition_collection_results_candidate_count",
        ),
        CheckConstraint(
            "(status = 'succeeded' AND error_code IS NULL AND error_message IS NULL) "
            "OR (status = 'failed' AND error_code IS NOT NULL "
            "AND error_message IS NOT NULL)",
            name="ck_source_definition_collection_results_error_shape",
        ),
    )

    collection_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("collection_runs.id"), primary_key=True
    )
    source_definition_id: Mapped[UUID] = mapped_column(
        ForeignKey("source_definitions.id"), primary_key=True
    )
    status: Mapped[str] = mapped_column(String(32))
    candidate_count: Mapped[int] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)


class CollectionDiscoveryRecord(Base):
    __tablename__ = "collection_discoveries"

    collection_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("collection_runs.id"), primary_key=True
    )
    source_definition_id: Mapped[UUID] = mapped_column(
        ForeignKey("source_definitions.id"), primary_key=True
    )
    document_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("document_versions.id"), primary_key=True
    )
    candidate_id: Mapped[UUID] = mapped_column(ForeignKey("candidates.id"))


def normalize_database_url(database_url: str) -> str:
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return database_url


def create_database_engine(database_url: str) -> Engine:
    normalized_url = normalize_database_url(database_url)
    if not normalized_url.startswith("postgresql+psycopg://"):
        raise ValueError("AI_INTEL_DATABASE_URL must point to PostgreSQL")
    return create_engine(normalized_url)


class SampleStoryRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def persist(self, sample: SampleStory) -> None:
        with Session(self._engine) as session, session.begin():
            _persist_sample_story(session, sample)


class SampleEditorialRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def persist(self, publication: SampleDigestPublication) -> None:
        with Session(self._engine) as session, session.begin():
            existing = session.get(DigestRecord, publication.digest.id)
            if existing is not None:
                _verify_existing_publication(session, publication, existing)
                return

            for sample in publication.stories:
                unreviewed = replace(
                    sample,
                    story=replace(
                        sample.story,
                        review_state=StoryReviewState.UNREVIEWED,
                    ),
                )
                _persist_sample_story(session, unreviewed)

            ordered_events = sorted(publication.audit_events, key=lambda event: event.sequence)
            if [event.sequence for event in ordered_events] != list(range(len(ordered_events))):
                raise ValueError("Audit event sequence must be contiguous")

            for event in ordered_events:
                if event.subject_type is AuditSubjectType.STORY:
                    _transition_story_record(session, event)
                elif event.action is AuditAction.DIGEST_COMPOSED:
                    _compose_digest_record(session, publication)
                elif event.action is AuditAction.DIGEST_PUBLISHED:
                    _publish_digest_record(session, publication)
                else:
                    raise ValueError(f"Unsupported audit action: {event.action}")
                _persist_audit_event(session, event)

            created = session.get(DigestRecord, publication.digest.id)
            if created is None:
                raise ValueError("Digest publication did not create a Digest")
            _verify_existing_publication(session, publication, created)


class FeedCollectionRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def persist(
        self,
        run: CollectionRun,
        source_definitions: tuple[ApprovedFeedSourceDefinition, ...],
        discoveries: tuple[CollectionDiscovery, ...],
    ) -> None:
        definitions_by_id = {
            source_definition.id: source_definition
            for source_definition in source_definitions
        }
        discoveries_by_definition: dict[UUID, list[CollectionDiscovery]] = {
            source_definition.id: [] for source_definition in source_definitions
        }
        for discovery in discoveries:
            discoveries_by_definition[discovery.source_definition_id].append(discovery)

        with Session(self._engine) as session, session.begin():
            if run.retry_of_run_id is not None and session.get(
                CollectionRunRecord, run.retry_of_run_id
            ) is None:
                raise ValueError(
                    f"Retry parent Collection Run {run.retry_of_run_id} does not exist"
                )

            for source_definition in source_definitions:
                _persist_source_definition(session, source_definition)

            run_record = CollectionRunRecord(
                id=run.id,
                retry_of_run_id=run.retry_of_run_id,
                status="running",
                started_at=run.started_at,
                completed_at=None,
            )
            session.add(run_record)
            session.flush()
            for result in run.source_definition_results:
                source_definition = definitions_by_id[result.source_definition_id]
                session.add(
                    SourceDefinitionCollectionResultRecord(
                        collection_run_id=run.id,
                        source_definition_id=source_definition.id,
                        status=result.status.value,
                        candidate_count=result.candidate_count,
                        error_code=result.error_code,
                        error_message=result.error_message,
                    )
                )
                for discovery in discoveries_by_definition[source_definition.id]:
                    _persist_collection_discovery(session, run.id, discovery)
            session.flush()
            run_record.status = run.status.value
            run_record.completed_at = run.completed_at


class GeminiDraftRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def known_document_version_ids(
        self,
        document_version_ids: set[UUID],
    ) -> set[UUID]:
        if not document_version_ids:
            return set()
        with Session(self._engine) as session:
            return set(
                session.scalars(
                    select(DocumentVersionRecord.id).where(
                        DocumentVersionRecord.id.in_(document_version_ids)
                    )
                )
            )

    def has_draft_for_candidate(self, candidate_id: UUID) -> bool:
        with Session(self._engine) as session:
            return (
                session.scalar(
                    select(StoryRecord.id)
                    .join(
                        DocumentVersionRecord,
                        StoryRecord.primary_document_version_id
                        == DocumentVersionRecord.id,
                    )
                    .where(
                        DocumentVersionRecord.candidate_id == candidate_id
                    )
                )
                is not None
            )

    def persist(
        self,
        story: Story,
        claims: tuple[Claim, ...],
        evidence_spans: tuple[EvidenceSpan, ...],
        traces: tuple[StructuredTrace, ...],
    ) -> bool:
        if not claims or not (
            len(claims) == len(evidence_spans) == len(traces)
        ):
            raise ValueError("A Gemini draft requires one Evidence Span and Trace per Claim")
        with Session(self._engine) as session, session.begin():
            if session.get(StoryRecord, story.id) is not None:
                return False
            document = session.get(DocumentVersionRecord, story.primary_document_version_id)
            if document is None:
                raise ValueError("Gemini draft Document Version does not exist")
            for position, (claim, evidence, trace) in enumerate(
                zip(claims, evidence_spans, traces, strict=True)
            ):
                if (
                    claim.story_id != story.id
                    or claim.position != position
                    or evidence.claim_id != claim.id
                    or evidence.document_version_id != document.id
                    or trace.evidence_span_id != evidence.id
                    or evidence.exact_text
                    != document.body[evidence.start_offset : evidence.end_offset]
                    or evidence.text_hash
                    != sha256(evidence.exact_text.encode("utf-8")).hexdigest()
                ):
                    raise ValueError("Gemini draft Claim provenance is invalid")
            session.add(
                StoryRecord(
                    **{
                        **story.__dict__,
                        "review_state": story.review_state.value,
                    }
                )
            )
            session.flush()
            session.add_all(ClaimRecord(**claim.__dict__) for claim in claims)
            session.flush()
            session.add_all(
                EvidenceSpanRecord(
                    **{
                        **evidence.__dict__,
                        "role": evidence.role.value,
                        "relation": evidence.relation.value,
                    }
                )
                for evidence in evidence_spans
            )
            session.flush()
            session.add_all(
                TraceRecord(
                    **{
                        **trace.__dict__,
                        "attributes": dict(trace.attributes),
                    }
                )
                for trace in traces
            )
            return True


def _persist_source_definition(
    session: Session,
    source_definition: ApprovedFeedSourceDefinition,
) -> None:
    values = {
        "id": source_definition.id,
        "name": source_definition.name,
        "publisher": source_definition.publisher,
        "entry_point": source_definition.entry_point,
        "audit_version": source_definition.audit_version,
        "activation_conclusion": "approved",
        "collection_schedule": source_definition.collection_schedule,
        "discovery_method": source_definition.discovery_method,
        "language": source_definition.language,
        "topic_scope": [topic.value for topic in source_definition.topic_scope],
        "access_constraints": list(source_definition.access_constraints),
        "extraction_adapter": source_definition.extraction_adapter,
        "health_policy": source_definition.health_policy,
        "cursor": source_definition.cursor,
        "storage_policy": source_definition.storage_policy,
        "public_excerpt_policy": source_definition.public_excerpt_policy,
        "public_excerpt_max_characters": (
            source_definition.public_excerpt_max_characters
        ),
        "pause_conditions": list(source_definition.pause_conditions),
        "canonical_url_prefixes": list(source_definition.canonical_url_prefixes),
    }
    existing = session.get(SourceDefinitionRecord, source_definition.id)
    if existing is None:
        session.add(SourceDefinitionRecord(**values))
        return
    if any(getattr(existing, key) != value for key, value in values.items()):
        raise ValueError(
            f"Existing Source Definition {source_definition.id} differs from its audit"
        )


def _persist_collection_discovery(
    session: Session,
    collection_run_id: UUID,
    discovery: CollectionDiscovery,
) -> None:
    candidate = discovery.candidate
    document_version = discovery.document_version
    session.execute(
        insert(CandidateRecord).values(**candidate.__dict__).on_conflict_do_nothing()
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
        .values(**document_version.__dict__)
        .on_conflict_do_nothing()
    )
    persisted_document_version_id = session.scalar(
        select(DocumentVersionRecord.id).where(
            DocumentVersionRecord.candidate_id == candidate.id,
            DocumentVersionRecord.content_hash == document_version.content_hash,
        )
    )
    if persisted_document_version_id != document_version.id:
        raise ValueError(
            f"Document Version hash for Candidate {candidate.id} belongs to another identity"
        )

    session.add(
        CollectionDiscoveryRecord(
            collection_run_id=collection_run_id,
            source_definition_id=discovery.source_definition_id,
            candidate_id=candidate.id,
            document_version_id=document_version.id,
        )
    )


def _persist_sample_story(session: Session, sample: SampleStory) -> None:
    rows = (
        (CandidateRecord, sample.candidate),
        (DocumentVersionRecord, sample.document_version),
        (StoryRecord, sample.story),
        (ClaimRecord, sample.claim),
        (EvidenceSpanRecord, sample.evidence_span),
        (TraceRecord, sample.trace),
    )
    for record_type, domain_record in rows:
        values = dict(domain_record.__dict__)
        if session.get(record_type, values["id"]) is not None:
            continue
        if record_type is StoryRecord:
            values["review_state"] = sample.story.review_state.value
        if record_type is EvidenceSpanRecord:
            values["role"] = values["role"].value
            values["relation"] = values["relation"].value
        if record_type is TraceRecord:
            values["attributes"] = dict(values["attributes"])
        session.execute(insert(record_type).values(**values).on_conflict_do_nothing())


def _transition_story_record(session: Session, event: AuditEvent) -> None:
    from_state = event.attributes.get("from_state")
    to_state = event.attributes.get("to_state")
    result = session.execute(
        update(StoryRecord)
        .where(
            StoryRecord.id == event.subject_id,
            StoryRecord.review_state == from_state,
        )
        .values(review_state=to_state)
    )
    if result.rowcount != 1:
        raise ValueError(f"Story {event.subject_id} cannot transition to {to_state}")


def _compose_digest_record(
    session: Session,
    publication: SampleDigestPublication,
) -> None:
    digest = publication.digest
    if digest.state is not DigestState.PUBLISHED or digest.published_at is None:
        raise ValueError("The application workflow must finish with a published Digest")
    session.execute(
        insert(DigestRecord).values(
            id=digest.id,
            stable_key=digest.stable_key,
            publication_date=digest.publication_date,
            state=DigestState.DRAFT.value,
            published_at=None,
        )
    )
    for position, story_id in enumerate(digest.story_ids):
        story_state = session.scalar(
            select(StoryRecord.review_state).where(StoryRecord.id == story_id)
        )
        if story_state != StoryReviewState.ACCEPTED.value:
            raise ValueError("Only accepted Stories may enter a Digest")
        session.execute(
            insert(DigestStoryRecord).values(
                digest_id=digest.id,
                story_id=story_id,
                position=position,
            )
        )


def _publish_digest_record(
    session: Session,
    publication: SampleDigestPublication,
) -> None:
    digest = publication.digest
    result = session.execute(
        update(DigestRecord)
        .where(
            DigestRecord.id == digest.id,
            DigestRecord.state == DigestState.DRAFT.value,
        )
        .values(
            state=DigestState.PUBLISHED.value,
            published_at=digest.published_at,
        )
    )
    if result.rowcount != 1:
        raise ValueError("Only a draft Digest may be published")


def _persist_audit_event(session: Session, event: AuditEvent) -> None:
    values = dict(event.__dict__)
    values["action"] = event.action.value
    values["subject_type"] = event.subject_type.value
    values["attributes"] = dict(event.attributes)
    session.execute(insert(AuditEventRecord).values(**values))


def _verify_existing_publication(
    session: Session,
    publication: SampleDigestPublication,
    existing: DigestRecord,
) -> None:
    digest = publication.digest
    digest_matches = (
        existing.stable_key == digest.stable_key
        and existing.publication_date == digest.publication_date
        and existing.state == digest.state.value
        and existing.published_at == digest.published_at
    )
    actual_story_ids = tuple(
        session.scalars(
            select(DigestStoryRecord.story_id)
            .where(DigestStoryRecord.digest_id == digest.id)
            .order_by(DigestStoryRecord.position)
        ).all()
    )
    expected_story_states = {
        sample.story.id: sample.story.review_state.value for sample in publication.stories
    }
    actual_story_states = dict(
        session.execute(
            select(StoryRecord.id, StoryRecord.review_state).where(
                StoryRecord.id.in_(expected_story_states)
            )
        ).all()
    )
    actual_events = {
        record.id: record
        for record in session.scalars(
            select(AuditEventRecord).where(
                AuditEventRecord.operation_key.like("sample-editorial-v1:%")
            )
        )
    }
    events_match = len(actual_events) == len(publication.audit_events) and all(
        (record := actual_events.get(event.id)) is not None
        and record.operation_key == event.operation_key
        and record.actor_identifier == event.actor_identifier
        and record.action == event.action.value
        and record.subject_type == event.subject_type.value
        and record.subject_id == event.subject_id
        and record.occurred_at == event.occurred_at
        and record.sequence == event.sequence
        and record.attributes == dict(event.attributes)
        for event in publication.audit_events
    )
    if not (
        digest_matches
        and actual_story_ids == digest.story_ids
        and actual_story_states == expected_story_states
        and events_match
    ):
        raise ValueError("Existing sample Digest differs from deterministic publication")


def upgrade_database(database_url: str) -> None:
    project_root = Path(__file__).resolve().parents[2]
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", normalize_database_url(database_url))
    command.upgrade(config, "head")


def database_url_from_environment() -> str:
    load_dotenv()
    database_url = os.getenv("AI_INTEL_DATABASE_URL")
    if not database_url:
        raise ValueError("Set AI_INTEL_DATABASE_URL to a PostgreSQL connection URL")
    return database_url
