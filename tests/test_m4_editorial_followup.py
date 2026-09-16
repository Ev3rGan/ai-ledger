from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

from ai_intel_agent.editorial import (
    RetrievalIndexFollowUp,
    RetrievalIndexFollowUpState,
)
from ai_intel_agent.index_followup import RetrievalIndexFollowUpWorker


def _running_follow_up(plan_id: UUID, claim_id: UUID) -> RetrievalIndexFollowUp:
    started_at = datetime(2026, 9, 16, 8, tzinfo=UTC)
    return RetrievalIndexFollowUp(
        plan_id=plan_id,
        publication_date=date(2026, 9, 16),
        state=RetrievalIndexFollowUpState.RUNNING,
        requested_at=started_at - timedelta(minutes=1),
        attempt_count=1,
        started_at=started_at,
        claim_id=claim_id,
        lease_expires_at=started_at + timedelta(minutes=30),
    )


class _RecordingFollowUpRepository:
    def __init__(self, claimed: tuple[RetrievalIndexFollowUp, ...]) -> None:
        self.claimed = claimed
        self.claim_calls: list[datetime] = []
        self.complete_calls: list[tuple[UUID, UUID, datetime]] = []
        self.fail_calls: list[tuple[UUID, str, str, datetime]] = []

    def claim_retrieval_index_follow_ups(
        self,
        *,
        claimed_at: datetime,
        lease_duration: timedelta = timedelta(minutes=30),
    ) -> tuple[RetrievalIndexFollowUp, ...]:
        self.claim_calls.append(claimed_at)
        return self.claimed

    def complete_retrieval_index_follow_ups(
        self,
        claim_id: UUID,
        *,
        index_id: UUID,
        completed_at: datetime,
    ) -> tuple[RetrievalIndexFollowUp, ...]:
        self.complete_calls.append((claim_id, index_id, completed_at))
        return tuple(
            replace(
                item,
                state=RetrievalIndexFollowUpState.SUCCEEDED,
                completed_at=completed_at,
                lease_expires_at=None,
                index_id=index_id,
            )
            for item in self.claimed
        )

    def fail_retrieval_index_follow_ups(
        self,
        claim_id: UUID,
        *,
        fault_code: str,
        last_error: str,
        completed_at: datetime,
    ) -> tuple[RetrievalIndexFollowUp, ...]:
        self.fail_calls.append((claim_id, fault_code, last_error, completed_at))
        return tuple(
            replace(
                item,
                state=RetrievalIndexFollowUpState.FAILED,
                completed_at=completed_at,
                lease_expires_at=None,
                fault_code=fault_code,
                last_error=last_error,
            )
            for item in self.claimed
        )


@dataclass(frozen=True)
class _BuildResult:
    index_id: UUID
    fault_code: str | None = None


class _RecordingIndexer:
    def __init__(self, result: _BuildResult | Exception) -> None:
        self.result = result
        self.calls = 0

    def rebuild(self) -> _BuildResult:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_worker_coalesces_one_claim_into_one_complete_rebuild() -> None:
    claim_id = uuid4()
    claimed = (
        _running_follow_up(uuid4(), claim_id),
        _running_follow_up(uuid4(), claim_id),
    )
    repository = _RecordingFollowUpRepository(claimed)
    result = _BuildResult(index_id=uuid4())
    indexer = _RecordingIndexer(result)
    completed_at = datetime(2026, 9, 16, 8, 5, tzinfo=UTC)

    completed = RetrievalIndexFollowUpWorker(
        repository,
        indexer=indexer,
        now=lambda: completed_at,
    ).run_pending()

    assert indexer.calls == 1
    assert repository.complete_calls == [(claim_id, result.index_id, completed_at)]
    assert repository.fail_calls == []
    assert len(completed) == 2
    assert all(item.state == "succeeded" for item in completed)


def test_worker_records_a_retryable_failure_without_undoing_publication() -> None:
    claim_id = uuid4()
    repository = _RecordingFollowUpRepository(
        (_running_follow_up(uuid4(), claim_id),)
    )
    indexer = _RecordingIndexer(RuntimeError("sensitive backend detail"))
    completed_at = datetime(2026, 9, 16, 8, 5, tzinfo=UTC)

    failed = RetrievalIndexFollowUpWorker(
        repository,
        indexer=indexer,
        now=lambda: completed_at,
    ).run_pending()

    assert indexer.calls == 1
    assert repository.complete_calls == []
    assert repository.fail_calls == [
        (
            claim_id,
            "index-rebuild-failed",
            "RuntimeError: full retrieval index rebuild failed",
            completed_at,
        )
    ]
    assert failed[0].state == "failed"
    assert "sensitive backend detail" not in (failed[0].last_error or "")
