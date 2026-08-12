from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


class EvidenceRole(StrEnum):
    PRIMARY = "primary"
    INDEPENDENT = "independent"
    SECONDARY = "secondary"
    COMMUNITY = "community"


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


@dataclass(frozen=True)
class Story:
    id: UUID
    primary_document_version_id: UUID
    stable_key: str
    headline: str
    occurred_at: datetime


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
