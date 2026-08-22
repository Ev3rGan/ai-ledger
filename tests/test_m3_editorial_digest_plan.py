from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from pg0 import Pg0
from sqlalchemy import event, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from typer.testing import CliRunner

import ai_intel_agent.cli as cli_module
from ai_intel_agent.cli import app
from ai_intel_agent.domain import (
    Candidate,
    Claim,
    DigestState,
    DocumentVersion,
    EvidenceRelation,
    EvidenceRole,
    EvidenceSpan,
    SampleStory,
    Story,
    StoryReviewState,
    StructuredTrace,
    Topic,
)
from ai_intel_agent.editorial import (
    ClaimInspection,
    DeepSeekEditorialPlanProvider,
    DigestPlanInclusion,
    DigestPublicationContract,
    EditorialContext,
    EditorialPlanProposal,
    EditorialStateError,
    EditorialStoryProposal,
    EvidenceSpanInspection,
    SchedulerHealthInspection,
    SourceHealthInspection,
    StoryInspection,
    compose_digest,
    editorial_window_for,
    load_editorial_agent_protocol,
    prepare_digest_plan,
    restore_digest_plan,
)
from ai_intel_agent.persistence import (
    CandidateRecord,
    ClaimRecord,
    CollectionRunRecord,
    DigestPlanApprovalRecord,
    DigestPlanRecord,
    DigestRecord,
    DigestStoryRecord,
    DigestWithdrawalRecord,
    DocumentVersionRecord,
    EditorialRepository,
    EvidenceSpanRecord,
    SampleStoryRepository,
    SchedulerStatusRepository,
    SourceCandidateResultRecord,
    SourceDefinitionRecord,
    SourceProfileStateRecord,
    StoryPresentationRecord,
    StoryRecord,
    create_database_engine,
    database_url_for_alembic_config,
    upgrade_database,
)
from ai_intel_agent.publication import PublicPublicationRepository
from ai_intel_agent.web import create_app
from alembic import command


def _id(name: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"m3-editorial-plan-test:{name}")


def _story(position: int, *, publisher: str, source_id: UUID) -> StoryInspection:
    published_at = datetime(2026, 8, 20, 2 + position, tzinfo=UTC)
    exact_text = f"{publisher} exact Evidence Span {position}."
    return StoryInspection(
        id=_id(f"story:{position}"),
        stable_key=f"story:{position}",
        headline=f"{publisher} AI development {position}",
        review_state=StoryReviewState.UNREVIEWED,
        claims=(
            ClaimInspection(
                id=_id(f"claim:{position}"),
                text=f"{publisher} confirmed development {position}.",
                evidence_spans=(
                    EvidenceSpanInspection(
                        id=_id(f"evidence:{position}"),
                        document_version_id=_id(f"document:{position}"),
                        exact_text=exact_text,
                        start_offset=0,
                        end_offset=len(exact_text),
                        text_hash=f"{position:064x}",
                        role=EvidenceRole.PRIMARY,
                        relation=EvidenceRelation.SUPPORTS,
                        publisher=publisher,
                        canonical_url=f"https://example.com/{position}",
                    ),
                ),
            ),
        ),
        publisher=publisher,
        canonical_url=f"https://example.com/{position}",
        original_published_at=published_at,
        primary_document_version_id=_id(f"document:{position}"),
        primary_document_content_hash=f"{position + 100:064x}",
        source_definition_id=source_id,
        source_definition_name=f"{publisher} feed",
        summary=None,
        why_it_matters=None,
        primary_topic=None,
        secondary_topics=(),
    )


class _FakeEditorialProvider:
    identifier = "fake-editorial:v1"
    protocol_version = "editorial-digest-plan-test.v1"

    def prepare(self, context: EditorialContext) -> EditorialPlanProposal:
        proposals = []
        for position, story in enumerate(context.stories):
            included = position < 9
            proposals.append(
                EditorialStoryProposal(
                    stable_key=story.stable_key,
                    inclusion=(
                        DigestPlanInclusion.INCLUDED if included else DigestPlanInclusion.EXCLUDED
                    ),
                    order=position if included else None,
                    summary=(
                        f"{story.publisher} 提供了足够完整的读者摘要，说明这项 AI 进展的核心内容。"
                    ),
                    why_it_matters=("这项进展会影响开发者的模型选择、验证工作以及后续迁移计划。"),
                    primary_topic=Topic.MODELS.value,
                    secondary_topics=(Topic.PRODUCTS_AND_TOOLS.value,),
                    exclusion_reason=(
                        None if included else "Held out to keep this edition focused."
                    ),
                )
            )
        return EditorialPlanProposal(
            digest_summary="本期 Digest 汇总多来源 AI 进展，并保留逐条 Evidence 可追溯性。",
            stories=tuple(proposals),
            provider_identifier=self.identifier,
            protocol_version=self.protocol_version,
        )


class _ExcludeUnsupportedEditorialProvider(_FakeEditorialProvider):
    identifier = "fake-editorial:v2"

    def prepare(self, context: EditorialContext) -> EditorialPlanProposal:
        original = super().prepare(context)
        proposals = []
        included_order = 0
        for item in original.stories:
            included = not item.stable_key.endswith(":0")
            proposals.append(
                EditorialStoryProposal(
                    stable_key=item.stable_key,
                    inclusion=(
                        DigestPlanInclusion.INCLUDED if included else DigestPlanInclusion.EXCLUDED
                    ),
                    order=included_order if included else None,
                    summary=f"  {item.summary}  ",
                    why_it_matters=f"  {item.why_it_matters}  ",
                    primary_topic=item.primary_topic,
                    secondary_topics=item.secondary_topics,
                    exclusion_reason=(
                        None if included else "Excluded because Evidence is blocking."
                    ),
                )
            )
            if included:
                included_order += 1
        return EditorialPlanProposal(
            digest_summary=f"  {original.digest_summary}  ",
            stories=tuple(proposals),
            provider_identifier=self.identifier,
            protocol_version=self.protocol_version,
        )


class _RecordingExcludeUnsupportedEditorialProvider(_ExcludeUnsupportedEditorialProvider):
    def __init__(self) -> None:
        self.contexts: list[EditorialContext] = []

    def prepare(self, context: EditorialContext) -> EditorialPlanProposal:
        self.contexts.append(context)
        return super().prepare(context)


class _StaticEditorialProvider:
    identifier = "fake-editorial:window-safety"
    protocol_version = "editorial-digest-plan-window-safety-test.v1"

    def __init__(self, proposals: tuple[EditorialStoryProposal, ...]) -> None:
        self._proposals = proposals

    def prepare(self, context: EditorialContext) -> EditorialPlanProposal:
        assert {item.stable_key for item in self._proposals} == {
            story.stable_key for story in context.stories
        }
        return EditorialPlanProposal(
            digest_summary="本期 Digest 对 Editorial Window 进行确定性安全归一化并保留完整证据。",
            stories=self._proposals,
            provider_identifier=self.identifier,
            protocol_version=self.protocol_version,
        )


def _editorial_context_for(stories: tuple[StoryInspection, ...]) -> EditorialContext:
    publication_date = date(2026, 8, 21)
    window_start, window_end = editorial_window_for(publication_date)
    observed_at = datetime(2026, 8, 20, 16, tzinfo=UTC)
    sources = {
        story.source_definition_id: story.publisher
        for story in stories
        if story.source_definition_id is not None
    }
    return EditorialContext(
        publication_date=publication_date,
        window_start=window_start,
        window_end=window_end,
        stories=stories,
        source_health=tuple(
            SourceHealthInspection(
                source_definition_id=source_id,
                name=f"{publisher} feed",
                publisher=publisher,
                recent_result="success",
                health="healthy",
                pause_state="active",
                consecutive_failures=0,
                updated_at=observed_at,
            )
            for source_id, publisher in sorted(sources.items(), key=lambda item: str(item[0]))
        ),
        scheduler_health=SchedulerHealthInspection(
            state="waiting",
            last_result="succeeded",
            last_completed_at=observed_at - timedelta(hours=1),
            updated_at=observed_at,
        ),
    )


def _editorial_story_proposal(
    story: StoryInspection,
    *,
    inclusion: DigestPlanInclusion,
    order: int | None,
    exclusion_reason: str | None,
) -> EditorialStoryProposal:
    return EditorialStoryProposal(
        stable_key=story.stable_key,
        inclusion=inclusion,
        order=order,
        summary=f"{story.publisher} 提供了足够完整的读者摘要，说明这项 AI 进展的核心内容。",
        why_it_matters="这项进展会影响开发者的模型选择、验证工作以及后续迁移计划。",
        primary_topic=Topic.MODELS.value,
        secondary_topics=(Topic.PRODUCTS_AND_TOOLS.value,),
        exclusion_reason=exclusion_reason,
    )


@pytest.fixture
def editorial_database_url():
    server = Pg0(name=f"ai_intel_m3_editorial_{_id('database').hex}")
    server.start()
    try:
        upgrade_database(server.uri)
        yield server.uri
    finally:
        server.drop()


def _persist_pending_stories(
    database_url: str,
    *,
    story_count: int = 10,
    tie_newest_discovery: bool = False,
) -> None:
    engine = create_database_engine(database_url)
    publishers = ("Gemini", "TechCrunch", "Hugging Face", "QbitAI")
    source_ids = tuple(_id(f"database-source:{position}") for position in range(4))
    run_id = _id("database-run")
    observed_at = datetime(2026, 8, 20, 12, tzinfo=UTC)
    persisted: list[tuple[UUID, UUID, UUID]] = []
    try:
        repository = SampleStoryRepository(engine)
        first_published_at = datetime(2026, 8, 20, 2, tzinfo=UTC)
        for position in range(story_count):
            publisher = publishers[position % len(publishers)]
            candidate_id = _id(f"database-candidate:{position}")
            document_id = _id(f"database-document:{position}")
            story_id = _id(f"database-story:{position}")
            claim_id = _id(f"database-claim:{position}")
            evidence_id = _id(f"database-evidence:{position}")
            exact_text = f"{publisher} exact persisted Evidence {position}."
            body = f"{exact_text} Additional private source text."
            published_at = first_published_at + timedelta(hours=position)
            discovered_at = (
                first_published_at + timedelta(hours=story_count - 1)
                if tie_newest_discovery and position >= story_count - 2
                else published_at
            )
            repository.persist(
                SampleStory(
                    candidate=Candidate(
                        id=candidate_id,
                        title=f"{publisher} source {position}",
                        canonical_url=f"https://example.com/persisted/{position}",
                        publisher=publisher,
                        discovered_at=discovered_at,
                    ),
                    document_version=DocumentVersion(
                        id=document_id,
                        candidate_id=candidate_id,
                        source_url=f"https://example.com/persisted/{position}",
                        title=f"{publisher} source {position}",
                        body=body,
                        content_hash=sha256(body.encode("utf-8")).hexdigest(),
                        observed_at=published_at,
                        published_at=published_at,
                        published_at_raw=published_at.isoformat(),
                    ),
                    story=Story(
                        id=story_id,
                        primary_document_version_id=document_id,
                        stable_key=f"persisted-story:{position}",
                        headline=f"{publisher} persisted AI development {position}",
                        occurred_at=published_at,
                        review_state=StoryReviewState.UNREVIEWED,
                    ),
                    claim=Claim(
                        id=claim_id,
                        story_id=story_id,
                        position=0,
                        text=f"{publisher} confirmed persisted development {position}.",
                    ),
                    evidence_span=EvidenceSpan(
                        id=evidence_id,
                        claim_id=claim_id,
                        document_version_id=document_id,
                        exact_text=exact_text,
                        start_offset=0,
                        end_offset=len(exact_text),
                        text_hash=sha256(exact_text.encode("utf-8")).hexdigest(),
                        role=(EvidenceRole.COMMUNITY if position == 0 else EvidenceRole.PRIMARY),
                        relation=EvidenceRelation.SUPPORTS,
                    ),
                    trace=StructuredTrace(
                        id=_id(f"database-trace:{position}"),
                        operation_key=f"m3-editorial-test:trace:{position}",
                        evidence_span_id=evidence_id,
                        occurred_at=published_at,
                        attributes={"provider": "deterministic-fixture"},
                    ),
                )
            )
            persisted.append((candidate_id, document_id, source_ids[position % 4]))

        with Session(engine) as session, session.begin():
            session.add(
                CollectionRunRecord(
                    id=run_id,
                    retry_of_run_id=None,
                    status="running",
                    started_at=observed_at - timedelta(minutes=5),
                    completed_at=None,
                    operation_key="m3-editorial-test:collection",
                )
            )
            session.add_all(
                SourceDefinitionRecord(
                    id=source_id,
                    name=f"{publisher} feed",
                    publisher=publisher,
                    entry_point=f"https://example.com/{position}/feed",
                    audit_version="m3-editorial-test.v1",
                    activation_conclusion="approved",
                    collection_schedule="06:00/18:00 Asia/Shanghai",
                    discovery_method="fixture",
                    language="en",
                    topic_scope=[Topic.MODELS.value],
                    access_constraints=[],
                    extraction_adapter="fixture",
                    health_policy="fixture",
                    cursor="fixture",
                    storage_policy="fixture",
                    public_excerpt_policy="fixture",
                    public_excerpt_max_characters=280,
                    pause_conditions=[],
                    canonical_url_prefixes=["https://example.com/"],
                    acceptance_group="core",
                    contribution_role="Structured Primary Record",
                    evidence_eligibility="body-valid",
                    body_eligibility="fixture body-valid",
                    pause_state="active",
                    expected_contribution="fixture",
                    overlap_rationale="fixture",
                )
                for position, (source_id, publisher) in enumerate(
                    zip(source_ids, publishers, strict=True)
                )
            )
            session.flush()
            session.add_all(
                SourceProfileStateRecord(
                    source_definition_id=source_id,
                    recent_result="success",
                    cursor_value="fixture",
                    health="healthy",
                    consecutive_failures=0,
                    last_collection_run_id=run_id,
                    updated_at=observed_at,
                    pause_state="active",
                )
                for source_id in source_ids
            )
            session.add_all(
                SourceCandidateResultRecord(
                    collection_run_id=run_id,
                    source_definition_id=source_id,
                    candidate_id=candidate_id,
                    document_version_id=document_id,
                    article_status="body-valid",
                    error_code=None,
                    error_message=None,
                    evidence_eligible=True,
                    eligibility_kind="body-valid",
                )
                for candidate_id, document_id, source_id in persisted
            )
            session.flush()
            session.execute(
                update(CollectionRunRecord)
                .where(CollectionRunRecord.id == run_id)
                .values(status="complete", completed_at=observed_at)
            )
        SchedulerStatusRepository(engine).succeeded(completed_at=observed_at)
    finally:
        engine.dispose()


def _inflate_withdrawn_research_prefix(database_url: str) -> None:
    """Put more withdrawn matches ahead of the visible legacy Story than the old cap."""
    engine = create_database_engine(database_url)
    try:
        with Session(engine) as session, session.begin():
            for position in range(1, 10):
                story_id = _id(f"database-story:{position}")
                original = session.scalar(
                    select(EvidenceSpanRecord)
                    .join(ClaimRecord, ClaimRecord.id == EvidenceSpanRecord.claim_id)
                    .where(ClaimRecord.story_id == story_id)
                    .order_by(ClaimRecord.position, EvidenceSpanRecord.start_offset)
                    .limit(1)
                )
                assert original is not None
                for extra_position in range(1, 13):
                    claim_id = _id(f"research-prefix-claim:{position}:{extra_position}")
                    session.add(
                        ClaimRecord(
                            id=claim_id,
                            story_id=story_id,
                            position=extra_position,
                            text=(
                                f"Persisted development prefix Evidence {position} "
                                f"copy {extra_position}."
                            ),
                        )
                    )
                    session.add(
                        EvidenceSpanRecord(
                            id=_id(f"research-prefix-evidence:{position}:{extra_position}"),
                            claim_id=claim_id,
                            document_version_id=original.document_version_id,
                            exact_text=original.exact_text,
                            start_offset=original.start_offset,
                            end_offset=original.end_offset,
                            text_hash=original.text_hash,
                            role=original.role,
                            relation=original.relation,
                        )
                    )
    finally:
        engine.dispose()


def _persist_visible_legacy_research_story(database_url: str) -> str:
    stable_key = "legacy-visible-research"
    observed_at = datetime(2026, 8, 19, 1, tzinfo=UTC)
    candidate_id = _id("legacy-visible-candidate")
    document_id = _id("legacy-visible-document")
    story_id = _id("legacy-visible-story")
    claim_id = _id("legacy-visible-claim")
    evidence_id = _id("legacy-visible-evidence")
    exact_text = "A visible legacy publisher confirmed a persisted development."
    body = f"{exact_text} This lower-ranked public Evidence remains available."
    engine = create_database_engine(database_url)
    try:
        SampleStoryRepository(engine).persist(
            SampleStory(
                candidate=Candidate(
                    id=candidate_id,
                    title="Visible legacy persisted development",
                    canonical_url="https://example.com/legacy-visible",
                    publisher="Visible Legacy Publisher",
                    discovered_at=observed_at,
                ),
                document_version=DocumentVersion(
                    id=document_id,
                    candidate_id=candidate_id,
                    source_url="https://example.com/legacy-visible",
                    title="Visible legacy persisted development",
                    body=body,
                    content_hash=sha256(body.encode("utf-8")).hexdigest(),
                    observed_at=observed_at,
                    published_at=observed_at,
                    published_at_raw=observed_at.isoformat(),
                ),
                story=Story(
                    id=story_id,
                    primary_document_version_id=document_id,
                    stable_key=stable_key,
                    headline="Visible legacy persisted development",
                    occurred_at=observed_at,
                    review_state=StoryReviewState.ACCEPTED,
                ),
                claim=Claim(
                    id=claim_id,
                    story_id=story_id,
                    position=0,
                    text="A visible legacy source confirmed a persisted development.",
                ),
                evidence_span=EvidenceSpan(
                    id=evidence_id,
                    claim_id=claim_id,
                    document_version_id=document_id,
                    exact_text=exact_text,
                    start_offset=0,
                    end_offset=len(exact_text),
                    text_hash=sha256(exact_text.encode("utf-8")).hexdigest(),
                    role=EvidenceRole.PRIMARY,
                    relation=EvidenceRelation.SUPPORTS,
                ),
                trace=StructuredTrace(
                    id=_id("legacy-visible-trace"),
                    operation_key="m3-editorial-test:legacy-visible-trace",
                    evidence_span_id=evidence_id,
                    occurred_at=observed_at,
                    attributes={"provider": "deterministic-fixture"},
                ),
            )
        )
        draft = compose_digest(date(2026, 8, 19), (story_id,))
        with Session(engine) as session, session.begin():
            session.add(
                DigestRecord(
                    id=draft.id,
                    stable_key=draft.stable_key,
                    publication_date=draft.publication_date,
                    state=DigestState.DRAFT.value,
                    published_at=None,
                    introduction=(
                        "This retained legacy fixture supplies one lower-ranked public result."
                    ),
                    publication_contract=DigestPublicationContract.LEGACY_FIXTURE.value,
                    digest_plan_id=None,
                )
            )
            session.add(DigestStoryRecord(digest_id=draft.id, story_id=story_id, position=0))
            session.flush()
            session.execute(
                update(DigestRecord)
                .where(DigestRecord.id == draft.id)
                .values(state=DigestState.PUBLISHED.value, published_at=observed_at)
            )
    finally:
        engine.dispose()
    return stable_key


def test_editorial_window_normalization_excludes_past_stories_before_persisting_plan() -> None:
    source_ids = tuple(_id(f"window-past-source:{position}") for position in range(4))
    publishers = ("Gemini", "TechCrunch", "Hugging Face", "QbitAI")
    window_start, _ = editorial_window_for(date(2026, 8, 21))
    stories = tuple(
        replace(
            _story(
                position,
                publisher=publishers[position % len(publishers)],
                source_id=source_ids[position % len(source_ids)],
            ),
            original_published_at=(
                window_start - timedelta(hours=4 - position)
                if position < 4
                else window_start + timedelta(hours=position - 4)
            ),
        )
        for position in range(12)
    )
    context = _editorial_context_for(stories)
    provider = _StaticEditorialProvider(
        tuple(
            _editorial_story_proposal(
                story,
                inclusion=DigestPlanInclusion.INCLUDED,
                order=position,
                exclusion_reason=None,
            )
            for position, story in enumerate(stories)
        )
    )
    prepared_at = datetime(2026, 8, 20, 16, tzinfo=UTC)

    plan = prepare_digest_plan(context, provider, version=1, prepared_at=prepared_at)

    assert tuple(story.stable_key for story in plan.excluded_stories) == tuple(
        f"story:{position}" for position in range(4)
    )
    assert {
        (story.order, story.exclusion_reason) for story in plan.excluded_stories
    } == {(None, "Source time is before the current Editorial Window.")}
    assert tuple(story.stable_key for story in plan.included_stories) == tuple(
        f"story:{position}" for position in range(4, 12)
    )
    assert tuple(story.order for story in plan.included_stories) == tuple(range(8))
    assert plan.excluded_stories[0].claims == stories[0].claims
    stale_anomalies = tuple(
        anomaly for anomaly in plan.anomalies if anomaly.code == "stale-material"
    )
    assert {anomaly.story_stable_key for anomaly in stale_anomalies} == {
        f"story:{position}" for position in range(4)
    }
    assert not any(anomaly.blocking for anomaly in stale_anomalies)
    assert not any(anomaly.blocking for anomaly in plan.anomalies)
    assert plan.current_state_hash == context.current_state_hash
    assert (
        restore_digest_plan(
            plan_id=plan.id,
            version=plan.version,
            prepared_at=plan.prepared_at,
            content_hash=plan.content_hash,
            payload=plan.content_payload(),
        )
        == plan
    )
    assert prepare_digest_plan(context, provider, version=1, prepared_at=prepared_at) == plan


def test_editorial_window_normalization_holds_future_without_upgrading_provider_decisions(
) -> None:
    source_ids = tuple(_id(f"window-future-source:{position}") for position in range(4))
    publishers = ("Gemini", "TechCrunch", "Hugging Face", "QbitAI")
    window_start, window_end = editorial_window_for(date(2026, 8, 21))
    stories = tuple(
        replace(
            _story(
                position,
                publisher=publishers[position % len(publishers)],
                source_id=source_ids[position % len(source_ids)],
            ),
            original_published_at=(
                window_end
                if position == 0
                else window_start + timedelta(hours=position - 1)
            ),
        )
        for position in range(11)
    )
    context = _editorial_context_for(stories)
    provider_order = (
        "story:7",
        "story:3",
        "story:10",
        "story:4",
        "story:0",
        "story:9",
        "story:5",
        "story:8",
        "story:6",
    )
    orders_by_key = {stable_key: order for order, stable_key in enumerate(provider_order)}
    provider = _StaticEditorialProvider(
        tuple(
            _editorial_story_proposal(
                story,
                inclusion=(
                    DigestPlanInclusion.EXCLUDED
                    if story.stable_key == "story:1"
                    else (
                        DigestPlanInclusion.HELD
                        if story.stable_key == "story:2"
                        else DigestPlanInclusion.INCLUDED
                    )
                ),
                order=orders_by_key.get(story.stable_key),
                exclusion_reason=(
                    "Provider excluded this in-window Story."
                    if story.stable_key == "story:1"
                    else (
                        "Provider held this in-window Story."
                        if story.stable_key == "story:2"
                        else None
                    )
                ),
            )
            for story in stories
        )
    )

    plan = prepare_digest_plan(
        context,
        provider,
        version=1,
        prepared_at=datetime(2026, 8, 20, 16, tzinfo=UTC),
    )

    stories_by_key = {story.stable_key: story for story in plan.stories}
    assert stories_by_key["story:0"].inclusion is DigestPlanInclusion.HELD
    assert stories_by_key["story:0"].order is None
    assert (
        stories_by_key["story:0"].exclusion_reason
        == "Source time has not entered the current Editorial Window."
    )
    assert stories_by_key["story:1"].inclusion is DigestPlanInclusion.EXCLUDED
    assert (
        stories_by_key["story:1"].exclusion_reason
        == "Provider excluded this in-window Story."
    )
    assert stories_by_key["story:2"].inclusion is DigestPlanInclusion.HELD
    assert stories_by_key["story:2"].exclusion_reason == "Provider held this in-window Story."
    expected_included_order = tuple(
        stable_key for stable_key in provider_order if stable_key != "story:0"
    )
    assert tuple(story.stable_key for story in plan.included_stories) == expected_included_order
    assert tuple(story.order for story in plan.included_stories) == tuple(range(8))
    future_anomalies = tuple(
        anomaly for anomaly in plan.anomalies if anomaly.code == "future-material"
    )
    assert len(future_anomalies) == 1
    assert future_anomalies[0].story_stable_key == "story:0"
    assert not future_anomalies[0].blocking
    assert not any(
        anomaly.code == "stale-material" and anomaly.story_stable_key == "story:0"
        for anomaly in plan.anomalies
    )
    assert not any(anomaly.blocking for anomaly in plan.anomalies)


def test_editorial_agent_prepares_one_complete_traceable_plan_without_rewriting_evidence() -> None:
    source_ids = tuple(_id(f"source:{position}") for position in range(4))
    publishers = ("Gemini", "TechCrunch", "Hugging Face", "QbitAI")
    stories = tuple(
        _story(
            position,
            publisher=publishers[position % len(publishers)],
            source_id=source_ids[position % len(source_ids)],
        )
        for position in range(10)
    )
    observed_at = datetime(2026, 8, 20, 10, tzinfo=UTC)
    window_start, window_end = editorial_window_for(date(2026, 8, 21))
    assert window_start == datetime(2026, 8, 19, 22, tzinfo=UTC)
    assert window_end == datetime(2026, 8, 20, 22, tzinfo=UTC)
    context = EditorialContext(
        publication_date=date(2026, 8, 21),
        window_start=window_start,
        window_end=window_end,
        stories=stories,
        source_health=tuple(
            SourceHealthInspection(
                source_definition_id=source_id,
                name=f"{publisher} feed",
                publisher=publisher,
                recent_result="success",
                health="healthy",
                pause_state="active",
                consecutive_failures=0,
                updated_at=observed_at,
            )
            for source_id, publisher in zip(source_ids, publishers, strict=True)
        ),
        scheduler_health=SchedulerHealthInspection(
            state="waiting",
            last_result="succeeded",
            last_completed_at=observed_at - timedelta(hours=1),
            updated_at=observed_at,
        ),
    )

    plan = prepare_digest_plan(
        context,
        _FakeEditorialProvider(),
        version=1,
        prepared_at=observed_at,
    )

    assert plan.version == 1
    assert plan.provider_identifier == "fake-editorial:v1"
    assert plan.protocol_version == "editorial-digest-plan-test.v1"
    assert len(plan.included_stories) == 9
    assert tuple(item.order for item in plan.included_stories) == tuple(range(9))
    assert len(plan.excluded_stories) == 1
    assert plan.source_coverage == publishers
    assert plan.topic_coverage == (
        Topic.MODELS.value,
        Topic.PRODUCTS_AND_TOOLS.value,
    )
    assert not any(anomaly.blocking for anomaly in plan.anomalies)
    assert len(plan.content_hash) == 64
    assert len(plan.current_state_hash) == 64
    assert (
        plan.stories[0].claims[0].evidence_spans[0].exact_text
        == stories[0].claims[0].evidence_spans[0].exact_text
    )
    assert (
        plan.stories[0].claims[0].evidence_spans[0].text_hash
        == stories[0].claims[0].evidence_spans[0].text_hash
    )
    assert (
        plan.content_payload()["stories"][0]["claims"][0]["evidence_spans"][0]["start_offset"] == 0
    )
    original_evidence = stories[0].claims[0].evidence_spans[0]
    moved_story = replace(
        stories[0],
        claims=(
            replace(
                stories[0].claims[0],
                evidence_spans=(
                    replace(
                        original_evidence,
                        start_offset=original_evidence.start_offset + 1,
                        end_offset=original_evidence.end_offset + 1,
                    ),
                ),
            ),
        ),
    )
    assert replace(context, stories=(moved_story, *stories[1:])).current_state_hash != (
        context.current_state_hash
    )


def test_versioned_editorial_provider_protocol_is_strict_and_uses_no_live_network() -> None:
    publication_date = date(2026, 8, 21)
    window_start, window_end = editorial_window_for(publication_date)
    source_ids = tuple(_id(f"provider-source:{position}") for position in range(4))
    publishers = ("Gemini", "TechCrunch", "Hugging Face", "QbitAI")
    stories = tuple(
        _story(
            position,
            publisher=publishers[position % 4],
            source_id=source_ids[position % 4],
        )
        for position in range(12)
    )
    context = EditorialContext(
        publication_date=publication_date,
        window_start=window_start,
        window_end=window_end,
        stories=stories,
        source_health=tuple(
            SourceHealthInspection(
                source_definition_id=source_id,
                name=f"Fixture source {position}",
                publisher=publishers[position],
                recent_result="success",
                health="healthy",
                pause_state="active",
                consecutive_failures=0,
                updated_at=datetime(2026, 8, 20, 12, tzinfo=UTC),
            )
            for position, source_id in enumerate(source_ids)
        ),
        scheduler_health=SchedulerHealthInspection(
            state="waiting",
            last_result="succeeded",
            last_completed_at=datetime(2026, 8, 20, 10, tzinfo=UTC),
            updated_at=datetime(2026, 8, 20, 10, tzinfo=UTC),
        ),
    )
    fake_proposal = _FakeEditorialProvider().prepare(context)
    provider_output = {
        "digest_summary": fake_proposal.digest_summary,
        "stories": [
            {
                "stable_key": item.stable_key,
                "inclusion": item.inclusion.value,
                "order": item.order,
                "summary": item.summary,
                "why_it_matters": item.why_it_matters,
                "primary_topic": item.primary_topic,
                "secondary_topics": list(item.secondary_topics),
                "exclusion_reason": item.exclusion_reason,
            }
            for item in fake_proposal.stories
        ],
    }
    observed_requests: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        observed_requests.append(payload)
        assert request.headers["Authorization"] == "Bearer fixture-key"
        assert payload["model"] == "deepseek-v4-pro"
        assert payload["thinking"] == {"type": "disabled"}
        user_message = payload["messages"][1]["content"]
        assert stories[0].claims[0].evidence_spans[0].exact_text in user_message
        return httpx.Response(
            200,
            json={
                "model": "deepseek-v4-pro",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(provider_output, ensure_ascii=False)},
                    }
                ],
            },
        )

    class Budget:
        calls = 0

        def reserve(self) -> bool:
            self.calls += 1
            return True

    budget = Budget()
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        proposal = DeepSeekEditorialPlanProvider(
            client,
            api_key="fixture-key",
            budget=budget,
        ).prepare(context)

    protocol = load_editorial_agent_protocol()
    assert proposal.stories == fake_proposal.stories
    assert proposal.provider_identifier.startswith("deepseek:v4-pro@")
    assert proposal.protocol_version == protocol.version
    assert len(protocol.content_sha256) == 64
    assert protocol.maximum_pending_stories == 12
    assert protocol.maximum_output_tokens == 4096
    assert budget.calls == 1
    assert len(observed_requests) == 1
    assert observed_requests[0]["max_tokens"] == 4096

    invalid_output = json.loads(json.dumps(provider_output))
    invalid_output["stories"][0]["evidence"] = "invented Evidence"

    def invalid_response(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "deepseek-v4-pro",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(invalid_output)},
                    }
                ],
            },
        )

    with (
        httpx.Client(transport=httpx.MockTransport(invalid_response)) as client,
        pytest.raises(ValueError, match="Story output keys"),
    ):
        DeepSeekEditorialPlanProvider(client, api_key="fixture-key").prepare(context)


def test_repository_prepares_and_approves_only_the_newest_twelve_story_batch(
    editorial_database_url: str,
) -> None:
    _persist_pending_stories(
        editorial_database_url,
        story_count=14,
        tie_newest_discovery=True,
    )
    engine = create_database_engine(editorial_database_url)
    provider = _RecordingExcludeUnsupportedEditorialProvider()
    observed_at = datetime(2026, 8, 20, 16, tzinfo=UTC)
    expected_batch = (
        "persisted-story:12",
        "persisted-story:13",
        *(f"persisted-story:{position}" for position in range(11, 1, -1)),
    )
    try:
        repository = EditorialRepository(engine)
        plan = repository.prepare_digest_plan(
            date(2026, 8, 21),
            provider=provider,
            prepared_at=observed_at,
        )

        assert len(provider.contexts) == 1
        assert tuple(story.stable_key for story in provider.contexts[0].stories) == expected_batch
        assert tuple(story.stable_key for story in plan.stories) == expected_batch
        assert len(plan.included_stories) == 12
        assert repository.pending_review_count() == 14

        digest = repository.approve_digest_plan(
            plan.id,
            expected_content_hash=plan.content_hash,
            actor_identifier="m5-editorial-operator",
            approved_at=observed_at + timedelta(minutes=1),
        )

        assert digest.story_ids == tuple(story.id for story in plan.included_stories)
        assert repository.pending_review_count() == 2
        assert {
            story.stable_key
            for story in repository.stories(review_state=StoryReviewState.UNREVIEWED)
        } == {"persisted-story:0", "persisted-story:1"}
        assert repository.story("persisted-story:12").review_state is StoryReviewState.ACCEPTED
        assert repository.story("persisted-story:13").review_state is StoryReviewState.ACCEPTED
    finally:
        engine.dispose()


def test_repository_refuses_approval_when_a_new_discovery_changes_the_selected_batch(
    editorial_database_url: str,
) -> None:
    _persist_pending_stories(editorial_database_url, story_count=14)
    engine = create_database_engine(editorial_database_url)
    provider = _RecordingExcludeUnsupportedEditorialProvider()
    observed_at = datetime(2026, 8, 20, 16, tzinfo=UTC)
    try:
        repository = EditorialRepository(engine)
        plan = repository.prepare_digest_plan(
            date(2026, 8, 21),
            provider=provider,
            prepared_at=observed_at,
        )
        assert "persisted-story:0" not in {
            story.stable_key for story in provider.contexts[0].stories
        }

        candidate_id = _id("database-candidate:newest")
        document_id = _id("database-document:newest")
        with Session(engine) as session, session.begin():
            session.add_all(
                (
                    CandidateRecord(
                        id=candidate_id,
                        title="Newest pending discovery",
                        canonical_url="https://example.com/persisted/newest",
                        publisher="Gemini",
                        discovered_at=observed_at + timedelta(hours=1),
                    ),
                    DocumentVersionRecord(
                        id=document_id,
                        candidate_id=candidate_id,
                        source_url="https://example.com/persisted/newest",
                        title="Newest pending discovery",
                        body="Newest immutable pending Story body.",
                        content_hash=sha256(b"Newest immutable pending Story body.").hexdigest(),
                        observed_at=observed_at + timedelta(hours=1),
                        published_at=observed_at + timedelta(hours=1),
                        published_at_raw=(observed_at + timedelta(hours=1)).isoformat(),
                        updated_at=None,
                        updated_at_raw=None,
                    ),
                    StoryRecord(
                        id=_id("database-story:newest"),
                        primary_document_version_id=document_id,
                        stable_key="persisted-story:newest",
                        headline="Newest pending discovery",
                        occurred_at=observed_at + timedelta(hours=1),
                        review_state=StoryReviewState.UNREVIEWED.value,
                    ),
                )
            )

        with pytest.raises(EditorialStateError, match="state changed"):
            repository.approve_digest_plan(
                plan.id,
                expected_content_hash=plan.content_hash,
                actor_identifier="m5-editorial-operator",
                approved_at=observed_at + timedelta(minutes=1),
            )

        assert repository.pending_review_count() == 15
        assert PublicPublicationRepository(engine).latest_digest() is None
    finally:
        engine.dispose()


def test_blocking_anomaly_requires_a_new_plan_version_and_old_plan_cannot_approve(
    editorial_database_url: str,
) -> None:
    _persist_pending_stories(editorial_database_url, story_count=9)
    engine = create_database_engine(editorial_database_url)
    observed_at = datetime(2026, 8, 20, 13, tzinfo=UTC)
    try:
        repository = EditorialRepository(engine)
        first = repository.prepare_digest_plan(
            date(2026, 8, 21),
            provider=_FakeEditorialProvider(),
            prepared_at=observed_at,
        )

        assert first.version == 1
        assert repository.digest_plan(first.id) == first
        assert any(
            anomaly.code == "missing-evidence" and anomaly.blocking for anomaly in first.anomalies
        )
        with pytest.raises(ValueError, match="blocking anomaly"):
            repository.approve_digest_plan(
                first.id,
                expected_content_hash=first.content_hash,
                actor_identifier="m3-operator",
                approved_at=observed_at + timedelta(minutes=1),
            )

        second = repository.prepare_digest_plan(
            date(2026, 8, 21),
            provider=_ExcludeUnsupportedEditorialProvider(),
            prepared_at=observed_at + timedelta(minutes=2),
        )

        assert second.version == 2
        assert second.content_hash != first.content_hash
        assert not any(anomaly.blocking for anomaly in second.anomalies)
        with pytest.raises(ValueError, match="latest Digest Plan version"):
            repository.approve_digest_plan(
                first.id,
                expected_content_hash=first.content_hash,
                actor_identifier="m3-operator",
                approved_at=observed_at + timedelta(minutes=3),
            )

        third = repository.prepare_digest_plan(
            date(2026, 8, 21),
            provider=_FakeEditorialProvider(),
            prepared_at=observed_at + timedelta(minutes=4),
        )
        assert third.version == 3
        assert third.content_hash == first.content_hash
        assert third.id != first.id
    finally:
        engine.dispose()


def test_one_exact_plan_approval_atomically_accepts_decisions_and_publishes_in_order(
    editorial_database_url: str,
) -> None:
    _persist_pending_stories(editorial_database_url)
    engine = create_database_engine(editorial_database_url)
    observed_at = datetime(2026, 8, 20, 14, tzinfo=UTC)
    try:
        repository = EditorialRepository(engine)
        public = PublicPublicationRepository(engine)
        plan = repository.prepare_digest_plan(
            date(2026, 8, 21),
            provider=_ExcludeUnsupportedEditorialProvider(),
            prepared_at=observed_at,
        )

        assert public.latest_digest() is None
        assert plan.digest_summary == plan.digest_summary.strip()
        assert all(item.summary == item.summary.strip() for item in plan.stories)
        assert all(item.why_it_matters == item.why_it_matters.strip() for item in plan.stories)
        approval_sql: list[str] = []

        def record_approval_sql(*args) -> None:
            approval_sql.append(args[2])

        event.listen(engine, "before_cursor_execute", record_approval_sql)
        try:
            digest = repository.approve_digest_plan(
                plan.id,
                expected_content_hash=plan.content_hash,
                actor_identifier="m3-operator",
                approved_at=observed_at + timedelta(minutes=1),
            )
        finally:
            event.remove(engine, "before_cursor_execute", record_approval_sql)

        assert digest.story_ids == tuple(item.id for item in plan.included_stories)
        assert any(statement.lstrip().startswith("LOCK TABLE") for statement in approval_sql)
        published = public.latest_digest()
        assert published is not None
        assert tuple(story.stable_key for story in published.stories) == tuple(
            item.stable_key for item in plan.included_stories
        )
        assert published.introduction == plan.digest_summary

        excluded = repository.story("persisted-story:0")
        included = repository.story("persisted-story:1")
        planned_included = next(
            item for item in plan.included_stories if item.stable_key == "persisted-story:1"
        )
        assert excluded is not None
        assert excluded.review_state is StoryReviewState.REJECTED
        assert included is not None
        assert included.review_state is StoryReviewState.ACCEPTED
        assert included.summary == planned_included.summary
        assert included.why_it_matters == planned_included.why_it_matters
        assert included.primary_topic is Topic.MODELS
        assert included.secondary_topics == (Topic.PRODUCTS_AND_TOOLS,)
        assert (
            included.claims[0].evidence_spans[0].exact_text
            == planned_included.claims[0].evidence_spans[0].exact_text
        )

        history = repository.digest_history(date(2026, 8, 21))
        assert history is not None
        assert history.plan.id == plan.id
        assert history.approval is not None
        assert history.approval.actor_identifier == "m3-operator"
        assert history.approval.content_hash == plan.content_hash
        assert history.withdrawal is None
        assert "digest-plan.prepared" in history.audit_actions
        assert "digest-plan.approved" in history.audit_actions
        assert history.audit_actions.count("story.accepted") == 9
        assert history.audit_actions.count("story.rejected") == 1
        assert "digest.published" in history.audit_actions

        retried = repository.approve_digest_plan(
            plan.id,
            expected_content_hash=plan.content_hash,
            actor_identifier="m3-operator",
            approved_at=observed_at + timedelta(minutes=2),
        )
        assert retried == digest
        assert repository.digest_history(date(2026, 8, 21)) == history
        with pytest.raises(ValueError, match="different actor"):
            repository.approve_digest_plan(
                plan.id,
                expected_content_hash=plan.content_hash,
                actor_identifier="different-operator",
                approved_at=observed_at + timedelta(minutes=3),
            )
    finally:
        engine.dispose()


def test_database_cannot_publish_an_editorial_plan_contract_without_approval(
    editorial_database_url: str,
) -> None:
    _persist_pending_stories(editorial_database_url)
    engine = create_database_engine(editorial_database_url)
    observed_at = datetime(2026, 8, 20, 14, tzinfo=UTC)
    try:
        repository = EditorialRepository(engine)
        plan = repository.prepare_digest_plan(
            date(2026, 8, 21),
            provider=_ExcludeUnsupportedEditorialProvider(),
            prepared_at=observed_at,
        )
        draft = compose_digest(
            plan.publication_date,
            tuple(item.id for item in plan.included_stories),
        )
        with Session(engine) as session:
            session.execute(
                update(StoryRecord)
                .where(StoryRecord.id.in_(draft.story_ids))
                .values(review_state=StoryReviewState.ACCEPTED.value)
            )
            session.add(
                DigestRecord(
                    id=draft.id,
                    stable_key=draft.stable_key,
                    publication_date=draft.publication_date,
                    state=DigestState.DRAFT.value,
                    published_at=None,
                    introduction=plan.digest_summary,
                    publication_contract=(DigestPublicationContract.M3_EDITORIAL_PLAN.value),
                    digest_plan_id=plan.id,
                )
            )
            session.flush()
            session.add_all(
                DigestStoryRecord(
                    digest_id=draft.id,
                    story_id=story_id,
                    position=position,
                )
                for position, story_id in enumerate(draft.story_ids)
            )
            session.flush()
            with pytest.raises(DBAPIError, match="exact Plan approval"):
                session.execute(
                    update(DigestRecord)
                    .where(DigestRecord.id == draft.id)
                    .values(
                        state=DigestState.PUBLISHED.value,
                        published_at=observed_at + timedelta(minutes=1),
                    )
                )
                session.flush()
            session.rollback()

        assert repository.digest_plan(plan.id) == plan
        assert PublicPublicationRepository(engine).latest_digest() is None
    finally:
        engine.dispose()


def test_database_requires_the_complete_approved_plan_projection(
    editorial_database_url: str,
) -> None:
    _persist_pending_stories(editorial_database_url)
    engine = create_database_engine(editorial_database_url)
    observed_at = datetime(2026, 8, 20, 14, tzinfo=UTC)
    try:
        repository = EditorialRepository(engine)
        plan = repository.prepare_digest_plan(
            date(2026, 8, 21),
            provider=_ExcludeUnsupportedEditorialProvider(),
            prepared_at=observed_at,
        )
        draft = compose_digest(
            plan.publication_date,
            tuple(item.id for item in plan.included_stories),
        )
        with Session(engine) as session:
            for item in plan.stories:
                story = session.get(StoryRecord, item.id)
                assert story is not None
                story.review_state = (
                    StoryReviewState.ACCEPTED.value
                    if item.inclusion is DigestPlanInclusion.INCLUDED
                    else StoryReviewState.REJECTED.value
                )
                if item.inclusion is DigestPlanInclusion.INCLUDED:
                    presentation = session.get(StoryPresentationRecord, item.id)
                    assert presentation is not None
                    presentation.summary = item.summary
                    presentation.why_it_matters = item.why_it_matters
                    presentation.primary_topic = item.primary_topic
                    presentation.secondary_topics = list(item.secondary_topics)
            session.add(
                DigestRecord(
                    id=draft.id,
                    stable_key=draft.stable_key,
                    publication_date=draft.publication_date,
                    state=DigestState.DRAFT.value,
                    published_at=None,
                    introduction=f"{plan.digest_summary} Tampered after approval.",
                    publication_contract=(DigestPublicationContract.M3_EDITORIAL_PLAN.value),
                    digest_plan_id=plan.id,
                )
            )
            session.flush()
            session.add_all(
                DigestStoryRecord(
                    digest_id=draft.id,
                    story_id=story_id,
                    position=position,
                )
                for position, story_id in enumerate(reversed(draft.story_ids))
            )
            session.add(
                DigestPlanApprovalRecord(
                    plan_id=plan.id,
                    digest_id=draft.id,
                    content_hash=plan.content_hash,
                    actor_identifier="m3-operator",
                    approved_at=observed_at + timedelta(minutes=1),
                )
            )
            session.flush()
            with pytest.raises(DBAPIError, match="exact approved Plan projection"):
                session.execute(
                    update(DigestRecord)
                    .where(DigestRecord.id == draft.id)
                    .values(
                        state=DigestState.PUBLISHED.value,
                        published_at=observed_at + timedelta(minutes=2),
                    )
                )
                session.flush()
            session.rollback()
    finally:
        engine.dispose()


def test_direct_story_acceptance_and_multisource_publication_are_retired(
    editorial_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _persist_pending_stories(editorial_database_url)
    monkeypatch.setenv("AI_INTEL_DATABASE_URL", editorial_database_url)
    runner = CliRunner()

    accepted = runner.invoke(
        app,
        [
            "story",
            "accept",
            "persisted-story:1",
            "--summary",
            "This direct acceptance must be rejected because the Plan owns decisions.",
            "--why-it-matters",
            "Without exact Plan approval this Story could bypass its anomaly gates.",
            "--topic",
            Topic.MODELS.value,
            "--actor",
            "m3-operator",
        ],
    )
    assert accepted.exit_code != 0
    assert "exact Digest Plan" in accepted.output
    assert "approval" in accepted.output

    engine = create_database_engine(editorial_database_url)
    try:
        selected_keys = tuple(f"persisted-story:{position}" for position in range(1, 10))
        with Session(engine) as session, session.begin():
            selected = session.scalars(
                select(StoryRecord).where(StoryRecord.stable_key.in_(selected_keys))
            ).all()
            for story in selected:
                story.review_state = StoryReviewState.ACCEPTED.value
                presentation = session.get(StoryPresentationRecord, story.id)
                assert presentation is not None
                presentation.summary = (
                    f"{story.stable_key} has enough fixture summary text for this gate."
                )
                presentation.why_it_matters = (
                    f"{story.stable_key} has enough fixture impact text for this gate."
                )
                presentation.primary_topic = Topic.MODELS.value
                presentation.secondary_topics = []
    finally:
        engine.dispose()

    arguments = [
        "digest",
        "publish",
        "--date",
        "2026-08-21",
        "--introduction",
        "This direct publication must be replaced by exact Digest Plan approval.",
        "--actor",
        "m3-operator",
    ]
    for stable_key in selected_keys:
        arguments.extend(("--story", stable_key))
    published = runner.invoke(app, arguments)
    assert published.exit_code != 0
    assert "exact Digest Plan" in published.output
    assert "approval" in published.output


def test_whole_digest_withdrawal_also_contains_visible_legacy_publications(
    editorial_database_url: str,
) -> None:
    _persist_pending_stories(editorial_database_url)
    engine = create_database_engine(editorial_database_url)
    observed_at = datetime(2026, 8, 20, 14, tzinfo=UTC)
    publication_date = date(2026, 8, 20)
    try:
        story_id = _id("database-story:1")
        draft = compose_digest(publication_date, (story_id,))
        with Session(engine) as session, session.begin():
            story = session.get(StoryRecord, story_id)
            assert story is not None
            story.review_state = StoryReviewState.ACCEPTED.value
            session.flush()
            session.add(
                DigestRecord(
                    id=draft.id,
                    stable_key=draft.stable_key,
                    publication_date=draft.publication_date,
                    state=DigestState.DRAFT.value,
                    published_at=None,
                    introduction=(
                        "This existing legacy publication must remain containable and auditable."
                    ),
                    publication_contract=DigestPublicationContract.LEGACY_FIXTURE.value,
                    digest_plan_id=None,
                )
            )
            session.add(DigestStoryRecord(digest_id=draft.id, story_id=story_id, position=0))
            session.flush()
            session.execute(
                update(DigestRecord)
                .where(DigestRecord.id == draft.id)
                .values(state=DigestState.PUBLISHED.value, published_at=observed_at)
            )

        public = PublicPublicationRepository(engine)
        assert public.digest_for_date(publication_date) is not None
        withdrawal = EditorialRepository(engine).withdraw_digest(
            publication_date,
            actor_identifier="m3-operator",
            reason="Contain this mistaken visible legacy publication without rewriting history.",
            withdrawn_at=observed_at + timedelta(minutes=1),
        )
        assert withdrawal.digest_id == draft.id
        assert public.digest_for_date(publication_date) is None
        history = EditorialRepository(engine).digest_history(publication_date)
        assert history is not None
        assert history.plan is None
        assert history.withdrawal == withdrawal
        assert "digest.withdrawn" in history.audit_actions
    finally:
        engine.dispose()


def test_changed_persisted_state_requires_a_new_immutable_plan_version(
    editorial_database_url: str,
) -> None:
    _persist_pending_stories(editorial_database_url)
    engine = create_database_engine(editorial_database_url)
    observed_at = datetime(2026, 8, 20, 14, tzinfo=UTC)
    try:
        repository = EditorialRepository(engine)
        first = repository.prepare_digest_plan(
            date(2026, 8, 21),
            provider=_ExcludeUnsupportedEditorialProvider(),
            prepared_at=observed_at,
        )
        SchedulerStatusRepository(engine).succeeded(completed_at=observed_at + timedelta(minutes=1))

        with pytest.raises(ValueError, match="state changed"):
            repository.approve_digest_plan(
                first.id,
                expected_content_hash=first.content_hash,
                actor_identifier="m3-operator",
                approved_at=observed_at + timedelta(minutes=2),
            )

        second = repository.prepare_digest_plan(
            date(2026, 8, 21),
            provider=_ExcludeUnsupportedEditorialProvider(),
            prepared_at=observed_at + timedelta(minutes=3),
        )
        assert second.version == first.version + 1
        assert second.id != first.id
        assert second.current_state_hash != first.current_state_hash

        with pytest.raises(ValueError, match="latest Digest Plan"):
            repository.approve_digest_plan(
                first.id,
                expected_content_hash=first.content_hash,
                actor_identifier="m3-operator",
                approved_at=observed_at + timedelta(minutes=4),
            )

        with Session(engine) as session:
            with pytest.raises(DBAPIError, match="Digest Plan is immutable"):
                session.execute(
                    update(DigestPlanRecord)
                    .where(DigestPlanRecord.id == second.id)
                    .values(provider_identifier="tampered")
                )
                session.flush()
            session.rollback()
            persisted_hash = session.scalar(
                text("SELECT content_hash FROM digest_plans WHERE id = :plan_id").bindparams(
                    plan_id=second.id
                )
            )
        assert persisted_hash == second.content_hash
    finally:
        engine.dispose()


def test_cli_prepares_displays_and_approves_one_exact_plan(
    editorial_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _persist_pending_stories(editorial_database_url)
    monkeypatch.setenv("AI_INTEL_DATABASE_URL", editorial_database_url)
    monkeypatch.setattr(
        cli_module,
        "_create_editorial_plan_provider",
        lambda _engine, _client: _ExcludeUnsupportedEditorialProvider(),
        raising=False,
    )
    runner = CliRunner()

    prepared = runner.invoke(
        app,
        ["digest", "plan", "prepare", "--date", "2026-08-21"],
    )
    assert prepared.exit_code == 0, prepared.output

    engine = create_database_engine(editorial_database_url)
    try:
        with Session(engine) as session:
            record = session.scalar(
                select(DigestPlanRecord).where(
                    DigestPlanRecord.publication_date == date(2026, 8, 21)
                )
            )
        assert record is not None
        plan = EditorialRepository(engine).digest_plan(record.id)
        assert plan is not None
        assert str(plan.id) in prepared.output
        assert plan.content_hash in prepared.output
        assert "Source health:" in prepared.output
        assert "Scheduler health:" in prepared.output
        assert "persisted-story:0" in prepared.output
        assert "excluded" in prepared.output
        assert "Excluded because Evidence is blocking." in prepared.output
        assert "missing-evidence" in prepared.output
        assert PublicPublicationRepository(engine).latest_digest() is None

        approved = runner.invoke(
            app,
            [
                "digest",
                "plan",
                "approve",
                str(plan.id),
                "--content-hash",
                plan.content_hash,
                "--actor",
                "m3-cli-operator",
            ],
        )
        assert approved.exit_code == 0, approved.output
        assert str(plan.id) in approved.output
        assert plan.content_hash in approved.output
        assert "published with 9 Stories" in approved.output

        history = EditorialRepository(engine).digest_history(date(2026, 8, 21))
        assert history is not None
        assert history.approval is not None
        assert history.approval.actor_identifier == "m3-cli-operator"
    finally:
        engine.dispose()


class _CitingResearchProvider:
    def __init__(self) -> None:
        self.calls = 0

    def stream(self, evidence_set):
        self.calls += 1
        evidence = evidence_set.evidence[0]
        yield json.dumps(
            {
                "answer": "已发布证据支持这项 AI 进展。",
                "citations": [
                    {
                        "story_id": str(evidence.story_id),
                        "claim_id": str(evidence.claim_id),
                        "evidence_span_id": str(evidence.evidence_span_id),
                    }
                ],
            },
            ensure_ascii=False,
        )


def test_cli_withdrawal_hides_every_public_surface_but_preserves_history(
    editorial_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _persist_pending_stories(editorial_database_url)
    _inflate_withdrawn_research_prefix(editorial_database_url)
    visible_legacy_key = _persist_visible_legacy_research_story(editorial_database_url)
    engine = create_database_engine(editorial_database_url)
    observed_at = datetime(2026, 8, 20, 14, tzinfo=UTC)
    try:
        repository = EditorialRepository(engine)
        plan = repository.prepare_digest_plan(
            date(2026, 8, 21),
            provider=_ExcludeUnsupportedEditorialProvider(),
            prepared_at=observed_at,
        )
        digest = repository.approve_digest_plan(
            plan.id,
            expected_content_hash=plan.content_hash,
            actor_identifier="m3-operator",
            approved_at=observed_at + timedelta(minutes=1),
        )
        visible_story = plan.included_stories[0]
    finally:
        engine.dispose()
    research_provider = _CitingResearchProvider()
    with TestClient(
        create_app(editorial_database_url, research_provider=research_provider)
    ) as client:
        assert visible_story.headline in client.get("/").text
        assert visible_story.headline in client.get("/archive").text
        assert visible_story.headline in client.get("/rss.xml").text
        assert client.get("/digests/2026-08-21").status_code == 200
        assert client.get(f"/stories/{visible_story.stable_key}").status_code == 200
        assert (
            visible_story.headline
            in client.get("/browse", params={"q": "persisted development"}).text
        )
        research_before = client.post(
            "/research/answer",
            json={"question": "TechCrunch persisted development"},
        )
        assert "persisted AI development" in research_before.text
        assert research_provider.calls == 1

    monkeypatch.setenv("AI_INTEL_DATABASE_URL", editorial_database_url)
    reason = "Withdraw the complete Digest because this acceptance fixture is mistaken."
    runner = CliRunner()
    withdrawn = runner.invoke(
        app,
        [
            "digest",
            "withdraw",
            "--date",
            "2026-08-21",
            "--reason",
            reason,
            "--actor",
            "m3-cli-operator",
        ],
    )
    assert withdrawn.exit_code == 0, withdrawn.output
    assert "withdrawn from public visibility" in " ".join(withdrawn.output.split())

    with TestClient(
        create_app(editorial_database_url, research_provider=research_provider)
    ) as client:
        assert visible_story.headline not in client.get("/").text
        assert visible_story.headline not in client.get("/archive").text
        assert visible_story.headline not in client.get("/rss.xml").text
        assert client.get("/digests/2026-08-21").status_code == 404
        assert client.get(f"/stories/{visible_story.stable_key}").status_code == 404
        assert (
            visible_story.headline
            not in client.get("/browse", params={"q": "persisted development"}).text
        )
        research_after = client.post(
            "/research/answer",
            json={"question": "persisted development"},
        )
        assert "insufficient-evidence" not in research_after.text
        assert visible_legacy_key in research_after.text
        assert "persisted AI development" not in research_after.text
        assert research_provider.calls == 2

    engine = create_database_engine(editorial_database_url)
    try:
        history = EditorialRepository(engine).digest_history(date(2026, 8, 21))
        assert history is not None
        assert history.digest == digest
        assert history.approval is not None
        assert history.withdrawal is not None
        assert history.withdrawal.actor_identifier == "m3-cli-operator"
        assert history.withdrawal.reason == reason
        assert "digest.withdrawn" in history.audit_actions
        with Session(engine) as session:
            assert session.get(DigestRecord, digest.id) is not None
            assert (
                len(
                    session.scalars(
                        select(DigestStoryRecord).where(DigestStoryRecord.digest_id == digest.id)
                    ).all()
                )
                == 9
            )
            withdrawal = session.get(DigestWithdrawalRecord, digest.id)
            assert withdrawal is not None
            with pytest.raises(DBAPIError, match="Digest withdrawal is immutable"):
                session.execute(
                    update(DigestWithdrawalRecord)
                    .where(DigestWithdrawalRecord.digest_id == digest.id)
                    .values(reason="tampered immutable withdrawal reason")
                )
                session.flush()
            session.rollback()

        history_output = runner.invoke(
            app,
            ["digest", "history", "--date", "2026-08-21"],
        )
        assert history_output.exit_code == 0, history_output.output
        assert plan.content_hash in history_output.output
        assert reason in history_output.output
    finally:
        engine.dispose()


def test_0009_to_0010_upgrade_preserves_predecessor_state_and_runs_cli_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = Pg0(name=f"ai_intel_m3_editorial_upgrade_{_id('upgrade-database').hex}")
    server.start()
    config = Config(str(Path("alembic.ini").resolve()))
    config.set_main_option(
        "sqlalchemy.url",
        database_url_for_alembic_config(server.uri),
    )
    try:
        command.upgrade(config, "0009")
        engine = create_database_engine(server.uri)
        predecessor_time = datetime(2026, 8, 20, 12, tzinfo=UTC)
        try:
            SchedulerStatusRepository(engine).succeeded(completed_at=predecessor_time)
        finally:
            engine.dispose()

        command.upgrade(config, "head")
        engine = create_database_engine(server.uri)
        try:
            with Session(engine) as session:
                assert (
                    session.scalar(
                        text(
                            "SELECT last_result FROM scheduler_status "
                            "WHERE scheduler_key = 'production'"
                        )
                    )
                    == "succeeded"
                )
                assert session.scalar(text("SELECT version_num FROM alembic_version")) == "0011"
                assert (
                    session.scalar(
                        text(
                            "SELECT count(*) FROM information_schema.tables "
                            "WHERE table_schema = 'public' "
                            "AND table_name IN "
                            "('digest_plans', 'digest_plan_approvals', 'digest_withdrawals')"
                        )
                    )
                    == 3
                )
                assert (
                    session.scalar(
                        text(
                            "SELECT count(*) FROM information_schema.columns "
                            "WHERE table_schema = 'public' "
                            "AND table_name = 'story_presentations' "
                            "AND column_name = 'secondary_topics'"
                        )
                    )
                    == 1
                )
        finally:
            engine.dispose()

        _persist_pending_stories(server.uri)
        monkeypatch.setenv("AI_INTEL_DATABASE_URL", server.uri)
        monkeypatch.setattr(
            cli_module,
            "_create_editorial_plan_provider",
            lambda _engine, _client: _ExcludeUnsupportedEditorialProvider(),
        )
        result = CliRunner().invoke(
            app,
            ["digest", "plan", "prepare", "--date", "2026-08-21"],
        )
        assert result.exit_code == 0, result.output
        assert "Version: 1" in result.output
        assert "blocking=false" in result.output
    finally:
        server.drop()
