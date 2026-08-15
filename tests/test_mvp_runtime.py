from __future__ import annotations

import signal
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from ai_intel_agent.cli import app
from ai_intel_agent.runtime import (
    DockerComposeDatabase,
    GeminiSchedule,
    GeminiScheduler,
    LocalMvpConfiguration,
    LocalMvpRuntime,
    LocalMvpState,
    MvpChildProcesses,
    SchedulerStopController,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
runner = CliRunner()


class RecordingDatabase:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.stopped = False

    def start(self) -> None:
        self.events.append("database:start")

    def stop(self) -> None:
        self.events.append("database:stop")
        self.stopped = True


class RecordingProcesses:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.stopped = False

    def start(self) -> None:
        self.events.append("processes:start")

    def wait(self) -> None:
        self.events.append("processes:wait")

    def stop(self) -> None:
        self.events.append("processes:stop")
        self.stopped = True


class FailingStopProcesses(RecordingProcesses):
    def stop(self) -> None:
        super().stop()
        raise RuntimeError("process cleanup failed")


def test_gemini_schedule_uses_the_next_0600_or_1800_asia_shanghai_slot() -> None:
    schedule = GeminiSchedule()

    assert schedule.next_after(datetime(2026, 8, 15, 5, 59, tzinfo=SHANGHAI)) == datetime(
        2026, 8, 15, 6, 0, tzinfo=SHANGHAI
    )
    assert schedule.next_after(datetime(2026, 8, 15, 6, 0, tzinfo=SHANGHAI)) == datetime(
        2026, 8, 15, 18, 0, tzinfo=SHANGHAI
    )
    assert schedule.next_after(datetime(2026, 8, 15, 18, 0, tzinfo=SHANGHAI)) == datetime(
        2026, 8, 16, 6, 0, tzinfo=SHANGHAI
    )


def test_local_runtime_reaches_running_state_and_always_stops_owned_resources() -> None:
    events: list[str] = []
    states: list[LocalMvpState] = []
    database = RecordingDatabase(events)
    processes = RecordingProcesses(events)

    runtime = LocalMvpRuntime(
        database=database,
        migrate=lambda: events.append("database:migrate"),
        processes=processes,
        state_changed=states.append,
    )

    runtime.run()

    assert states == [
        LocalMvpState.DATABASE_STARTING,
        LocalMvpState.DATABASE_HEALTHY,
        LocalMvpState.MIGRATED,
        LocalMvpState.SCHEDULER_AND_WEB_RUNNING,
        LocalMvpState.STOPPING,
        LocalMvpState.STOPPED,
    ]
    assert events == [
        "database:start",
        "database:migrate",
        "processes:start",
        "processes:wait",
        "processes:stop",
        "database:stop",
    ]
    assert database.stopped
    assert processes.stopped


def test_local_runtime_stops_database_when_migration_fails() -> None:
    events: list[str] = []
    states: list[LocalMvpState] = []
    database = RecordingDatabase(events)
    processes = RecordingProcesses(events)

    def fail_migration() -> None:
        events.append("database:migrate")
        raise RuntimeError("migration failed")

    runtime = LocalMvpRuntime(
        database=database,
        migrate=fail_migration,
        processes=processes,
        state_changed=states.append,
    )

    with pytest.raises(RuntimeError, match="migration failed"):
        runtime.run()

    assert states == [
        LocalMvpState.DATABASE_STARTING,
        LocalMvpState.DATABASE_HEALTHY,
        LocalMvpState.STOPPING,
        LocalMvpState.STOPPED,
    ]
    assert events == ["database:start", "database:migrate", "database:stop"]
    assert database.stopped
    assert not processes.stopped


def test_local_runtime_stops_database_and_reaches_stopped_if_process_cleanup_fails() -> None:
    events: list[str] = []
    states: list[LocalMvpState] = []
    database = RecordingDatabase(events)
    processes = FailingStopProcesses(events)
    runtime = LocalMvpRuntime(
        database=database,
        migrate=lambda: events.append("database:migrate"),
        processes=processes,
        state_changed=states.append,
    )

    with pytest.raises(RuntimeError, match="process cleanup failed"):
        runtime.run()

    assert database.stopped
    assert states[-1] is LocalMvpState.STOPPED


def test_local_configuration_derives_compose_settings_without_exposing_credentials() -> None:
    configuration = LocalMvpConfiguration.from_environment(
        {
            "AI_INTEL_DATABASE_URL": (
                "postgresql+psycopg://mvp-user:mvp-password@127.0.0.1:55432/mvp-db"
            ),
            "DEEPSEEK_API_KEY": "provider-secret",
        }
    )

    assert configuration.compose_environment == {
        "MVP_POSTGRES_DATABASE": "mvp-db",
        "MVP_POSTGRES_PASSWORD": "mvp-password",
        "MVP_POSTGRES_PORT": "55432",
        "MVP_POSTGRES_USER": "mvp-user",
    }
    assert "mvp-password" not in repr(configuration)
    assert "provider-secret" not in repr(configuration)


@pytest.mark.parametrize(
    ("environment", "message"),
    [
        ({}, "AI_INTEL_DATABASE_URL"),
        (
            {
                "AI_INTEL_DATABASE_URL": "postgresql://user:pass@db.example/mvp",
                "DEEPSEEK_API_KEY": "present",
            },
            "local PostgreSQL",
        ),
        (
            {
                "AI_INTEL_DATABASE_URL": "postgresql://user:pass@localhost/mvp",
                "DEEPSEEK_API_KEY": "",
            },
            "DEEPSEEK_API_KEY",
        ),
        (
            {
                "AI_INTEL_DATABASE_URL": "not a URL",
                "DEEPSEEK_API_KEY": "present",
            },
            "valid PostgreSQL URL",
        ),
    ],
)
def test_local_configuration_fails_closed_without_explicit_local_credentials(
    environment: dict[str, str], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        LocalMvpConfiguration.from_environment(environment)


def test_scheduler_collects_once_after_waiting_for_the_next_fixed_slot() -> None:
    waits: list[float] = []
    collections: list[str] = []
    wait_results = iter((False, True))
    current = datetime(2026, 8, 15, 5, 59, tzinfo=SHANGHAI)

    def wait(seconds: float) -> bool:
        waits.append(seconds)
        return next(wait_results)

    scheduler = GeminiScheduler(
        collect=lambda: collections.append("collected"),
        now=lambda: current,
        wait=wait,
    )

    scheduler.run()

    assert waits == [60.0, 60.0]
    assert collections == ["collected"]


def test_scheduler_stop_controller_handles_windows_break_and_restores_handler() -> None:
    previous_handler = signal.getsignal(signal.SIGBREAK)

    with SchedulerStopController() as controller:
        installed_handler = signal.getsignal(signal.SIGBREAK)
        assert callable(installed_handler)
        installed_handler(signal.SIGBREAK, None)
        assert controller.wait(0)

    assert signal.getsignal(signal.SIGBREAK) is previous_handler


class RecordingCommandRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path, dict[str, str]]] = []

    def __call__(
        self,
        command: list[str],
        *,
        cwd: Path,
        environment: dict[str, str],
    ) -> None:
        self.calls.append((command, cwd, environment))


def test_compose_database_keeps_password_in_child_environment_not_command() -> None:
    runner = RecordingCommandRunner()
    project_root = Path("C:/managed-worktree")
    environment = {
        "MVP_POSTGRES_DATABASE": "mvp",
        "MVP_POSTGRES_PASSWORD": "not-in-argv",
        "MVP_POSTGRES_PORT": "55432",
        "MVP_POSTGRES_USER": "mvp",
    }
    database = DockerComposeDatabase(
        project_root=project_root,
        compose_environment=environment,
        run_command=runner,
    )

    database.start()
    database.stop()

    assert [call[0][-4:] for call in runner.calls] == [
        ["--wait", "--wait-timeout", "60", "postgres"],
        ["stop", "--timeout", "10", "postgres"],
    ]
    assert all(call[1] == project_root for call in runner.calls)
    assert all(call[2]["COMPOSE_DISABLE_ENV_FILE"] == "1" for call in runner.calls)
    assert all(call[2]["MVP_POSTGRES_PASSWORD"] == "not-in-argv" for call in runner.calls)
    assert all("not-in-argv" not in " ".join(call[0]) for call in runner.calls)


class RecordingChild:
    def __init__(
        self,
        command: list[str],
        *,
        timeout_during_graceful_stop: bool = False,
    ) -> None:
        self.command = command
        self.returncode: int | None = None
        self.stop_signals: list[int] = []
        self.killed = False
        self.timeout_during_graceful_stop = timeout_during_graceful_stop
        self.lifecycle: list[str] = []

    def poll(self) -> int | None:
        return self.returncode

    def send_signal(self, requested_signal: int) -> None:
        self.stop_signals.append(requested_signal)
        self.lifecycle.append("graceful-stop")

    def wait(self, timeout: float | None = None) -> int:
        self.lifecycle.append("wait")
        if self.timeout_during_graceful_stop and not self.killed:
            raise subprocess.TimeoutExpired(self.command, timeout)
        self.returncode = 0
        return 0

    def kill(self) -> None:
        self.killed = True
        self.lifecycle.append("kill")


class RecordingProcessFactory:
    def __init__(
        self,
        *,
        fail_on: int | None = None,
        timeout_during_graceful_stop: bool = False,
    ) -> None:
        self.fail_on = fail_on
        self.timeout_during_graceful_stop = timeout_during_graceful_stop
        self.calls: list[tuple[list[str], Path, dict[str, str]]] = []
        self.children: list[RecordingChild] = []

    def __call__(
        self,
        command: list[str],
        *,
        cwd: Path,
        environment: dict[str, str],
    ) -> RecordingChild:
        self.calls.append((command, cwd, environment))
        if self.fail_on == len(self.calls):
            raise OSError("process start failed")
        child = RecordingChild(
            command,
            timeout_during_graceful_stop=self.timeout_during_graceful_stop,
        )
        self.children.append(child)
        return child


def test_child_processes_start_supported_scheduler_and_serve_commands_and_stop_both() -> None:
    factory = RecordingProcessFactory()
    project_root = Path("C:/managed-worktree")
    processes = MvpChildProcesses(
        project_root=project_root,
        python_executable="C:/Python/python.exe",
        host="127.0.0.1",
        port=8125,
        environment={"AI_INTEL_DATABASE_URL": "in-memory", "DEEPSEEK_API_KEY": "secret"},
        start_process=factory,
    )

    processes.start()
    processes.stop()

    assert [call[0] for call in factory.calls] == [
        ["C:/Python/python.exe", "-m", "ai_intel_agent.cli", "schedule-gemini"],
        [
            "C:/Python/python.exe",
            "-m",
            "ai_intel_agent.cli",
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            "8125",
        ],
    ]
    assert all(child.stop_signals == [signal.CTRL_BREAK_EVENT] for child in factory.children)
    assert all(not child.killed for child in factory.children)
    assert all("secret" not in " ".join(child.command) for child in factory.children)


def test_child_processes_roll_back_scheduler_when_web_process_cannot_start() -> None:
    factory = RecordingProcessFactory(fail_on=2)
    processes = MvpChildProcesses(
        project_root=Path("C:/managed-worktree"),
        python_executable="python.exe",
        host="127.0.0.1",
        port=8000,
        environment={},
        start_process=factory,
    )

    with pytest.raises(OSError, match="process start failed"):
        processes.start()

    assert factory.children[0].stop_signals == [signal.CTRL_BREAK_EVENT]


def test_child_processes_force_kill_only_after_graceful_stop_times_out() -> None:
    factory = RecordingProcessFactory(timeout_during_graceful_stop=True)
    processes = MvpChildProcesses(
        project_root=Path("C:/managed-worktree"),
        python_executable="python.exe",
        host="127.0.0.1",
        port=8000,
        environment={},
        start_process=factory,
    )
    processes.start()

    processes.stop()

    assert all(
        child.lifecycle == ["graceful-stop", "wait", "kill", "wait"]
        for child in factory.children
    )


def test_cli_exposes_the_supported_local_start_and_scheduler_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "start-local" in result.output
    assert "schedule-gemini" in result.output


def test_local_start_fails_before_docker_without_explicit_process_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AI_INTEL_DATABASE_URL", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    result = runner.invoke(app, ["start-local"])

    assert result.exit_code != 0
    assert "AI_INTEL_DATABASE_URL" in result.output


def test_mvp_compose_is_loopback_only_healthy_and_persistent() -> None:
    compose = (Path(__file__).parents[1] / "docker" / "mvp.compose.yml").read_text(
        encoding="utf-8"
    )

    assert "pgvector/pgvector:0.8.6-pg16-bookworm" in compose
    assert "127.0.0.1:${MVP_POSTGRES_PORT:?required}:5432" in compose
    assert "POSTGRES_PASSWORD: ${MVP_POSTGRES_PASSWORD:?required}" in compose
    assert "healthcheck:" in compose
    assert "mvp-postgres:/var/lib/postgresql/data" in compose
