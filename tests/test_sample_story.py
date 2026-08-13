from __future__ import annotations

import os
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from pg0 import Pg0
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from ai_intel_agent.cli import app
from ai_intel_agent.persistence import (
    AuditEventRecord,
    CandidateRecord,
    ClaimRecord,
    DigestRecord,
    DigestStoryRecord,
    DocumentVersionRecord,
    EvidenceSpanRecord,
    StoryRecord,
    TraceRecord,
    create_database_engine,
    upgrade_database,
)
from ai_intel_agent.pipeline import persist_sample_story

runner = CliRunner()
RECORD_TYPES = (
    CandidateRecord,
    DocumentVersionRecord,
    StoryRecord,
    ClaimRecord,
    EvidenceSpanRecord,
    TraceRecord,
    DigestRecord,
    DigestStoryRecord,
    AuditEventRecord,
)


@pytest.fixture(scope="session")
def postgres_url():
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is not None:
        if not database_url.startswith(("postgresql://", "postgresql+psycopg://")):
            pytest.fail("TEST_DATABASE_URL must point to PostgreSQL")
        yield database_url
        return

    server = Pg0(name=f"ai_intel_agent_test_{uuid4().hex}")
    server.start()
    try:
        yield server.uri
    finally:
        server.drop()


@pytest.fixture
def empty_database(postgres_url: str):
    upgrade_database(postgres_url)
    engine = create_database_engine(postgres_url)
    with Session(engine) as session:
        for record_type in reversed(RECORD_TYPES):
            session.execute(delete(record_type))
        session.commit()
    yield engine
    engine.dispose()


@pytest.mark.postgres
def test_sample_cli_twice_reviews_stories_and_publishes_one_digest(
    postgres_url: str, empty_database, tmp_path: Path
) -> None:
    output_path = tmp_path / "daily.md"
    environment = {"AI_INTEL_DATABASE_URL": postgres_url}

    first = runner.invoke(
        app,
        ["run", "--sample", "--output", str(output_path)],
        env=environment,
    )
    first_report = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
    with Session(empty_database) as session:
        first_digest_id = session.scalar(select(DigestRecord.id))

    second = runner.invoke(
        app,
        ["run", "--sample", "--output", str(output_path)],
        env=environment,
    )

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert output_path.read_text(encoding="utf-8") == first_report

    with Session(empty_database) as session:
        counts = {
            record_type.__tablename__: session.scalar(
                select(func.count()).select_from(record_type)
            )
            for record_type in RECORD_TYPES
        }
        story_states = dict(
            session.execute(select(StoryRecord.stable_key, StoryRecord.review_state)).all()
        )
        accepted_story_id = session.scalar(
            select(StoryRecord.id).where(StoryRecord.stable_key == "sample-story-v1")
        )
        accepted_claim_id = session.scalar(
            select(ClaimRecord.id).where(ClaimRecord.story_id == accepted_story_id)
        )
        accepted_document_version_id = session.scalar(
            select(StoryRecord.primary_document_version_id).where(
                StoryRecord.id == accepted_story_id
            )
        )
        evidence_span_id = session.scalar(
            select(EvidenceSpanRecord.id)
            .join(ClaimRecord, EvidenceSpanRecord.claim_id == ClaimRecord.id)
            .where(ClaimRecord.story_id == accepted_story_id)
        )
        trace_evidence_span_id = session.scalar(
            select(TraceRecord.evidence_span_id).where(
                TraceRecord.operation_key == "sample-story-v1"
            )
        )
        document_body = session.scalar(
            select(DocumentVersionRecord.body)
            .join(
                StoryRecord,
                StoryRecord.primary_document_version_id == DocumentVersionRecord.id,
            )
            .where(StoryRecord.id == accepted_story_id)
        )
        exact_text, start_offset, end_offset, text_hash = session.execute(
            select(
                EvidenceSpanRecord.exact_text,
                EvidenceSpanRecord.start_offset,
                EvidenceSpanRecord.end_offset,
                EvidenceSpanRecord.text_hash,
            )
            .join(ClaimRecord, EvidenceSpanRecord.claim_id == ClaimRecord.id)
            .join(StoryRecord, ClaimRecord.story_id == StoryRecord.id)
            .where(StoryRecord.stable_key == "sample-story-v1")
        ).one()
        digest_id, digest_stable_key, digest_state = session.execute(
            select(DigestRecord.id, DigestRecord.stable_key, DigestRecord.state)
        ).one()
        digest_story_keys = session.scalars(
            select(StoryRecord.stable_key)
            .join(DigestStoryRecord, DigestStoryRecord.story_id == StoryRecord.id)
            .order_by(DigestStoryRecord.position)
        ).all()
        audit_events = session.execute(
            select(
                AuditEventRecord.sequence,
                AuditEventRecord.action,
                AuditEventRecord.actor_identifier,
                AuditEventRecord.attributes,
            ).order_by(AuditEventRecord.sequence)
        ).all()
        pgvector_version = session.scalar(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        )

    assert counts == {
        "candidates": 3,
        "document_versions": 3,
        "stories": 3,
        "claims": 3,
        "evidence_spans": 3,
        "structured_traces": 3,
        "digests": 1,
        "digest_stories": 1,
        "audit_events": 4,
    }
    assert story_states == {
        "sample-story-v1": "accepted",
        "sample-story-v1-rejected": "rejected",
        "sample-story-v1-unreviewed": "unreviewed",
    }
    assert trace_evidence_span_id == evidence_span_id
    assert document_body[start_offset:end_offset] == exact_text
    assert sha256(exact_text.encode("utf-8")).hexdigest() == text_hash
    assert digest_id == first_digest_id
    assert digest_stable_key == "sample-digest:2026-08-12"
    assert digest_state == "published"
    assert digest_story_keys == ["sample-story-v1"]
    assert str(digest_id) in first_report
    assert str(evidence_span_id) in first_report
    assert "AI Agent 用任务轨迹支持结果复现" in first_report
    assert "证据不足的 AI 性能声明" not in first_report
    assert "等待审核的 AI 工具候选" not in first_report
    assert audit_events == [
        (
            0,
            "story.accepted",
            "fake-administrator",
            {"from_state": "unreviewed", "to_state": "accepted"},
        ),
        (
            1,
            "story.rejected",
            "fake-administrator",
            {"from_state": "unreviewed", "to_state": "rejected"},
        ),
        (
            2,
            "digest.composed",
            "fake-administrator",
            {"included_story_ids": [str(accepted_story_id)]},
        ),
        (
            3,
            "digest.published",
            "fake-administrator",
            {"from_state": "draft", "to_state": "published"},
        ),
    ]
    assert pgvector_version

    persist_sample_story(postgres_url)
    with Session(empty_database) as session:
        assert session.scalar(
            select(StoryRecord.review_state).where(StoryRecord.id == accepted_story_id)
        ) == "accepted"
        with pytest.raises(ProgrammingError, match="published Story content is immutable"):
            session.execute(
                update(StoryRecord)
                .where(StoryRecord.id == accepted_story_id)
                .values(review_state="unreviewed")
            )

    immutable_writes = (
        update(StoryRecord)
        .where(StoryRecord.id == accepted_story_id)
        .values(headline="静默改写"),
        update(ClaimRecord)
        .where(ClaimRecord.story_id == accepted_story_id)
        .values(text="静默改写"),
        update(EvidenceSpanRecord)
        .where(EvidenceSpanRecord.id == evidence_span_id)
        .values(exact_text="静默改写"),
        update(DocumentVersionRecord)
        .where(
            DocumentVersionRecord.id
            == select(StoryRecord.primary_document_version_id)
            .where(StoryRecord.id == accepted_story_id)
            .scalar_subquery()
        )
        .values(body="静默改写"),
        insert(ClaimRecord).values(
            id=uuid4(),
            story_id=accepted_story_id,
            position=1,
            text="静默增加的 Claim",
        ),
        insert(EvidenceSpanRecord).values(
            id=uuid4(),
            claim_id=accepted_claim_id,
            document_version_id=accepted_document_version_id,
            exact_text="静默增加的 Evidence",
            start_offset=0,
            end_offset=1,
            text_hash="0" * 64,
            role="primary",
        ),
    )
    for statement in immutable_writes:
        with Session(empty_database) as session, pytest.raises(
            ProgrammingError, match="published Story content is immutable"
        ):
            session.execute(statement)
