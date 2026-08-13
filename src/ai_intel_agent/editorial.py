from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from ai_intel_agent.domain import (
    AuditAction,
    AuditEvent,
    AuditSubjectType,
    Digest,
    DigestState,
    SampleDigestPublication,
    SampleStory,
    Story,
    StoryReviewState,
)


class Clock(Protocol):
    def now(self) -> datetime: ...


class Administrator(Protocol):
    identifier: str

    def review_state_for(self, story: Story) -> StoryReviewState | None: ...


class EditorialStateError(ValueError):
    pass


def _id(name: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"ai-intel-agent:sample-editorial-v1:{name}")


def _review_story(story: Story, decision: StoryReviewState) -> Story:
    if story.review_state is not StoryReviewState.UNREVIEWED:
        raise EditorialStateError("Only unreviewed Stories may be reviewed")
    if decision not in (StoryReviewState.ACCEPTED, StoryReviewState.REJECTED):
        raise EditorialStateError("A review must accept or reject a Story")
    return replace(story, review_state=decision)


def _compose_digest(stories: tuple[SampleStory, ...], now: datetime) -> Digest:
    accepted_story_ids = tuple(
        sample.story.id
        for sample in stories
        if sample.story.review_state is StoryReviewState.ACCEPTED
    )
    return Digest(
        id=_id("digest:2026-08-12"),
        stable_key="sample-digest:2026-08-12",
        publication_date=now.date(),
        state=DigestState.DRAFT,
        published_at=None,
        story_ids=accepted_story_ids,
    )


def _publish_digest(digest: Digest, now: datetime) -> Digest:
    if digest.state is not DigestState.DRAFT:
        raise EditorialStateError("Only a draft Digest may be published")
    if not digest.story_ids:
        raise EditorialStateError("A Digest must contain at least one accepted Story")
    return replace(digest, state=DigestState.PUBLISHED, published_at=now)


def review_and_publish_digest(
    stories: tuple[SampleStory, ...],
    *,
    administrator: Administrator,
    clock: Clock,
) -> SampleDigestPublication:
    reviewed_stories: list[SampleStory] = []
    audit_events: list[AuditEvent] = []
    now = clock.now()

    for sample in stories:
        decision = administrator.review_state_for(sample.story)
        if decision is None:
            reviewed_stories.append(sample)
            continue
        reviewed_story = _review_story(sample.story, decision)
        reviewed_stories.append(replace(sample, story=reviewed_story))
        action = (
            AuditAction.STORY_ACCEPTED
            if decision is StoryReviewState.ACCEPTED
            else AuditAction.STORY_REJECTED
        )
        audit_events.append(
            AuditEvent(
                id=_id(f"{sample.story.stable_key}:{action.value}"),
                operation_key=(
                    f"sample-editorial-v1:{sample.story.stable_key}:{action.value}"
                ),
                actor_identifier=administrator.identifier,
                action=action,
                subject_type=AuditSubjectType.STORY,
                subject_id=sample.story.id,
                occurred_at=now,
                sequence=len(audit_events),
                attributes={
                    "from_state": StoryReviewState.UNREVIEWED.value,
                    "to_state": decision.value,
                },
            )
        )

    reviewed = tuple(reviewed_stories)
    draft_digest = _compose_digest(reviewed, now)
    audit_events.append(
        AuditEvent(
            id=_id("digest:2026-08-12:composed"),
            operation_key="sample-editorial-v1:digest:2026-08-12:composed",
            actor_identifier=administrator.identifier,
            action=AuditAction.DIGEST_COMPOSED,
            subject_type=AuditSubjectType.DIGEST,
            subject_id=draft_digest.id,
            occurred_at=now,
            sequence=len(audit_events),
            attributes={
                "included_story_ids": [str(story_id) for story_id in draft_digest.story_ids]
            },
        )
    )
    digest = _publish_digest(draft_digest, now)
    audit_events.append(
        AuditEvent(
            id=_id("digest:2026-08-12:published"),
            operation_key="sample-editorial-v1:digest:2026-08-12:published",
            actor_identifier=administrator.identifier,
            action=AuditAction.DIGEST_PUBLISHED,
            subject_type=AuditSubjectType.DIGEST,
            subject_id=digest.id,
            occurred_at=now,
            sequence=len(audit_events),
            attributes={
                "from_state": DigestState.DRAFT.value,
                "to_state": DigestState.PUBLISHED.value,
            },
        )
    )
    return SampleDigestPublication(
        stories=reviewed,
        digest=digest,
        audit_events=tuple(audit_events),
    )
