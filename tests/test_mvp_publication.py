from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import pytest
from fastapi.testclient import TestClient
from pg0 import Pg0
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from ai_intel_agent.cli import app
from ai_intel_agent.domain import (
    Claim,
    EvidenceRelation,
    EvidenceRole,
    EvidenceSpan,
    Story,
    StoryReviewState,
    StructuredTrace,
)
from ai_intel_agent.persistence import (
    DigestRecord,
    DigestStoryRecord,
    GeminiDraftRepository,
    StoryPresentationRecord,
    StoryRecord,
    create_database_engine,
    upgrade_database,
)
from ai_intel_agent.web import create_app

runner = CliRunner()
STORY_KEY = "gemini-release-notes:2026-08-12"
HEADLINE = "Gemini 3.6 Flash 正式发布"
CANONICAL_URL = "https://ai.google.dev/gemini-api/docs/changelog#august-12-2026"
CLAIMS_AND_EVIDENCE = (
    (
        "Google 已正式发布 Gemini 3.6 Flash。",
        "Gemini 3.6 Flash is generally available.",
    ),
    (
        "该模型面向代码与智能体规划任务。",
        "It is a stable, production-ready model for code and agentic planning. "
        + "Bounded public evidence. " * 12,
    ),
)


def _id(name: str):
    return uuid5(NAMESPACE_URL, f"mvp-m2-test:{name}")


@pytest.fixture
def mvp_database_url():
    name = f"ai_intel_mvp_{_id(os.urandom(8).hex).hex}"
    data_root = os.getenv("PG0_TEST_DATA_ROOT")
    data_dir = str(Path(data_root) / name) if data_root else None
    server = Pg0(name=name, data_dir=data_dir)
    server.start()
    try:
        upgrade_database(server.uri)
        yield server.uri
    finally:
        server.drop()


def _persist_m1_draft(
    database_url: str,
    *,
    identity: str = "real",
    stable_key: str = STORY_KEY,
    headline: str = HEADLINE,
    publisher: str = "Google",
) -> None:
    body = "August 12, 2026\n" + "\n".join(
        evidence for _, evidence in CLAIMS_AND_EVIDENCE
    )
    document_id = _id(f"{identity}:document")
    story = Story(
        id=_id(f"{identity}:story"),
        primary_document_version_id=document_id,
        stable_key=stable_key,
        headline=headline,
        occurred_at=datetime(2026, 8, 12, tzinfo=UTC),
        review_state=StoryReviewState.UNREVIEWED,
    )
    from ai_intel_agent.domain import Candidate, DocumentVersion

    candidate = Candidate(
        id=_id(f"{identity}:candidate"),
        title="Gemini API Release Notes — August 12, 2026",
        canonical_url=f"{CANONICAL_URL}-{identity}",
        publisher=publisher,
        discovered_at=datetime(2026, 8, 14, tzinfo=UTC),
    )
    document = DocumentVersion(
        id=document_id,
        candidate_id=candidate.id,
        source_url=CANONICAL_URL,
        title=candidate.title,
        body=body,
        content_hash=sha256(body.encode("utf-8")).hexdigest(),
        observed_at=candidate.discovered_at,
        published_at=datetime(2026, 8, 12, tzinfo=UTC),
        published_at_raw="August 12, 2026",
    )
    claims = tuple(
        Claim(
            id=_id(f"{identity}:claim:{position}"),
            story_id=story.id,
            position=position,
            text=claim_text,
        )
        for position, (claim_text, _) in enumerate(CLAIMS_AND_EVIDENCE)
    )
    evidence_spans = tuple(
        EvidenceSpan(
            id=_id(f"{identity}:evidence:{position}"),
            claim_id=claim.id,
            document_version_id=document.id,
            exact_text=evidence_text,
            start_offset=body.index(evidence_text),
            end_offset=body.index(evidence_text) + len(evidence_text),
            text_hash=sha256(evidence_text.encode("utf-8")).hexdigest(),
            role=EvidenceRole.PRIMARY,
            relation=EvidenceRelation.SUPPORTS,
        )
        for position, (claim, (_, evidence_text)) in enumerate(
            zip(claims, CLAIMS_AND_EVIDENCE, strict=True)
        )
    )
    traces = tuple(
        StructuredTrace(
            id=_id(f"{identity}:trace:{position}"),
            operation_key=f"mvp-m2-test:{identity}:trace:{position}",
            evidence_span_id=evidence.id,
            occurred_at=candidate.discovered_at,
            attributes={"route_identifier": "deepseek:v4-pro"},
        )
        for position, evidence in enumerate(evidence_spans)
    )

    engine = create_database_engine(database_url)
    try:
        from sqlalchemy.dialects.postgresql import insert
        from sqlalchemy.orm import Session

        from ai_intel_agent.persistence import CandidateRecord, DocumentVersionRecord

        with Session(engine) as session, session.begin():
            session.execute(insert(CandidateRecord).values(**candidate.__dict__))
            session.execute(insert(DocumentVersionRecord).values(**document.__dict__))
        GeminiDraftRepository(engine).persist(story, claims, evidence_spans, traces)
    finally:
        engine.dispose()


def _seed_accepted_with_reader_metadata(
    database_url: str,
    stable_key: str,
    *,
    topic: str,
) -> None:
    engine = create_database_engine(database_url)
    try:
        with Session(engine) as session, session.begin():
            story = session.scalar(
                select(StoryRecord).where(StoryRecord.stable_key == stable_key)
            )
            assert story is not None
            story.review_state = StoryReviewState.ACCEPTED.value
            session.add(
                StoryPresentationRecord(
                    story_id=story.id,
                    summary=f"{stable_key} 的读者摘要由 operator 明确输入并审核。",
                    why_it_matters=(
                        f"{stable_key} 会影响开发者的模型评估、采用或迁移计划。"
                    ),
                    primary_topic=topic,
                    secondary_topics=[],
                )
            )
    finally:
        engine.dispose()


@pytest.mark.postgres
def test_operator_lists_and_shows_the_persisted_m1_draft(mvp_database_url: str) -> None:
    _persist_m1_draft(mvp_database_url)
    environment = {"AI_INTEL_DATABASE_URL": mvp_database_url}

    listed = runner.invoke(app, ["story", "list"], env=environment)

    assert listed.exit_code == 0, listed.output
    assert STORY_KEY in listed.output
    assert HEADLINE in listed.output
    assert "unreviewed" in listed.output

    shown = runner.invoke(app, ["story", "show", STORY_KEY], env=environment)

    assert shown.exit_code == 0, shown.output
    assert HEADLINE in shown.output
    assert "Google" in shown.output
    assert f"{CANONICAL_URL}-real" in shown.output
    for claim, evidence in CLAIMS_AND_EVIDENCE:
        assert claim in shown.output
        assert evidence in shown.output


@pytest.mark.postgres
def test_operator_reviews_and_publishes_only_the_accepted_story(
    mvp_database_url: str,
) -> None:
    rejected_key = "gemini-release-notes:rejected"
    draft_key = "gemini-release-notes:draft"
    _persist_m1_draft(mvp_database_url)
    _persist_m1_draft(
        mvp_database_url,
        identity="rejected",
        stable_key=rejected_key,
        headline="不应公开的 rejected Story",
    )
    _persist_m1_draft(
        mvp_database_url,
        identity="draft",
        stable_key=draft_key,
        headline="不应公开的 draft Story",
    )
    selected_keys = [STORY_KEY]
    publishers_and_topics = (
        ("TechCrunch", "Models"),
        ("Hugging Face", "Research"),
        ("The Decoder", "Business"),
    )
    for position in range(8):
        publisher, topic = publishers_and_topics[position % len(publishers_and_topics)]
        stable_key = f"gemini-release-notes:selected-{position}"
        _persist_m1_draft(
            mvp_database_url,
            identity=f"selected-{position}",
            stable_key=stable_key,
            headline=f"{publisher} 已审核 Story {position}",
            publisher=publisher,
        )
        _seed_accepted_with_reader_metadata(mvp_database_url, stable_key, topic=topic)
        selected_keys.append(stable_key)
    environment = {"AI_INTEL_DATABASE_URL": mvp_database_url}

    _seed_accepted_with_reader_metadata(mvp_database_url, STORY_KEY, topic="Models")
    rejected = runner.invoke(
        app,
        ["story", "reject", rejected_key, "--actor", "m2-operator"],
        env=environment,
    )

    assert rejected.exit_code == 0, rejected.output
    assert "rejected" in rejected.output

    preview = runner.invoke(
        app,
        [
            "digest",
            "preview",
            "--date",
            "2026-08-15",
            *[
                option
                for stable_key in selected_keys
                for option in ("--story", stable_key)
            ],
        ],
        env=environment,
    )

    assert preview.exit_code == 0, preview.output
    assert STORY_KEY in preview.output
    assert HEADLINE in preview.output
    assert rejected_key not in preview.output
    assert draft_key not in preview.output

    engine = create_database_engine(mvp_database_url)
    try:
        with Session(engine) as session, session.begin():
            story_ids_by_key = {
                key: story_id
                for key, story_id in session.execute(
                    select(StoryRecord.stable_key, StoryRecord.id).where(
                        StoryRecord.stable_key.in_(selected_keys)
                    )
                )
            }
            digest_id = _id("public-web-regression:digest")
            session.add(
                DigestRecord(
                    id=digest_id,
                    stable_key="digest:2026-08-15",
                    publication_date=datetime(2026, 8, 15, tzinfo=UTC).date(),
                    state="draft",
                    published_at=None,
                    introduction="本期 Digest 汇集三家以上发布者的九条已审核 AI 进展。",
                    publication_contract="legacy-fixture",
                )
            )
            session.flush()
            session.add_all(
                DigestStoryRecord(
                    digest_id=digest_id,
                    story_id=story_ids_by_key[stable_key],
                    position=position,
                )
                for position, stable_key in enumerate(selected_keys)
            )
            session.flush()
            session.execute(
                update(DigestRecord)
                .where(DigestRecord.id == digest_id)
                .values(state="published", published_at=datetime(2026, 8, 15, tzinfo=UTC))
            )
    finally:
        engine.dispose()

    shown = runner.invoke(app, ["story", "show", STORY_KEY], env=environment)
    assert shown.exit_code == 0, shown.output
    assert "Review state: accepted" in shown.output

    bounded_evidence = CLAIMS_AND_EVIDENCE[1][1][:279] + "…"
    with TestClient(create_app(mvp_database_url)) as client:
        home = client.get("/")
        assert home.status_code == 200
        assert '/digests/2026-08-15' in home.text
        assert HEADLINE in home.text

        digest = client.get("/digests/2026-08-15")
        assert digest.status_code == 200
        assert '/stories/gemini-release-notes%3A2026-08-12' in digest.text
        assert HEADLINE in digest.text
        assert "Google" in digest.text
        assert f"{CANONICAL_URL}-real" not in digest.text
        assert bounded_evidence not in digest.text
        assert CLAIMS_AND_EVIDENCE[1][1] not in digest.text
        assert digest.text.count('class="story-card"') == 9

        story = client.get(f"/stories/{STORY_KEY}")
        assert story.status_code == 200
        assert HEADLINE in story.text
        assert bounded_evidence in story.text
        assert f"{CANONICAL_URL}-real" in story.text
        for claim, _ in CLAIMS_AND_EVIDENCE:
            assert claim in story.text
        assert client.get(f"/stories/{rejected_key}").status_code == 404
        assert client.get(f"/stories/{draft_key}").status_code == 404


@pytest.mark.postgres
def test_serve_command_starts_the_database_backed_web_app(
    mvp_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def run_server(web_app: object, *, host: str, port: int) -> None:
        captured.update(app=web_app, host=host, port=port)

    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=run_server))

    result = runner.invoke(
        app,
        ["serve", "--host", "127.0.0.2", "--port", "8123"],
        env={"AI_INTEL_DATABASE_URL": mvp_database_url},
    )

    assert result.exit_code == 0, result.output
    assert captured["host"] == "127.0.0.2"
    assert captured["port"] == 8123
    with TestClient(captured["app"]) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "暂无已发布 Digest" in response.text
