from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from pg0 import Pg0
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from ai_intel_agent.persistence import (
    PersistentMeteredProviderBudget,
    create_database_engine,
    database_url_for_alembic_config,
)
from ai_intel_agent.provider_budget import (
    ProviderTokenUsage,
    estimate_provider_usage_usd,
    load_provider_budget_pricing,
    provider_token_usage,
)
from alembic import command


@pytest.fixture
def budget_hotfix_database_url() -> Iterator[str]:
    server = Pg0(name=f"ai_intel_m2_budget_hotfix_{uuid4().hex}")
    server.start()
    try:
        yield server.uri
    finally:
        server.drop()


def _alembic_config(database_url: str) -> Config:
    project_root = Path(__file__).resolve().parents[1]
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option(
        "sqlalchemy.url",
        database_url_for_alembic_config(database_url),
    )
    return config


def _seed_0008_budget(database_url: str) -> Config:
    config = _alembic_config(database_url)
    command.upgrade(config, "0008")
    engine = create_database_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO metered_provider_budget (
                        billing_month, reserved_cents, updated_at
                    ) VALUES (
                        DATE '2026-08-01', 10000, :updated_at
                    )
                    """
                ),
                {"updated_at": datetime(2026, 8, 20, tzinfo=UTC)},
            )
    finally:
        engine.dispose()
    return config


def _seed_0011_budget(database_url: str) -> Config:
    config = _alembic_config(database_url)
    command.upgrade(config, "0011")
    engine = create_database_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO metered_provider_budget (
                        billing_month, reserved_cents, updated_at
                    ) VALUES (
                        DATE '2026-08-01', 11500, :updated_at
                    )
                    """
                ),
                {"updated_at": datetime(2026, 8, 20, tzinfo=UTC)},
            )
    finally:
        engine.dispose()
    return config


def test_budget_uses_versioned_peak_prices_and_normalized_provider_usage() -> None:
    pricing = load_provider_budget_pricing()
    usage = provider_token_usage(
        {
            "prompt_tokens": 100,
            "prompt_cache_hit_tokens": 20,
            "completion_tokens": 40,
        }
    )

    assert pricing.version == "provider-budget-pricing-2026-09-07.v1"
    assert pricing.accounting_policy == "peak-rate-ceiling"
    assert usage == ProviderTokenUsage(100, 20, 40)
    assert estimate_provider_usage_usd("deepseek-v4-pro", usage) == Decimal(
        "0.00026488"
    )


def test_invalid_or_missing_provider_usage_cannot_be_treated_as_zero_cost() -> None:
    assert provider_token_usage({}) is None
    assert provider_token_usage(
        {
            "prompt_tokens": 10,
            "prompt_cache_hit_tokens": 11,
            "completion_tokens": 1,
        }
    ) is None
    assert provider_token_usage(
        {
            "prompt_tokens": 10,
            "prompt_tokens_details": "invalid",
            "completion_tokens": 1,
        }
    ) is None


def test_committed_provider_budget_contract_uses_50000_cap() -> None:
    project_root = Path(__file__).resolve().parents[1]
    expected_contracts = {
        "deploy/m1/release.env.example": (
            "AI_INTEL_PROVIDER_MONTHLY_BUDGET_CENTS=50000",
            "AI_INTEL_PROVIDER_REQUEST_RESERVATION_CENTS=100",
        ),
        "deploy/m1/production.compose.yml": (
            (
                "AI_INTEL_PROVIDER_MONTHLY_BUDGET_CENTS: "
                "${AI_INTEL_PROVIDER_MONTHLY_BUDGET_CENTS:-50000}"
            ),
            (
                "AI_INTEL_PROVIDER_REQUEST_RESERVATION_CENTS: "
                "${AI_INTEL_PROVIDER_REQUEST_RESERVATION_CENTS:-100}"
            ),
        ),
        "tests/fixtures/m1_operator_lifecycle_harness.sh": (
            "AI_INTEL_PROVIDER_MONTHLY_BUDGET_CENTS=50000",
            "AI_INTEL_PROVIDER_REQUEST_RESERVATION_CENTS=100",
        ),
        "docs/mvp-production-runbook.md": (
            "Set `AI_INTEL_PROVIDER_MONTHLY_BUDGET_CENTS` no higher than `50000`.",
            "AI_INTEL_PROVIDER_REQUEST_RESERVATION_CENTS` to exactly `100` cents.",
            "temporary concurrency hold, not booked spend",
            "legacy_reserved_cents",
        ),
    }

    for relative_path, expected_fragments in expected_contracts.items():
        content = (project_root / relative_path).read_text(encoding="utf-8")
        assert all(fragment in content for fragment in expected_fragments), relative_path

    historical_schema = (
        project_root / "alembic/versions/0005_m1_public_service.py"
    ).read_text(encoding="utf-8")
    assert "reserved_cents >= 1 AND reserved_cents <= 10000" in historical_schema


@pytest.mark.postgres
def test_populated_0008_budget_migrates_to_11500_hard_cap(
    budget_hotfix_database_url: str,
) -> None:
    config = _seed_0008_budget(budget_hotfix_database_url)
    command.upgrade(config, "0009")

    engine = create_database_engine(budget_hotfix_database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE metered_provider_budget
                    SET reserved_cents = 11500
                    WHERE billing_month = DATE '2026-08-01'
                    """
                )
            )

        with engine.connect() as connection:
            assert connection.scalar(
                text(
                    """
                    SELECT reserved_cents
                    FROM metered_provider_budget
                    WHERE billing_month = DATE '2026-08-01'
                    """
                )
            ) == 11_500
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0009"

        for invalid_reserved_cents in (0, 11_501):
            with (
                pytest.raises(
                    DBAPIError,
                    match="ck_metered_provider_budget_range",
                ),
                engine.begin() as connection,
            ):
                connection.execute(
                    text(
                        """
                        UPDATE metered_provider_budget
                        SET reserved_cents = :reserved_cents
                        WHERE billing_month = DATE '2026-08-01'
                        """
                    ),
                    {"reserved_cents": invalid_reserved_cents},
                )
    finally:
        engine.dispose()


@pytest.mark.postgres
def test_legacy_reservations_do_not_count_as_actual_spend_after_upgrade(
    budget_hotfix_database_url: str,
) -> None:
    config = _seed_0011_budget(budget_hotfix_database_url)
    command.upgrade(config, "head")

    engine = create_database_engine(budget_hotfix_database_url)
    budget = PersistentMeteredProviderBudget(
        engine,
        monthly_limit_cents=50_000,
        request_reservation_cents=100,
        today=lambda: date(2026, 8, 20),
    )
    try:
        reservation = budget.reserve()
        assert reservation is not None
        reservation.release()
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT legacy_reserved_cents, spent_microusd, held_microusd
                    FROM metered_provider_budget
                    WHERE billing_month = DATE '2026-08-01'
                    """
                )
            ).one()
        assert row.legacy_reserved_cents == 11_500
        assert row.spent_microusd == 0
        assert row.held_microusd == 0
    finally:
        engine.dispose()


@pytest.mark.postgres
def test_low_actual_spend_does_not_exhaust_budget_after_696_requests(
    budget_hotfix_database_url: str,
) -> None:
    config = _alembic_config(budget_hotfix_database_url)
    command.upgrade(config, "head")
    engine = create_database_engine(budget_hotfix_database_url)
    budget = PersistentMeteredProviderBudget(
        engine,
        monthly_limit_cents=50_000,
        request_reservation_cents=100,
        today=lambda: date(2026, 9, 7),
    )
    try:
        # The attached Provider statement reports CNY 10.45 for 696 requests.
        # USD 0.0021/request is a deliberately rounded-up reproduction input.
        for _ in range(696):
            reservation = budget.reserve()
            assert reservation is not None
            reservation.settle_usd(Decimal("0.0021"))

        next_reservation = budget.reserve()
        assert next_reservation is not None
        next_reservation.release()

        with engine.connect() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT spent_microusd, held_microusd
                    FROM metered_provider_budget
                    WHERE billing_month = DATE '2026-09-01'
                    """
                )
            ).one()
        assert row.spent_microusd == 1_461_600
        assert row.held_microusd == 0
    finally:
        engine.dispose()


@pytest.mark.postgres
def test_failed_requests_release_temporary_provider_budget_holds(
    budget_hotfix_database_url: str,
) -> None:
    config = _alembic_config(budget_hotfix_database_url)
    command.upgrade(config, "head")
    engine = create_database_engine(budget_hotfix_database_url)
    budget = PersistentMeteredProviderBudget(
        engine,
        monthly_limit_cents=100,
        request_reservation_cents=100,
        today=lambda: date(2026, 9, 7),
    )
    try:
        for _ in range(501):
            reservation = budget.reserve()
            assert reservation is not None
            reservation.release()

        with engine.connect() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT spent_microusd, held_microusd
                    FROM metered_provider_budget
                    WHERE billing_month = DATE '2026-09-01'
                    """
                )
            ).one()
        assert row.spent_microusd == 0
        assert row.held_microusd == 0
    finally:
        engine.dispose()


@pytest.mark.postgres
def test_budget_cap_downgrade_is_fail_closed_and_restores_0008_constraint(
    budget_hotfix_database_url: str,
) -> None:
    config = _seed_0008_budget(budget_hotfix_database_url)
    command.upgrade(config, "0009")
    engine = create_database_engine(budget_hotfix_database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE metered_provider_budget
                    SET reserved_cents = 11500
                    WHERE billing_month = DATE '2026-08-01'
                    """
                )
            )
    finally:
        engine.dispose()

    with pytest.raises(
        DBAPIError,
        match="0009 Provider budget data exceeds the 0008 hard cap",
    ):
        command.downgrade(config, "0008")

    engine = create_database_engine(budget_hotfix_database_url)
    try:
        with engine.begin() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0009"
            assert connection.scalar(
                text(
                    """
                    SELECT reserved_cents
                    FROM metered_provider_budget
                    WHERE billing_month = DATE '2026-08-01'
                    """
                )
            ) == 11_500
            connection.execute(
                text(
                    """
                    UPDATE metered_provider_budget
                    SET reserved_cents = 10000
                    WHERE billing_month = DATE '2026-08-01'
                    """
                )
            )
    finally:
        engine.dispose()

    command.downgrade(config, "0008")

    engine = create_database_engine(budget_hotfix_database_url)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0008"
        with (
            pytest.raises(
                DBAPIError,
                match="ck_metered_provider_budget_range",
            ),
            engine.begin() as connection,
        ):
            connection.execute(
                text(
                    """
                    UPDATE metered_provider_budget
                    SET reserved_cents = 10001
                    WHERE billing_month = DATE '2026-08-01'
                    """
                )
            )
    finally:
        engine.dispose()


@pytest.mark.postgres
def test_populated_0011_budget_migrates_to_50000_hard_cap(
    budget_hotfix_database_url: str,
) -> None:
    config = _seed_0011_budget(budget_hotfix_database_url)
    command.upgrade(config, "head")

    engine = create_database_engine(budget_hotfix_database_url)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0013"
            row = connection.execute(
                text(
                    """
                    SELECT legacy_reserved_cents, spent_microusd, held_microusd
                    FROM metered_provider_budget
                    WHERE billing_month = DATE '2026-08-01'
                    """
                )
            ).one()
            assert row.legacy_reserved_cents == 11_500
            assert row.spent_microusd == 0
            assert row.held_microusd == 0

        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE metered_provider_budget
                    SET legacy_reserved_cents = 50000
                    WHERE billing_month = DATE '2026-08-01'
                    """
                )
            )

        for invalid_reserved_cents in (-1, 50_001):
            with (
                pytest.raises(
                    DBAPIError,
                    match="ck_metered_provider_budget_legacy_range",
                ),
                engine.begin() as connection,
            ):
                connection.execute(
                    text(
                        """
                        UPDATE metered_provider_budget
                        SET legacy_reserved_cents = :reserved_cents
                        WHERE billing_month = DATE '2026-08-01'
                        """
                    ),
                    {"reserved_cents": invalid_reserved_cents},
                )

        with engine.connect() as connection:
            assert connection.scalar(
                text(
                    """
                    SELECT legacy_reserved_cents
                    FROM metered_provider_budget
                    WHERE billing_month = DATE '2026-08-01'
                    """
                )
            ) == 50_000
    finally:
        engine.dispose()


@pytest.mark.postgres
def test_50000_budget_downgrade_is_fail_closed_and_restores_0011_constraint(
    budget_hotfix_database_url: str,
) -> None:
    config = _seed_0011_budget(budget_hotfix_database_url)
    command.upgrade(config, "head")
    command.downgrade(config, "0012")
    engine = create_database_engine(budget_hotfix_database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE metered_provider_budget
                    SET reserved_cents = 50000
                    WHERE billing_month = DATE '2026-08-01'
                    """
                )
            )
    finally:
        engine.dispose()

    with pytest.raises(
        DBAPIError,
        match="0012 Provider budget data exceeds the 0011 hard cap",
    ):
        command.downgrade(config, "0011")

    engine = create_database_engine(budget_hotfix_database_url)
    try:
        with engine.begin() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0012"
            assert connection.scalar(
                text(
                    """
                    SELECT reserved_cents
                    FROM metered_provider_budget
                    WHERE billing_month = DATE '2026-08-01'
                    """
                )
            ) == 50_000
            connection.execute(
                text(
                    """
                    UPDATE metered_provider_budget
                    SET reserved_cents = 11500
                    WHERE billing_month = DATE '2026-08-01'
                    """
                )
            )
    finally:
        engine.dispose()

    command.downgrade(config, "0011")

    engine = create_database_engine(budget_hotfix_database_url)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0011"
        with (
            pytest.raises(
                DBAPIError,
                match="ck_metered_provider_budget_range",
            ),
            engine.begin() as connection,
        ):
            connection.execute(
                text(
                    """
                    UPDATE metered_provider_budget
                    SET reserved_cents = 11501
                    WHERE billing_month = DATE '2026-08-01'
                    """
                )
            )
    finally:
        engine.dispose()
