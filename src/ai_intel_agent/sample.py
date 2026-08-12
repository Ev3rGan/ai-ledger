from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo

from ai_intel_agent.domain import (
    Candidate,
    Claim,
    DocumentVersion,
    EvidenceRole,
    EvidenceSpan,
    SampleStory,
    Story,
    StructuredTrace,
)

SAMPLE_VERSION = "sample-story-v1"
SAMPLE_TIME = datetime(2026, 8, 12, 6, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
SAMPLE_URL = "https://example.com/ai-agent-evidence"
SAMPLE_BODY = "示例发布者宣布：其 AI Agent 现在会记录任务轨迹，以便复现实验结果。"
SAMPLE_CLAIM = "示例发布者的 AI Agent 会记录任务轨迹。"


def _id(name: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"ai-intel-agent:{SAMPLE_VERSION}:{name}")


def _hash(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


class Clock(Protocol):
    def now(self) -> datetime: ...


class CandidateSource(Protocol):
    def fetch(self) -> Candidate: ...


@dataclass(frozen=True)
class FixedClock:
    instant: datetime = SAMPLE_TIME

    def now(self) -> datetime:
        return self.instant


@dataclass(frozen=True)
class FakeCandidateSource:
    clock: Clock

    def fetch(self) -> Candidate:
        return Candidate(
            id=_id("candidate"),
            title="AI Agent 任务轨迹示例",
            canonical_url=SAMPLE_URL,
            publisher="示例发布者",
            discovered_at=self.clock.now(),
        )


def build_sample_story(
    clock: Clock | None = None,
    candidate_source: CandidateSource | None = None,
) -> SampleStory:
    fixed_clock = clock or FixedClock()
    source = candidate_source or FakeCandidateSource(fixed_clock)
    candidate = source.fetch()
    evidence_text = "其 AI Agent 现在会记录任务轨迹"
    start_offset = SAMPLE_BODY.index(evidence_text)
    end_offset = start_offset + len(evidence_text)

    document_version = DocumentVersion(
        id=_id("document-version"),
        candidate_id=candidate.id,
        source_url=candidate.canonical_url,
        title=candidate.title,
        body=SAMPLE_BODY,
        content_hash=_hash(SAMPLE_BODY),
        observed_at=fixed_clock.now(),
    )
    story = Story(
        id=_id("story"),
        primary_document_version_id=document_version.id,
        stable_key=SAMPLE_VERSION,
        headline="AI Agent 用任务轨迹支持结果复现",
        occurred_at=fixed_clock.now(),
    )
    claim = Claim(id=_id("claim"), story_id=story.id, position=0, text=SAMPLE_CLAIM)
    evidence_span = EvidenceSpan(
        id=_id("evidence-span"),
        claim_id=claim.id,
        document_version_id=document_version.id,
        exact_text=evidence_text,
        start_offset=start_offset,
        end_offset=end_offset,
        text_hash=_hash(evidence_text),
        role=EvidenceRole.PRIMARY,
    )
    trace = StructuredTrace(
        id=_id("trace"),
        operation_key=SAMPLE_VERSION,
        evidence_span_id=evidence_span.id,
        occurred_at=fixed_clock.now(),
        attributes={
            "mode": "sample",
            "sample_version": SAMPLE_VERSION,
            "evidence_span_id": str(evidence_span.id),
        },
    )
    return SampleStory(
        candidate=candidate,
        document_version=document_version,
        story=story,
        claim=claim,
        evidence_span=evidence_span,
        trace=trace,
    )
