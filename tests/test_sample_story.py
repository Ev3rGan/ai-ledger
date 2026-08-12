from __future__ import annotations

import os
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from pg0 import Pg0
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from ai_intel_agent.cli import app
from ai_intel_agent.persistence import (
    CandidateRecord,
    ClaimRecord,
    DocumentVersionRecord,
    EvidenceSpanRecord,
    StoryRecord,
    TraceRecord,
    create_database_engine,
    upgrade_database,
)

runner = CliRunner()
RECORD_TYPES = (
    CandidateRecord,
    DocumentVersionRecord,
    StoryRecord,
    ClaimRecord,
    EvidenceSpanRecord,
    TraceRecord,
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
def test_sample_cli_twice_persists_one_traceable_story(
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
        evidence_span_id = session.scalar(select(EvidenceSpanRecord.id))
        trace_evidence_span_id = session.scalar(select(TraceRecord.evidence_span_id))
        document_body = session.scalar(select(DocumentVersionRecord.body))
        exact_text, start_offset, end_offset, text_hash = session.execute(
            select(
                EvidenceSpanRecord.exact_text,
                EvidenceSpanRecord.start_offset,
                EvidenceSpanRecord.end_offset,
                EvidenceSpanRecord.text_hash,
            )
        ).one()
        pgvector_version = session.scalar(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        )

    assert counts == {
        "candidates": 1,
        "document_versions": 1,
        "stories": 1,
        "claims": 1,
        "evidence_spans": 1,
        "structured_traces": 1,
    }
    assert trace_evidence_span_id == evidence_span_id
    assert str(evidence_span_id) in first_report
    assert document_body[start_offset:end_offset] == exact_text
    assert sha256(exact_text.encode("utf-8")).hexdigest() == text_hash
    assert pgvector_version
