from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
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


def test_committed_provider_budget_contract_uses_11500_cap() -> None:
    project_root = Path(__file__).resolve().parents[1]
    expected_contracts = {
        "deploy/m1/release.env.example": (
            "AI_INTEL_PROVIDER_MONTHLY_BUDGET_CENTS=11500",
            "AI_INTEL_PROVIDER_REQUEST_RESERVATION_CENTS=100",
        ),
        "deploy/m1/production.compose.yml": (
            (
                "AI_INTEL_PROVIDER_MONTHLY_BUDGET_CENTS: "
                "${AI_INTEL_PROVIDER_MONTHLY_BUDGET_CENTS:-11500}"
            ),
            (
                "AI_INTEL_PROVIDER_REQUEST_RESERVATION_CENTS: "
                "${AI_INTEL_PROVIDER_REQUEST_RESERVATION_CENTS:-100}"
            ),
        ),
        "tests/fixtures/m1_operator_lifecycle_harness.sh": (
            "AI_INTEL_PROVIDER_MONTHLY_BUDGET_CENTS=11500",
            "AI_INTEL_PROVIDER_REQUEST_RESERVATION_CENTS=100",
        ),
        "docs/mvp-production-runbook.md": (
            "Set `AI_INTEL_PROVIDER_MONTHLY_BUDGET_CENTS` no higher than `11500`.",
            "The ledger never\nrefunds a reservation",
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
def test_existing_10000_budget_can_reserve_to_11500_without_refunds(
    budget_hotfix_database_url: str,
) -> None:
    config = _seed_0008_budget(budget_hotfix_database_url)
    command.upgrade(config, "head")

    engine = create_database_engine(budget_hotfix_database_url)
    budget = PersistentMeteredProviderBudget(
        engine,
        monthly_limit_cents=11_500,
        request_reservation_cents=100,
        today=lambda: date(2026, 8, 20),
    )
    try:
        assert [budget.reserve() for _ in range(15)] == [True] * 15
        assert budget.reserve() is False
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
