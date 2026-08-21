from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from urllib.parse import quote
from uuid import NAMESPACE_URL, uuid5
from xml.etree import ElementTree

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from pg0 import Pg0
from sqlalchemy import insert, select, update
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from ai_intel_agent.cli import app
from ai_intel_agent.domain import (
    Candidate,
    Claim,
    DocumentVersion,
    EvidenceRelation,
    EvidenceRole,
    EvidenceSpan,
    Story,
    StoryReviewState,
    StructuredTrace,
)
from ai_intel_agent.persistence import (
    CandidateRecord,
    DigestRecord,
    DigestStoryRecord,
    DocumentVersionRecord,
    GeminiDraftRepository,
    StoryPresentationRecord,
    StoryRecord,
    create_database_engine,
    database_url_for_alembic_config,
    upgrade_database,
)
from ai_intel_agent.web import create_app
from alembic import command

runner = CliRunner()


def _id(name: str):
    return uuid5(NAMESPACE_URL, f"m3-digest-test:{name}")


def _upgrade_database_to(database_url: str, revision: str) -> None:
    project_root = Path(__file__).resolve().parents[1]
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option(
        "sqlalchemy.url",
        database_url_for_alembic_config(database_url),
    )
    command.upgrade(config, revision)


@pytest.fixture
def m3_database_url():
    name = f"ai_intel_m3_{_id(os.urandom(8).hex).hex}"
    data_root = os.getenv("PG0_TEST_DATA_ROOT")
    data_dir = str(Path(data_root) / name) if data_root else None
    server = Pg0(name=name, data_dir=data_dir)
    server.start()
    try:
        upgrade_database(server.uri)
        yield server.uri
    finally:
        server.drop()


def _persist_m2_draft(
    database_url: str,
    *,
    identity: str,
    publisher: str,
    published_at: datetime,
) -> str:
    stable_key = f"m3-story:{identity}"
    canonical_url = f"https://{identity}.example.com/articles/{identity}"
    evidence_text = f"{publisher} published exact evidence for {identity}."
    body = f"Source article for {identity}. {evidence_text}"
    candidate = Candidate(
        id=_id(f"{identity}:candidate"),
        title=f"{publisher} source article {identity}",
        canonical_url=canonical_url,
        publisher=publisher,
        discovered_at=datetime(2026, 8, 18, 1, tzinfo=UTC),
    )
    document = DocumentVersion(
        id=_id(f"{identity}:document"),
        candidate_id=candidate.id,
        source_url=canonical_url,
        title=candidate.title,
        body=body,
        content_hash=sha256(body.encode("utf-8")).hexdigest(),
        observed_at=candidate.discovered_at,
        published_at=published_at,
        published_at_raw=published_at.isoformat(),
    )
    story = Story(
        id=_id(f"{identity}:story"),
        primary_document_version_id=document.id,
        stable_key=stable_key,
        headline=f"{publisher} 的 {identity} AI 进展",
        occurred_at=published_at,
        review_state=StoryReviewState.UNREVIEWED,
    )
    claim = Claim(
        id=_id(f"{identity}:claim"),
        story_id=story.id,
        position=0,
        text=f"{publisher} 已确认 {identity} AI 进展。",
    )
    evidence = EvidenceSpan(
        id=_id(f"{identity}:evidence"),
        claim_id=claim.id,
        document_version_id=document.id,
        exact_text=evidence_text,
        start_offset=body.index(evidence_text),
        end_offset=body.index(evidence_text) + len(evidence_text),
        text_hash=sha256(evidence_text.encode("utf-8")).hexdigest(),
        role=EvidenceRole.PRIMARY,
        relation=EvidenceRelation.SUPPORTS,
    )
    trace = StructuredTrace(
        id=_id(f"{identity}:trace"),
        operation_key=f"m3-digest-test:{identity}:trace",
        evidence_span_id=evidence.id,
        occurred_at=candidate.discovered_at,
        attributes={"route_identifier": "deepseek:v4-pro"},
    )

    engine = create_database_engine(database_url)
    try:
        with Session(engine) as session, session.begin():
            session.add(CandidateRecord(**candidate.__dict__))
            session.add(DocumentVersionRecord(**document.__dict__))
        GeminiDraftRepository(engine).persist(story, (claim,), (evidence,), (trace,))
    finally:
        engine.dispose()
    return stable_key


def _seed_accepted_story(
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
                    summary=f"{stable_key} 的摘要由 operator 明确输入并经过审核。",
                    why_it_matters=(
                        f"{stable_key} 会影响 AI 开发者的技术评估与采用计划。"
                    ),
                    primary_topic=topic,
                    secondary_topics=[],
                )
            )
    finally:
        engine.dispose()


@pytest.mark.postgres
def test_operator_filters_and_inspects_but_cannot_directly_accept_m2_draft(
    m3_database_url: str,
) -> None:
    target_key = _persist_m2_draft(
        m3_database_url,
        identity="techcrunch-model",
        publisher="TechCrunch",
        published_at=datetime(2026, 8, 17, 3, tzinfo=UTC),
    )
    other_key = _persist_m2_draft(
        m3_database_url,
        identity="hugging-face-research",
        publisher="Hugging Face",
        published_at=datetime(2026, 8, 16, 3, tzinfo=UTC),
    )
    environment = {"AI_INTEL_DATABASE_URL": m3_database_url}

    listed = runner.invoke(
        app,
        [
            "story",
            "list",
            "--source",
            "TechCrunch",
            "--date",
            "2026-08-17",
            "--state",
            "unreviewed",
        ],
        env=environment,
    )

    assert listed.exit_code == 0, listed.output
    assert target_key in listed.output
    assert other_key not in listed.output
    assert "TechCrunch" in listed.output
    assert "2026-08-17" in listed.output

    shown = runner.invoke(app, ["story", "show", target_key], env=environment)
    assert shown.exit_code == 0, shown.output
    assert "TechCrunch" in shown.output
    assert "2026-08-17T03:00:00+00:00" in shown.output
    assert "published exact evidence" in shown.output

    missing_metadata = runner.invoke(
        app,
        ["story", "accept", target_key, "--actor", "m3-operator"],
        env=environment,
    )
    assert missing_metadata.exit_code != 0

    accepted = runner.invoke(
        app,
        [
            "story",
            "accept",
            target_key,
            "--summary",
            "TechCrunch 报道了一项新的 AI 模型进展。",
            "--why-it-matters",
            "该进展会影响开发者选择模型与规划迁移窗口。",
            "--topic",
            "Models",
            "--actor",
            "m3-operator",
        ],
        env=environment,
    )

    assert accepted.exit_code != 0
    assert "exact Digest Plan" in accepted.output
    assert "approval" in accepted.output
    unchanged_story = runner.invoke(app, ["story", "show", target_key], env=environment)
    assert "Review state: unreviewed" in unchanged_story.output


@pytest.mark.postgres
def test_direct_multisource_digest_publication_is_retired(
    m3_database_url: str,
) -> None:
    publishers = (
        ("TechCrunch", "Models"),
        ("Hugging Face", "Research"),
        ("The Decoder", "Business"),
    )
    keys_by_publisher: dict[str, list[str]] = {name: [] for name, _ in publishers}
    for publisher_position, (publisher, topic) in enumerate(publishers):
        for item_position in range(4):
            stable_key = _persist_m2_draft(
                m3_database_url,
                identity=f"source-{publisher_position}-item-{item_position}",
                publisher=publisher,
                published_at=datetime(
                    2026,
                    8,
                    17,
                    publisher_position * 4 + item_position,
                    tzinfo=UTC,
                ),
            )
            _seed_accepted_story(m3_database_url, stable_key, topic=topic)
            keys_by_publisher[publisher].append(stable_key)

    environment = {"AI_INTEL_DATABASE_URL": m3_database_url}
    introduction = "今天的 Digest 聚焦模型、研究与产业三个来源方向的重要进展。"

    def publish(story_keys: list[str]):
        arguments = [
            "digest",
            "publish",
            "--date",
            "2026-08-18",
            "--introduction",
            introduction,
            "--actor",
            "m3-operator",
        ]
        for story_key in story_keys:
            arguments.extend(("--story", story_key))
        return runner.invoke(app, arguments, env=environment)

    selected_order = [
        keys_by_publisher["The Decoder"][2],
        keys_by_publisher["TechCrunch"][1],
        keys_by_publisher["Hugging Face"][3],
        keys_by_publisher["The Decoder"][0],
        keys_by_publisher["TechCrunch"][3],
        keys_by_publisher["Hugging Face"][1],
        keys_by_publisher["The Decoder"][1],
        keys_by_publisher["TechCrunch"][0],
        keys_by_publisher["Hugging Face"][0],
    ]
    published = publish(selected_order)

    assert published.exit_code != 0
    assert "exact Digest Plan" in published.output
    assert "approval" in published.output

    operational_result = runner.invoke(app, ["operator", "status"], env=environment)
    assert operational_result.exit_code == 0, operational_result.output
    operational = json.loads(operational_result.output)
    assert operational["latest_digest"] is None
    assert operational["pending_reviews"] == 0


@pytest.mark.postgres
def test_database_rejects_every_invalid_m3_publication_entry_path(
    m3_database_url: str,
) -> None:
    story_key = _persist_m2_draft(
        m3_database_url,
        identity="database-boundary",
        publisher="TechCrunch",
        published_at=datetime(2026, 8, 17, 21, tzinfo=UTC),
    )
    _seed_accepted_story(m3_database_url, story_key, topic="Models")
    engine = create_database_engine(m3_database_url)
    try:
        direct_publish_id = _id("direct-published-digest")
        with Session(engine) as session, pytest.raises(
            ProgrammingError,
            match="exact Digest Plan approval is required",
        ):
            session.add(
                DigestRecord(
                    id=direct_publish_id,
                    stable_key="arbitrary-publication-key",
                    publication_date=date(2026, 8, 18),
                    state="published",
                    published_at=datetime(2026, 8, 18, tzinfo=UTC),
                    introduction="这是一段由 operator 明确提供但成员数量无效的 Digest 介绍。",
                )
            )
            session.flush()

        transition_id = _id("transitioned-digest")
        with Session(engine) as session, session.begin():
            story_id = session.scalar(
                select(StoryRecord.id).where(StoryRecord.stable_key == story_key)
            )
            session.add(
                DigestRecord(
                    id=transition_id,
                    stable_key="another-arbitrary-publication-key",
                    publication_date=date(2026, 8, 18),
                    state="draft",
                    published_at=None,
                    introduction="这是一段由 operator 明确提供但成员数量无效的 Digest 介绍。",
                )
            )
            session.flush()
            session.add(
                DigestStoryRecord(
                    digest_id=transition_id,
                    story_id=story_id,
                    position=0,
                )
            )

        with Session(engine) as session, pytest.raises(
            ProgrammingError,
            match="exact Digest Plan approval is required",
        ):
            session.execute(
                update(DigestRecord)
                .where(DigestRecord.id == transition_id)
                .values(
                    state="published",
                    published_at=datetime(2026, 8, 18, tzinfo=UTC),
                )
            )
    finally:
        engine.dispose()


@pytest.mark.postgres
def test_0006_published_story_remains_visible_after_0007_upgrade_without_backfill(
) -> None:
    server = Pg0(name=f"ai_intel_m3_upgrade_{_id(os.urandom(8).hex).hex}")
    server.start()
    try:
        _upgrade_database_to(server.uri, "0006")
        story_key = _persist_m2_draft(
            server.uri,
            identity="legacy-published",
            publisher="Legacy Publisher",
            published_at=datetime(2026, 8, 16, 6, tzinfo=UTC),
        )
        digest_id = _id("legacy-published:digest")
        engine = create_database_engine(server.uri)
        try:
            with Session(engine) as session, session.begin():
                story_id = session.scalar(
                    select(StoryRecord.id).where(StoryRecord.stable_key == story_key)
                )
                session.execute(
                    update(StoryRecord)
                    .where(StoryRecord.id == story_id)
                    .values(review_state="accepted")
                )
                session.execute(
                    insert(DigestRecord).values(
                        id=digest_id,
                        stable_key="legacy-digest:2026-08-16",
                        publication_date=date(2026, 8, 16),
                        state="draft",
                        published_at=None,
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
                        state="published",
                        published_at=datetime(2026, 8, 16, 7, tzinfo=UTC),
                    )
                )
        finally:
            engine.dispose()

        _upgrade_database_to(server.uri, "head")
        with TestClient(create_app(server.uri)) as client:
            home = client.get("/")
            digest = client.get("/digests/2026-08-16")
            story = client.get(f"/stories/{quote(story_key, safe='')}")
            browse = client.get("/browse")
            rss = client.get("/rss.xml")

        headline = "Legacy Publisher 的 legacy-published AI 进展"
        assert all(
            response.status_code == 200
            for response in (home, digest, story, browse, rss)
        )
        assert all(
            headline in response.text for response in (home, digest, story, browse, rss)
        )
        assert "关键事实" in story.text
        assert "为什么重要" not in story.text

        engine = create_database_engine(server.uri)
        try:
            with Session(engine) as session:
                record = session.get(DigestRecord, digest_id)
                presentation = session.get(
                    StoryPresentationRecord,
                    _id("legacy-published:story"),
                )
            assert record is not None
            assert record.publication_contract == "legacy-fixture"
            assert presentation is None
        finally:
            engine.dispose()
    finally:
        server.drop()


@pytest.mark.postgres
def test_reader_scans_verifies_browses_and_finds_only_public_fixture_digest(
    m3_database_url: str,
) -> None:
    publisher_topics = (
        ("TechCrunch", "Models"),
        ("Hugging Face", "Research"),
        ("The Decoder", "Business"),
    )
    selected_order: list[str] = []
    for publisher_position, (publisher, topic) in enumerate(publisher_topics):
        for item_position in range(3):
            stable_key = _persist_m2_draft(
                m3_database_url,
                identity=f"public-{publisher_position}-item-{item_position}",
                publisher=publisher,
                published_at=datetime(
                    2026,
                    8,
                    17,
                    publisher_position * 3 + item_position,
                    tzinfo=UTC,
                ),
            )
            _seed_accepted_story(m3_database_url, stable_key, topic=topic)
            selected_order.append(stable_key)

    unreviewed_key = _persist_m2_draft(
        m3_database_url,
        identity="private-unreviewed",
        publisher="TechCrunch",
        published_at=datetime(2026, 8, 17, 12, tzinfo=UTC),
    )
    rejected_key = _persist_m2_draft(
        m3_database_url,
        identity="private-rejected",
        publisher="Hugging Face",
        published_at=datetime(2026, 8, 17, 13, tzinfo=UTC),
    )
    unpublished_key = _persist_m2_draft(
        m3_database_url,
        identity="private-accepted-unpublished",
        publisher="The Decoder",
        published_at=datetime(2026, 8, 17, 14, tzinfo=UTC),
    )
    environment = {"AI_INTEL_DATABASE_URL": m3_database_url}
    rejected = runner.invoke(
        app,
        ["story", "reject", rejected_key, "--actor", "m3-operator"],
        env=environment,
    )
    assert rejected.exit_code == 0, rejected.output
    _seed_accepted_story(m3_database_url, unpublished_key, topic="Business")

    introduction = "本期聚焦三个发布者在模型、研究和产业方向的九项已审核进展。"
    engine = create_database_engine(m3_database_url)
    try:
        with Session(engine) as session, session.begin():
            story_ids_by_key = {
                key: story_id
                for key, story_id in session.execute(
                    select(StoryRecord.stable_key, StoryRecord.id).where(
                        StoryRecord.stable_key.in_(selected_order)
                    )
                )
            }
            assert len(story_ids_by_key) == 9
            digest_id = _id("public-web-regression:digest")
            session.add(
                DigestRecord(
                    id=digest_id,
                    stable_key="digest:2026-08-18",
                    publication_date=date(2026, 8, 18),
                    state="draft",
                    published_at=None,
                    introduction=introduction,
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
                for position, stable_key in enumerate(selected_order)
            )
            session.flush()
            session.execute(
                update(DigestRecord)
                .where(DigestRecord.id == digest_id)
                .values(state="published", published_at=datetime(2026, 8, 18, tzinfo=UTC))
            )
    finally:
        engine.dispose()

    first_story_key = selected_order[0]
    first_story_url = f"/stories/{quote(first_story_key, safe='')}"
    first_identity = "public-0-item-0"
    first_summary = f"{first_story_key} 的摘要由 operator 明确输入并经过审核。"
    first_why = f"{first_story_key} 会影响 AI 开发者的技术评估与采用计划。"
    first_source_url = f"https://{first_identity}.example.com/articles/{first_identity}"
    with TestClient(create_app(m3_database_url)) as client:
        home = client.get("/")
        digest = client.get("/digests/2026-08-18")
        story = client.get(first_story_url)
        browse = client.get("/browse")
        research = client.get("/research")
        rss_landing = client.get("/rss")
        filtered = client.get(
            "/browse",
            params={
                "q": "public-2-item-2",
                "source": "The Decoder",
                "topic": "Business",
                "date": "2026-08-17",
            },
        )
        rss = client.get("/rss.xml")
        private_responses = {
            key: client.get(f"/stories/{quote(key, safe='')}")
            for key in (unreviewed_key, rejected_key, unpublished_key)
        }

    assert home.status_code == 200
    assert "今日 AI Digest" in home.text
    assert introduction in home.text
    assert "近期 Digest" in home.text
    assert "来源覆盖" in home.text
    for publisher, _ in publisher_topics:
        assert publisher in home.text
    assert 'href="/browse"' in home.text
    assert 'href="/research"' in home.text
    for page in (home, digest, story, browse, research, rss_landing):
        assert 'href="/rss"' in page.text
        assert 'href="/rss.xml">RSS</a>' not in page.text

    assert digest.status_code == 200
    assert introduction in digest.text
    assert digest.text.index(selected_order[0]) < digest.text.index(selected_order[1])
    assert digest.text.count('class="story-card"') == 9
    assert first_summary in digest.text
    assert "TechCrunch" in digest.text
    assert "Models" in digest.text
    assert "2026-08-17" in digest.text
    assert "Evidence Role" not in digest.text
    assert "Evidence Relation" not in digest.text
    assert "Claim：" not in digest.text

    assert story.status_code == 200
    assert first_summary in story.text
    assert first_why in story.text
    assert "为什么重要" in story.text
    assert "关键事实" in story.text
    assert "来源与依据" in story.text
    assert "<details" in story.text
    assert first_source_url in story.text
    assert "原始发布时间" in story.text
    assert 'id="claim-' in story.text
    assert 'id="evidence-' in story.text
    assert "text_hash" not in story.text
    assert "start_offset" not in story.text
    assert "state-machine" not in story.text

    assert browse.status_code == 200
    assert 'name="q"' in browse.text
    assert 'name="source"' in browse.text
    assert 'name="topic"' in browse.text
    assert 'name="date"' in browse.text
    assert first_story_key in browse.text
    assert unreviewed_key not in browse.text
    assert rejected_key not in browse.text
    assert unpublished_key not in browse.text

    assert filtered.status_code == 200
    assert "public-2-item-2" in filtered.text
    assert "public-2-item-1" not in filtered.text
    assert "public-0-item" not in filtered.text

    assert all(response.status_code == 404 for response in private_responses.values())
    for response in (home, digest, browse, filtered, rss):
        assert unreviewed_key not in response.text
        assert rejected_key not in response.text
        assert unpublished_key not in response.text

    assert rss.status_code == 200
    assert rss.headers["content-type"] == "application/rss+xml; charset=utf-8"
    channel = ElementTree.fromstring(rss.content).find("channel")
    assert channel is not None
    items = channel.findall("item")
    assert len(items) == 1
    assert items[0].findtext("link") == "http://testserver/digests/2026-08-18"
    description = items[0].findtext("description") or ""
    assert description.count('class="story-card"') == 9
    assert f"http://testserver/stories/{first_story_key}" in description
    assert first_summary in description

    assert rss_landing.status_code == 200
    assert "RSS 订阅" in rss_landing.text
    assert "机器可读" in rss_landing.text
    assert 'href="/rss.xml"' in rss_landing.text
    assert 'href="/"' in rss_landing.text
    assert 'href="/browse"' in rss_landing.text

    for response in (home, digest, story, browse):
        assert '<meta name="viewport"' in response.text
        assert "@media (max-width: 42rem)" in response.text
        assert "overflow-wrap:anywhere" in response.text
