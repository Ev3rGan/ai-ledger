from __future__ import annotations

import os
from dataclasses import replace
from datetime import date, datetime
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
    AuditAction,
    AuditEvent,
    AuditSubjectType,
    DigestState,
    SampleDigestPublication,
    SampleStory,
    StoryReviewState,
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
