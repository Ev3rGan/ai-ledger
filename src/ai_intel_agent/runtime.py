from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from enum import StrEnum
from pathlib import Path
from threading import Event, Thread
from time import sleep
from types import FrameType
from typing import Protocol, Self
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.engine import URL, Connection, Engine, make_url
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


class ServiceJsonFormatter(logging.Formatter):
    """Render service stdout as stable one-record-per-line JSON."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info is not None:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_structured_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(ServiceJsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(logger_name)
        logger.handlers.clear()
        logger.propagate = True


def injected_secret_from_environment(environment: Mapping[str, str], key: str) -> str:
    path_value = environment.get(f"{key}_FILE", "").strip()
    if not path_value:
        raise ValueError(f"Set {key}_FILE to an injected secret file")
    secret_path = Path(path_value)
    try:
        value = secret_path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ValueError(f"Cannot read injected secret file for {key}") from error
    if not value:
        raise ValueError(f"Injected secret file for {key} must not be empty")
    return value


def production_database_url(environment: Mapping[str, str]) -> str:
    required_values = {
        key: environment.get(key, "").strip()
        for key in (
            "AI_INTEL_DATABASE_HOST",
            "AI_INTEL_DATABASE_NAME",
            "AI_INTEL_DATABASE_USER",
        )
    }
    missing = [key for key, value in required_values.items() if not value]
    if missing:
        raise ValueError(f"Set {missing[0]} for the production service")
    try:
        port = int(environment.get("AI_INTEL_DATABASE_PORT", "5432"))
    except ValueError as error:
        raise ValueError("AI_INTEL_DATABASE_PORT must be an integer") from error
    if not 1 <= port <= 65535:
        raise ValueError("AI_INTEL_DATABASE_PORT must be between 1 and 65535")
    password = injected_secret_from_environment(
        environment, "AI_INTEL_DATABASE_PASSWORD"
    )
    return URL.create(
        "postgresql+psycopg",
        username=required_values["AI_INTEL_DATABASE_USER"],
        password=password,
        host=required_values["AI_INTEL_DATABASE_HOST"],
        port=port,
        database=required_values["AI_INTEL_DATABASE_NAME"],
    ).render_as_string(hide_password=False)


def bounded_integer_from_environment(
    environment: Mapping[str, str],
    key: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(environment.get(key, ""))
    except ValueError as error:
        raise ValueError(f"{key} must be an integer") from error
    if not minimum <= value <= maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True)
class M1DatabaseConfiguration:
    """The database-only production contract used by private operator commands."""

    database_url: str = field(repr=False)

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> M1DatabaseConfiguration:
        return cls(database_url=production_database_url(environment))


@dataclass(frozen=True)
class M1ProviderConfiguration:
    """Provider credential and conservative aggregate monthly budget."""

    api_key: str = field(repr=False)
    monthly_budget_cents: int
    request_reservation_cents: int

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> M1ProviderConfiguration:
        monthly_budget_cents = bounded_integer_from_environment(
            environment,
            "AI_INTEL_PROVIDER_MONTHLY_BUDGET_CENTS",
            minimum=1,
            maximum=50_000,
        )
        request_reservation_cents = bounded_integer_from_environment(
            environment,
            "AI_INTEL_PROVIDER_REQUEST_RESERVATION_CENTS",
            minimum=1,
            maximum=monthly_budget_cents,
        )
        return cls(
            api_key=injected_secret_from_environment(environment, "DEEPSEEK_API_KEY"),
            monthly_budget_cents=monthly_budget_cents,
            request_reservation_cents=request_reservation_cents,
        )


@dataclass(frozen=True)
class M1WebConfiguration:
    """Web-only production contract loaded from its three injected secrets."""

    database: M1DatabaseConfiguration
    provider: M1ProviderConfiguration
    anonymous_identity_salt: bytes = field(repr=False)
    anonymous_research_daily_limit: int

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> M1WebConfiguration:
        daily_limit = bounded_integer_from_environment(
            environment,
            "AI_INTEL_ANONYMOUS_RESEARCH_DAILY_LIMIT",
            minimum=1,
            maximum=100,
        )
        identity_salt = injected_secret_from_environment(
            environment, "AI_INTEL_ANONYMOUS_ID_SALT"
        )
        return cls(
            database=M1DatabaseConfiguration.from_environment(environment),
            provider=M1ProviderConfiguration.from_environment(environment),
            anonymous_identity_salt=identity_salt.encode("utf-8"),
            anonymous_research_daily_limit=daily_limit,
        )


@dataclass(frozen=True)
class M1SchedulerConfiguration:
    """Scheduler-only production contract without anonymous Web secrets."""

    database: M1DatabaseConfiguration
    provider: M1ProviderConfiguration

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> M1SchedulerConfiguration:
        return cls(
            database=M1DatabaseConfiguration.from_environment(environment),
            provider=M1ProviderConfiguration.from_environment(environment),
        )


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


class SchedulerStatusWriter(Protocol):
    def waiting(self, *, next_run_at: datetime, observed_at: datetime) -> None: ...

    def running(self, *, started_at: datetime) -> None: ...

    def succeeded(self, *, completed_at: datetime) -> None: ...

    def failed(self, *, completed_at: datetime) -> None: ...

    def stopped(self, *, observed_at: datetime) -> None: ...


class NullSchedulerStatus:
    def waiting(self, *, next_run_at: datetime, observed_at: datetime) -> None:
        pass

    def running(self, *, started_at: datetime) -> None:
        pass

    def succeeded(self, *, completed_at: datetime) -> None:
        pass

    def failed(self, *, completed_at: datetime) -> None:
        pass

    def stopped(self, *, observed_at: datetime) -> None:
        pass


class GeminiScheduler:
    """Run collection at the next fixed Shanghai schedule slot until stopped."""

    def __init__(
        self,
        *,
        collect: Callable[[], None],
        now: Callable[[], datetime],
        wait: Callable[[float], bool],
        schedule: GeminiSchedule | None = None,
        status: SchedulerStatusWriter | None = None,
    ) -> None:
        self._collect = collect
        self._now = now
        self._wait = wait
        self._schedule = schedule or GeminiSchedule()
        self._status = status or NullSchedulerStatus()

    def run(self) -> None:
        while True:
            current = self._now()
            due = self._schedule.next_after(current)
            self._status.waiting(next_run_at=due, observed_at=current)
            delay = (due.astimezone(UTC) - current.astimezone(UTC)).total_seconds()
            if self._wait(max(0.0, delay)):
                self._status.stopped(observed_at=self._now())
                return
            self._status.running(started_at=self._now())
            try:
                self._collect()
            except BaseException:
                self._status.failed(completed_at=self._now())
                raise
            self._status.succeeded(completed_at=self._now())


class PostgresSchedulerLease:
    """Hold the database advisory lock that makes one Scheduler effective."""

    _LOCK_KEY = 6_607_056_341_054_001_049

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._connection: Connection | None = None

    def __enter__(self) -> Self:
        connection = self._engine.connect()
        acquired = connection.scalar(
            text("SELECT pg_try_advisory_lock(:lock_key)"),
            {"lock_key": self._LOCK_KEY},
        )
        if acquired is not True:
            connection.close()
            raise RuntimeError("A production Scheduler is already active")
        connection.commit()
        self._connection = connection
        return self

    def guarded_wait(
        self,
        stop_wait: Callable[[float], bool],
        delay_seconds: float,
        *,
        check_interval_seconds: float = 30.0,
    ) -> bool:
        """Poll the lock-holding session so a database restart stops this worker."""
        if check_interval_seconds <= 0:
            raise ValueError("Scheduler lease check interval must be positive")
        remaining = max(0.0, delay_seconds)
        while remaining > 0:
            interval = min(remaining, check_interval_seconds)
            if stop_wait(interval):
                return True
            self.assert_held()
            remaining -= interval
        self.assert_held()
        return False

    def assert_held(self) -> None:
        """Fail if the database session which owns the advisory lock was lost."""
        connection = self._connection
        if connection is None or connection.closed:
            raise RuntimeError("Production Scheduler lease was lost")
        try:
            connection.exec_driver_sql("SELECT 1")
            connection.commit()
        except Exception as error:
            raise RuntimeError("Production Scheduler lease was lost") from error

    @contextmanager
    def monitor(
        self,
        on_lost: Callable[[], None],
        *,
        check_interval_seconds: float = 2.0,
    ) -> Iterator[None]:
        """Continuously guard the lock, including while collection is blocked on I/O."""
        if check_interval_seconds <= 0:
            raise ValueError("Scheduler lease check interval must be positive")
        finished = Event()

        def watch() -> None:
            while not finished.wait(check_interval_seconds):
                try:
                    self.assert_held()
                except RuntimeError:
                    on_lost()
                    return

        watcher = Thread(target=watch, name="scheduler-lease-monitor", daemon=True)
        watcher.start()
        try:
            yield
        finally:
            finished.set()
            watcher.join(timeout=check_interval_seconds * 2)

    def __exit__(self, *_: object) -> None:
        if self._connection is None:
            return
        try:
            self._connection.execute(
                text("SELECT pg_advisory_unlock(:lock_key)"),
                {"lock_key": self._LOCK_KEY},
            )
        finally:
            self._connection.close()
            self._connection = None


class PostgresCollectionLease:
    """Prevent scheduled and manual multi-source collections from overlapping."""

    _LOCK_KEY = 6_607_056_341_054_002_055

    def __init__(
        self,
        engine: Engine,
        *,
        monitor_check_interval_seconds: float = 2.0,
        activation_grace_seconds: float = 2.5,
    ) -> None:
        if monitor_check_interval_seconds <= 0:
            raise ValueError("Collection lease check interval must be positive")
        if activation_grace_seconds <= monitor_check_interval_seconds:
            raise ValueError(
                "Collection lease activation grace must exceed its check interval"
            )
        self._engine = engine
        self._connection: Connection | None = None
        self._monitor_check_interval_seconds = monitor_check_interval_seconds
        self._activation_grace_seconds = activation_grace_seconds

    def __enter__(self) -> Self:
        connection = self._engine.connect()
        acquired = connection.scalar(
            text("SELECT pg_try_advisory_lock(:lock_key)"),
            {"lock_key": self._LOCK_KEY},
        )
        if acquired is not True:
            connection.close()
            raise RuntimeError("Another multi-source Collection is already active")
        connection.commit()
        self._connection = connection
        sleep(self._activation_grace_seconds)
        try:
            self.assert_held()
        except RuntimeError:
            connection.close()
            self._connection = None
            raise
        return self

    def assert_held(self) -> None:
        """Fail if the database session which owns the advisory lock was lost."""
        connection = self._connection
        if connection is None or connection.closed:
            raise RuntimeError("Multi-source Collection lease was lost")
        try:
            connection.exec_driver_sql("SELECT 1")
            connection.commit()
        except Exception as error:
            raise RuntimeError("Multi-source Collection lease was lost") from error

    @contextmanager
    def monitor(
        self,
        on_lost: Callable[[], None],
        *,
        check_interval_seconds: float | None = None,
    ) -> Iterator[None]:
        """Continuously guard the lock while collection is blocked on I/O."""
        interval = (
            self._monitor_check_interval_seconds
            if check_interval_seconds is None
            else check_interval_seconds
        )
        if interval <= 0:
            raise ValueError("Collection lease check interval must be positive")
        if interval >= self._activation_grace_seconds:
            raise ValueError(
                "Collection lease check interval must be shorter than activation grace"
            )
        finished = Event()

        def watch() -> None:
            while not finished.wait(interval):
                try:
                    self.assert_held()
                except RuntimeError:
                    on_lost()
                    return

        watcher = Thread(target=watch, name="collection-lease-monitor", daemon=True)
        watcher.start()
        try:
            yield
        finally:
            finished.set()
            watcher.join(timeout=interval * 2)

    def __exit__(self, *_: object) -> None:
        if self._connection is None:
            return
        connection = self._connection
        self._connection = None
        try:
            if not connection.closed and not connection.invalidated:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_key)"),
                    {"lock_key": self._LOCK_KEY},
                )
        finally:
            connection.close()


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
                "schedule-sources",
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
