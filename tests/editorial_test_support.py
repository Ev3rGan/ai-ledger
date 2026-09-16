from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import update
from sqlalchemy.orm import Session

from ai_intel_agent.domain import (
    Candidate,
    Claim,
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
    DigestPlanInclusion,
    EditorialContext,
    EditorialPlanProposal,
    EditorialStoryProposal,
)
from ai_intel_agent.persistence import (
    CollectionRunRecord,
    SampleStoryRepository,
    SchedulerStatusRepository,
    SourceCandidateResultRecord,
    SourceDefinitionRecord,
    SourceProfileStateRecord,
    create_database_engine,
)


def _support_id(name: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"m3-editorial-plan-test:{name}")


class FakeEditorialProvider:
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
            stories=tuple(proposals),
            provider_identifier=self.identifier,
            protocol_version=self.protocol_version,
        )


def persist_pending_stories(
    database_url: str,
    *,
    story_count: int = 10,
    tie_newest_discovery: bool = False,
    future_story_positions: frozenset[int] = frozenset(),
    batch: str = "",
    source_day: date = date(2026, 8, 20),
) -> None:
    engine = create_database_engine(database_url)
    publishers = ("Gemini", "TechCrunch", "Hugging Face", "QbitAI")
    scope = f"{batch}:" if batch else ""
    source_ids = tuple(_support_id(f"{scope}database-source:{position}") for position in range(4))
    run_id = _support_id(f"{scope}database-run")
    observed_at = datetime.combine(source_day, datetime.min.time(), UTC) + timedelta(hours=12)
    persisted: list[tuple[UUID, UUID, UUID]] = []
    try:
        repository = SampleStoryRepository(engine)
        first_published_at = datetime.combine(
            source_day,
            datetime.min.time(),
            UTC,
        ) + timedelta(hours=2)
        for position in range(story_count):
            publisher = publishers[position % len(publishers)]
            candidate_id = _support_id(f"{scope}database-candidate:{position}")
            document_id = _support_id(f"{scope}database-document:{position}")
            story_id = _support_id(f"{scope}database-story:{position}")
            claim_id = _support_id(f"{scope}database-claim:{position}")
            evidence_id = _support_id(f"{scope}database-evidence:{position}")
            exact_text = f"{publisher} exact persisted Evidence {position}."
            body = f"{exact_text} Additional private source text."
            published_at = (
                first_published_at + timedelta(days=1, minutes=position)
                if position in future_story_positions
                else first_published_at + timedelta(hours=position)
            )
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
                        canonical_url=f"https://example.com/{scope}persisted/{position}",
                        publisher=publisher,
                        discovered_at=discovered_at,
                    ),
                    document_version=DocumentVersion(
                        id=document_id,
                        candidate_id=candidate_id,
                        source_url=f"https://example.com/{scope}persisted/{position}",
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
                        stable_key=f"persisted-{scope}story:{position}",
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
                        id=_support_id(f"{scope}database-trace:{position}"),
                        operation_key=f"m3-editorial-test:{scope}trace:{position}",
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
                    operation_key=f"m3-editorial-test:{scope}collection",
                )
            )
            session.add_all(
                SourceDefinitionRecord(
                    id=source_id,
                    name=f"{publisher} feed",
                    publisher=publisher,
                    entry_point=f"https://example.com/{scope}{position}/feed",
                    audit_version=f"m3-editorial-test.{batch or 'default'}.v1",
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
