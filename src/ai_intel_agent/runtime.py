from __future__ import annotations

import os
import signal
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from enum import StrEnum
from pathlib import Path
from threading import Event
from time import sleep
from types import FrameType
from typing import Protocol, Self
from zoneinfo import ZoneInfo

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

SHANGHAI = ZoneInfo("Asia/Shanghai")
COLLECTION_TIMES = (time(6, 0), time(18, 0))

if os.name == "nt":
    CHILD_PROCESS_OPTIONS: dict[str, bool | int] = {
        "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP
    }
    CHILD_PROCESS_STOP_SIGNAL = signal.CTRL_BREAK_EVENT
    SCHEDULER_STOP_SIGNALS = (signal.SIGINT, signal.SIGBREAK)
else:
    CHILD_PROCESS_OPTIONS = {"start_new_session": True}
    CHILD_PROCESS_STOP_SIGNAL = signal.SIGTERM
    SCHEDULER_STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)


class LocalMvpState(StrEnum):
    DATABASE_STARTING = "database-starting"
    DATABASE_HEALTHY = "database-healthy"
    MIGRATED = "migrated"
    SCHEDULER_AND_WEB_RUNNING = "scheduler-and-web-running"
    STOPPING = "stopping"
    STOPPED = "stopped"


class GeminiSchedule:
    """The fixed twice-daily Gemini collection schedule."""

    def next_after(self, instant: datetime) -> datetime:
        if instant.tzinfo is None:
            raise ValueError("Schedule instants must be timezone-aware")
        local = instant.astimezone(SHANGHAI)
        for collection_time in COLLECTION_TIMES:
            candidate = datetime.combine(local.date(), collection_time, SHANGHAI)
            if candidate > local:
                return candidate
        return datetime.combine(
            local.date() + timedelta(days=1), COLLECTION_TIMES[0], SHANGHAI
        )


class GeminiScheduler:
    """Run collection at the next fixed Shanghai schedule slot until stopped."""

    def __init__(
        self,
        *,
        collect: Callable[[], None],
        now: Callable[[], datetime],
        wait: Callable[[float], bool],
        schedule: GeminiSchedule | None = None,
    ) -> None:
        self._collect = collect
        self._now = now
        self._wait = wait
        self._schedule = schedule or GeminiSchedule()

    def run(self) -> None:
        while True:
            current = self._now()
            due = self._schedule.next_after(current)
            delay = (due.astimezone(UTC) - current.astimezone(UTC)).total_seconds()
            if self._wait(max(0.0, delay)):
                return
            self._collect()


class SchedulerStopController:
    """Translate process stop signals into the scheduler's cooperative wait event."""

    def __init__(self) -> None:
        self._stopped = Event()
        self._previous_handlers: dict[signal.Signals, object] = {}

    def __enter__(self) -> Self:
        for stop_signal in SCHEDULER_STOP_SIGNALS:
            self._previous_handlers[stop_signal] = signal.signal(
                stop_signal, self._handle_signal
            )
        return self

    def __exit__(self, *_: object) -> None:
        for stop_signal, previous_handler in self._previous_handlers.items():
            signal.signal(stop_signal, previous_handler)
        self._previous_handlers.clear()

    def wait(self, seconds: float) -> bool:
        return self._stopped.wait(seconds)

    def _handle_signal(self, _signum: int, _frame: FrameType | None) -> None:
        self._stopped.set()


@dataclass(frozen=True)
class LocalMvpConfiguration:
    """Validated process-only configuration for the supported local runtime."""

    database_url: str = field(repr=False)
    compose_environment: Mapping[str, str] = field(repr=False)

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> LocalMvpConfiguration:
        database_url = environment.get("AI_INTEL_DATABASE_URL", "").strip()
        if not database_url:
            raise ValueError("Set AI_INTEL_DATABASE_URL explicitly in this process")
        if not environment.get("DEEPSEEK_API_KEY", "").strip():
            raise ValueError("Set DEEPSEEK_API_KEY explicitly in this process")

        try:
            parsed = make_url(database_url)
        except (ArgumentError, TypeError, ValueError) as error:
            raise ValueError(
                "AI_INTEL_DATABASE_URL must be a valid PostgreSQL URL"
            ) from error
        if not parsed.drivername.startswith("postgresql"):
            raise ValueError("AI_INTEL_DATABASE_URL must point to local PostgreSQL")
        if parsed.host not in {"127.0.0.1", "localhost"}:
            raise ValueError("AI_INTEL_DATABASE_URL must point to local PostgreSQL")
        if not parsed.username or not parsed.password or not parsed.database:
            raise ValueError(
                "AI_INTEL_DATABASE_URL requires a database, user, and password"
            )

        return cls(
            database_url=database_url,
            compose_environment={
                "MVP_POSTGRES_DATABASE": parsed.database,
                "MVP_POSTGRES_PASSWORD": parsed.password,
                "MVP_POSTGRES_PORT": str(parsed.port or 5432),
                "MVP_POSTGRES_USER": parsed.username,
            },
        )


class ManagedDatabase(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...


class ManagedProcesses(Protocol):
    def start(self) -> None: ...

    def wait(self) -> None: ...

    def stop(self) -> None: ...


class ChildProcess(Protocol):
    returncode: int | None

    def poll(self) -> int | None: ...

    def send_signal(self, requested_signal: int) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def kill(self) -> None: ...


CommandRunner = Callable[..., None]
ProcessFactory = Callable[..., ChildProcess]


def _run_command(
    command: list[str], *, cwd: Path, environment: dict[str, str]
) -> None:
    subprocess.run(command, cwd=cwd, env=environment, check=True)


def _start_process(
    command: list[str], *, cwd: Path, environment: dict[str, str]
) -> ChildProcess:
    return subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        **CHILD_PROCESS_OPTIONS,
    )


class DockerComposeDatabase:
    """Own the local MVP's single Compose PostgreSQL service."""

    def __init__(
        self,
        *,
        project_root: Path,
        compose_environment: Mapping[str, str],
        run_command: CommandRunner = _run_command,
    ) -> None:
        self._project_root = project_root
        self._environment = {**os.environ, **compose_environment}
        self._environment["COMPOSE_DISABLE_ENV_FILE"] = "1"
        self._run_command = run_command
        self._compose_prefix = [
            "docker",
            "compose",
            "--project-name",
            "ai-ledger-mvp",
            "--file",
            str(project_root / "docker" / "mvp.compose.yml"),
        ]

    def start(self) -> None:
        self._run_command(
            [
                *self._compose_prefix,
                "up",
                "--detach",
                "--wait",
                "--wait-timeout",
                "60",
                "postgres",
            ],
            cwd=self._project_root,
            environment=self._environment,
        )

    def stop(self) -> None:
        self._run_command(
            [*self._compose_prefix, "stop", "--timeout", "10", "postgres"],
            cwd=self._project_root,
            environment=self._environment,
        )


class MvpChildProcesses:
    """Own the scheduled collector and formal Web server child processes."""

    def __init__(
        self,
        *,
        project_root: Path,
        host: str,
        port: int,
        environment: Mapping[str, str],
        python_executable: str = sys.executable,
        start_process: ProcessFactory = _start_process,
        idle: Callable[[float], None] = sleep,
    ) -> None:
        self._project_root = project_root
        self._python_executable = python_executable
        self._host = host
        self._port = port
        self._environment = dict(environment)
        self._start_process = start_process
        self._idle = idle
        self._children: list[ChildProcess] = []

    def start(self) -> None:
        commands = (
            [
                self._python_executable,
                "-m",
                "ai_intel_agent.cli",
                "schedule-gemini",
            ],
            [
                self._python_executable,
                "-m",
                "ai_intel_agent.cli",
                "serve",
                "--host",
                self._host,
                "--port",
                str(self._port),
            ],
        )
        try:
            for command in commands:
                child = self._start_process(
                    command,
                    cwd=self._project_root,
                    environment=self._environment,
                )
                self._children.append(child)
        except BaseException:
            self.stop()
            raise

    def wait(self) -> None:
        while True:
            for child in self._children:
                return_code = child.poll()
                if return_code is None:
                    continue
                if return_code != 0:
                    raise RuntimeError(
                        f"A local MVP child process exited with status {return_code}"
                    )
                return
            self._idle(0.25)

    def stop(self) -> None:
        for child in reversed(self._children):
            if child.poll() is None:
                child.send_signal(CHILD_PROCESS_STOP_SIGNAL)
        for child in reversed(self._children):
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=10)
        self._children.clear()


class LocalMvpRuntime:
    """Own the complete local MVP lifecycle behind one blocking operation."""

    def __init__(
        self,
        *,
        database: ManagedDatabase,
        migrate: Callable[[], None],
        processes: ManagedProcesses,
        state_changed: Callable[[LocalMvpState], None],
    ) -> None:
        self._database = database
        self._migrate = migrate
        self._processes = processes
        self._state_changed = state_changed

    def run(self) -> None:
        database_start_attempted = False
        processes_started = False
        try:
            self._state_changed(LocalMvpState.DATABASE_STARTING)
            database_start_attempted = True
            self._database.start()
            self._state_changed(LocalMvpState.DATABASE_HEALTHY)
            self._migrate()
            self._state_changed(LocalMvpState.MIGRATED)
            self._processes.start()
            processes_started = True
            self._state_changed(LocalMvpState.SCHEDULER_AND_WEB_RUNNING)
            self._processes.wait()
        finally:
            self._state_changed(LocalMvpState.STOPPING)
            try:
                if processes_started:
                    self._processes.stop()
            finally:
                try:
                    if database_start_attempted:
                        self._database.stop()
                finally:
                    self._state_changed(LocalMvpState.STOPPED)
