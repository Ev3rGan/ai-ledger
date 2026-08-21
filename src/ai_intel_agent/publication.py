from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from uuid import UUID

from sqlalchemy import Select, exists, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, aliased
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import Exists

from ai_intel_agent.domain import (
    DigestState,
    EvidenceRelation,
    EvidenceRole,
    EvidenceState,
    StoryReviewState,
    Topic,
)
from ai_intel_agent.editorial import DigestPublicationContract
from ai_intel_agent.persistence import (
    CandidateRecord,
    ClaimRecord,
    DigestRecord,
    DigestStoryRecord,
    DigestWithdrawalRecord,
    DocumentVersionRecord,
    EvidenceSpanRecord,
    StoryPresentationRecord,
    StoryRecord,
)

PUBLIC_EVIDENCE_EXCERPT_MAX_CHARACTERS = 280


@dataclass(frozen=True)
class PublicEvidence:
    id: UUID
    exact_text: str
    role: EvidenceRole
    relation: EvidenceRelation
    canonical_url: str
    publisher: str


@dataclass(frozen=True)
class PublicClaim:
    id: UUID
    text: str
    evidence: tuple[PublicEvidence, ...]

    @property
    def evidence_state(self) -> EvidenceState:
        non_community = tuple(
            item for item in self.evidence if item.role is not EvidenceRole.COMMUNITY
        )
        if not non_community:
            return EvidenceState.INSUFFICIENT_EVIDENCE

        if any(item.relation is EvidenceRelation.CONTRADICTS for item in non_community):
            return EvidenceState.CONFLICT

        corroborating_sources = {
            item.canonical_url
            for item in non_community
            if item.relation is EvidenceRelation.SUPPORTS
            and item.role in (EvidenceRole.PRIMARY, EvidenceRole.INDEPENDENT)
        }
        independently_confirmed = len(corroborating_sources) > 1 and any(
            item.role is EvidenceRole.INDEPENDENT for item in non_community
        )
        if independently_confirmed:
            return EvidenceState.MULTI_SOURCE
        return EvidenceState.SINGLE_SOURCE


@dataclass(frozen=True)
class PublicStory:
    id: UUID
    stable_key: str
    headline: str
    summary: str | None
    why_it_matters: str | None
    primary_topic: Topic | None
    secondary_topics: tuple[Topic, ...]
    publisher: str
    canonical_url: str
    original_published_at: datetime | None
    claims: tuple[PublicClaim, ...]


@dataclass(frozen=True)
class PublicDigest:
    stable_key: str
    publication_date: date
    published_at: datetime
    introduction: str
    stories: tuple[PublicStory, ...]


@dataclass
class _ClaimBuilder:
    id: UUID
    text: str
    evidence: list[PublicEvidence] = field(default_factory=list)
    evidence_ids: set[UUID] = field(default_factory=set)


@dataclass
class _StoryBuilder:
    id: UUID
    stable_key: str
    headline: str
    summary: str | None
    why_it_matters: str | None
    primary_topic: Topic | None
    secondary_topics: tuple[Topic, ...]
    publisher: str
    canonical_url: str
    original_published_at: datetime | None
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

    def browse_published_stories(
        self,
        *,
        keyword: str | None = None,
        publisher: str | None = None,
        topic: Topic | None = None,
        publication_date: date | None = None,
    ) -> tuple[PublicStory, ...]:
        with Session(self._engine) as session:
            return self._load_public_stories(
                session,
                keyword=keyword,
                publisher=publisher,
                topic=topic,
                publication_date=publication_date,
            )

    @staticmethod
    def public_story_exists(story_id: ColumnElement[UUID]) -> Exists:
        """Correlate one Story with a still-visible supported publication."""
        return exists(
            select(DigestStoryRecord.story_id)
            .join(DigestRecord, DigestRecord.id == DigestStoryRecord.digest_id)
            .where(
                DigestStoryRecord.story_id == story_id,
                DigestRecord.state == DigestState.PUBLISHED.value,
                DigestRecord.publication_contract.in_(
                    (
                        DigestPublicationContract.LEGACY_FIXTURE.value,
                        DigestPublicationContract.M3_MULTISOURCE.value,
                        DigestPublicationContract.M3_EDITORIAL_PLAN.value,
                    )
                ),
                ~exists(
                    select(DigestWithdrawalRecord.digest_id).where(
                        DigestWithdrawalRecord.digest_id == DigestRecord.id
                    )
                ),
            )
        )

    @staticmethod
    def _published_digests_statement() -> Select[tuple[DigestRecord]]:
        return (
            select(DigestRecord)
            .where(
                DigestRecord.state == DigestState.PUBLISHED.value,
                DigestRecord.publication_contract.in_(
                    (
                        DigestPublicationContract.LEGACY_FIXTURE.value,
                        DigestPublicationContract.M3_MULTISOURCE.value,
                        DigestPublicationContract.M3_EDITORIAL_PLAN.value,
                    )
                ),
                ~exists(
                    select(DigestWithdrawalRecord.digest_id).where(
                        DigestWithdrawalRecord.digest_id == DigestRecord.id
                    )
                ),
            )
            .order_by(DigestRecord.publication_date.desc())
        )

    def _to_public_digest(self, session: Session, record: DigestRecord) -> PublicDigest:
        if record.published_at is None:
            raise ValueError("A published Digest must have a publication time")
        return PublicDigest(
            stable_key=record.stable_key,
            publication_date=record.publication_date,
            published_at=record.published_at,
            introduction=record.introduction or "",
            stories=self._load_public_stories(session, digest_id=record.id),
        )

    @staticmethod
    def _load_public_stories(
        session: Session,
        *,
        digest_id: UUID | None = None,
        stable_key: str | None = None,
        keyword: str | None = None,
        publisher: str | None = None,
        topic: Topic | None = None,
        publication_date: date | None = None,
    ) -> tuple[PublicStory, ...]:
        primary_document = aliased(DocumentVersionRecord)
        primary_candidate = aliased(CandidateRecord)
        evidence_document = aliased(DocumentVersionRecord)
        evidence_candidate = aliased(CandidateRecord)
        statement = (
            select(
                StoryRecord.id.label("story_id"),
                StoryRecord.stable_key,
                StoryRecord.headline,
                StoryPresentationRecord.summary,
                StoryPresentationRecord.why_it_matters,
                StoryPresentationRecord.primary_topic,
                StoryPresentationRecord.secondary_topics,
                primary_candidate.publisher.label("primary_publisher"),
                primary_candidate.canonical_url.label("primary_canonical_url"),
                primary_document.published_at.label("original_published_at"),
                ClaimRecord.id.label("claim_id"),
                ClaimRecord.text.label("claim_text"),
                EvidenceSpanRecord.id.label("evidence_id"),
                EvidenceSpanRecord.exact_text,
                EvidenceSpanRecord.role,
                EvidenceSpanRecord.relation,
                evidence_candidate.canonical_url,
                evidence_candidate.publisher,
            )
            .join(DigestStoryRecord, DigestStoryRecord.story_id == StoryRecord.id)
            .join(DigestRecord, DigestRecord.id == DigestStoryRecord.digest_id)
            .outerjoin(
                StoryPresentationRecord,
                StoryPresentationRecord.story_id == StoryRecord.id,
            )
            .join(
                primary_document,
                primary_document.id == StoryRecord.primary_document_version_id,
            )
            .join(
                primary_candidate,
                primary_candidate.id == primary_document.candidate_id,
            )
            .outerjoin(ClaimRecord, ClaimRecord.story_id == StoryRecord.id)
            .outerjoin(EvidenceSpanRecord, EvidenceSpanRecord.claim_id == ClaimRecord.id)
            .outerjoin(
                evidence_document,
                evidence_document.id == EvidenceSpanRecord.document_version_id,
            )
            .outerjoin(
                evidence_candidate,
                evidence_candidate.id == evidence_document.candidate_id,
            )
            .where(
                DigestRecord.state == DigestState.PUBLISHED.value,
                DigestRecord.publication_contract.in_(
                    (
                        DigestPublicationContract.LEGACY_FIXTURE.value,
                        DigestPublicationContract.M3_MULTISOURCE.value,
                        DigestPublicationContract.M3_EDITORIAL_PLAN.value,
                    )
                ),
                ~exists(
                    select(DigestWithdrawalRecord.digest_id).where(
                        DigestWithdrawalRecord.digest_id == DigestRecord.id
                    )
                ),
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
        if keyword is not None and (normalized_keyword := keyword.strip()):
            pattern = f"%{normalized_keyword}%"
            search_claim = aliased(ClaimRecord)
            matching_claim = (
                select(search_claim.id)
                .where(
                    search_claim.story_id == StoryRecord.id,
                    search_claim.text.ilike(pattern),
                )
                .correlate(StoryRecord)
                .exists()
            )
            statement = statement.where(
                StoryRecord.headline.ilike(pattern)
                | StoryPresentationRecord.summary.ilike(pattern)
                | StoryPresentationRecord.why_it_matters.ilike(pattern)
                | matching_claim
            )
        if publisher is not None and publisher.strip():
            statement = statement.where(primary_candidate.publisher == publisher.strip())
        if topic is not None:
            statement = statement.where(StoryPresentationRecord.primary_topic == topic.value)
        if publication_date is not None:
            statement = statement.where(
                primary_document.published_at.is_not(None),
                func.date(primary_document.published_at) == publication_date,
            )

        builders: dict[UUID, _StoryBuilder] = {}
        for row in session.execute(statement):
            story = builders.setdefault(
                row.story_id,
                _StoryBuilder(
                    id=row.story_id,
                    stable_key=row.stable_key,
                    headline=row.headline,
                    summary=row.summary,
                    why_it_matters=row.why_it_matters,
                    primary_topic=(
                        Topic(row.primary_topic) if row.primary_topic is not None else None
                    ),
                    secondary_topics=tuple(Topic(value) for value in (row.secondary_topics or ())),
                    publisher=row.primary_publisher,
                    canonical_url=row.primary_canonical_url,
                    original_published_at=row.original_published_at,
                ),
            )
            if row.claim_id is None:
                continue
            claim = story.claims_by_id.get(row.claim_id)
            if claim is None:
                claim = _ClaimBuilder(id=row.claim_id, text=row.claim_text)
                story.claims_by_id[row.claim_id] = claim
                story.claims.append(claim)
            if row.evidence_id is not None and row.evidence_id not in claim.evidence_ids:
                claim.evidence_ids.add(row.evidence_id)
                claim.evidence.append(
                    PublicEvidence(
                        id=row.evidence_id,
                        exact_text=bounded_public_evidence_excerpt(row.exact_text),
                        role=EvidenceRole(row.role),
                        relation=EvidenceRelation(row.relation),
                        canonical_url=row.canonical_url,
                        publisher=row.publisher,
                    )
                )

        return tuple(
            PublicStory(
                id=story.id,
                stable_key=story.stable_key,
                headline=story.headline,
                summary=story.summary,
                why_it_matters=story.why_it_matters,
                primary_topic=story.primary_topic,
                secondary_topics=story.secondary_topics,
                publisher=story.publisher,
                canonical_url=story.canonical_url,
                original_published_at=story.original_published_at,
                claims=tuple(
                    PublicClaim(
                        id=claim.id,
                        text=claim.text,
                        evidence=tuple(claim.evidence),
                    )
                    for claim in story.claims
                ),
            )
            for story in builders.values()
        )


def bounded_public_evidence_excerpt(exact_text: str) -> str:
    if len(exact_text) <= PUBLIC_EVIDENCE_EXCERPT_MAX_CHARACTERS:
        return exact_text
    return exact_text[: PUBLIC_EVIDENCE_EXCERPT_MAX_CHARACTERS - 1] + "…"
