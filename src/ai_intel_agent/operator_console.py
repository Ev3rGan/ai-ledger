from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import urlencode, urlsplit
from uuid import UUID

import httpx
from sqlalchemy import JSON, BigInteger, CheckConstraint, DateTime, Integer, String, delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column

from ai_intel_agent.editorial import (
    DigestPlan,
    DigestPlanAnomaly,
    DigestPlanStory,
    EditorialApprovalOutcome,
    EditorialWorkflow,
    EvidenceSpanInspection,
    RetrievalIndexFollowUp,
)
from ai_intel_agent.persistence import (
    Base,
    DigestPlanRecord,
    DocumentVersionRecord,
    EditorialRepository,
    MultiSourceCollectionRepository,
    SchedulerStatusRepository,
)
from ai_intel_agent.source_portfolio import load_source_universe


class GitHubOAuthError(RuntimeError):
    """A bounded authentication failure that must not expose provider details."""


@dataclass(frozen=True)
class GitHubIdentity:
    user_id: int
    login: str


class GitHubOAuthClient(Protocol):
    def authorization_url(self, *, state: str, redirect_uri: str) -> str: ...

    def identity_for_code(self, *, code: str, redirect_uri: str) -> GitHubIdentity: ...


class HttpGitHubOAuthClient:
    """Use a GitHub token only long enough to resolve one stable numeric identity."""

    def __init__(self, *, client_id: str, client_secret: str) -> None:
        self._client_id = client_id
        self._client_secret = client_secret

    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        query = urlencode(
            {
                "client_id": self._client_id,
                "redirect_uri": redirect_uri,
                "scope": "read:user",
                "state": state,
            }
        )
        return f"https://github.com/login/oauth/authorize?{query}"

    def identity_for_code(self, *, code: str, redirect_uri: str) -> GitHubIdentity:
        try:
            with httpx.Client(timeout=15.0, trust_env=False) as client:
                token_response = client.post(
                    "https://github.com/login/oauth/access_token",
                    headers={"Accept": "application/json"},
                    data={
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "code": code,
                        "redirect_uri": redirect_uri,
                    },
                )
                token_response.raise_for_status()
                token_payload = token_response.json()
                access_token = token_payload.get("access_token")
                if not isinstance(access_token, str) or not access_token:
                    raise GitHubOAuthError("GitHub did not issue an access token")
                user_response = client.get(
                    "https://api.github.com/user",
                    headers={
                        "Accept": "application/vnd.github+json",
                        "Authorization": f"Bearer {access_token}",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                )
                user_response.raise_for_status()
                user_payload = user_response.json()
        except GitHubOAuthError:
            raise
        except (httpx.HTTPError, TypeError, ValueError) as error:
            raise GitHubOAuthError("GitHub OAuth provider unavailable") from error

        user_id = user_payload.get("id")
        login = user_payload.get("login")
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            raise GitHubOAuthError("GitHub returned an invalid numeric user ID")
        if not isinstance(login, str) or not login.strip():
            raise GitHubOAuthError("GitHub returned an invalid login")
        return GitHubIdentity(user_id=user_id, login=login.strip())


@dataclass(frozen=True)
class OperatorSecurityConfiguration:
    public_host: str
    operator_host: str
    operator_origin: str
    allowed_github_user_ids: frozenset[int]
    session_idle_ttl: timedelta = timedelta(minutes=30)
    session_absolute_ttl: timedelta = timedelta(hours=8)
    login_ttl: timedelta = timedelta(minutes=10)

    def __post_init__(self) -> None:
        public_host = _validated_host(self.public_host, "public_host")
        operator_host = _validated_host(self.operator_host, "operator_host")
        if public_host == operator_host:
            raise ValueError("Public and Operator hosts must differ")
        parsed_origin = urlsplit(self.operator_origin)
        if (
            parsed_origin.scheme != "https"
            or parsed_origin.netloc != operator_host
            or parsed_origin.path
            or parsed_origin.query
            or parsed_origin.fragment
        ):
            raise ValueError("Operator origin must be the exact HTTPS Operator host")
        if not self.allowed_github_user_ids or any(
            isinstance(user_id, bool) or user_id <= 0
            for user_id in self.allowed_github_user_ids
        ):
            raise ValueError("Operator allowlist must contain positive numeric GitHub user IDs")
        for name, value in (
            ("session_idle_ttl", self.session_idle_ttl),
            ("session_absolute_ttl", self.session_absolute_ttl),
            ("login_ttl", self.login_ttl),
        ):
            if value <= timedelta(0):
                raise ValueError(f"{name} must be positive")
        if self.session_idle_ttl > self.session_absolute_ttl:
            raise ValueError("Session idle lifetime cannot exceed absolute lifetime")

    @property
    def callback_uri(self) -> str:
        return f"{self.operator_origin}/operator/oauth/callback"


class OperatorLoginAttemptRecord(Base):
    __tablename__ = "operator_login_attempts"

    state_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    browser_nonce_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OperatorSessionRecord(Base):
    __tablename__ = "operator_sessions"
    __table_args__ = (
        CheckConstraint("github_user_id > 0", name="ck_operator_sessions_user_id_positive"),
        CheckConstraint(
            "idle_expires_at <= absolute_expires_at",
            name="ck_operator_sessions_expiry_order",
        ),
    )

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    github_user_id: Mapped[int] = mapped_column(BigInteger)
    github_login: Mapped[str] = mapped_column(String(255))
    csrf_token: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    idle_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OperatorMutationReceiptRecord(Base):
    __tablename__ = "operator_mutation_receipts"
    __table_args__ = (
        CheckConstraint(
            "state IN ('pending', 'completed')",
            name="ck_operator_mutation_receipts_state",
        ),
        CheckConstraint(
            "(state = 'pending' AND response_status IS NULL AND response_body IS NULL "
            "AND completed_at IS NULL) OR "
            "(state = 'completed' AND response_status BETWEEN 200 AND 299 "
            "AND response_body IS NOT NULL AND completed_at IS NOT NULL)",
            name="ck_operator_mutation_receipts_shape",
        ),
        CheckConstraint(
            "length(request_hash) = 64",
            name="ck_operator_mutation_receipts_request_hash",
        ),
    )

    github_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16))
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


@dataclass(frozen=True)
class OperatorSession:
    github_user_id: int
    github_login: str
    csrf_token: str
    absolute_expires_at: datetime


@dataclass(frozen=True)
class NewOperatorSession:
    token: str
    session: OperatorSession


class OperatorSessionStore:
    """Persist login challenges and opaque browser sessions in PostgreSQL."""

    def __init__(self, engine: Engine, configuration: OperatorSecurityConfiguration) -> None:
        self._engine = engine
        self._configuration = configuration

    def begin_login(self, *, created_at: datetime) -> tuple[str, str]:
        state = secrets.token_urlsafe(32)
        browser_nonce = secrets.token_urlsafe(32)
        with Session(self._engine) as session, session.begin():
            session.execute(
                delete(OperatorLoginAttemptRecord).where(
                    OperatorLoginAttemptRecord.expires_at <= created_at
                )
            )
            session.add(
                OperatorLoginAttemptRecord(
                    state_hash=_digest(state),
                    browser_nonce_hash=_digest(browser_nonce),
                    created_at=created_at,
                    expires_at=created_at + self._configuration.login_ttl,
                )
            )
        return state, browser_nonce

    def consume_login(self, *, state: str, browser_nonce: str, consumed_at: datetime) -> bool:
        with Session(self._engine) as session, session.begin():
            record = session.scalar(
                select(OperatorLoginAttemptRecord)
                .where(OperatorLoginAttemptRecord.state_hash == _digest(state))
                .with_for_update()
            )
            if record is None:
                return False
            session.delete(record)
            return record.expires_at > consumed_at and secrets.compare_digest(
                record.browser_nonce_hash,
                _digest(browser_nonce),
            )

    def create(
        self,
        identity: GitHubIdentity,
        *,
        created_at: datetime,
        rotated_token: str | None = None,
    ) -> NewOperatorSession:
        token = secrets.token_urlsafe(48)
        csrf_token = secrets.token_urlsafe(32)
        absolute_expires_at = created_at + self._configuration.session_absolute_ttl
        idle_expires_at = min(
            created_at + self._configuration.session_idle_ttl,
            absolute_expires_at,
        )
        with Session(self._engine) as session, session.begin():
            if rotated_token:
                existing = session.get(OperatorSessionRecord, _digest(rotated_token))
                if existing is not None and existing.revoked_at is None:
                    existing.revoked_at = created_at
            session.add(
                OperatorSessionRecord(
                    token_hash=_digest(token),
                    github_user_id=identity.user_id,
                    github_login=identity.login,
                    csrf_token=csrf_token,
                    created_at=created_at,
                    last_seen_at=created_at,
                    idle_expires_at=idle_expires_at,
                    absolute_expires_at=absolute_expires_at,
                    revoked_at=None,
                )
            )
        return NewOperatorSession(
            token=token,
            session=OperatorSession(
                github_user_id=identity.user_id,
                github_login=identity.login,
                csrf_token=csrf_token,
                absolute_expires_at=absolute_expires_at,
            ),
        )

    def resolve(self, token: str, *, observed_at: datetime) -> OperatorSession | None:
        with Session(self._engine) as session, session.begin():
            record = session.scalar(
                select(OperatorSessionRecord)
                .where(OperatorSessionRecord.token_hash == _digest(token))
                .with_for_update()
            )
            if record is None or record.revoked_at is not None:
                return None
            if record.github_user_id not in self._configuration.allowed_github_user_ids:
                record.revoked_at = observed_at
                return None
            if observed_at >= record.idle_expires_at or observed_at >= record.absolute_expires_at:
                record.revoked_at = observed_at
                return None
            record.last_seen_at = observed_at
            record.idle_expires_at = min(
                observed_at + self._configuration.session_idle_ttl,
                record.absolute_expires_at,
            )
            return OperatorSession(
                github_user_id=record.github_user_id,
                github_login=record.github_login,
                csrf_token=record.csrf_token,
                absolute_expires_at=record.absolute_expires_at,
            )

    def revoke(self, token: str, *, revoked_at: datetime) -> None:
        with Session(self._engine) as session, session.begin():
            record = session.get(OperatorSessionRecord, _digest(token))
            if record is not None and record.revoked_at is None:
                record.revoked_at = revoked_at


class OperatorIdempotencyConflictError(ValueError):
    pass


class OperatorMutationInProgressError(ValueError):
    pass


@dataclass(frozen=True)
class OperatorMutationReplay:
    status_code: int
    body: dict[str, object]


class OperatorMutationReceiptStore:
    """Persist transport idempotency without duplicating EditorialWorkflow rules."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def begin(
        self,
        *,
        github_user_id: int,
        idempotency_key: str,
        request_hash: str,
        created_at: datetime,
    ) -> OperatorMutationReplay | None:
        with Session(self._engine) as session, session.begin():
            created = session.scalar(
                insert(OperatorMutationReceiptRecord)
                .values(
                    github_user_id=github_user_id,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    state="pending",
                    created_at=created_at,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        OperatorMutationReceiptRecord.github_user_id,
                        OperatorMutationReceiptRecord.idempotency_key,
                    ]
                )
                .returning(OperatorMutationReceiptRecord.idempotency_key)
            )
            if created is not None:
                return None
            record = session.get(
                OperatorMutationReceiptRecord,
                (github_user_id, idempotency_key),
            )
            if record is None:
                raise RuntimeError("Operator mutation receipt disappeared")
            if not secrets.compare_digest(record.request_hash, request_hash):
                raise OperatorIdempotencyConflictError(
                    "Idempotency key was already used for a different request"
                )
            if record.state == "pending":
                raise OperatorMutationInProgressError(
                    "An identical Operator mutation is still in progress"
                )
            if record.response_status is None or record.response_body is None:
                raise RuntimeError("Completed Operator mutation receipt is incomplete")
            return OperatorMutationReplay(
                status_code=record.response_status,
                body=dict(record.response_body),
            )

    def complete(
        self,
        *,
        github_user_id: int,
        idempotency_key: str,
        request_hash: str,
        status_code: int,
        body: dict[str, object],
        completed_at: datetime,
    ) -> None:
        with Session(self._engine) as session, session.begin():
            record = session.scalar(
                select(OperatorMutationReceiptRecord)
                .where(
                    OperatorMutationReceiptRecord.github_user_id == github_user_id,
                    OperatorMutationReceiptRecord.idempotency_key == idempotency_key,
                )
                .with_for_update()
            )
            if (
                record is None
                or record.state != "pending"
                or not secrets.compare_digest(record.request_hash, request_hash)
            ):
                raise RuntimeError("Operator mutation receipt cannot be completed")
            record.state = "completed"
            record.response_status = status_code
            record.response_body = body
            record.completed_at = completed_at

    def abandon(
        self,
        *,
        github_user_id: int,
        idempotency_key: str,
        request_hash: str,
    ) -> None:
        with Session(self._engine) as session, session.begin():
            session.execute(
                delete(OperatorMutationReceiptRecord).where(
                    OperatorMutationReceiptRecord.github_user_id == github_user_id,
                    OperatorMutationReceiptRecord.idempotency_key == idempotency_key,
                    OperatorMutationReceiptRecord.request_hash == request_hash,
                    OperatorMutationReceiptRecord.state == "pending",
                )
            )


class OperatorReadProjection:
    """Compose existing operational seams into one deliberately read-only projection."""

    def __init__(self, engine: Engine, *, public_origin: str | None = None) -> None:
        self._engine = engine
        self._public_origin = public_origin
        self._editorial_repository = EditorialRepository(engine)
        self._editorial = EditorialWorkflow(self._editorial_repository)
        self._scheduler = SchedulerStatusRepository(engine)
        self._collection = MultiSourceCollectionRepository(engine)

    def dashboard(self, session: OperatorSession) -> dict[str, object]:
        profiles = load_source_universe()
        snapshots = {
            snapshot.source_definition_id: snapshot
            for snapshot in self._collection.source_statuses({profile.id for profile in profiles})
        }
        sources: list[dict[str, object]] = []
        for profile in profiles:
            snapshot = snapshots.get(profile.id)
            sources.append(
                {
                    "id": str(profile.id),
                    "key": profile.key,
                    "name": profile.key,
                    "publisher": profile.publisher,
                    "enabled": profile.enabled,
                    "health": snapshot.health if snapshot is not None else "unknown",
                    "recent_result": (
                        snapshot.recent_result if snapshot is not None else "never"
                    ),
                    "consecutive_failures": (
                        snapshot.consecutive_failures if snapshot is not None else 0
                    ),
                    "pending_drafts": snapshot.pending_drafts if snapshot is not None else 0,
                    "updated_at": _iso(snapshot.updated_at) if snapshot is not None else None,
                }
            )
        scheduler = self._scheduler.snapshot()
        return {
            "operator": {
                "github_user_id": session.github_user_id,
                "github_login": session.github_login,
            },
            "sources": sources,
            "scheduler": (
                {
                    "state": scheduler.state,
                    "next_run_at": _iso(scheduler.next_run_at),
                    "last_started_at": _iso(scheduler.last_started_at),
                    "last_completed_at": _iso(scheduler.last_completed_at),
                    "last_result": scheduler.last_result,
                    "updated_at": _iso(scheduler.updated_at),
                }
                if scheduler is not None
                else None
            ),
            "pending_story_count": self._editorial_repository.pending_review_count(),
            "plans": self.plan_history()["items"],
        }

    def plan_history(self) -> dict[str, object]:
        with Session(self._engine) as session:
            plan_ids = session.scalars(
                select(DigestPlanRecord.id).order_by(
                    DigestPlanRecord.publication_date.desc(),
                    DigestPlanRecord.version.desc(),
                )
            ).all()
        items: list[dict[str, object]] = []
        for plan_id in plan_ids:
            plan = self._editorial.plan(plan_id)
            if plan is not None:
                items.append(self._plan_summary(plan))
        return {"items": items}

    def plan(self, plan_id: UUID) -> dict[str, object] | None:
        plan = self._editorial.plan(plan_id)
        if plan is None:
            return None
        payload = self._plan_summary(plan)
        payload.update(
            {
                "window_start": _iso(plan.window_start),
                "window_end": _iso(plan.window_end),
                "digest_summary": plan.digest_summary,
                "source_coverage": list(plan.source_coverage),
                "topic_coverage": list(plan.topic_coverage),
                "warnings": [
                    _anomaly_payload(item) for item in plan.anomalies if not item.blocking
                ],
                "blockers": [
                    _anomaly_payload(item) for item in plan.anomalies if item.blocking
                ],
                "stories": [_story_payload(story) for story in plan.stories],
            }
        )
        return payload

    def document_version(self, document_version_id: UUID) -> dict[str, object] | None:
        with Session(self._engine) as session:
            record = session.get(DocumentVersionRecord, document_version_id)
            if record is None:
                return None
            return {
                "id": str(record.id),
                "title": record.title,
                "body": record.body,
                "source_url": _safe_external_url(record.source_url),
                "content_hash": record.content_hash,
                "observed_at": _iso(record.observed_at),
                "published_at": _iso(record.published_at),
                "updated_at": _iso(record.updated_at),
            }

    def _plan_summary(self, plan: DigestPlan) -> dict[str, object]:
        outcome = self._editorial.outcome(plan.id)
        follow_up = self._editorial.follow_up_status(plan.id)
        latest = self._editorial.latest_plan(plan.publication_date)
        return {
            "id": str(plan.id),
            "publication_date": plan.publication_date.isoformat(),
            "version": plan.version,
            "prepared_at": _iso(plan.prepared_at),
            "content_hash": plan.content_hash,
            "included_story_count": len(plan.included_stories),
            "excluded_story_count": len(plan.excluded_stories),
            "held_story_count": len(plan.held_stories),
            "warning_count": sum(not item.blocking for item in plan.anomalies),
            "blocker_count": sum(item.blocking for item in plan.anomalies),
            "is_latest": latest is not None and latest.id == plan.id,
            "derivation": (
                {
                    "previous_plan_id": str(plan.derivation.previous_plan_id),
                    "removed_story_stable_key": plan.derivation.removed_story_stable_key,
                    "removal_reason": plan.derivation.removal_reason,
                }
                if plan.derivation is not None
                else None
            ),
            "completion": _outcome_payload(outcome, public_origin=self._public_origin),
            "index_follow_up": _follow_up_payload(follow_up),
        }


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validated_host(value: str, name: str) -> str:
    if (
        not value
        or value != value.strip().lower()
        or "/" in value
        or "@" in value
        or any(character.isspace() for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase host without a scheme or path")
    return value


def utc_now() -> datetime:
    return datetime.now(UTC)


def _story_payload(story: DigestPlanStory) -> dict[str, object]:
    return {
        "id": str(story.id),
        "stable_key": story.stable_key,
        "headline": story.headline,
        "review_state": story.review_state.value,
        "publisher": story.publisher,
        "source_url": _safe_external_url(story.canonical_url),
        "original_published_at": _iso(story.original_published_at),
        "primary_document_version_id": str(story.primary_document_version_id),
        "inclusion": story.inclusion.value,
        "order": story.order,
        "summary": story.summary,
        "why_it_matters": story.why_it_matters,
        "primary_topic": story.primary_topic,
        "secondary_topics": list(story.secondary_topics),
        "exclusion_reason": story.exclusion_reason,
        "claims": [
            {
                "id": str(claim.id),
                "text": claim.text,
                "evidence": [_evidence_payload(item) for item in claim.evidence_spans],
            }
            for claim in story.claims
        ],
    }


def _evidence_payload(evidence: EvidenceSpanInspection) -> dict[str, object]:
    return {
        "id": str(evidence.id),
        "document_version_id": str(evidence.document_version_id),
        "exact_text": evidence.exact_text,
        "role": evidence.role.value,
        "relation": evidence.relation.value,
        "publisher": evidence.publisher,
        "source_url": _safe_external_url(evidence.canonical_url),
    }


def _anomaly_payload(anomaly: DigestPlanAnomaly) -> dict[str, object]:
    payload: dict[str, object] = {"code": anomaly.code, "message": anomaly.message}
    if anomaly.story_stable_key is not None:
        payload["story_stable_key"] = anomaly.story_stable_key
    return payload


def _outcome_payload(
    outcome: EditorialApprovalOutcome | None,
    *,
    public_origin: str | None = None,
) -> dict[str, object] | None:
    if outcome is None:
        return None
    return {
        "kind": outcome.kind.value,
        "plan_id": str(outcome.plan_id),
        "publication_date": outcome.publication_date.isoformat(),
        "content_hash": outcome.content_hash,
        "actor_identifier": outcome.actor_identifier,
        "completed_at": _iso(outcome.completed_at),
        "digest_id": str(outcome.digest.id) if outcome.digest is not None else None,
        "public_url": (
            f"{public_origin or ''}/digests/{outcome.publication_date.isoformat()}"
            if outcome.digest is not None
            else None
        ),
        "follow_up": _follow_up_payload(outcome.follow_up),
    }


def operator_outcome_payload(
    outcome: EditorialApprovalOutcome,
    *,
    public_origin: str,
) -> dict[str, object]:
    payload = _outcome_payload(outcome, public_origin=public_origin)
    if payload is None:
        raise AssertionError("Editorial outcome payload unexpectedly missing")
    return payload


def operator_follow_up_payload(follow_up: RetrievalIndexFollowUp) -> dict[str, object]:
    payload = _follow_up_payload(follow_up)
    if payload is None:
        raise AssertionError("Retrieval-index follow-up payload unexpectedly missing")
    return payload


def _follow_up_payload(follow_up: RetrievalIndexFollowUp | None) -> dict[str, object] | None:
    if follow_up is None:
        return None
    return {
        "state": follow_up.state.value,
        "requested_at": _iso(follow_up.requested_at),
        "attempt_count": follow_up.attempt_count,
        "started_at": _iso(follow_up.started_at),
        "completed_at": _iso(follow_up.completed_at),
        "index_id": str(follow_up.index_id) if follow_up.index_id is not None else None,
        "fault_code": follow_up.fault_code,
        "last_error": follow_up.last_error,
    }


def _safe_external_url(value: str | None) -> str | None:
    if value is None or value != value.strip():
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return None
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return value


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
