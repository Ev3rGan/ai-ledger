from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ai_intel_agent.domain import (
    DigestState,
    EvidenceRelation,
    EvidenceRole,
    EvidenceState,
    StoryReviewState,
)
from ai_intel_agent.persistence import (
    CandidateRecord,
    ClaimRecord,
    DigestRecord,
    DigestStoryRecord,
    DocumentVersionRecord,
    EvidenceSpanRecord,
    StoryRecord,
)

PUBLIC_EVIDENCE_EXCERPT_MAX_CHARACTERS = 280


@dataclass(frozen=True)
class PublicEvidence:
    exact_text: str
    role: EvidenceRole
    relation: EvidenceRelation
    canonical_url: str
    publisher: str


@dataclass(frozen=True)
class PublicClaim:
    text: str
    evidence: tuple[PublicEvidence, ...]

    @property
    def evidence_state(self) -> EvidenceState:
        non_community = tuple(
            item for item in self.evidence if item.role is not EvidenceRole.COMMUNITY
        )
        if not non_community:
            return EvidenceState.INSUFFICIENT_EVIDENCE

        if any(
            item.relation is EvidenceRelation.CONTRADICTS for item in non_community
        ):
            return EvidenceState.CONFLICT

        corroborating_sources = {
            item.canonical_url
            for item in non_community
            if item.relation is EvidenceRelation.SUPPORTS
            and item.role in (EvidenceRole.PRIMARY, EvidenceRole.INDEPENDENT)
        }
        independently_confirmed = (
            len(corroborating_sources) > 1
            and any(item.role is EvidenceRole.INDEPENDENT for item in non_community)
        )
        if independently_confirmed:
            return EvidenceState.MULTI_SOURCE
        return EvidenceState.SINGLE_SOURCE


@dataclass(frozen=True)
class PublicStory:
    stable_key: str
    headline: str
    claims: tuple[PublicClaim, ...]


@dataclass(frozen=True)
class PublicDigest:
    stable_key: str
    publication_date: date
    published_at: datetime
    stories: tuple[PublicStory, ...]


@dataclass
class _ClaimBuilder:
    text: str
    evidence: list[PublicEvidence] = field(default_factory=list)
    evidence_ids: set[UUID] = field(default_factory=set)


@dataclass
class _StoryBuilder:
    stable_key: str
    headline: str
    claims: list[_ClaimBuilder] = field(default_factory=list)
    claims_by_id: dict[UUID, _ClaimBuilder] = field(default_factory=dict)


class PublicPublicationRepository:
    """Read the bounded public projection of published Digests."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def latest_digest(self) -> PublicDigest | None:
        with Session(self._engine) as session:
            record = session.scalars(self._published_digests_statement()).first()
            return self._to_public_digest(session, record) if record is not None else None

    def digest_for_date(self, publication_date: date) -> PublicDigest | None:
        with Session(self._engine) as session:
            record = session.scalars(
                self._published_digests_statement().where(
                    DigestRecord.publication_date == publication_date
                )
            ).first()
            return self._to_public_digest(session, record) if record is not None else None

    def published_digests(self) -> tuple[PublicDigest, ...]:
        with Session(self._engine) as session:
            records = session.scalars(self._published_digests_statement()).all()
            return tuple(self._to_public_digest(session, record) for record in records)

    def published_story(self, stable_key: str) -> PublicStory | None:
        with Session(self._engine) as session:
            stories = self._load_public_stories(session, stable_key=stable_key)
            return stories[0] if stories else None

    def browse_published_stories(self) -> tuple[PublicStory, ...]:
        with Session(self._engine) as session:
            return self._load_public_stories(session)

    @staticmethod
    def _published_digests_statement() -> Select[tuple[DigestRecord]]:
        return (
            select(DigestRecord)
            .where(DigestRecord.state == DigestState.PUBLISHED.value)
            .order_by(DigestRecord.publication_date.desc())
        )

    def _to_public_digest(
        self, session: Session, record: DigestRecord
    ) -> PublicDigest:
        if record.published_at is None:
            raise ValueError("A published Digest must have a publication time")
        return PublicDigest(
            stable_key=record.stable_key,
            publication_date=record.publication_date,
            published_at=record.published_at,
            stories=self._load_public_stories(session, digest_id=record.id),
        )

    @staticmethod
    def _load_public_stories(
        session: Session,
        *,
        digest_id: UUID | None = None,
        stable_key: str | None = None,
    ) -> tuple[PublicStory, ...]:
        statement = (
            select(
                StoryRecord.id.label("story_id"),
                StoryRecord.stable_key,
                StoryRecord.headline,
                ClaimRecord.id.label("claim_id"),
                ClaimRecord.text.label("claim_text"),
                EvidenceSpanRecord.id.label("evidence_id"),
                EvidenceSpanRecord.exact_text,
                EvidenceSpanRecord.role,
                EvidenceSpanRecord.relation,
                CandidateRecord.canonical_url,
                CandidateRecord.publisher,
            )
            .join(DigestStoryRecord, DigestStoryRecord.story_id == StoryRecord.id)
            .join(DigestRecord, DigestRecord.id == DigestStoryRecord.digest_id)
            .outerjoin(ClaimRecord, ClaimRecord.story_id == StoryRecord.id)
            .outerjoin(
                EvidenceSpanRecord, EvidenceSpanRecord.claim_id == ClaimRecord.id
            )
            .outerjoin(
                DocumentVersionRecord,
                DocumentVersionRecord.id == EvidenceSpanRecord.document_version_id,
            )
            .outerjoin(
                CandidateRecord, CandidateRecord.id == DocumentVersionRecord.candidate_id
            )
            .where(
                DigestRecord.state == DigestState.PUBLISHED.value,
                StoryRecord.review_state == StoryReviewState.ACCEPTED.value,
            )
            .order_by(
                DigestRecord.publication_date.desc(),
                DigestStoryRecord.position,
                ClaimRecord.position,
                EvidenceSpanRecord.start_offset,
            )
        )
        if digest_id is not None:
            statement = statement.where(DigestRecord.id == digest_id)
        if stable_key is not None:
            statement = statement.where(StoryRecord.stable_key == stable_key)

        builders: dict[UUID, _StoryBuilder] = {}
        for row in session.execute(statement):
            story = builders.setdefault(
                row.story_id,
                _StoryBuilder(stable_key=row.stable_key, headline=row.headline),
            )
            if row.claim_id is None:
                continue
            claim = story.claims_by_id.get(row.claim_id)
            if claim is None:
                claim = _ClaimBuilder(text=row.claim_text)
                story.claims_by_id[row.claim_id] = claim
                story.claims.append(claim)
            if row.evidence_id is not None and row.evidence_id not in claim.evidence_ids:
                claim.evidence_ids.add(row.evidence_id)
                claim.evidence.append(
                    PublicEvidence(
                        exact_text=_bounded_public_excerpt(row.exact_text),
                        role=EvidenceRole(row.role),
                        relation=EvidenceRelation(row.relation),
                        canonical_url=row.canonical_url,
                        publisher=row.publisher,
                    )
                )

        return tuple(
            PublicStory(
                stable_key=story.stable_key,
                headline=story.headline,
                claims=tuple(
                    PublicClaim(text=claim.text, evidence=tuple(claim.evidence))
                    for claim in story.claims
                ),
            )
            for story in builders.values()
        )


def _bounded_public_excerpt(exact_text: str) -> str:
    if len(exact_text) <= PUBLIC_EVIDENCE_EXCERPT_MAX_CHARACTERS:
        return exact_text
    return exact_text[: PUBLIC_EVIDENCE_EXCERPT_MAX_CHARACTERS - 1] + "…"
