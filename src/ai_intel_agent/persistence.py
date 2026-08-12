from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from alembic.config import Config
from dotenv import load_dotenv
from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from ai_intel_agent.domain import SampleStory
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

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    primary_document_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("document_versions.id")
    )
    stable_key: Mapped[str] = mapped_column(String(255), unique=True)
    headline: Mapped[str] = mapped_column(String(500))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


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


class TraceRecord(Base):
    __tablename__ = "structured_traces"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    operation_key: Mapped[str] = mapped_column(String(255), unique=True)
    evidence_span_id: Mapped[UUID] = mapped_column(ForeignKey("evidence_spans.id"))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
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
        rows = (
            (CandidateRecord, sample.candidate),
            (DocumentVersionRecord, sample.document_version),
            (StoryRecord, sample.story),
            (ClaimRecord, sample.claim),
            (EvidenceSpanRecord, sample.evidence_span),
            (TraceRecord, sample.trace),
        )
        with Session(self._engine) as session, session.begin():
            for record_type, domain_record in rows:
                values = dict(domain_record.__dict__)
                if record_type is EvidenceSpanRecord:
                    values["role"] = values["role"].value
                if record_type is TraceRecord:
                    values["attributes"] = dict(values["attributes"])
                session.execute(insert(record_type).values(**values).on_conflict_do_nothing())


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
