from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from alembic.config import Config
from dotenv import load_dotenv
from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import TSVECTOR, insert
from sqlalchemy.engine import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from ai_intel_agent.domain import (
    ApprovedFeedSourceDefinition,
    AuditAction,
    AuditEvent,
    AuditSubjectType,
    Candidate,
    Claim,
    CollectionDiscovery,
    CollectionRun,
    Digest,
    DigestState,
    DocumentVersion,
    EvidenceRelation,
    EvidenceRole,
    EvidenceSpan,
    SampleDigestPublication,
    SampleStory,
    SourceDefinitionCollectionStatus,
    SourceProfileHealth,
    Story,
    StoryReviewState,
    StructuredTrace,
    Topic,
)
from ai_intel_agent.editorial import (
    ClaimInspection,
    DigestPlan,
    DigestPlanInclusion,
    DigestPreview,
    DigestPublicationContract,
    EditorialContext,
    EditorialPlanProvider,
    EditorialStateError,
    EvidenceSpanInspection,
    SchedulerHealthInspection,
    SourceHealthInspection,
    StoryInspection,
    compose_digest,
    editorial_window_for,
    publish_digest,
    restore_digest_plan,
    review_story,
)
from ai_intel_agent.editorial import (
    prepare_digest_plan as build_digest_plan,
)
from ai_intel_agent.source_portfolio import SourcePortfolioDefinition
from alembic import command


class Base(DeclarativeBase):
    pass


class PersistableStatus(Protocol):
    value: str


class PersistableSourceRecord(Protocol):
    id: UUID
    source_definition_id: UUID
    candidate_id: UUID
    document_version_id: UUID | None
    record_kind: str
    external_id: str
    external_version: str
    canonical_url: str
    record_hash: str
    provenance: Mapping[str, Any]
    policy_metadata: Mapping[str, Any]
    structured_metadata: Mapping[str, Any]
    evidence_eligible: bool
    observed_at: datetime


class PersistableCandidateResult(Protocol):
    source_definition_id: UUID
    candidate: Candidate
    status: PersistableStatus
    evidence_eligible: bool
    eligibility_kind: str
    document_version: DocumentVersion | None
    source_record: PersistableSourceRecord | None
    error_code: str | None
    error_message: str | None


class PersistableSourceState(Protocol):
    source_definition_id: UUID
    recent_result: SourceDefinitionCollectionStatus
    cursor_value: str | None
    health: SourceProfileHealth
    consecutive_failures: int
    last_collection_run_id: UUID
    updated_at: datetime
    pause_state: str


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
    primary_document_version_id: Mapped[UUID] = mapped_column(ForeignKey("document_versions.id"))
    stable_key: Mapped[str] = mapped_column(String(255), unique=True)
    headline: Mapped[str] = mapped_column(String(500))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    review_state: Mapped[str] = mapped_column(String(32))


class StoryPresentationRecord(Base):
    __tablename__ = "story_presentations"
    __table_args__ = (
        CheckConstraint(
            "length(btrim(summary)) BETWEEN 20 AND 1000",
            name="ck_story_presentations_summary_length",
        ),
        CheckConstraint(
            "length(btrim(why_it_matters)) BETWEEN 20 AND 1000",
            name="ck_story_presentations_why_length",
        ),
        CheckConstraint(
            "primary_topic IN ('Models', 'Research', 'Products and Tools', "
            "'Industry and Infrastructure', 'Business', 'Applications', "
            "'Policy and Safety', 'Community')",
            name="ck_story_presentations_primary_topic",
        ),
    )

    story_id: Mapped[UUID] = mapped_column(ForeignKey("stories.id"), primary_key=True)
    summary: Mapped[str] = mapped_column(Text)
    why_it_matters: Mapped[str] = mapped_column(Text)
    primary_topic: Mapped[str] = mapped_column(String(64))
    secondary_topics: Mapped[list[str]] = mapped_column(JSON, default=list)


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
        CheckConstraint(
            "publication_contract IN ('legacy-fixture', 'm3-multisource', 'm3-editorial-plan')",
            name="ck_digests_publication_contract",
        ),
        CheckConstraint(
            "(publication_contract = 'm3-editorial-plan' "
            "AND digest_plan_id IS NOT NULL) OR "
            "(publication_contract <> 'm3-editorial-plan' "
            "AND digest_plan_id IS NULL)",
            name="ck_digests_editorial_plan_contract",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    stable_key: Mapped[str] = mapped_column(String(255), unique=True)
    publication_date: Mapped[date] = mapped_column(Date)
    state: Mapped[str] = mapped_column(String(32))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    introduction: Mapped[str | None] = mapped_column(Text)
    publication_contract: Mapped[str] = mapped_column(
        String(32),
        server_default=DigestPublicationContract.M3_MULTISOURCE.value,
    )
    digest_plan_id: Mapped[UUID | None] = mapped_column(ForeignKey("digest_plans.id"), unique=True)


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


class DigestPlanRecord(Base):
    __tablename__ = "digest_plans"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_digest_plans_version_positive"),
        CheckConstraint("window_end > window_start", name="ck_digest_plans_window_order"),
        CheckConstraint(
            "length(content_hash) = 64 AND length(current_state_hash) = 64",
            name="ck_digest_plans_hash_lengths",
        ),
        UniqueConstraint(
            "publication_date",
            "version",
            name="uq_digest_plans_publication_version",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    publication_date: Mapped[date] = mapped_column(Date)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer)
    prepared_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    content: Mapped[dict[str, Any]] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))
    current_state_hash: Mapped[str] = mapped_column(String(64))
    provider_identifier: Mapped[str] = mapped_column(String(255))
    protocol_version: Mapped[str] = mapped_column(String(255))


class DigestPlanApprovalRecord(Base):
    __tablename__ = "digest_plan_approvals"
    __table_args__ = (
        CheckConstraint(
            "length(content_hash) = 64",
            name="ck_digest_plan_approvals_hash_length",
        ),
    )

    plan_id: Mapped[UUID] = mapped_column(ForeignKey("digest_plans.id"), primary_key=True)
    digest_id: Mapped[UUID] = mapped_column(ForeignKey("digests.id"), unique=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    actor_identifier: Mapped[str] = mapped_column(String(255))
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DigestWithdrawalRecord(Base):
    __tablename__ = "digest_withdrawals"
    __table_args__ = (
        CheckConstraint(
            "length(btrim(reason)) BETWEEN 20 AND 1000",
            name="ck_digest_withdrawals_reason_length",
        ),
    )

    digest_id: Mapped[UUID] = mapped_column(ForeignKey("digests.id"), primary_key=True)
    actor_identifier: Mapped[str] = mapped_column(String(255))
    reason: Mapped[str] = mapped_column(Text)
    withdrawn_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SourceDefinitionRecord(Base):
    __tablename__ = "source_definitions"
    __table_args__ = (
        CheckConstraint(
            "activation_conclusion IN ('approved', 'disabled')",
            name="ck_source_definitions_activation",
        ),
        CheckConstraint(
            "evidence_eligibility IN ('body-valid', 'policy-valid-structured', 'never')",
            name="ck_source_definitions_evidence_eligibility",
        ),
        CheckConstraint(
            "pause_state IN ('active', 'authorization-required')",
            name="ck_source_definitions_pause_state",
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
    acceptance_group: Mapped[str] = mapped_column(String(32))
    contribution_role: Mapped[str] = mapped_column(String(64))
    evidence_eligibility: Mapped[str] = mapped_column(String(32))
    body_eligibility: Mapped[str] = mapped_column(Text)
    pause_state: Mapped[str] = mapped_column(String(32))
    expected_contribution: Mapped[str] = mapped_column(Text)
    overlap_rationale: Mapped[str] = mapped_column(Text)


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
    retry_of_run_id: Mapped[UUID | None] = mapped_column(ForeignKey("collection_runs.id"))
    status: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    operation_key: Mapped[str | None] = mapped_column(String(255), unique=True)


class SourceDefinitionCollectionResultRecord(Base):
    __tablename__ = "source_definition_collection_results"
    __table_args__ = (
        CheckConstraint(
            "status IN ('success', 'empty', 'invalid-format', 'access-blocked', "
            "'temporary-failure', 'succeeded', 'failed')",
            name="ck_source_definition_collection_results_status",
        ),
        CheckConstraint(
            "candidate_count >= 0",
            name="ck_source_definition_collection_results_candidate_count",
        ),
        CheckConstraint(
            "(status IN ('success', 'empty', 'succeeded') AND error_code IS NULL "
            "AND error_message IS NULL) OR (status IN ('invalid-format', "
            "'access-blocked', 'temporary-failure', 'failed') "
            "AND error_code IS NOT NULL AND error_message IS NOT NULL)",
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


class SourceProfileStateRecord(Base):
    __tablename__ = "source_profile_states"
    __table_args__ = (
        CheckConstraint(
            "recent_result IN ('success', 'empty', 'invalid-format', "
            "'access-blocked', 'temporary-failure')",
            name="ck_source_profile_states_recent_result",
        ),
        CheckConstraint(
            "health IN ('healthy', 'degraded', 'blocked')",
            name="ck_source_profile_states_health",
        ),
        CheckConstraint(
            "consecutive_failures >= 0",
            name="ck_source_profile_states_consecutive_failures",
        ),
        CheckConstraint(
            "pause_state IN ('active', 'authorization-required')",
            name="ck_source_profile_states_pause_state",
        ),
    )

    source_definition_id: Mapped[UUID] = mapped_column(
        ForeignKey("source_definitions.id"), primary_key=True
    )
    recent_result: Mapped[str] = mapped_column(String(32))
    cursor_value: Mapped[str | None] = mapped_column(Text)
    health: Mapped[str] = mapped_column(String(32))
    consecutive_failures: Mapped[int] = mapped_column(Integer)
    last_collection_run_id: Mapped[UUID] = mapped_column(ForeignKey("collection_runs.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    pause_state: Mapped[str] = mapped_column(String(32))


class SourceCandidateResultRecord(Base):
    __tablename__ = "source_candidate_results"
    __table_args__ = (
        CheckConstraint(
            "article_status IN ('body-valid', 'policy-valid-structured', "
            "'metadata-only', 'signal-only', 'invalid-format', 'access-blocked', "
            "'temporary-failure')",
            name="ck_source_candidate_results_status",
        ),
        CheckConstraint(
            "(article_status IN ('body-valid', 'policy-valid-structured') "
            "AND document_version_id IS NOT NULL AND error_code IS NULL "
            "AND error_message IS NULL AND evidence_eligible = true "
            "AND eligibility_kind = article_status) OR "
            "(article_status IN ('metadata-only', 'signal-only') "
            "AND document_version_id IS NULL AND error_code IS NULL "
            "AND error_message IS NULL AND evidence_eligible = false "
            "AND eligibility_kind = 'ineligible') OR "
            "(article_status IN ('invalid-format', 'access-blocked', "
            "'temporary-failure') AND document_version_id IS NULL "
            "AND error_code IS NOT NULL AND error_message IS NOT NULL "
            "AND evidence_eligible = false AND eligibility_kind = 'ineligible')",
            name="ck_source_candidate_results_shape",
        ),
    )

    collection_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("collection_runs.id"), primary_key=True
    )
    source_definition_id: Mapped[UUID] = mapped_column(
        ForeignKey("source_definitions.id"), primary_key=True
    )
    candidate_id: Mapped[UUID] = mapped_column(ForeignKey("candidates.id"), primary_key=True)
    document_version_id: Mapped[UUID | None] = mapped_column(ForeignKey("document_versions.id"))
    article_status: Mapped[str] = mapped_column(String(32))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    evidence_eligible: Mapped[bool] = mapped_column(Boolean)
    eligibility_kind: Mapped[str] = mapped_column(String(32))


class SourceSpecificRecordRecord(Base):
    __tablename__ = "source_specific_records"
    __table_args__ = (
        CheckConstraint(
            "(evidence_eligible = true AND document_version_id IS NOT NULL) OR "
            "(evidence_eligible = false AND document_version_id IS NULL)",
            name="ck_source_specific_records_evidence_document",
        ),
        UniqueConstraint(
            "source_definition_id",
            "record_kind",
            "external_id",
            "external_version",
            "record_hash",
            name="uq_source_specific_record_identity",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    source_definition_id: Mapped[UUID] = mapped_column(ForeignKey("source_definitions.id"))
    candidate_id: Mapped[UUID] = mapped_column(ForeignKey("candidates.id"))
    document_version_id: Mapped[UUID | None] = mapped_column(ForeignKey("document_versions.id"))
    record_kind: Mapped[str] = mapped_column(String(64))
    external_id: Mapped[str] = mapped_column(String(512))
    external_version: Mapped[str] = mapped_column(String(255))
    canonical_url: Mapped[str] = mapped_column(String(2048))
    record_hash: Mapped[str] = mapped_column(String(64))
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON)
    policy_metadata: Mapped[dict[str, Any]] = mapped_column(JSON)
    structured_metadata: Mapped[dict[str, Any]] = mapped_column(JSON)
    evidence_eligible: Mapped[bool] = mapped_column(Boolean)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


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


class AnonymousResearchUsageRecord(Base):
    __tablename__ = "anonymous_research_usage"
    __table_args__ = (
        CheckConstraint(
            "provider_calls_used >= 1",
            name="ck_anonymous_research_usage_positive",
        ),
    )

    usage_date: Mapped[date] = mapped_column(Date, primary_key=True)
    client_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider_calls_used: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MeteredProviderBudgetRecord(Base):
    __tablename__ = "metered_provider_budget"
    __table_args__ = (
        CheckConstraint(
            "reserved_cents >= 1 AND reserved_cents <= 11500",
            name="ck_metered_provider_budget_range",
        ),
    )

    billing_month: Mapped[date] = mapped_column(Date, primary_key=True)
    reserved_cents: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SchedulerStatusRecord(Base):
    __tablename__ = "scheduler_status"
    __table_args__ = (
        CheckConstraint(
            "state IN ('waiting', 'running', 'failed', 'stopped')",
            name="ck_scheduler_status_state",
        ),
        CheckConstraint(
            "last_result IS NULL OR last_result IN ('succeeded', 'failed')",
            name="ck_scheduler_status_last_result",
        ),
    )

    scheduler_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    state: Mapped[str] = mapped_column(String(32))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_result: Mapped[str | None] = mapped_column(String(32))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RetrievalIndexRecord(Base):
    __tablename__ = "retrieval_indexes"
    __table_args__ = (
        CheckConstraint(
            "state IN ('building', 'active', 'retired', 'failed')",
            name="ck_retrieval_indexes_state",
        ),
        CheckConstraint(
            "documents_indexed >= 0 AND chunks_indexed >= 0 AND embeddings_indexed >= 0",
            name="ck_retrieval_indexes_counts_nonnegative",
        ),
        CheckConstraint(
            "length(profile_sha256) = 64",
            name="ck_retrieval_indexes_profile_hash_length",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    profile_id: Mapped[str] = mapped_column(String(255))
    profile_sha256: Mapped[str] = mapped_column(String(64))
    profile_definition: Mapped[dict[str, Any]] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    documents_indexed: Mapped[int] = mapped_column(Integer, default=0)
    chunks_indexed: Mapped[int] = mapped_column(Integer, default=0)
    embeddings_indexed: Mapped[int] = mapped_column(Integer, default=0)
    fault_code: Mapped[str | None] = mapped_column(String(64))


class RetrievalChunkRecord(Base):
    __tablename__ = "retrieval_chunks"
    __table_args__ = (
        CheckConstraint(
            "start_offset >= 0 AND end_offset > start_offset",
            name="ck_retrieval_chunks_offsets",
        ),
        CheckConstraint("token_count >= 1", name="ck_retrieval_chunks_token_count"),
        CheckConstraint("length(text_hash) = 64", name="ck_retrieval_chunks_text_hash_length"),
        UniqueConstraint(
            "index_id",
            "document_version_id",
            "ordinal",
            name="uq_retrieval_chunks_index_document_ordinal",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    index_id: Mapped[UUID] = mapped_column(ForeignKey("retrieval_indexes.id", ondelete="CASCADE"))
    document_version_id: Mapped[UUID] = mapped_column(ForeignKey("document_versions.id"))
    document_type: Mapped[str] = mapped_column(String(32))
    language: Mapped[str] = mapped_column(String(32))
    ordinal: Mapped[int] = mapped_column(Integer)
    start_offset: Mapped[int] = mapped_column(Integer)
    end_offset: Mapped[int] = mapped_column(Integer)
    token_count: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    text_hash: Mapped[str] = mapped_column(String(64))
    search_vector: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('simple', text)", persisted=True),
    )
    embedding: Mapped[list[float] | None] = mapped_column(Vector(384))


class RetrievalChunkEntityRecord(Base):
    __tablename__ = "retrieval_chunk_entities"

    chunk_id: Mapped[UUID] = mapped_column(
        ForeignKey("retrieval_chunks.id", ondelete="CASCADE"), primary_key=True
    )
    normalized_name: Mapped[str] = mapped_column(String(512), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(512))
    entity_type: Mapped[str] = mapped_column(String(64))


class RetrievalRuntimeStateRecord(Base):
    __tablename__ = "retrieval_runtime_states"
    __table_args__ = (
        CheckConstraint(
            "stage IN ('index', 'embedding', 'reranker')",
            name="ck_retrieval_runtime_states_stage",
        ),
        CheckConstraint(
            "state IN ('ready', 'degraded', 'unavailable')",
            name="ck_retrieval_runtime_states_state",
        ),
    )

    stage: Mapped[str] = mapped_column(String(32), primary_key=True)
    index_id: Mapped[UUID | None] = mapped_column(ForeignKey("retrieval_indexes.id"))
    state: Mapped[str] = mapped_column(String(32))
    model_id: Mapped[str | None] = mapped_column(String(255))
    revision: Mapped[str | None] = mapped_column(String(64))
    artifact_sha256: Mapped[str | None] = mapped_column(String(64))
    fault_code: Mapped[str | None] = mapped_column(String(64))
    fault_detail: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def normalize_database_url(database_url: str) -> str:
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return database_url


def database_url_for_alembic_config(database_url: str) -> str:
    """Escape percent signs at Alembic's ConfigParser boundary."""
    return normalize_database_url(database_url).replace("%", "%%")


def create_database_engine(database_url: str) -> Engine:
    normalized_url = normalize_database_url(database_url)
    if not normalized_url.startswith("postgresql+psycopg://"):
        raise ValueError("AI_INTEL_DATABASE_URL must point to PostgreSQL")
    return create_engine(normalized_url)


class AnonymousResearchAllowanceRepository:
    """Atomically reserve one bounded Provider call for an anonymous client-day."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def reserve(
        self,
        *,
        usage_date: date,
        client_hash: str,
        daily_limit: int,
    ) -> bool:
        if daily_limit < 1:
            raise ValueError("Anonymous Research daily limit must be positive")
        if len(client_hash) != 64:
            raise ValueError("Anonymous Research client hash must be SHA-256")

        now = datetime.now(UTC)
        statement = (
            insert(AnonymousResearchUsageRecord)
            .values(
                usage_date=usage_date,
                client_hash=client_hash,
                provider_calls_used=1,
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=[
                    AnonymousResearchUsageRecord.usage_date,
                    AnonymousResearchUsageRecord.client_hash,
                ],
                set_={
                    "provider_calls_used": (AnonymousResearchUsageRecord.provider_calls_used + 1),
                    "updated_at": now,
                },
                where=(AnonymousResearchUsageRecord.provider_calls_used < daily_limit),
            )
            .returning(AnonymousResearchUsageRecord.provider_calls_used)
        )
        with Session(self._engine) as session, session.begin():
            return session.scalar(statement) is not None


class MeteredProviderBudgetRepository:
    """Reserve a conservative request cost under the aggregate USD 115 cap."""

    MAXIMUM_MONTHLY_CENTS = 11_500

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def reserve(
        self,
        *,
        billing_month: date,
        reservation_cents: int,
        monthly_limit_cents: int,
    ) -> bool:
        if billing_month.day != 1:
            raise ValueError("Provider budget month must be the first day of a month")
        if not 1 <= monthly_limit_cents <= self.MAXIMUM_MONTHLY_CENTS:
            raise ValueError("Provider monthly budget must be between 1 and 11500 cents")
        if not 1 <= reservation_cents <= monthly_limit_cents:
            raise ValueError("Provider request reservation must fit the monthly budget")

        now = datetime.now(UTC)
        statement = (
            insert(MeteredProviderBudgetRecord)
            .values(
                billing_month=billing_month,
                reserved_cents=reservation_cents,
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=[MeteredProviderBudgetRecord.billing_month],
                set_={
                    "reserved_cents": (
                        MeteredProviderBudgetRecord.reserved_cents + reservation_cents
                    ),
                    "updated_at": now,
                },
                where=(
                    MeteredProviderBudgetRecord.reserved_cents
                    <= monthly_limit_cents - reservation_cents
                ),
            )
            .returning(MeteredProviderBudgetRecord.reserved_cents)
        )
        with Session(self._engine) as session, session.begin():
            return session.scalar(statement) is not None


class PersistentMeteredProviderBudget:
    """Fail before each metered request once its conservative USD cap is spent."""

    def __init__(
        self,
        engine: Engine,
        *,
        monthly_limit_cents: int,
        request_reservation_cents: int,
        today: Callable[[], date] | None = None,
    ) -> None:
        self._repository = MeteredProviderBudgetRepository(engine)
        self._monthly_limit_cents = monthly_limit_cents
        self._request_reservation_cents = request_reservation_cents
        self._today = today or (lambda: datetime.now(UTC).date())

    def reserve(self) -> bool:
        observed_date = self._today()
        return self._repository.reserve(
            billing_month=observed_date.replace(day=1),
            reservation_cents=self._request_reservation_cents,
            monthly_limit_cents=self._monthly_limit_cents,
        )


@dataclass(frozen=True)
class SchedulerStatusSnapshot:
    state: str
    next_run_at: datetime | None
    last_started_at: datetime | None
    last_completed_at: datetime | None
    last_result: str | None
    updated_at: datetime


@dataclass(frozen=True)
class DigestPlanApprovalSnapshot:
    plan_id: UUID
    digest_id: UUID
    content_hash: str
    actor_identifier: str
    approved_at: datetime


@dataclass(frozen=True)
class DigestWithdrawalSnapshot:
    digest_id: UUID
    actor_identifier: str
    reason: str
    withdrawn_at: datetime


@dataclass(frozen=True)
class DigestHistorySnapshot:
    digest: Digest
    publication_contract: str
    plan: DigestPlan | None
    approval: DigestPlanApprovalSnapshot | None
    withdrawal: DigestWithdrawalSnapshot | None
    audit_actions: tuple[str, ...]


class SchedulerStatusRepository:
    """Persist the operator-visible status of the single production Scheduler."""

    _KEY = "production"

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def waiting(self, *, next_run_at: datetime, observed_at: datetime) -> None:
        self._upsert(
            state="waiting",
            observed_at=observed_at,
            next_run_at=next_run_at,
        )

    def running(self, *, started_at: datetime) -> None:
        self._upsert(
            state="running",
            observed_at=started_at,
            last_started_at=started_at,
        )

    def succeeded(self, *, completed_at: datetime) -> None:
        self._upsert(
            state="waiting",
            observed_at=completed_at,
            last_completed_at=completed_at,
            last_result="succeeded",
        )

    def failed(self, *, completed_at: datetime) -> None:
        self._upsert(
            state="failed",
            observed_at=completed_at,
            last_completed_at=completed_at,
            last_result="failed",
        )

    def stopped(self, *, observed_at: datetime) -> None:
        self._upsert(state="stopped", observed_at=observed_at)

    def snapshot(self) -> SchedulerStatusSnapshot | None:
        with Session(self._engine) as session:
            record = session.get(SchedulerStatusRecord, self._KEY)
            if record is None:
                return None
            return SchedulerStatusSnapshot(
                state=record.state,
                next_run_at=record.next_run_at,
                last_started_at=record.last_started_at,
                last_completed_at=record.last_completed_at,
                last_result=record.last_result,
                updated_at=record.updated_at,
            )

    def _upsert(
        self,
        *,
        state: str,
        observed_at: datetime,
        next_run_at: datetime | None = None,
        last_started_at: datetime | None = None,
        last_completed_at: datetime | None = None,
        last_result: str | None = None,
    ) -> None:
        values: dict[str, object] = {
            "scheduler_key": self._KEY,
            "state": state,
            "updated_at": observed_at,
        }
        updates: dict[str, object] = {
            "state": state,
            "updated_at": observed_at,
        }
        optional_values = {
            "next_run_at": next_run_at,
            "last_started_at": last_started_at,
            "last_completed_at": last_completed_at,
            "last_result": last_result,
        }
        for name, value in optional_values.items():
            if value is not None:
                values[name] = value
                updates[name] = value
        statement = (
            insert(SchedulerStatusRecord)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[SchedulerStatusRecord.scheduler_key],
                set_=updates,
            )
        )
        with Session(self._engine) as session, session.begin():
            session.execute(statement)


class SampleStoryRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def persist(self, sample: SampleStory) -> None:
        with Session(self._engine) as session, session.begin():
            _persist_sample_story(session, sample)


class EditorialRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def prepare_digest_plan(
        self,
        publication_date: date,
        *,
        provider: EditorialPlanProvider,
        prepared_at: datetime,
    ) -> DigestPlan:
        context = self._editorial_context(publication_date)
        with Session(self._engine) as session:
            latest = session.scalar(
                select(DigestPlanRecord)
                .where(DigestPlanRecord.publication_date == publication_date)
                .order_by(DigestPlanRecord.version.desc())
                .limit(1)
            )
            next_version = 1 if latest is None else latest.version + 1
        plan = build_digest_plan(
            context,
            provider,
            version=next_version,
            prepared_at=prepared_at,
        )
        if latest is not None and latest.content_hash == plan.content_hash:
            return self._plan(latest)
        with Session(self._engine) as session, session.begin():
            locked_latest = session.scalar(
                select(DigestPlanRecord)
                .where(DigestPlanRecord.publication_date == publication_date)
                .order_by(DigestPlanRecord.version.desc())
                .limit(1)
                .with_for_update()
            )
            if (locked_latest is None and next_version != 1) or (
                locked_latest is not None and locked_latest.version != next_version - 1
            ):
                raise EditorialStateError(
                    "Digest Plan version changed while the Editorial Agent was preparing"
                )
            if locked_latest is not None and locked_latest.content_hash == plan.content_hash:
                return self._plan(locked_latest)
            session.add(
                DigestPlanRecord(
                    id=plan.id,
                    publication_date=plan.publication_date,
                    window_start=plan.window_start,
                    window_end=plan.window_end,
                    version=plan.version,
                    prepared_at=plan.prepared_at,
                    content=plan.content_payload(),
                    content_hash=plan.content_hash,
                    current_state_hash=plan.current_state_hash,
                    provider_identifier=plan.provider_identifier,
                    protocol_version=plan.protocol_version,
                )
            )
            session.flush()
            _persist_raw_audit_event(
                session,
                operation_key=f"m3-editorial-plan:{plan.id}:prepared",
                actor_identifier=plan.provider_identifier,
                action="digest-plan.prepared",
                subject_type="digest-plan",
                subject_id=plan.id,
                occurred_at=plan.prepared_at,
                sequence=0,
                attributes={
                    "version": plan.version,
                    "content_hash": plan.content_hash,
                    "current_state_hash": plan.current_state_hash,
                    "protocol_version": plan.protocol_version,
                },
            )
        return plan

    def digest_plan(self, plan_id: UUID) -> DigestPlan | None:
        with Session(self._engine) as session:
            record = session.get(DigestPlanRecord, plan_id)
            return self._plan(record) if record is not None else None

    def approve_digest_plan(
        self,
        plan_id: UUID,
        *,
        expected_content_hash: str,
        actor_identifier: str,
        approved_at: datetime,
    ) -> Digest:
        normalized_actor = actor_identifier.strip()
        if not normalized_actor:
            raise EditorialStateError("Digest Plan approval requires an actor")
        with Session(self._engine) as session, session.begin():
            session.connection().exec_driver_sql(
                """
                LOCK TABLE
                    source_definitions,
                    collection_runs,
                    candidates,
                    document_versions,
                    source_candidate_results,
                    source_profile_states,
                    scheduler_status,
                    stories,
                    claims,
                    evidence_spans,
                    story_presentations,
                    digest_plans
                IN SHARE ROW EXCLUSIVE MODE
                """
            )
            record = session.scalar(
                select(DigestPlanRecord).where(DigestPlanRecord.id == plan_id).with_for_update()
            )
            if record is None:
                raise EditorialStateError(f"Digest Plan {plan_id} does not exist")
            plan = self._plan(record)
            if plan.content_hash != expected_content_hash:
                raise EditorialStateError("Digest Plan content hash does not match approval")
            existing_approval = session.get(DigestPlanApprovalRecord, plan_id)
            if existing_approval is not None:
                if existing_approval.content_hash != expected_content_hash:
                    raise EditorialStateError(
                        "Existing Digest Plan approval has a different content hash"
                    )
                if existing_approval.actor_identifier != normalized_actor:
                    raise EditorialStateError(
                        "Existing Digest Plan approval belongs to a different actor"
                    )
                existing_digest = session.get(
                    DigestRecord,
                    existing_approval.digest_id,
                )
                if existing_digest is None:
                    raise EditorialStateError(
                        "Existing Digest Plan approval has no publication record"
                    )
                return self._digest(session, existing_digest)

            latest = session.scalar(
                select(DigestPlanRecord)
                .where(DigestPlanRecord.publication_date == record.publication_date)
                .order_by(DigestPlanRecord.version.desc())
                .limit(1)
                .with_for_update()
            )
            if latest is None or plan.version != latest.version:
                raise EditorialStateError("Only the latest Digest Plan version may be approved")
            if any(anomaly.blocking for anomaly in plan.anomalies):
                raise EditorialStateError("A Digest Plan with a blocking anomaly cannot publish")
            current_context = self._editorial_context_in_session(
                session,
                plan.publication_date,
                lock=True,
            )
            if current_context.current_state_hash != plan.current_state_hash:
                raise EditorialStateError(
                    "Persisted Story, Evidence, Source, or Scheduler state changed; "
                    "prepare a new Plan"
                )
            existing_digest = session.scalar(
                select(DigestRecord).where(DigestRecord.publication_date == plan.publication_date)
            )
            if existing_digest is not None:
                raise EditorialStateError("An existing Digest already uses this publication date")

            included_story_ids: list[UUID] = []
            for item in plan.stories:
                story_record = session.get(StoryRecord, item.id)
                if story_record is None:
                    raise EditorialStateError(f"Story {item.stable_key!r} no longer exists")
                if item.inclusion is DigestPlanInclusion.HELD:
                    continue
                decision = (
                    StoryReviewState.ACCEPTED
                    if item.inclusion is DigestPlanInclusion.INCLUDED
                    else StoryReviewState.REJECTED
                )
                reviewed, event = review_story(
                    Story(
                        id=story_record.id,
                        primary_document_version_id=(story_record.primary_document_version_id),
                        stable_key=story_record.stable_key,
                        headline=story_record.headline,
                        occurred_at=story_record.occurred_at,
                        review_state=StoryReviewState(story_record.review_state),
                    ),
                    decision,
                    actor_identifier=normalized_actor,
                    now=approved_at,
                )
                story_record.review_state = reviewed.review_state.value
                if decision is StoryReviewState.ACCEPTED:
                    session.execute(
                        insert(StoryPresentationRecord)
                        .values(
                            story_id=story_record.id,
                            summary=item.summary,
                            why_it_matters=item.why_it_matters,
                            primary_topic=Topic(item.primary_topic).value,
                            secondary_topics=[
                                Topic(topic).value for topic in item.secondary_topics
                            ],
                        )
                        .on_conflict_do_update(
                            index_elements=[StoryPresentationRecord.story_id],
                            set_={
                                "summary": item.summary,
                                "why_it_matters": item.why_it_matters,
                                "primary_topic": Topic(item.primary_topic).value,
                                "secondary_topics": [
                                    Topic(topic).value for topic in item.secondary_topics
                                ],
                            },
                        )
                    )
                    included_story_ids.append(story_record.id)
                _persist_audit_event(session, event)

            ordered_story_ids = tuple(item.id for item in plan.included_stories)
            if set(included_story_ids) != set(ordered_story_ids):
                raise EditorialStateError("Digest Plan included Story decisions are inconsistent")
            draft = compose_digest(plan.publication_date, ordered_story_ids)
            published, digest_events = publish_digest(
                draft,
                actor_identifier=normalized_actor,
                now=approved_at,
            )
            digest_record = DigestRecord(
                id=draft.id,
                stable_key=draft.stable_key,
                publication_date=draft.publication_date,
                state=draft.state.value,
                published_at=draft.published_at,
                introduction=plan.digest_summary,
                publication_contract=DigestPublicationContract.M3_EDITORIAL_PLAN.value,
                digest_plan_id=plan.id,
            )
            session.add(digest_record)
            session.flush()
            session.add_all(
                DigestStoryRecord(
                    digest_id=draft.id,
                    story_id=story_id,
                    position=position,
                )
                for position, story_id in enumerate(ordered_story_ids)
            )
            _persist_audit_event(session, digest_events[0])
            session.flush()
            session.add(
                DigestPlanApprovalRecord(
                    plan_id=plan.id,
                    digest_id=draft.id,
                    content_hash=plan.content_hash,
                    actor_identifier=normalized_actor,
                    approved_at=approved_at,
                )
            )
            _persist_raw_audit_event(
                session,
                operation_key=f"m3-editorial-plan:{plan.id}:approved",
                actor_identifier=normalized_actor,
                action="digest-plan.approved",
                subject_type="digest-plan",
                subject_id=plan.id,
                occurred_at=approved_at,
                sequence=0,
                attributes={
                    "content_hash": plan.content_hash,
                    "digest_id": str(draft.id),
                },
            )
            session.flush()
            digest_record.state = published.state.value
            digest_record.published_at = published.published_at
            _persist_audit_event(session, digest_events[1])
            session.flush()
            return published

    def digest_history(self, publication_date: date) -> DigestHistorySnapshot | None:
        with Session(self._engine) as session:
            digest_record = session.scalar(
                select(DigestRecord)
                .where(DigestRecord.publication_date == publication_date)
                .order_by(DigestRecord.published_at.desc())
                .limit(1)
            )
            if digest_record is None:
                return None
            plan_record = (
                session.get(DigestPlanRecord, digest_record.digest_plan_id)
                if digest_record.digest_plan_id is not None
                else None
            )
            if digest_record.digest_plan_id is not None and plan_record is None:
                raise EditorialStateError("Digest publication has no persisted Plan")
            approval_record = (
                session.get(DigestPlanApprovalRecord, digest_record.digest_plan_id)
                if digest_record.digest_plan_id is not None
                else None
            )
            approval = (
                DigestPlanApprovalSnapshot(
                    plan_id=approval_record.plan_id,
                    digest_id=approval_record.digest_id,
                    content_hash=approval_record.content_hash,
                    actor_identifier=approval_record.actor_identifier,
                    approved_at=approval_record.approved_at,
                )
                if approval_record is not None
                else None
            )
            withdrawal_record = session.get(
                DigestWithdrawalRecord,
                digest_record.id,
            )
            withdrawal = (
                DigestWithdrawalSnapshot(
                    digest_id=withdrawal_record.digest_id,
                    actor_identifier=withdrawal_record.actor_identifier,
                    reason=withdrawal_record.reason,
                    withdrawn_at=withdrawal_record.withdrawn_at,
                )
                if withdrawal_record is not None
                else None
            )
            plan = self._plan(plan_record) if plan_record is not None else None
            subject_ids = {digest_record.id}
            if plan is not None:
                subject_ids.add(plan.id)
                subject_ids.update(item.id for item in plan.stories)
            audit_actions = tuple(
                session.scalars(
                    select(AuditEventRecord.action)
                    .where(AuditEventRecord.subject_id.in_(subject_ids))
                    .order_by(
                        AuditEventRecord.occurred_at,
                        AuditEventRecord.sequence,
                        AuditEventRecord.operation_key,
                    )
                )
            )
            return DigestHistorySnapshot(
                digest=self._digest(session, digest_record),
                publication_contract=digest_record.publication_contract,
                plan=plan,
                approval=approval,
                withdrawal=withdrawal,
                audit_actions=audit_actions,
            )

    def withdraw_digest(
        self,
        publication_date: date,
        *,
        actor_identifier: str,
        reason: str,
        withdrawn_at: datetime,
    ) -> DigestWithdrawalSnapshot:
        normalized_actor = actor_identifier.strip()
        normalized_reason = reason.strip()
        if not normalized_actor:
            raise EditorialStateError("Digest withdrawal requires an actor")
        if not 20 <= len(normalized_reason) <= 1000:
            raise EditorialStateError(
                "Digest withdrawal reason must contain between 20 and 1000 characters"
            )
        with Session(self._engine) as session, session.begin():
            digest = session.scalar(
                select(DigestRecord)
                .where(DigestRecord.publication_date == publication_date)
                .with_for_update()
            )
            if digest is None or digest.state != DigestState.PUBLISHED.value:
                raise EditorialStateError(
                    f"Published Digest {publication_date.isoformat()} does not exist"
                )
            existing = session.get(DigestWithdrawalRecord, digest.id)
            if existing is not None:
                if (
                    existing.actor_identifier != normalized_actor
                    or existing.reason != normalized_reason
                ):
                    raise EditorialStateError(
                        "Withdrawn Digest retry must preserve actor and reason"
                    )
                return DigestWithdrawalSnapshot(
                    digest_id=existing.digest_id,
                    actor_identifier=existing.actor_identifier,
                    reason=existing.reason,
                    withdrawn_at=existing.withdrawn_at,
                )
            session.add(
                DigestWithdrawalRecord(
                    digest_id=digest.id,
                    actor_identifier=normalized_actor,
                    reason=normalized_reason,
                    withdrawn_at=withdrawn_at,
                )
            )
            _persist_raw_audit_event(
                session,
                operation_key=f"m3-digest:{digest.id}:withdrawn",
                actor_identifier=normalized_actor,
                action="digest.withdrawn",
                subject_type=AuditSubjectType.DIGEST.value,
                subject_id=digest.id,
                occurred_at=withdrawn_at,
                sequence=0,
                attributes={
                    "publication_date": publication_date.isoformat(),
                    "reason": normalized_reason,
                },
            )
            session.flush()
            return DigestWithdrawalSnapshot(
                digest_id=digest.id,
                actor_identifier=normalized_actor,
                reason=normalized_reason,
                withdrawn_at=withdrawn_at,
            )

    def _editorial_context(self, publication_date: date) -> EditorialContext:
        with Session(self._engine) as session:
            return self._editorial_context_in_session(session, publication_date)

    def _editorial_context_in_session(
        self,
        session: Session,
        publication_date: date,
        *,
        lock: bool = False,
    ) -> EditorialContext:
        window_start, window_end = editorial_window_for(publication_date)
        story_statement = (
            select(StoryRecord.id)
            .where(StoryRecord.review_state == StoryReviewState.UNREVIEWED.value)
            .order_by(StoryRecord.occurred_at, StoryRecord.stable_key)
        )
        source_statement = (
            select(SourceProfileStateRecord, SourceDefinitionRecord)
            .join(
                SourceDefinitionRecord,
                SourceDefinitionRecord.id == SourceProfileStateRecord.source_definition_id,
            )
            .order_by(SourceDefinitionRecord.id)
        )
        scheduler_statement = select(SchedulerStatusRecord).where(
            SchedulerStatusRecord.scheduler_key == SchedulerStatusRepository._KEY
        )
        if lock:
            story_statement = story_statement.with_for_update()
            source_statement = source_statement.with_for_update()
            scheduler_statement = scheduler_statement.with_for_update()
        story_ids = session.scalars(story_statement).all()
        if lock and story_ids:
            session.scalars(
                select(StoryPresentationRecord.story_id)
                .where(StoryPresentationRecord.story_id.in_(story_ids))
                .with_for_update()
            ).all()
            claim_ids = session.scalars(
                select(ClaimRecord.id).where(ClaimRecord.story_id.in_(story_ids)).with_for_update()
            ).all()
            if claim_ids:
                session.scalars(
                    select(EvidenceSpanRecord.id)
                    .where(EvidenceSpanRecord.claim_id.in_(claim_ids))
                    .with_for_update()
                ).all()
        stories = tuple(self._story(session, story_id) for story_id in story_ids)
        source_health = tuple(
            SourceHealthInspection(
                source_definition_id=definition.id,
                name=definition.name,
                publisher=definition.publisher,
                recent_result=state.recent_result,
                health=state.health,
                pause_state=state.pause_state,
                consecutive_failures=state.consecutive_failures,
                updated_at=state.updated_at,
            )
            for state, definition in session.execute(source_statement)
        )
        scheduler = session.scalar(scheduler_statement)
        scheduler_health = (
            SchedulerHealthInspection(
                state=scheduler.state,
                last_result=scheduler.last_result,
                last_completed_at=scheduler.last_completed_at,
                updated_at=scheduler.updated_at,
            )
            if scheduler is not None
            else None
        )
        return EditorialContext(
            publication_date=publication_date,
            window_start=window_start,
            window_end=window_end,
            stories=stories,
            source_health=source_health,
            scheduler_health=scheduler_health,
        )

    @staticmethod
    def _plan(record: DigestPlanRecord) -> DigestPlan:
        plan = restore_digest_plan(
            plan_id=record.id,
            version=record.version,
            prepared_at=record.prepared_at,
            content_hash=record.content_hash,
            payload=record.content,
        )
        if (
            plan.publication_date != record.publication_date
            or plan.window_start != record.window_start
            or plan.window_end != record.window_end
            or plan.current_state_hash != record.current_state_hash
            or plan.provider_identifier != record.provider_identifier
            or plan.protocol_version != record.protocol_version
        ):
            raise EditorialStateError("Persisted Digest Plan columns do not match content")
        return plan

    def stories(
        self,
        *,
        publisher: str | None = None,
        publication_date: date | None = None,
        review_state: StoryReviewState | None = None,
    ) -> tuple[StoryInspection, ...]:
        with Session(self._engine) as session:
            statement = (
                select(StoryRecord.id)
                .join(
                    DocumentVersionRecord,
                    DocumentVersionRecord.id == StoryRecord.primary_document_version_id,
                )
                .join(
                    CandidateRecord,
                    CandidateRecord.id == DocumentVersionRecord.candidate_id,
                )
                .order_by(StoryRecord.occurred_at.desc(), StoryRecord.stable_key)
            )
            if publisher is not None:
                statement = statement.where(CandidateRecord.publisher == publisher)
            if publication_date is not None:
                statement = statement.where(
                    func.date(DocumentVersionRecord.published_at) == publication_date
                )
            if review_state is not None:
                statement = statement.where(StoryRecord.review_state == review_state.value)
            story_ids = session.scalars(statement).all()
            return tuple(self._story(session, story_id) for story_id in story_ids)

    def pending_review_count(self) -> int:
        with Session(self._engine) as session:
            count = session.scalar(
                select(func.count())
                .select_from(StoryRecord)
                .where(StoryRecord.review_state == StoryReviewState.UNREVIEWED.value)
            )
            return int(count or 0)

    def latest_published_digest(self) -> Digest | None:
        with Session(self._engine) as session:
            record = session.scalar(
                select(DigestRecord)
                .where(DigestRecord.state == DigestState.PUBLISHED.value)
                .order_by(
                    DigestRecord.publication_date.desc(),
                    DigestRecord.published_at.desc(),
                )
                .limit(1)
            )
            return self._digest(session, record) if record is not None else None

    def story(self, stable_key: str) -> StoryInspection | None:
        with Session(self._engine) as session:
            story_id = session.scalar(
                select(StoryRecord.id).where(StoryRecord.stable_key == stable_key)
            )
            return self._story(session, story_id) if story_id is not None else None

    def review(
        self,
        stable_key: str,
        decision: StoryReviewState,
        *,
        actor_identifier: str,
        occurred_at: datetime,
        summary: str | None = None,
        why_it_matters: str | None = None,
        primary_topic: Topic | None = None,
    ) -> StoryInspection:
        if decision is StoryReviewState.ACCEPTED:
            raise EditorialStateError(
                "Direct Story acceptance is retired; use exact Digest Plan approval"
            )
        with Session(self._engine) as session, session.begin():
            record = session.scalar(
                select(StoryRecord).where(StoryRecord.stable_key == stable_key).with_for_update()
            )
            if record is None:
                raise ValueError(f"Story {stable_key!r} does not exist")
            reviewed, event = review_story(
                Story(
                    id=record.id,
                    primary_document_version_id=record.primary_document_version_id,
                    stable_key=record.stable_key,
                    headline=record.headline,
                    occurred_at=record.occurred_at,
                    review_state=StoryReviewState(record.review_state),
                ),
                decision,
                actor_identifier=actor_identifier,
                now=occurred_at,
            )
            if decision is StoryReviewState.ACCEPTED:
                if summary is None or why_it_matters is None or primary_topic is None:
                    raise EditorialStateError(
                        "Accepting a Story requires summary, why-it-matters, and topic"
                    )
                record_summary = summary.strip()
                record_why = why_it_matters.strip()
                if not 20 <= len(record_summary) <= 1000:
                    raise EditorialStateError(
                        "Story summary must contain between 20 and 1000 characters"
                    )
                if not 20 <= len(record_why) <= 1000:
                    raise EditorialStateError(
                        "Story why-it-matters must contain between 20 and 1000 characters"
                    )
                session.add(
                    StoryPresentationRecord(
                        story_id=record.id,
                        summary=record_summary,
                        why_it_matters=record_why,
                        primary_topic=primary_topic.value,
                        secondary_topics=[],
                    )
                )
            record.review_state = reviewed.review_state.value
            _persist_audit_event(session, event)
            session.flush()
            return self._story(session, record.id)

    def preview_digest(
        self,
        publication_date: date,
        *,
        story_keys: tuple[str, ...] | None = None,
    ) -> DigestPreview:
        with Session(self._engine) as session:
            story_ids = (
                self._eligible_story_ids(session)
                if story_keys is None
                else self._selected_story_ids(session, story_keys)
            )
            return DigestPreview(
                publication_date=publication_date,
                stories=tuple(self._story(session, story_id) for story_id in story_ids),
            )

    def publish_digest(
        self,
        publication_date: date,
        *,
        introduction: str,
        story_keys: tuple[str, ...],
        actor_identifier: str,
        published_at: datetime,
    ) -> Digest:
        raise EditorialStateError(
            "Direct Digest publication is retired; use exact Digest Plan approval"
        )

    @staticmethod
    def _selected_story_ids(
        session: Session,
        story_keys: tuple[str, ...],
        *,
        lock: bool = False,
    ) -> tuple[UUID, ...]:
        if len(story_keys) != len(set(story_keys)):
            raise EditorialStateError("Digest Story selection cannot contain duplicates")
        if not 8 <= len(story_keys) <= 12:
            raise EditorialStateError("A Digest requires between 8 and 12 Stories")

        statement = select(StoryRecord).where(StoryRecord.stable_key.in_(story_keys))
        if lock:
            statement = statement.with_for_update()
        records = {record.stable_key: record for record in session.scalars(statement)}
        selected_ids: list[UUID] = []
        publishers: set[str] = set()
        for stable_key in story_keys:
            record = records.get(stable_key)
            if record is None:
                raise EditorialStateError(f"Story {stable_key!r} does not exist")
            if record.review_state != StoryReviewState.ACCEPTED.value:
                raise EditorialStateError(f"Story {stable_key!r} is not accepted")
            if session.get(StoryPresentationRecord, record.id) is None:
                raise EditorialStateError(
                    f"Story {stable_key!r} has no reviewed reader presentation"
                )
            if (
                session.scalar(
                    select(DigestStoryRecord.story_id).where(
                        DigestStoryRecord.story_id == record.id
                    )
                )
                is not None
            ):
                raise EditorialStateError(f"Story {stable_key!r} already belongs to a Digest")
            publisher = session.scalar(
                select(CandidateRecord.publisher)
                .join(
                    DocumentVersionRecord,
                    DocumentVersionRecord.candidate_id == CandidateRecord.id,
                )
                .where(DocumentVersionRecord.id == record.primary_document_version_id)
            )
            if publisher is None:
                raise EditorialStateError(f"Story {stable_key!r} has no primary Publisher")
            selected_ids.append(record.id)
            publishers.add(publisher)
        if len(publishers) < 3:
            raise EditorialStateError("A Digest requires Stories from at least three Publishers")
        return tuple(selected_ids)

    @staticmethod
    def _eligible_story_ids(session: Session, *, lock: bool = False) -> tuple[UUID, ...]:
        already_composed = (
            select(DigestStoryRecord.story_id)
            .where(DigestStoryRecord.story_id == StoryRecord.id)
            .exists()
        )
        statement = (
            select(StoryRecord.id)
            .where(
                StoryRecord.review_state == StoryReviewState.ACCEPTED.value,
                ~already_composed,
            )
            .order_by(StoryRecord.occurred_at, StoryRecord.stable_key)
        )
        if lock:
            statement = statement.with_for_update()
        return tuple(session.scalars(statement))

    @staticmethod
    def _digest(session: Session, record: DigestRecord) -> Digest:
        story_ids = tuple(
            session.scalars(
                select(DigestStoryRecord.story_id)
                .where(DigestStoryRecord.digest_id == record.id)
                .order_by(DigestStoryRecord.position)
            )
        )
        return Digest(
            id=record.id,
            stable_key=record.stable_key,
            publication_date=record.publication_date,
            state=DigestState(record.state),
            published_at=record.published_at,
            story_ids=story_ids,
        )

    @staticmethod
    def _story(session: Session, story_id: UUID) -> StoryInspection:
        story = session.get(StoryRecord, story_id)
        if story is None:
            raise ValueError(f"Story {story_id} does not exist")
        source = session.execute(
            select(
                CandidateRecord.publisher,
                CandidateRecord.canonical_url,
                DocumentVersionRecord.published_at,
                DocumentVersionRecord.content_hash,
            )
            .join(
                DocumentVersionRecord,
                DocumentVersionRecord.candidate_id == CandidateRecord.id,
            )
            .where(DocumentVersionRecord.id == story.primary_document_version_id)
        ).one()
        source_definition = session.execute(
            select(SourceDefinitionRecord.id, SourceDefinitionRecord.name)
            .join(
                SourceCandidateResultRecord,
                SourceCandidateResultRecord.source_definition_id == SourceDefinitionRecord.id,
            )
            .join(
                CollectionRunRecord,
                CollectionRunRecord.id == SourceCandidateResultRecord.collection_run_id,
            )
            .where(
                SourceCandidateResultRecord.document_version_id == story.primary_document_version_id
            )
            .order_by(
                CollectionRunRecord.completed_at.desc().nullslast(),
                CollectionRunRecord.started_at.desc(),
                SourceDefinitionRecord.id,
            )
            .limit(1)
        ).first()
        presentation = session.get(StoryPresentationRecord, story.id)
        claims: list[ClaimInspection] = []
        claim_records = session.scalars(
            select(ClaimRecord)
            .where(ClaimRecord.story_id == story.id)
            .order_by(ClaimRecord.position)
        ).all()
        for claim in claim_records:
            evidence_spans = tuple(
                EvidenceSpanInspection(
                    id=row.id,
                    document_version_id=row.document_version_id,
                    exact_text=row.exact_text,
                    start_offset=row.start_offset,
                    end_offset=row.end_offset,
                    text_hash=row.text_hash,
                    role=EvidenceRole(row.role),
                    relation=EvidenceRelation(row.relation),
                    publisher=row.publisher,
                    canonical_url=row.canonical_url,
                )
                for row in session.execute(
                    select(
                        EvidenceSpanRecord.id,
                        EvidenceSpanRecord.document_version_id,
                        EvidenceSpanRecord.exact_text,
                        EvidenceSpanRecord.start_offset,
                        EvidenceSpanRecord.end_offset,
                        EvidenceSpanRecord.text_hash,
                        EvidenceSpanRecord.role,
                        EvidenceSpanRecord.relation,
                        CandidateRecord.publisher,
                        CandidateRecord.canonical_url,
                    )
                    .join(
                        DocumentVersionRecord,
                        DocumentVersionRecord.id == EvidenceSpanRecord.document_version_id,
                    )
                    .join(
                        CandidateRecord,
                        CandidateRecord.id == DocumentVersionRecord.candidate_id,
                    )
                    .where(EvidenceSpanRecord.claim_id == claim.id)
                    .order_by(
                        EvidenceSpanRecord.start_offset,
                        EvidenceSpanRecord.document_version_id,
                        EvidenceSpanRecord.end_offset,
                        EvidenceSpanRecord.id,
                    )
                )
            )
            claims.append(
                ClaimInspection(
                    id=claim.id,
                    text=claim.text,
                    evidence_spans=evidence_spans,
                )
            )
        return StoryInspection(
            id=story.id,
            stable_key=story.stable_key,
            headline=story.headline,
            review_state=StoryReviewState(story.review_state),
            claims=tuple(claims),
            publisher=source.publisher,
            canonical_url=source.canonical_url,
            original_published_at=source.published_at,
            primary_document_version_id=story.primary_document_version_id,
            primary_document_content_hash=source.content_hash,
            source_definition_id=(source_definition.id if source_definition is not None else None),
            source_definition_name=(
                source_definition.name if source_definition is not None else None
            ),
            summary=presentation.summary if presentation is not None else None,
            why_it_matters=(presentation.why_it_matters if presentation is not None else None),
            primary_topic=(Topic(presentation.primary_topic) if presentation is not None else None),
            secondary_topics=(
                tuple(Topic(value) for value in presentation.secondary_topics)
                if presentation is not None
                else ()
            ),
        )


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
            source_definition.id: source_definition for source_definition in source_definitions
        }
        discoveries_by_definition: dict[UUID, list[CollectionDiscovery]] = {
            source_definition.id: [] for source_definition in source_definitions
        }
        for discovery in discoveries:
            discoveries_by_definition[discovery.source_definition_id].append(discovery)

        with Session(self._engine) as session, session.begin():
            if (
                run.retry_of_run_id is not None
                and session.get(CollectionRunRecord, run.retry_of_run_id) is None
            ):
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
                operation_key=run.operation_key,
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


@dataclass(frozen=True)
class PersistedCollectionOperation:
    collection_run_id: UUID
    operation_key: str
    status: str
    started_at: datetime
    completed_at: datetime | None
    source_results: dict[str, str]
    candidates_processed: int


@dataclass(frozen=True)
class SourceStatusSnapshot:
    source_definition_id: UUID
    name: str
    publisher: str
    recent_result: str
    cursor_value: str | None
    health: str
    consecutive_failures: int
    last_collection_run_id: UUID
    updated_at: datetime
    pending_drafts: int
    acceptance_group: str
    contribution_role: str
    evidence_eligibility: str
    body_eligibility: str
    pause_state: str


class MultiSourceCollectionRepository:
    """Persist one body-gated multi-source run and its mutable source health."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def operation(self, operation_key: str) -> PersistedCollectionOperation | None:
        with Session(self._engine) as session:
            run = session.scalar(
                select(CollectionRunRecord).where(
                    CollectionRunRecord.operation_key == operation_key
                )
            )
            if run is None:
                return None
            return self._operation_snapshot(session, run)

    def latest_operation(
        self,
        source_definition_ids: set[UUID],
    ) -> PersistedCollectionOperation | None:
        if not source_definition_ids:
            return None
        with Session(self._engine) as session:
            run = session.scalar(
                select(CollectionRunRecord)
                .join(
                    SourceDefinitionCollectionResultRecord,
                    SourceDefinitionCollectionResultRecord.collection_run_id
                    == CollectionRunRecord.id,
                )
                .where(
                    SourceDefinitionCollectionResultRecord.source_definition_id.in_(
                        source_definition_ids
                    )
                )
                .order_by(
                    CollectionRunRecord.started_at.desc(),
                    CollectionRunRecord.id.desc(),
                )
                .limit(1)
            )
            if run is None or run.operation_key is None:
                return None
            return self._operation_snapshot(session, run)

    @staticmethod
    def _operation_snapshot(
        session: Session,
        run: CollectionRunRecord,
    ) -> PersistedCollectionOperation:
        if run.operation_key is None:
            raise ValueError("A multi-source Collection Run requires an operation key")
        source_results = dict(
            session.execute(
                select(
                    SourceDefinitionRecord.name,
                    SourceDefinitionCollectionResultRecord.status,
                )
                .join(
                    SourceDefinitionRecord,
                    SourceDefinitionRecord.id
                    == SourceDefinitionCollectionResultRecord.source_definition_id,
                )
                .where(SourceDefinitionCollectionResultRecord.collection_run_id == run.id)
            ).all()
        )
        candidates_processed = session.scalar(
            select(func.count())
            .select_from(SourceCandidateResultRecord)
            .where(SourceCandidateResultRecord.collection_run_id == run.id)
        )
        return PersistedCollectionOperation(
            collection_run_id=run.id,
            operation_key=run.operation_key,
            status=run.status,
            started_at=run.started_at,
            completed_at=run.completed_at,
            source_results=source_results,
            candidates_processed=int(candidates_processed or 0),
        )

    def cursor_values(self, source_definition_ids: set[UUID]) -> dict[UUID, str]:
        if not source_definition_ids:
            return {}
        with Session(self._engine) as session:
            return {
                source_definition_id: cursor_value
                for source_definition_id, cursor_value in session.execute(
                    select(
                        SourceProfileStateRecord.source_definition_id,
                        SourceProfileStateRecord.cursor_value,
                    ).where(
                        SourceProfileStateRecord.source_definition_id.in_(source_definition_ids),
                        SourceProfileStateRecord.cursor_value.is_not(None),
                    )
                )
                if cursor_value is not None
            }

    def known_paper_identities(self) -> frozenset[tuple[str, str]]:
        with Session(self._engine) as session:
            return frozenset(
                session.execute(
                    select(
                        SourceSpecificRecordRecord.external_id,
                        SourceSpecificRecordRecord.external_version,
                    ).where(SourceSpecificRecordRecord.record_kind == "paper")
                ).all()
            )

    def known_signal_targets(self) -> frozenset[str]:
        with Session(self._engine) as session:
            return frozenset(
                session.scalars(
                    select(SourceSpecificRecordRecord.canonical_url).where(
                        SourceSpecificRecordRecord.record_kind.in_(
                            ("community-signal", "github-trending")
                        )
                    )
                ).all()
            )

    def acceptance_counts(self, collection_run_id: UUID) -> tuple[int, int]:
        with Session(self._engine) as session:
            core_results = session.scalar(
                select(
                    func.count(
                        func.distinct(SourceDefinitionCollectionResultRecord.source_definition_id)
                    )
                )
                .select_from(SourceDefinitionCollectionResultRecord)
                .join(
                    SourceDefinitionRecord,
                    SourceDefinitionRecord.id
                    == SourceDefinitionCollectionResultRecord.source_definition_id,
                )
                .where(
                    SourceDefinitionCollectionResultRecord.collection_run_id == collection_run_id,
                    SourceDefinitionRecord.acceptance_group == "core",
                )
            )
            eligible_contributors = session.scalar(
                select(func.count(func.distinct(SourceCandidateResultRecord.source_definition_id)))
                .select_from(SourceCandidateResultRecord)
                .join(
                    SourceDefinitionRecord,
                    SourceDefinitionRecord.id == SourceCandidateResultRecord.source_definition_id,
                )
                .where(
                    SourceCandidateResultRecord.collection_run_id == collection_run_id,
                    SourceCandidateResultRecord.evidence_eligible.is_(True),
                    SourceDefinitionRecord.acceptance_group == "core",
                )
            )
            return int(core_results or 0), int(eligible_contributors or 0)

    def persist(
        self,
        run: CollectionRun,
        source_definitions: tuple[ApprovedFeedSourceDefinition, ...],
        candidate_results: tuple[PersistableCandidateResult, ...],
        states: tuple[PersistableSourceState, ...],
    ) -> bool:
        definitions_by_id = {
            source_definition.id: source_definition for source_definition in source_definitions
        }
        with Session(self._engine) as session, session.begin():
            if run.operation_key is None:
                raise ValueError("A multi-source Collection Run requires an operation key")
            for source_definition in source_definitions:
                _persist_source_definition(session, source_definition)
            inserted_run_id = session.scalar(
                insert(CollectionRunRecord)
                .values(
                    id=run.id,
                    retry_of_run_id=run.retry_of_run_id,
                    status="running",
                    started_at=run.started_at,
                    completed_at=None,
                    operation_key=run.operation_key,
                )
                .on_conflict_do_nothing(index_elements=["operation_key"])
                .returning(CollectionRunRecord.id)
            )
            if inserted_run_id is None:
                return False
            run_record = session.get(CollectionRunRecord, inserted_run_id)
            if run_record is None:
                raise RuntimeError("Inserted Collection Run could not be loaded")
            for result in run.source_definition_results:
                if result.source_definition_id not in definitions_by_id:
                    raise ValueError("Collection result has no Source Profile")
                session.add(
                    SourceDefinitionCollectionResultRecord(
                        collection_run_id=run.id,
                        source_definition_id=result.source_definition_id,
                        status=result.status.value,
                        candidate_count=result.candidate_count,
                        error_code=result.error_code,
                        error_message=result.error_message,
                    )
                )
            for candidate_result in candidate_results:
                self._persist_candidate_result(session, run.id, candidate_result)
            session.flush()
            for state in states:
                if state.last_collection_run_id != run.id:
                    raise ValueError("Source Profile state points to another Collection Run")
                values = {
                    "recent_result": state.recent_result.value,
                    "cursor_value": state.cursor_value,
                    "health": state.health.value,
                    "consecutive_failures": state.consecutive_failures,
                    "last_collection_run_id": state.last_collection_run_id,
                    "updated_at": state.updated_at,
                    "pause_state": state.pause_state,
                }
                existing = session.get(SourceProfileStateRecord, state.source_definition_id)
                if existing is None:
                    session.add(
                        SourceProfileStateRecord(
                            source_definition_id=state.source_definition_id,
                            **values,
                        )
                    )
                else:
                    for key, value in values.items():
                        setattr(existing, key, value)
            session.flush()
            run_record.status = run.status.value
            run_record.completed_at = run.completed_at
            return True

    @staticmethod
    def _persist_candidate_result(
        session: Session,
        collection_run_id: UUID,
        result: PersistableCandidateResult,
    ) -> None:
        candidate = result.candidate
        document = result.document_version
        if document is not None:
            _persist_collection_discovery(
                session,
                collection_run_id,
                CollectionDiscovery(
                    source_definition_id=result.source_definition_id,
                    candidate=candidate,
                    document_version=document,
                ),
            )
        else:
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
                    f"Candidate URL {candidate.canonical_url} belongs to another identity"
                )
        session.add(
            SourceCandidateResultRecord(
                collection_run_id=collection_run_id,
                source_definition_id=result.source_definition_id,
                candidate_id=candidate.id,
                document_version_id=document.id if document is not None else None,
                article_status=result.status.value,
                error_code=result.error_code,
                error_message=result.error_message,
                evidence_eligible=result.evidence_eligible,
                eligibility_kind=result.eligibility_kind,
            )
        )
        source_record = result.source_record
        if source_record is not None:
            record_values = {
                "id": source_record.id,
                "source_definition_id": source_record.source_definition_id,
                "candidate_id": source_record.candidate_id,
                "document_version_id": source_record.document_version_id,
                "record_kind": source_record.record_kind,
                "external_id": source_record.external_id,
                "external_version": source_record.external_version,
                "canonical_url": source_record.canonical_url,
                "record_hash": source_record.record_hash,
                "provenance": dict(source_record.provenance),
                "policy_metadata": dict(source_record.policy_metadata),
                "structured_metadata": dict(source_record.structured_metadata),
                "evidence_eligible": source_record.evidence_eligible,
                "observed_at": source_record.observed_at,
            }
            persisted_record_id = session.scalar(
                insert(SourceSpecificRecordRecord)
                .values(**record_values)
                .on_conflict_do_nothing(constraint="uq_source_specific_record_identity")
                .returning(SourceSpecificRecordRecord.id)
            )
            if persisted_record_id is None:
                persisted_record_id = session.scalar(
                    select(SourceSpecificRecordRecord.id).where(
                        SourceSpecificRecordRecord.source_definition_id
                        == source_record.source_definition_id,
                        SourceSpecificRecordRecord.record_kind == source_record.record_kind,
                        SourceSpecificRecordRecord.external_id == source_record.external_id,
                        SourceSpecificRecordRecord.external_version
                        == source_record.external_version,
                        SourceSpecificRecordRecord.record_hash == source_record.record_hash,
                    )
                )
            if persisted_record_id != source_record.id:
                raise ValueError("Source-specific record identity differs from its policy")

    def source_statuses(
        self,
        source_definition_ids: set[UUID],
    ) -> tuple[SourceStatusSnapshot, ...]:
        if not source_definition_ids:
            return ()
        with Session(self._engine) as session:
            rows = session.execute(
                select(SourceProfileStateRecord, SourceDefinitionRecord)
                .join(
                    SourceDefinitionRecord,
                    SourceDefinitionRecord.id == SourceProfileStateRecord.source_definition_id,
                )
                .where(SourceProfileStateRecord.source_definition_id.in_(source_definition_ids))
                .order_by(SourceDefinitionRecord.name)
            ).all()
            snapshots: list[SourceStatusSnapshot] = []
            for state, definition in rows:
                pending_drafts = session.scalar(
                    select(
                        func.count(func.distinct(SourceCandidateResultRecord.document_version_id))
                    )
                    .select_from(SourceCandidateResultRecord)
                    .outerjoin(
                        StoryRecord,
                        StoryRecord.primary_document_version_id
                        == SourceCandidateResultRecord.document_version_id,
                    )
                    .where(
                        SourceCandidateResultRecord.source_definition_id == definition.id,
                        SourceCandidateResultRecord.evidence_eligible.is_(True),
                        SourceCandidateResultRecord.document_version_id.is_not(None),
                        StoryRecord.id.is_(None),
                    )
                )
                snapshots.append(
                    SourceStatusSnapshot(
                        source_definition_id=definition.id,
                        name=definition.name,
                        publisher=definition.publisher,
                        recent_result=state.recent_result,
                        cursor_value=state.cursor_value,
                        health=state.health,
                        consecutive_failures=state.consecutive_failures,
                        last_collection_run_id=state.last_collection_run_id,
                        updated_at=state.updated_at,
                        pending_drafts=int(pending_drafts or 0),
                        acceptance_group=definition.acceptance_group,
                        contribution_role=definition.contribution_role,
                        evidence_eligibility=definition.evidence_eligibility,
                        body_eligibility=definition.body_eligibility,
                        pause_state=state.pause_state,
                    )
                )
            return tuple(snapshots)

    def pending_draft_documents(
        self,
        source_definition_ids: set[UUID],
    ) -> tuple[DocumentVersion, ...]:
        if not source_definition_ids:
            return ()
        pending_document_ids = (
            select(SourceCandidateResultRecord.document_version_id)
            .outerjoin(
                StoryRecord,
                StoryRecord.primary_document_version_id
                == SourceCandidateResultRecord.document_version_id,
            )
            .where(
                SourceCandidateResultRecord.source_definition_id.in_(source_definition_ids),
                SourceCandidateResultRecord.evidence_eligible.is_(True),
                SourceCandidateResultRecord.document_version_id.is_not(None),
                StoryRecord.id.is_(None),
            )
            .distinct()
        )
        with Session(self._engine) as session:
            records = session.scalars(
                select(DocumentVersionRecord)
                .where(DocumentVersionRecord.id.in_(pending_document_ids))
                .order_by(DocumentVersionRecord.observed_at, DocumentVersionRecord.id)
            ).all()
            return tuple(
                DocumentVersion(
                    id=record.id,
                    candidate_id=record.candidate_id,
                    source_url=record.source_url,
                    title=record.title,
                    body=record.body,
                    content_hash=record.content_hash,
                    observed_at=record.observed_at,
                    published_at=record.published_at,
                    published_at_raw=record.published_at_raw,
                    updated_at=record.updated_at,
                    updated_at_raw=record.updated_at_raw,
                )
                for record in records
            )


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
                        StoryRecord.primary_document_version_id == DocumentVersionRecord.id,
                    )
                    .where(DocumentVersionRecord.candidate_id == candidate_id)
                )
                is not None
            )

    def has_draft_for_document_version(self, document_version_id: UUID) -> bool:
        with Session(self._engine) as session:
            return (
                session.scalar(
                    select(StoryRecord.id).where(
                        StoryRecord.primary_document_version_id == document_version_id
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
        if not claims or not (len(claims) == len(evidence_spans) == len(traces)):
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
                    or evidence.text_hash != sha256(evidence.exact_text.encode("utf-8")).hexdigest()
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
    if isinstance(source_definition, SourcePortfolioDefinition):
        activation_conclusion = source_definition.activation_conclusion
        acceptance_group = source_definition.acceptance_group
        contribution_role = source_definition.contribution_role
        evidence_eligibility = source_definition.evidence_eligibility
        body_eligibility = source_definition.body_eligibility
        pause_state = source_definition.pause_state
        expected_contribution = source_definition.expected_contribution
        overlap_rationale = source_definition.overlap_rationale
    else:
        activation_conclusion = "approved"
        acceptance_group = "legacy"
        contribution_role = "Legacy"
        evidence_eligibility = "body-valid"
        body_eligibility = "Legacy body-valid policy"
        pause_state = "active"
        expected_contribution = "Legacy source contribution"
        overlap_rationale = "Legacy source overlap policy"
    values = {
        "id": source_definition.id,
        "name": source_definition.name,
        "publisher": source_definition.publisher,
        "entry_point": source_definition.entry_point,
        "audit_version": source_definition.audit_version,
        "activation_conclusion": activation_conclusion,
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
        "public_excerpt_max_characters": (source_definition.public_excerpt_max_characters),
        "pause_conditions": list(source_definition.pause_conditions),
        "canonical_url_prefixes": list(source_definition.canonical_url_prefixes),
        "acceptance_group": acceptance_group,
        "contribution_role": contribution_role,
        "evidence_eligibility": evidence_eligibility,
        "body_eligibility": body_eligibility,
        "pause_state": pause_state,
        "expected_contribution": expected_contribution,
        "overlap_rationale": overlap_rationale,
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
    session.execute(insert(CandidateRecord).values(**candidate.__dict__).on_conflict_do_nothing())
    persisted_candidate_id = session.scalar(
        select(CandidateRecord.id).where(CandidateRecord.canonical_url == candidate.canonical_url)
    )
    if persisted_candidate_id != candidate.id:
        raise ValueError(
            f"Candidate URL {candidate.canonical_url} already belongs to another identity"
        )

    session.execute(
        insert(DocumentVersionRecord).values(**document_version.__dict__).on_conflict_do_nothing()
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
    if session.get(StoryPresentationRecord, sample.story.id) is None:
        session.execute(
            insert(StoryPresentationRecord).values(
                story_id=sample.story.id,
                summary=f"{sample.story.headline}：{sample.claim.text}",
                why_it_matters=(
                    f"这条信息帮助读者理解 {sample.story.headline} 对 AI 开发与研究的影响。"
                ),
                primary_topic=Topic.PRODUCTS_AND_TOOLS.value,
            )
        )


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
            publication_contract=DigestPublicationContract.LEGACY_FIXTURE.value,
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


def _persist_raw_audit_event(
    session: Session,
    *,
    operation_key: str,
    actor_identifier: str,
    action: str,
    subject_type: str,
    subject_id: UUID,
    occurred_at: datetime,
    sequence: int,
    attributes: Mapping[str, Any],
) -> None:
    session.execute(
        insert(AuditEventRecord).values(
            id=uuid5(
                NAMESPACE_URL,
                f"ai-intel-agent:audit-event:{operation_key}:{sequence}",
            ),
            operation_key=operation_key,
            actor_identifier=actor_identifier,
            action=action,
            subject_type=subject_type,
            subject_id=subject_id,
            occurred_at=occurred_at,
            sequence=sequence,
            attributes=dict(attributes),
        )
    )


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
        and existing.publication_contract == DigestPublicationContract.LEGACY_FIXTURE.value
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
    configured_root = os.getenv("AI_INTEL_PROJECT_ROOT", "").strip()
    project_root = (
        Path(configured_root).resolve() if configured_root else Path(__file__).resolve().parents[2]
    )
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option(
        "sqlalchemy.url",
        database_url_for_alembic_config(database_url),
    )
    command.upgrade(config, "head")


def database_url_from_environment() -> str:
    database_url = os.getenv("AI_INTEL_DATABASE_URL")
    if not database_url and os.getenv("AI_INTEL_DATABASE_HOST"):
        from ai_intel_agent.runtime import production_database_url

        return production_database_url(os.environ)
    if not database_url:
        load_dotenv()
        database_url = os.getenv("AI_INTEL_DATABASE_URL")
    if not database_url:
        raise ValueError("Set AI_INTEL_DATABASE_URL to a PostgreSQL connection URL")
    return database_url
