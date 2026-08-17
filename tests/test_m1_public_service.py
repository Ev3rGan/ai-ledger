from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient
from pg0 import Pg0
from sqlalchemy.engine import make_url
from typer.testing import CliRunner

import ai_intel_agent.cli as cli_module
from ai_intel_agent.cli import app
from ai_intel_agent.persistence import (
    PersistentMeteredProviderBudget,
    SchedulerStatusRepository,
    create_database_engine,
    upgrade_database,
)
from ai_intel_agent.pipeline import publish_sample_digest
from ai_intel_agent.research import (
    DeepSeekResearchProvider,
    ResearchError,
    ResearchEvidenceSet,
    ResearchRepository,
)
from ai_intel_agent.runtime import (
    GeminiScheduler,
    M1WebConfiguration,
    PostgresSchedulerLease,
    ServiceJsonFormatter,
)
from ai_intel_agent.web import create_app

runner = CliRunner()


@pytest.fixture(scope="module")
def m1_database_url() -> Iterator[str]:
    name = f"ai_intel_m1_{os.urandom(8).hex()}"
    data_dir = Path(tempfile.gettempdir()) / name
    server = Pg0(name=name, data_dir=data_dir)
    server.start()
    try:
        upgrade_database(server.uri)
        yield server.uri
    finally:
        server.drop()


@pytest.fixture
def production_environment(
    m1_database_url: str,
    tmp_path: Path,
) -> dict[str, str]:
    parsed_database = make_url(m1_database_url)
    secret_values = {
        "database-password": parsed_database.password or "",
        "provider-key": "fixture-provider-key",
        "identity-salt": "fixture-production-salt",
    }
    secret_paths: dict[str, str] = {}
    for name, value in secret_values.items():
        path = tmp_path / name
        path.write_text(value, encoding="utf-8")
        secret_paths[name] = str(path)
    return {
        "AI_INTEL_DATABASE_HOST": parsed_database.host or "",
        "AI_INTEL_DATABASE_PORT": str(parsed_database.port or 5432),
        "AI_INTEL_DATABASE_NAME": parsed_database.database or "",
        "AI_INTEL_DATABASE_USER": parsed_database.username or "",
        "AI_INTEL_DATABASE_PASSWORD_FILE": secret_paths["database-password"],
        "DEEPSEEK_API_KEY_FILE": secret_paths["provider-key"],
        "AI_INTEL_ANONYMOUS_ID_SALT_FILE": secret_paths["identity-salt"],
        "AI_INTEL_ANONYMOUS_RESEARCH_DAILY_LIMIT": "1",
        "AI_INTEL_PROVIDER_MONTHLY_BUDGET_CENTS": "10000",
        "AI_INTEL_PROVIDER_REQUEST_RESERVATION_CENTS": "100",
    }


class EvidenceEchoProvider:
    def __init__(self) -> None:
        self.calls = 0

    def stream(self, evidence_set: ResearchEvidenceSet) -> Iterator[str]:
        self.calls += 1
        evidence = evidence_set.evidence[0]
        yield json.dumps(
            {
                "answer": "AI Agent 会记录任务轨迹。",
                "citations": [
                    {
                        "story_id": str(evidence.story_id),
                        "claim_id": str(evidence.claim_id),
                        "evidence_span_id": str(evidence.evidence_span_id),
                    }
                ],
            },
            ensure_ascii=False,
        )


def _sse_events(body: str) -> list[tuple[str, dict[str, object]]]:
    events: list[tuple[str, dict[str, object]]] = []
    for block in body.strip().split("\n\n"):
        lines = block.splitlines()
        event = next(line.removeprefix("event: ") for line in lines if line.startswith("event: "))
        data = next(line.removeprefix("data: ") for line in lines if line.startswith("data: "))
        events.append((event, json.loads(data)))
    return events


def test_anonymous_research_allowance_persists_and_blocks_before_provider(
    m1_database_url: str,
) -> None:
    publish_sample_digest(m1_database_url)
    provider = EvidenceEchoProvider()
    client_headers = {"X-AI-Anonymous-Client": "203.0.113.7"}

    with TestClient(
        create_app(
            m1_database_url,
            research_provider=provider,
            anonymous_research_daily_limit=1,
            anonymous_identity_salt=b"fixture-only-salt",
        )
    ) as client:
        first = client.post(
            "/research/answer",
            json={"question": "示例发布者的 AI Agent 会记录任务轨迹"},
            headers=client_headers,
        )
        excess = client.post(
            "/research/answer",
            json={"question": "示例发布者的 AI Agent 会记录任务轨迹"},
            headers=client_headers,
        )

    assert first.status_code == 200
    first_events = _sse_events(first.text)
    assert first_events[-1][1]["status"] == "answered", (first_events, provider.calls)
    assert [event for event, _ in _sse_events(excess.text)] == [
        "status",
        "refusal",
        "done",
    ]
    assert _sse_events(excess.text)[1][1]["code"] == "anonymous-allowance-exhausted"
    assert provider.calls == 1

    with TestClient(
        create_app(
            m1_database_url,
            research_provider=provider,
            anonymous_research_daily_limit=1,
            anonymous_identity_salt=b"fixture-only-salt",
        )
    ) as restarted_client:
        after_restart = restarted_client.post(
            "/research/answer",
            json={"question": "示例发布者的 AI Agent 会记录任务轨迹"},
            headers=client_headers,
        )

    assert _sse_events(after_restart.text)[1][1]["code"] == "anonymous-allowance-exhausted"
    assert provider.calls == 1


def test_aggregate_provider_budget_persists_and_fails_before_http(
    m1_database_url: str,
) -> None:
    publish_sample_digest(m1_database_url)
    engine = create_database_engine(m1_database_url)
    def fixed_day() -> date:
        return datetime(2026, 8, 17, tzinfo=UTC).date()
    first_process_budget = PersistentMeteredProviderBudget(
        engine,
        monthly_limit_cents=100,
        request_reservation_cents=60,
        today=fixed_day,
    )
    restarted_process_budget = PersistentMeteredProviderBudget(
        engine,
        monthly_limit_cents=100,
        request_reservation_cents=60,
        today=fixed_day,
    )
    requests: list[httpx.Request] = []

    def unexpected_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500, request=request)

    try:
        assert first_process_budget.reserve() is True
        evidence = ResearchRepository(engine).retrieve(
            "示例发布者的 AI Agent 会记录任务轨迹"
        )
        with (
            httpx.Client(transport=httpx.MockTransport(unexpected_request)) as client,
            pytest.raises(ResearchError, match="monthly Provider budget"),
        ):
            tuple(
                DeepSeekResearchProvider(
                    client,
                    api_key="fixture-provider-key",
                    budget=restarted_process_budget,
                ).stream(evidence)
            )
    finally:
        engine.dispose()

    assert requests == []

def test_internal_health_endpoints_expose_only_liveness_and_database_readiness(
    m1_database_url: str,
) -> None:
    with TestClient(create_app(m1_database_url)) as client:
        live = client.get("/health/live")
        ready = client.get("/health/ready")

    assert live.status_code == 200
    assert live.json() == {"status": "live"}
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}


def test_scheduler_has_one_effective_lease_and_persists_recent_status(
    m1_database_url: str,
) -> None:
    engine = create_database_engine(m1_database_url)
    status = SchedulerStatusRepository(engine)
    now = datetime(2026, 8, 17, 5, 59, tzinfo=ZoneInfo("Asia/Shanghai"))
    wait_results = iter((False, True))
    collections: list[str] = []
    scheduler = GeminiScheduler(
        collect=lambda: collections.append("collected"),
        now=lambda: now,
        wait=lambda _: next(wait_results),
        status=status,
    )

    try:
        with PostgresSchedulerLease(engine), pytest.raises(
            RuntimeError, match="already active"
        ), PostgresSchedulerLease(engine):
            pass
        with PostgresSchedulerLease(engine):
            scheduler.run()

        snapshot = status.snapshot()
    finally:
        engine.dispose()

    assert collections == ["collected"]
    assert snapshot is not None
    assert snapshot.state == "stopped"
    assert snapshot.last_result == "succeeded"
    assert snapshot.next_run_at is not None


def test_scheduler_lease_guard_fails_when_lock_holding_session_is_lost(
    m1_database_url: str,
) -> None:
    engine = create_database_engine(m1_database_url)
    lease = PostgresSchedulerLease(engine)
    try:
        with lease:
            pass
        with pytest.raises(RuntimeError, match="lease was lost"):
            lease.guarded_wait(lambda _: False, 0)
    finally:
        engine.dispose()


def test_scheduler_lease_monitor_guards_collection_io(
    m1_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_database_engine(m1_database_url)
    lease = PostgresSchedulerLease(engine)
    lease_lost = Event()

    def fail_lease_check() -> None:
        raise RuntimeError("Production Scheduler lease was lost")

    try:
        with lease, lease.monitor(
            lease_lost.set,
            check_interval_seconds=0.01,
        ):
            assert lease_lost.wait(timeout=0.05) is False

        monkeypatch.setattr(lease, "assert_held", fail_lease_check)
        with lease.monitor(lease_lost.set, check_interval_seconds=0.01):
            assert lease_lost.wait(timeout=1)
    finally:
        engine.dispose()


def test_production_configuration_reads_secrets_from_files_and_redacts_repr(
    production_environment: dict[str, str],
) -> None:
    configuration = M1WebConfiguration.from_environment(production_environment)

    parsed_database = make_url(configuration.database.database_url)
    assert parsed_database.host == production_environment["AI_INTEL_DATABASE_HOST"]
    assert parsed_database.database == production_environment["AI_INTEL_DATABASE_NAME"]
    assert configuration.anonymous_research_daily_limit == 1
    assert configuration.provider.monthly_budget_cents == 10_000
    assert parsed_database.password not in repr(configuration)
    assert "fixture-provider-key" not in repr(configuration)
    assert "fixture-production-salt" not in repr(configuration)


def test_private_operator_status_reports_database_and_recent_scheduler_only(
    m1_database_url: str,
) -> None:
    result = runner.invoke(
        app,
        ["operator", "status"],
        env={"AI_INTEL_DATABASE_URL": m1_database_url},
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["database"] == "ready"
    assert set(payload) == {"database", "scheduler"}
    assert "database_url" not in result.output


def test_production_serve_wires_secret_files_and_persistent_allowance(
    m1_database_url: str,
    production_environment: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish_sample_digest(m1_database_url)
    provider = EvidenceEchoProvider()
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        cli_module,
        "DeepSeekResearchProvider",
        lambda *_args, **_kwargs: provider,
    )
    monkeypatch.setitem(
        sys.modules,
        "uvicorn",
        SimpleNamespace(run=lambda web_app, **_: captured.update(app=web_app)),
    )
    result = runner.invoke(
        app,
        ["serve", "--production"],
        env=production_environment,
    )

    assert result.exit_code == 0
    with TestClient(captured["app"]) as client:
        headers = {"X-AI-Anonymous-Client": "198.51.100.23"}
        first = client.post(
            "/research/answer",
            json={"question": "示例发布者的 AI Agent 会记录任务轨迹"},
            headers=headers,
        )
        excess = client.post(
            "/research/answer",
            json={"question": "示例发布者的 AI Agent 会记录任务轨迹"},
            headers=headers,
        )

    production_events = _sse_events(first.text)
    assert production_events[-1][1]["status"] == "answered", (
        production_events,
        provider.calls,
    )
    assert _sse_events(excess.text)[1][1]["code"] == "anonymous-allowance-exhausted"
    assert provider.calls == 1


def test_production_scheduler_fails_before_collection_when_lease_is_held(
    m1_database_url: str,
    production_environment: dict[str, str],
) -> None:
    engine = create_database_engine(m1_database_url)
    try:
        with PostgresSchedulerLease(engine):
            result = runner.invoke(
                app,
                ["schedule-gemini", "--production"],
                env=production_environment,
            )
    finally:
        engine.dispose()

    assert result.exit_code != 0
    assert "already active" in result.output


def test_manual_collection_in_production_cannot_bypass_provider_budget(
    production_environment: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def capture_collection(
        _backfill_days: int,
        **arguments: object,
    ) -> None:
        captured.update(arguments)

    monkeypatch.setattr(cli_module, "_run_gemini_collection", capture_collection)

    result = runner.invoke(
        app,
        ["collect-gemini"],
        env=production_environment,
    )

    assert result.exit_code == 0
    assert isinstance(
        captured["provider_budget"],
        PersistentMeteredProviderBudget,
    )


def test_service_log_formatter_emits_one_structured_json_record() -> None:
    record = logging.LogRecord(
        name="ai_intel_agent.service",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="service-ready",
        args=(),
        exc_info=None,
    )

    payload = json.loads(ServiceJsonFormatter().format(record))

    assert payload["level"] == "INFO"
    assert payload["logger"] == "ai_intel_agent.service"
    assert payload["message"] == "service-ready"
    assert payload["timestamp"].endswith("Z")


def test_versioned_linux_bundle_keeps_only_https_boundary_public() -> None:
    project_root = Path(__file__).parents[1]
    compose = (project_root / "deploy" / "m1" / "production.compose.yml").read_text(
        encoding="utf-8"
    )
    caddy = (project_root / "deploy" / "m1" / "Caddyfile").read_text(
        encoding="utf-8"
    )
    dockerfile = (project_root / "deploy" / "m1" / "production.Dockerfile").read_text(
        encoding="utf-8"
    )
    dockerignore = (project_root / ".dockerignore").read_text(encoding="utf-8")

    assert "AI_INTEL_IMAGE:?" in compose
    assert all(f"  {service}:" in compose for service in ("caddy", "web", "scheduler", "postgres", "backup"))
    assert 'command: ["serve", "--production"' in compose
    assert 'command: ["schedule-gemini", "--production"' in compose
    assert "80:80" in compose and "443:443" in compose
    postgres_block = compose.split("\n  postgres:\n", 1)[1].split("\n  web:\n", 1)[0]
    assert "ports:" not in postgres_block
    assert "internal: true" in compose
    assert "restart: unless-stopped" in compose
    assert "max-size: \"10m\"" in compose
    assert "max-file: \"5\"" in compose
    assert "AI_INTEL_DATABASE_PASSWORD_FILE" in compose
    assert "DEEPSEEK_API_KEY_FILE" in compose
    assert "AI_INTEL_ANONYMOUS_ID_SALT_FILE" in compose
    migrate_block = compose.split("\n  migrate:\n", 1)[1].split(
        "\n  restore-postgres:\n", 1
    )[0]
    assert "deepseek-api-key" not in migrate_block
    assert "anonymous-id-salt" not in migrate_block
    assert "AI_INTEL_OFFSITE_BACKUP_DIR" in compose
    assert 'group_add:\n    - "10001"' in compose
    assert "health/ready" in compose
    assert "{$AI_INTEL_DOMAIN}" in caddy
    assert "X-AI-Anonymous-Client {client_ip}" in caddy
    assert "respond @internal_health 404" in caddy
    assert "USER 10001:10001" in dockerfile
    assert "org.opencontainers.image.revision" in dockerfile
    assert "uv sync --locked --no-dev --no-editable" in dockerfile
    assert "python:3.12.10-slim-bookworm@sha256:" in dockerfile
    assert all(pattern in dockerignore for pattern in (".env", ".git", "secrets", "backups", "reports"))


def test_operator_script_supports_lifecycle_backup_restore_and_rollback() -> None:
    project_root = Path(__file__).parents[1]
    operator = (project_root / "deploy" / "m1" / "operate.sh").read_text(
        encoding="utf-8"
    )
    backup = (project_root / "deploy" / "m1" / "backup.sh").read_text(
        encoding="utf-8"
    )
    restore = (project_root / "deploy" / "m1" / "restore.sh").read_text(
        encoding="utf-8"
    )

    assert all(
        f'"{operation}")' in operator
        for operation in (
            "validate",
            "start",
            "stop",
            "restart",
            "upgrade",
            "rollback",
            "status",
            "logs",
            "backup",
            "restore-isolated",
        )
    )
    assert "@sha256:" in operator
    assert "AI_INTEL_RELEASE_DIR" in operator
    assert 'exec sh "$recorded_operator" "$@"' in operator
    assert "status --porcelain --untracked-files=all" in operator
    assert "mountpoint --quiet" in operator
    assert "operator migrate" in operator
    activate_block = operator.split("activate_release() {", 1)[1].split(
        "\n}\n\nstart_release()", 1
    )[0]
    assert activate_block.index("up --detach --wait postgres") < activate_block.index(
        'migrate "$release_file"'
    )
    assert "restart caddy web scheduler backup postgres" in operator
    assert 'activate_release "$previous_release" 0' in operator
    assert "AI_INTEL_BACKUP_ONCE" in backup
    assert "pg_dump" in backup and "pg_restore --list" in backup
    assert "/offsite-backups" in backup and '"offsite_copy":"verified"' in backup
    assert "pg_restore" in restore and "restore-postgres" in restore
    assert "/run/secrets/database-password" in backup
    assert "/run/secrets/database-password" in restore


def test_private_operator_migrate_accepts_production_secret_contract(
    production_environment: dict[str, str],
) -> None:
    database_only_environment = {
        key: value
        for key, value in production_environment.items()
        if key.startswith("AI_INTEL_DATABASE_")
    }
    result = runner.invoke(
        app,
        ["operator", "migrate", "--production"],
        env=database_only_environment,
    )

    assert result.exit_code == 0
    assert '"database": "migrated"' in result.output


def test_scheduler_health_command_fails_closed_for_non_active_status(
    m1_database_url: str,
) -> None:
    engine = create_database_engine(m1_database_url)
    status = SchedulerStatusRepository(engine)
    now = datetime.now(UTC)
    try:
        status.waiting(next_run_at=now + timedelta(hours=1), observed_at=now)
        healthy = runner.invoke(
            app,
            ["operator", "scheduler-health"],
            env={"AI_INTEL_DATABASE_URL": m1_database_url},
        )
        status.failed(completed_at=now)
        failed = runner.invoke(
            app,
            ["operator", "scheduler-health"],
            env={"AI_INTEL_DATABASE_URL": m1_database_url},
        )
    finally:
        engine.dispose()

    assert healthy.exit_code == 0
    assert failed.exit_code != 0


def test_existing_private_editorial_cli_uses_production_secret_files(
    production_environment: dict[str, str],
) -> None:
    database_only_environment = {
        key: value
        for key, value in production_environment.items()
        if key.startswith("AI_INTEL_DATABASE_")
    }
    result = runner.invoke(
        app,
        ["story", "list"],
        env=database_only_environment,
    )

    assert result.exit_code == 0
    assert "database" not in result.output.lower()
