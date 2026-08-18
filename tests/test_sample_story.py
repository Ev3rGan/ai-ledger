from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4
from xml.etree import ElementTree

import pytest
from fastapi.testclient import TestClient
from pg0 import Pg0
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from ai_intel_agent.cli import app
from ai_intel_agent.domain import (
    DigestState,
    EvidenceRelation,
    EvidenceRole,
    StoryReviewState,
)
from ai_intel_agent.editorial import DigestPublicationContract
from ai_intel_agent.persistence import (
    AuditEventRecord,
    CandidateRecord,
    ClaimRecord,
    DigestRecord,
    DigestStoryRecord,
    DocumentVersionRecord,
    EvidenceSpanRecord,
    SampleStoryRepository,
    StoryRecord,
    TraceRecord,
    create_database_engine,
    upgrade_database,
)
from ai_intel_agent.pipeline import persist_sample_story, publish_sample_digest
from ai_intel_agent.sample import build_sample_story
from ai_intel_agent.web import create_app

runner = CliRunner()
PUBLIC_EVIDENCE_EXCERPT_MAX_CHARACTERS = 280
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
        session.execute(
            text(
                """
                TRUNCATE TABLE
                    audit_events,
                    digest_stories,
                    digests,
                    structured_traces,
                    evidence_spans,
                    claims,
                    stories,
                    document_versions,
                    candidates
                RESTART IDENTITY CASCADE
                """
            )
        )
        session.commit()
    yield engine
    engine.dispose()


def _publish_story_record(
    session: Session,
    *,
    story_id: UUID,
    publication_date: date,
) -> None:
    digest_id = uuid4()
    session.execute(
        update(StoryRecord)
        .where(StoryRecord.id == story_id)
        .values(review_state=StoryReviewState.ACCEPTED.value)
    )
    session.execute(
        insert(DigestRecord).values(
            id=digest_id,
            stable_key=f"test-digest:{publication_date.isoformat()}:{digest_id}",
            publication_date=publication_date,
            state=DigestState.DRAFT.value,
            published_at=None,
            publication_contract=DigestPublicationContract.LEGACY_FIXTURE.value,
        )
    )
    session.execute(
        insert(DigestStoryRecord).values(
            digest_id=digest_id,
            story_id=story_id,
            position=0,
        )
    )
    session.execute(
        update(DigestRecord)
        .where(DigestRecord.id == digest_id)
        .values(
            state=DigestState.PUBLISHED.value,
            published_at=datetime(2026, 8, 13, 6, tzinfo=UTC),
        )
    )


def _insert_evidence_source(
    session: Session,
    *,
    claim_id: UUID,
    canonical_url: str,
    publisher: str,
    evidence_text: str,
    role: EvidenceRole,
    relation: EvidenceRelation = EvidenceRelation.SUPPORTS,
) -> None:
    candidate_id = uuid4()
    document_id = uuid4()
    body = f"{publisher}：{evidence_text}"
    start_offset = body.index(evidence_text)
    session.execute(
        insert(CandidateRecord).values(
            id=candidate_id,
            title=f"{publisher} Evidence",
            canonical_url=canonical_url,
            publisher=publisher,
            discovered_at=datetime(2026, 8, 13, tzinfo=UTC),
        )
    )
    session.execute(
        insert(DocumentVersionRecord).values(
            id=document_id,
            candidate_id=candidate_id,
            source_url=canonical_url,
            title=f"{publisher} Evidence",
            body=body,
            content_hash=sha256(body.encode("utf-8")).hexdigest(),
            observed_at=datetime(2026, 8, 13, tzinfo=UTC),
        )
    )
    session.execute(
        insert(EvidenceSpanRecord).values(
            id=uuid4(),
            claim_id=claim_id,
            document_version_id=document_id,
            exact_text=evidence_text,
            start_offset=start_offset,
            end_offset=start_offset + len(evidence_text),
            text_hash=sha256(evidence_text.encode("utf-8")).hexdigest(),
            role=role.value,
            relation=relation.value,
        )
    )


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
        exact_text, start_offset, end_offset, text_hash, relation = session.execute(
            select(
                EvidenceSpanRecord.exact_text,
                EvidenceSpanRecord.start_offset,
                EvidenceSpanRecord.end_offset,
                EvidenceSpanRecord.text_hash,
                EvidenceSpanRecord.relation,
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
    assert relation == EvidenceRelation.SUPPORTS.value
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
            role=EvidenceRole.PRIMARY.value,
            relation=EvidenceRelation.SUPPORTS.value,
        ),
    )
    for statement in immutable_writes:
        with Session(empty_database) as session, pytest.raises(
            ProgrammingError, match="published Story content is immutable"
        ):
            session.execute(statement)


@pytest.mark.postgres
def test_published_digest_membership_cannot_move_to_a_draft_digest(
    postgres_url: str, empty_database, tmp_path: Path
) -> None:
    output_path = tmp_path / "daily.md"
    result = runner.invoke(
        app,
        ["run", "--sample", "--output", str(output_path)],
        env={"AI_INTEL_DATABASE_URL": postgres_url},
    )
    assert result.exit_code == 0, result.output

    draft_digest_id = uuid4()
    with Session(empty_database) as session, session.begin():
        published_digest = session.scalars(select(DigestRecord)).one()
        published_digest_id = published_digest.id
        session.add(
            DigestRecord(
                id=draft_digest_id,
                stable_key=f"draft-digest:{draft_digest_id}",
                publication_date=published_digest.publication_date,
                state="draft",
                published_at=None,
            )
        )

    with Session(empty_database) as session, pytest.raises(
        ProgrammingError,
        match="published Digest membership is immutable",
    ):
        session.execute(
            update(DigestStoryRecord)
            .where(DigestStoryRecord.digest_id == published_digest_id)
            .values(digest_id=draft_digest_id)
        )


@pytest.mark.postgres
def test_anonymous_visitor_reads_published_digest_through_web_and_rss(
    postgres_url: str, empty_database
) -> None:
    publish_sample_digest(postgres_url)

    headline = "AI Agent 用任务轨迹支持结果复现"
    claim = "示例发布者的 AI Agent 会记录任务轨迹。"
    evidence_excerpt = "其 AI Agent 现在会记录任务轨迹"
    evidence_state = "单一来源"
    evidence_role = "第一方证据"
    source_url = "https://example.com/ai-agent-evidence"
    private_source_text = (
        "示例发布者宣布：其 AI Agent 现在会记录任务轨迹，以便复现实验结果。"
    )
    card_values = (headline, claim, "示例发布者", "Products and Tools")

    with TestClient(create_app(postgres_url)) as client:
        home = client.get("/")
        assert 'href="/digests/2026-08-12"' in home.text
        assert 'href="/stories/sample-story-v1"' in home.text
        assert 'href="/browse"' in home.text
        assert 'href="/rss"' in home.text

        digest = client.get("/digests/2026-08-12")
        assert 'href="/stories/sample-story-v1"' in digest.text

        story = client.get("/stories/sample-story-v1")
        browse = client.get("/browse")

        for response in (home, digest, browse):
            assert response.status_code == 200
            assert all(value in response.text for value in card_values)
            assert evidence_excerpt not in response.text
            assert source_url not in response.text
            assert private_source_text not in response.text
            assert "证据不足的 AI 性能声明" not in response.text
            assert "等待审核的 AI 工具候选" not in response.text

        assert story.status_code == 200
        assert all(
            value in story.text
            for value in (
                *card_values,
                evidence_excerpt,
                evidence_state,
                evidence_role,
                source_url,
                "为什么重要",
                "关键事实",
                "来源与依据",
            )
        )
        assert "Evidence Role" not in story.text
        assert "Evidence Relation" not in story.text
        assert private_source_text not in story.text

        rss = client.get("/rss.xml")

    assert rss.status_code == 200
    assert rss.headers["content-type"].startswith("application/rss+xml")
    assert private_source_text not in rss.text

    channel = ElementTree.fromstring(rss.content).find("channel")
    assert channel is not None
    items = channel.findall("item")
    assert len(items) == 1
    assert items[0].findtext("link") == "http://testserver/digests/2026-08-12"
    description = items[0].findtext("description") or ""
    assert all(value in description for value in card_values)
    assert evidence_excerpt not in description
    assert source_url not in description


@pytest.mark.postgres
def test_public_surfaces_use_canonical_links_and_bounded_evidence_excerpts(
    postgres_url: str, empty_database
) -> None:
    canonical_url = "https://example.com/canonical-story"
    redirected_source_url = "https://cdn.example.com/redirected-copy"
    private_text = "证据" * 150 + "不得公开的原文结尾"
    private_text_hash = sha256(private_text.encode("utf-8")).hexdigest()
    expected_excerpt = (
        private_text[: PUBLIC_EVIDENCE_EXCERPT_MAX_CHARACTERS - 1] + "…"
    )

    original_sample = build_sample_story()
    sample = replace(
        original_sample,
        candidate=replace(
            original_sample.candidate,
            canonical_url=canonical_url,
        ),
        document_version=replace(
            original_sample.document_version,
            source_url=redirected_source_url,
            body=private_text,
            content_hash=private_text_hash,
        ),
        evidence_span=replace(
            original_sample.evidence_span,
            exact_text=private_text,
            start_offset=0,
            end_offset=len(private_text),
            text_hash=private_text_hash,
        ),
    )
    SampleStoryRepository(empty_database).persist(sample)

    with Session(empty_database) as session:
        _publish_story_record(
            session,
            story_id=sample.story.id,
            publication_date=date(2026, 8, 13),
        )
        session.commit()

    with TestClient(create_app(postgres_url)) as client:
        card_responses = (
            client.get("/"),
            client.get("/digests/2026-08-13"),
            client.get("/browse"),
            client.get("/rss.xml"),
        )
        story = client.get("/stories/sample-story-v1")

    for response in card_responses:
        assert response.status_code == 200
        assert canonical_url not in response.text
        assert redirected_source_url not in response.text
        assert private_text not in response.text
        assert "不得公开的原文结尾" not in response.text
    assert story.status_code == 200
    assert canonical_url in story.text
    assert redirected_source_url not in story.text
    assert expected_excerpt in story.text
    assert private_text not in story.text
    assert "不得公开的原文结尾" not in story.text


@pytest.mark.postgres
def test_public_surfaces_fail_closed_when_a_digest_contains_a_rejected_story(
    postgres_url: str, empty_database
) -> None:
    publish_sample_digest(postgres_url)
    publication_date = date(2026, 8, 14)
    digest_id = uuid4()

    with Session(empty_database) as session:
        rejected_story_id = session.scalar(
            select(StoryRecord.id).where(
                StoryRecord.stable_key == "sample-story-v1-rejected"
            )
        )
        session.execute(
            update(StoryRecord)
            .where(StoryRecord.id == rejected_story_id)
            .values(review_state=StoryReviewState.ACCEPTED.value)
        )
        session.execute(
            insert(DigestRecord).values(
                id=digest_id,
                stable_key="test-digest:2026-08-14",
                publication_date=publication_date,
                state=DigestState.DRAFT.value,
                published_at=None,
                publication_contract=DigestPublicationContract.LEGACY_FIXTURE.value,
            )
        )
        session.execute(
            insert(DigestStoryRecord).values(
                digest_id=digest_id,
                story_id=rejected_story_id,
                position=0,
            )
        )
        session.execute(
            update(StoryRecord)
            .where(StoryRecord.id == rejected_story_id)
            .values(review_state=StoryReviewState.REJECTED.value)
        )
        session.execute(
            update(DigestRecord)
            .where(DigestRecord.id == digest_id)
            .values(
                state=DigestState.PUBLISHED.value,
                published_at=datetime(2026, 8, 14, 6, tzinfo=UTC),
            )
        )
        session.commit()

    rejected_headline = "证据不足的 AI 性能声明"
    with TestClient(create_app(postgres_url)) as client:
        responses = (
            client.get("/"),
            client.get("/digests/2026-08-14"),
            client.get("/browse"),
            client.get("/rss.xml"),
        )
        rejected_story = client.get("/stories/sample-story-v1-rejected")

    assert rejected_story.status_code == 404
    for response in responses:
        assert response.status_code == 200
        assert rejected_headline not in response.text


@pytest.mark.postgres
def test_evidence_state_is_evaluated_relative_to_each_claim(
    postgres_url: str, empty_database
) -> None:
    sample = persist_sample_story(postgres_url)
    second_claim_id = uuid4()

    with Session(empty_database) as session:
        session.execute(
            insert(ClaimRecord).values(
                id=second_claim_id,
                story_id=sample.story.id,
                position=1,
                text="第二个发布者宣布了另一个事实。",
            )
        )
        _insert_evidence_source(
            session,
            claim_id=second_claim_id,
            canonical_url="https://second.example.com/fact",
            publisher="第二个发布者",
            evidence_text="另一个可独立判断的事实",
            role=EvidenceRole.PRIMARY,
        )
        _publish_story_record(
            session,
            story_id=sample.story.id,
            publication_date=date(2026, 8, 13),
        )
        session.commit()

    with TestClient(create_app(postgres_url)) as client:
        story = client.get("/stories/sample-story-v1")

    assert story.status_code == 200
    assert 'data-evidence-state="single-source"' in story.text
    assert "单一来源" in story.text
    assert "多来源" not in story.text


@pytest.mark.postgres
def test_claim_without_evidence_is_visible_as_insufficient_evidence(
    postgres_url: str, empty_database
) -> None:
    sample = persist_sample_story(postgres_url)
    unsupported_claim = "这个 Claim 尚无可公开的 Evidence Span。"

    with Session(empty_database) as session:
        session.execute(
            insert(ClaimRecord).values(
                id=uuid4(),
                story_id=sample.story.id,
                position=1,
                text=unsupported_claim,
            )
        )
        _publish_story_record(
            session,
            story_id=sample.story.id,
            publication_date=date(2026, 8, 13),
        )
        session.commit()

    with TestClient(create_app(postgres_url)) as client:
        story = client.get("/stories/sample-story-v1")

    assert story.status_code == 200
    assert unsupported_claim in story.text
    assert 'data-evidence-state="insufficient-evidence"' in story.text
    assert "证据不足" in story.text


@pytest.mark.postgres
def test_two_independent_sources_are_multi_source_for_the_same_claim(
    postgres_url: str, empty_database
) -> None:
    sample = persist_sample_story(postgres_url)

    with Session(empty_database) as session:
        session.execute(
            update(EvidenceSpanRecord)
            .where(EvidenceSpanRecord.id == sample.evidence_span.id)
            .values(role=EvidenceRole.INDEPENDENT.value)
        )
        _insert_evidence_source(
            session,
            claim_id=sample.claim.id,
            canonical_url="https://independent.example.com/confirmation",
            publisher="独立确认者",
            evidence_text="独立确认了相同的 Claim",
            role=EvidenceRole.INDEPENDENT,
        )
        _publish_story_record(
            session,
            story_id=sample.story.id,
            publication_date=date(2026, 8, 13),
        )
        session.commit()

    with TestClient(create_app(postgres_url)) as client:
        story = client.get("/stories/sample-story-v1")

    assert story.status_code == 200
    assert 'data-evidence-state="multi-source"' in story.text
    assert "多来源" in story.text


@pytest.mark.postgres
def test_supporting_and_contradicting_evidence_is_visible_as_conflict(
    postgres_url: str, empty_database
) -> None:
    sample = persist_sample_story(postgres_url)

    with Session(empty_database) as session:
        _insert_evidence_source(
            session,
            claim_id=sample.claim.id,
            canonical_url="https://independent.example.com/contradiction",
            publisher="独立核查者",
            evidence_text="独立核查结果与该 Claim 矛盾",
            role=EvidenceRole.INDEPENDENT,
            relation=EvidenceRelation.CONTRADICTS,
        )
        _publish_story_record(
            session,
            story_id=sample.story.id,
            publication_date=date(2026, 8, 13),
        )
        session.commit()

    with TestClient(create_app(postgres_url)) as client:
        story = client.get("/stories/sample-story-v1")

    assert story.status_code == 200
    assert 'data-evidence-state="conflict"' in story.text
    assert "证据冲突" in story.text
