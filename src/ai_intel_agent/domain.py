from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


class EvidenceRole(StrEnum):
    PRIMARY = "primary"
    INDEPENDENT = "independent"
    SECONDARY = "secondary"
    COMMUNITY = "community"


class StoryReviewState(StrEnum):
    UNREVIEWED = "unreviewed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class DigestState(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"


class CollectionRunStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


class SourceDefinitionCollectionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class AuditAction(StrEnum):
    STORY_ACCEPTED = "story.accepted"
    STORY_REJECTED = "story.rejected"
    DIGEST_COMPOSED = "digest.composed"
    DIGEST_PUBLISHED = "digest.published"


class AuditSubjectType(StrEnum):
    STORY = "story"
    DIGEST = "digest"


class Topic(StrEnum):
    MODELS = "Models"
    RESEARCH = "Research"
    PRODUCTS_AND_TOOLS = "Products and Tools"
    INDUSTRY_AND_INFRASTRUCTURE = "Industry and Infrastructure"
    BUSINESS = "Business"
    APPLICATIONS = "Applications"
    POLICY_AND_SAFETY = "Policy and Safety"
    COMMUNITY = "Community"


@dataclass(frozen=True)
class Candidate:
    id: UUID
    title: str
    canonical_url: str
    publisher: str
    discovered_at: datetime


@dataclass(frozen=True)
class DocumentVersion:
    id: UUID
    candidate_id: UUID
    source_url: str
    title: str
    body: str
    content_hash: str
    observed_at: datetime
    published_at: datetime | None = None
    published_at_raw: str | None = None
    updated_at: datetime | None = None
    updated_at_raw: str | None = None


@dataclass(frozen=True)
class ApprovedFeedSourceDefinition:
    id: UUID
    name: str
    publisher: str
    entry_point: str
    audit_version: str
    collection_schedule: str
    discovery_method: str
    language: str
    topic_scope: tuple[Topic, ...]
    access_constraints: tuple[str, ...]
    extraction_adapter: str
    health_policy: str
    cursor: str
    storage_policy: str
    public_excerpt_policy: str
    public_excerpt_max_characters: int
    pause_conditions: tuple[str, ...]
    canonical_url_prefixes: tuple[str, ...]


@dataclass(frozen=True)
class SourceDefinitionCollectionResult:
    source_definition_id: UUID
    status: SourceDefinitionCollectionStatus
    candidate_count: int
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class CollectionRun:
    id: UUID
    retry_of_run_id: UUID | None
    status: CollectionRunStatus
    started_at: datetime
    completed_at: datetime
    source_definition_results: tuple[SourceDefinitionCollectionResult, ...]


@dataclass(frozen=True)
class CollectionDiscovery:
    source_definition_id: UUID
    candidate: Candidate
    document_version: DocumentVersion


@dataclass(frozen=True)
class Story:
    id: UUID
    primary_document_version_id: UUID
    stable_key: str
    headline: str
    occurred_at: datetime
    review_state: StoryReviewState


@dataclass(frozen=True)
class Claim:
    id: UUID
    story_id: UUID
    position: int
    text: str


@dataclass(frozen=True)
class EvidenceSpan:
    id: UUID
    claim_id: UUID
    document_version_id: UUID
    exact_text: str
    start_offset: int
    end_offset: int
    text_hash: str
    role: EvidenceRole


@dataclass(frozen=True)
class StructuredTrace:
    id: UUID
    operation_key: str
    evidence_span_id: UUID
    occurred_at: datetime
    attributes: Mapping[str, Any]


@dataclass(frozen=True)
class Digest:
    id: UUID
    stable_key: str
    publication_date: date
    state: DigestState
    published_at: datetime | None
    story_ids: tuple[UUID, ...]


@dataclass(frozen=True)
class AuditEvent:
    id: UUID
    operation_key: str
    actor_identifier: str
    action: AuditAction
    subject_type: AuditSubjectType
    subject_id: UUID
    occurred_at: datetime
    sequence: int
    attributes: Mapping[str, Any]


@dataclass(frozen=True)
class SampleStory:
    candidate: Candidate
    document_version: DocumentVersion
    story: Story
    claim: Claim
    evidence_span: EvidenceSpan
    trace: StructuredTrace

    def to_markdown(self) -> str:
        return (
            "# AI Intelligence Sample\n\n"
            f"Generated at: {self.trace.occurred_at.isoformat()}\n\n"
            f"## Story: {self.story.headline}\n\n"
            f"Claim: {self.claim.text}\n\n"
            f"> {self.evidence_span.exact_text}\n\n"
            f"Source: {self.document_version.source_url}\n\n"
            f"Trace: {self.trace.id} -> Evidence Span {self.evidence_span.id}\n"
        )


@dataclass(frozen=True)
class SampleDigestPublication:
    stories: tuple[SampleStory, ...]
    digest: Digest
    audit_events: tuple[AuditEvent, ...]

    def to_markdown(self) -> str:
        stories_by_id = {sample.story.id: sample for sample in self.stories}
        story_sections = "\n".join(
            (
                f"## Story: {stories_by_id[story_id].story.headline}\n\n"
                f"Claim: {stories_by_id[story_id].claim.text}\n\n"
                f"> {stories_by_id[story_id].evidence_span.exact_text}\n\n"
                f"Source: {stories_by_id[story_id].document_version.source_url}\n\n"
                f"Trace: {stories_by_id[story_id].trace.id} -> Evidence Span "
                f"{stories_by_id[story_id].evidence_span.id}\n"
            )
            for story_id in self.digest.story_ids
        )
        return (
            "# AI Intelligence Sample Digest\n\n"
            f"Publication date: {self.digest.publication_date.isoformat()}\n\n"
            f"Digest: {self.digest.id}\n\n"
            f"{story_sections}"
        )
