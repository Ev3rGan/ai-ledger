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
    StoryReviewState,
    StructuredTrace,
)

SAMPLE_VERSION = "sample-story-v1"
SAMPLE_TIME = datetime(2026, 8, 12, 6, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
SAMPLE_URL = "https://example.com/ai-agent-evidence"
SAMPLE_BODY = "示例发布者宣布：其 AI Agent 现在会记录任务轨迹，以便复现实验结果。"
SAMPLE_CLAIM = "示例发布者的 AI Agent 会记录任务轨迹。"


def _id(name: str, version: str = SAMPLE_VERSION) -> UUID:
    return uuid5(NAMESPACE_URL, f"ai-intel-agent:{version}:{name}")


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


@dataclass(frozen=True)
class FakeAdministrator:
    identifier: str = "fake-administrator"

    def review_state_for(self, story: Story) -> StoryReviewState | None:
        decisions = {
            SAMPLE_VERSION: StoryReviewState.ACCEPTED,
            f"{SAMPLE_VERSION}-rejected": StoryReviewState.REJECTED,
        }
        return decisions.get(story.stable_key)


def build_sample_story(
    clock: Clock | None = None,
    candidate_source: CandidateSource | None = None,
) -> SampleStory:
    fixed_clock = clock or FixedClock()
    source = candidate_source or FakeCandidateSource(fixed_clock)
    return _assemble_sample_story(
        version=SAMPLE_VERSION,
        candidate=source.fetch(),
        body=SAMPLE_BODY,
        claim_text=SAMPLE_CLAIM,
        evidence_text="其 AI Agent 现在会记录任务轨迹",
        headline="AI Agent 用任务轨迹支持结果复现",
        clock=fixed_clock,
    )


def _build_fixture_sample_story(
    *,
    version: str,
    title: str,
    body: str,
    claim_text: str,
    evidence_text: str,
    headline: str,
    source_url: str,
    clock: Clock,
) -> SampleStory:
    candidate = Candidate(
        id=_id("candidate", version),
        title=title,
        canonical_url=source_url,
        publisher="示例发布者",
        discovered_at=clock.now(),
    )
    return _assemble_sample_story(
        version=version,
        candidate=candidate,
        body=body,
        claim_text=claim_text,
        evidence_text=evidence_text,
        headline=headline,
        clock=clock,
    )


def _assemble_sample_story(
    *,
    version: str,
    candidate: Candidate,
    body: str,
    claim_text: str,
    evidence_text: str,
    headline: str,
    clock: Clock,
) -> SampleStory:
    start_offset = body.index(evidence_text)
    document_version = DocumentVersion(
        id=_id("document-version", version),
        candidate_id=candidate.id,
        source_url=candidate.canonical_url,
        title=candidate.title,
        body=body,
        content_hash=_hash(body),
        observed_at=clock.now(),
    )
    story = Story(
        id=_id("story", version),
        primary_document_version_id=document_version.id,
        stable_key=version,
        headline=headline,
        occurred_at=clock.now(),
        review_state=StoryReviewState.UNREVIEWED,
    )
    claim = Claim(
        id=_id("claim", version),
        story_id=story.id,
        position=0,
        text=claim_text,
    )
    evidence_span = EvidenceSpan(
        id=_id("evidence-span", version),
        claim_id=claim.id,
        document_version_id=document_version.id,
        exact_text=evidence_text,
        start_offset=start_offset,
        end_offset=start_offset + len(evidence_text),
        text_hash=_hash(evidence_text),
        role=EvidenceRole.PRIMARY,
    )
    trace = StructuredTrace(
        id=_id("trace", version),
        operation_key=version,
        evidence_span_id=evidence_span.id,
        occurred_at=clock.now(),
        attributes={
            "mode": "sample",
            "sample_version": version,
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


def build_sample_stories(clock: Clock | None = None) -> tuple[SampleStory, ...]:
    fixed_clock = clock or FixedClock()
    return (
        build_sample_story(clock=fixed_clock),
        _build_fixture_sample_story(
            version=f"{SAMPLE_VERSION}-rejected",
            title="缺少证据的 AI 性能声明",
            body="示例发布者声称其 AI 系统准确率达到 99%，但没有提供可验证的实验来源。",
            claim_text="示例发布者的 AI 系统准确率达到 99%。",
            evidence_text="AI 系统准确率达到 99%",
            headline="证据不足的 AI 性能声明",
            source_url=f"{SAMPLE_URL}/rejected",
            clock=fixed_clock,
        ),
        _build_fixture_sample_story(
            version=f"{SAMPLE_VERSION}-unreviewed",
            title="等待审核的 AI 工具候选",
            body="示例发布者宣布一个仍需管理员审核的新 AI 工具候选。",
            claim_text="示例发布者宣布了一个新 AI 工具候选。",
            evidence_text="一个仍需管理员审核的新 AI 工具候选",
            headline="等待审核的 AI 工具候选",
            source_url=f"{SAMPLE_URL}/unreviewed",
            clock=fixed_clock,
        ),
    )
