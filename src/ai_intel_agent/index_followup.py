from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from ai_intel_agent.editorial import RetrievalIndexFollowUp


class FullIndexBuildResult(Protocol):
    index_id: UUID
    fault_code: str | None


class FullIndexBuilder(Protocol):
    def rebuild(self) -> FullIndexBuildResult: ...


class RetrievalIndexFollowUpRepository(Protocol):
    def claim_retrieval_index_follow_ups(
        self,
        *,
        claimed_at: datetime,
        lease_duration: timedelta = timedelta(minutes=30),
    ) -> tuple[RetrievalIndexFollowUp, ...]: ...

    def complete_retrieval_index_follow_ups(
        self,
        claim_id: UUID,
        *,
        index_id: UUID,
        completed_at: datetime,
    ) -> tuple[RetrievalIndexFollowUp, ...]: ...

    def fail_retrieval_index_follow_ups(
        self,
        claim_id: UUID,
        *,
        fault_code: str,
        last_error: str,
        completed_at: datetime,
    ) -> tuple[RetrievalIndexFollowUp, ...]: ...


class RetrievalIndexFollowUpWorker:
    """Claim every pending publication and satisfy the batch with one full rebuild."""

    def __init__(
        self,
        repository: RetrievalIndexFollowUpRepository,
        *,
        indexer: FullIndexBuilder,
        now: Callable[[], datetime] | None = None,
        lease_duration: timedelta = timedelta(minutes=30),
    ) -> None:
        if lease_duration <= timedelta(0):
            raise ValueError("Retrieval-index follow-up lease duration must be positive")
        self._repository = repository
        self._indexer = indexer
        self._now = now or (lambda: datetime.now(UTC))
        self._lease_duration = lease_duration

    def run_pending(self) -> tuple[RetrievalIndexFollowUp, ...]:
        claimed = self._repository.claim_retrieval_index_follow_ups(
            claimed_at=self._now(),
            lease_duration=self._lease_duration,
        )
        if not claimed:
            return ()
        claim_ids = {item.claim_id for item in claimed}
        if len(claim_ids) != 1 or None in claim_ids:
            raise RuntimeError("Claimed retrieval-index follow-ups do not form one batch")
        claim_id = claimed[0].claim_id
        assert claim_id is not None

        try:
            result = self._indexer.rebuild()
        except Exception as error:  # noqa: BLE001 - every rebuild failure is durable job state
            return self._repository.fail_retrieval_index_follow_ups(
                claim_id,
                fault_code="index-rebuild-failed",
                last_error=(
                    f"{type(error).__name__}: full retrieval index rebuild failed"
                ),
                completed_at=self._now(),
            )
        if result.fault_code is not None:
            return self._repository.fail_retrieval_index_follow_ups(
                claim_id,
                fault_code="index-rebuild-incomplete",
                last_error="IndexBuildResult: full retrieval index rebuild was incomplete",
                completed_at=self._now(),
            )
        return self._repository.complete_retrieval_index_follow_ups(
            claim_id,
            index_id=result.index_id,
            completed_at=self._now(),
        )
