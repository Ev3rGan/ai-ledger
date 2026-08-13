from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import subprocess
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit, urlunsplit
from urllib.request import ProxyHandler, Request, build_opener, urlopen

WORKLOAD_VERSION = "hong-kong-runtime-workload-2026-08-13.v2"
USER_AGENT = "ai-ledger-hong-kong-runtime-workload/0.1"
SSE_EVENT_COUNT = 3
SSE_INTERVAL_SECONDS = 0.05
CPU_ITERATIONS = 100_000
DISK_PROBE_MIB = 16
MEMORY_PROBE_MIB = 128
HONG_KONG_METADATA_REGIONS = {
    "tencent-lighthouse-hk": "ap-hongkong",
    "aws-lightsail-hk": "ap-east-1",
    "alibaba-swas-hk": "cn-hongkong",
}


@dataclass(frozen=True)
class EgressTarget:
    url: str
    accepted_statuses: tuple[int, ...]


EGRESS_TARGETS = {
    "source-github-releases": EgressTarget(
        "https://api.github.com/repos/openai/openai-python/releases/latest",
        (200,),
    ),
    "source-arxiv-api": EgressTarget(
        "https://export.arxiv.org/api/query?search_query=cat:cs.AI&max_results=1",
        (200,),
    ),
    "model-deepseek-api": EgressTarget(
        "https://api.deepseek.com/models",
        (200, 401, 403),
    ),
    "model-kimi-api": EgressTarget(
        "https://api.moonshot.cn/v1/models",
        (200, 401, 403),
    ),
    "oauth-github": EgressTarget(
        "https://github.com/login/oauth/authorize?client_id=runtime-benchmark-invalid",
        (200, 302, 400, 401, 403, 404, 422),
    ),
}


def _target_origin(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _effective_vcpus() -> int:
    cpu_max = Path("/sys/fs/cgroup/cpu.max")
    try:
        quota, period = cpu_max.read_text(encoding="utf-8").strip().split()
        if quota != "max":
            return max(1, math.ceil(int(quota) / int(period)))
    except (OSError, ValueError):
        pass
    return os.cpu_count() or 1


def _windows_memory_total_bytes() -> int:
    import ctypes

    class MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_ulong),
            ("memory_load", ctypes.c_ulong),
            ("total_physical", ctypes.c_ulonglong),
            ("available_physical", ctypes.c_ulonglong),
            ("total_page_file", ctypes.c_ulonglong),
            ("available_page_file", ctypes.c_ulonglong),
            ("total_virtual", ctypes.c_ulonglong),
            ("available_virtual", ctypes.c_ulonglong),
            ("available_extended_virtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatus()
    status.length = ctypes.sizeof(MemoryStatus)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise OSError("GlobalMemoryStatusEx failed")
    return int(status.total_physical)


def _effective_memory_total_bytes() -> int:
    memory_max = Path("/sys/fs/cgroup/memory.max")
    try:
        value = memory_max.read_text(encoding="utf-8").strip()
        if value != "max":
            return int(value)
    except (OSError, ValueError):
        pass
    if hasattr(os, "sysconf"):
        try:
            return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
        except (OSError, ValueError):
            pass
    if os.name == "nt":
        return _windows_memory_total_bytes()
    raise OSError("cannot determine available memory")


def run_resource_probe() -> dict[str, int | float]:
    seed = b"ai-ledger-hong-kong-runtime-benchmark"
    started = time.perf_counter()
    digest = seed
    for _ in range(CPU_ITERATIONS):
        digest = hashlib.sha256(digest).digest()
    cpu_sha256_ms = (time.perf_counter() - started) * 1000

    memory = bytearray(MEMORY_PROBE_MIB * 1024 * 1024)
    for offset in range(0, len(memory), 4096):
        memory[offset] = digest[offset % len(digest)]

    probe_path: str | None = None
    payload = digest * (1024 * 1024 // len(digest))
    try:
        with tempfile.NamedTemporaryFile(delete=False) as probe_file:
            probe_path = probe_file.name
            write_started = time.perf_counter()
            for _ in range(DISK_PROBE_MIB):
                probe_file.write(payload)
            probe_file.flush()
            os.fsync(probe_file.fileno())
        write_seconds = max(time.perf_counter() - write_started, 0.000001)

        read_started = time.perf_counter()
        with open(probe_path, "rb") as probe_file:
            while probe_file.read(1024 * 1024):
                pass
        read_seconds = max(time.perf_counter() - read_started, 0.000001)
    finally:
        if probe_path is not None:
            Path(probe_path).unlink(missing_ok=True)

    return {
        "vcpus": _effective_vcpus(),
        "memory_total_bytes": _effective_memory_total_bytes(),
        "cpu_sha256_ms": round(cpu_sha256_ms, 3),
        "disk_write_mib_per_second": round(DISK_PROBE_MIB / write_seconds, 3),
        "disk_read_mib_per_second": round(DISK_PROBE_MIB / read_seconds, 3),
        "memory_probe_mib": MEMORY_PROBE_MIB,
    }


def _execute_database_restore_probe(psycopg, database_url: str) -> dict[str, int | float]:
    parsed = urlsplit(database_url)
    if parsed.scheme not in {"postgresql", "postgres"} or not parsed.hostname:
        raise OSError("RUNTIME_BENCHMARK_DATABASE_URL must be a PostgreSQL URL")
    database_name = unquote(parsed.path.lstrip("/"))
    command_base = [
        "--host",
        parsed.hostname,
        "--port",
        str(parsed.port or 5432),
        "--username",
        unquote(parsed.username or "postgres"),
        "--dbname",
        database_name,
    ]
    process_environment = os.environ.copy()
    if parsed.password:
        process_environment["PGPASSWORD"] = unquote(parsed.password)

    dump_path: str | None = None
    with psycopg.connect(database_url, autocommit=True) as connection:
        try:
            connection.execute("DROP SCHEMA IF EXISTS runtime_benchmark CASCADE")
            connection.execute("CREATE SCHEMA runtime_benchmark")
            connection.execute(
                "CREATE TABLE runtime_benchmark.restore_fixture AS "
                "SELECT value, md5(value::text) AS digest "
                "FROM generate_series(1, 10000) AS value"
            )
            with tempfile.NamedTemporaryFile(suffix=".dump", delete=False) as dump_file:
                dump_path = dump_file.name

            dump_started = time.perf_counter()
            subprocess.run(
                [
                    "pg_dump",
                    *command_base,
                    "--format",
                    "custom",
                    "--schema",
                    "runtime_benchmark",
                    "--file",
                    dump_path,
                ],
                check=True,
                capture_output=True,
                env=process_environment,
                timeout=30,
            )
            database_dump_ms = (time.perf_counter() - dump_started) * 1000
            connection.execute("DROP SCHEMA runtime_benchmark CASCADE")

            restore_started = time.perf_counter()
            subprocess.run(
                [
                    "pg_restore",
                    *command_base,
                    "--no-owner",
                    "--no-privileges",
                    dump_path,
                ],
                check=True,
                capture_output=True,
                env=process_environment,
                timeout=30,
            )
            database_restore_ms = (time.perf_counter() - restore_started) * 1000
            database_rows_restored = connection.execute(
                "SELECT count(*) FROM runtime_benchmark.restore_fixture"
            ).fetchone()[0]
        except (subprocess.SubprocessError, OSError) as error:
            raise OSError("PostgreSQL dump/restore probe failed") from error
        finally:
            connection.execute("DROP SCHEMA IF EXISTS runtime_benchmark CASCADE")
            if dump_path is not None:
                Path(dump_path).unlink(missing_ok=True)

    return {
        "database_dump_ms": round(database_dump_ms, 3),
        "database_restore_ms": round(database_restore_ms, 3),
        "database_rows_restored": int(database_rows_restored),
    }


def run_database_restore_probe(database_url: str) -> dict[str, int | float]:
    try:
        import psycopg
    except ImportError as error:
        raise OSError("psycopg is required by the representative workload") from error
    try:
        return _execute_database_restore_probe(psycopg, database_url)
    except psycopg.Error as error:
        raise OSError("PostgreSQL dump/restore probe failed") from error


def _metadata_request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
) -> str:
    opener = build_opener(ProxyHandler({}))
    request = Request(url, method=method, headers=headers or {})
    try:
        with opener.open(request, timeout=2) as response:
            return response.read(64_000).decode("utf-8").strip()
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        raise OSError("cloud instance metadata is unavailable") from error


def _metadata_json(url: str, *, headers: dict[str, str]) -> dict[str, object]:
    try:
        payload = json.loads(_metadata_request(url, headers=headers))
    except (json.JSONDecodeError, TypeError) as error:
        raise OSError("cloud instance identity document is invalid") from error
    if not isinstance(payload, dict):
        raise OSError("cloud instance identity document is invalid")
    return payload


def run_node_identity_probe(candidate_identifier: str) -> dict[str, str]:
    if candidate_identifier == "tencent-lighthouse-hk":
        base = "http://metadata.tencentyun.com/latest/meta-data"
        node_id = _metadata_request(f"{base}/instance-id")
        region = _metadata_request(f"{base}/placement/region")
    elif candidate_identifier == "aws-lightsail-hk":
        base = "http://169.254.169.254/latest"
        token = _metadata_request(
            f"{base}/api/token",
            method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
        )
        document = _metadata_json(
            f"{base}/dynamic/instance-identity/document",
            headers={"X-aws-ec2-metadata-token": token},
        )
        try:
            node_id = str(document["instanceId"])
            region = str(document["region"])
        except KeyError as error:
            raise OSError("AWS instance identity document is incomplete") from error
    elif candidate_identifier == "alibaba-swas-hk":
        base = "http://100.100.100.200/latest"
        token = _metadata_request(
            f"{base}/api/token",
            method="PUT",
            headers={"X-aliyun-ecs-metadata-token-ttl-seconds": "60"},
        )
        document = _metadata_json(
            f"{base}/dynamic/instance-identity/document",
            headers={"X-aliyun-ecs-metadata-token": token},
        )
        try:
            node_id = str(document["instance-id"])
            region = str(document["region-id"])
        except KeyError as error:
            raise OSError("Alibaba instance identity document is incomplete") from error
    else:
        raise OSError("unknown configured runtime candidate")

    if region != HONG_KONG_METADATA_REGIONS[candidate_identifier] or not node_id:
        raise OSError("node metadata does not identify the configured Hong Kong region")
    return {
        "candidate_identifier": candidate_identifier,
        "region": region,
        "node_id": node_id,
    }


def run_egress_probe(identifier: str) -> tuple[int, dict[str, object]]:
    target = EGRESS_TARGETS.get(identifier)
    if target is None:
        return HTTPStatus.NOT_FOUND, {"error": "unknown fixed egress probe"}

    request = Request(
        target.url,
        method="GET",
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
    )
    started = time.perf_counter()
    try:
        with urlopen(request, timeout=10) as response:
            status_code = response.status
            response.read(1)
        error = None
    except HTTPError as http_error:
        status_code = http_error.code
        error = None if status_code in target.accepted_statuses else "HTTPError"
    except (URLError, TimeoutError, OSError) as network_error:
        status_code = None
        error = type(network_error).__name__
    latency_ms = round((time.perf_counter() - started) * 1000, 3)
    reachable = status_code in target.accepted_statuses
    return HTTPStatus.OK, {
        "reachable": reachable,
        "status_code": status_code,
        "latency_ms": latency_ms,
        "target_origin": _target_origin(target.url),
        "error": error if not reachable else None,
    }


class RuntimeWorkloadHandler(BaseHTTPRequestHandler):
    server_version = "AiLedgerRuntimeWorkload/0.1"
    protocol_version = "HTTP/1.1"

    @property
    def expected_token(self) -> str:
        return self.server.benchmark_token  # type: ignore[attr-defined]

    @property
    def database_probe(self) -> Callable[[], dict[str, int | float]]:
        return self.server.database_probe  # type: ignore[attr-defined]

    @property
    def node_identity_probe(self) -> Callable[[], dict[str, str]]:
        return self.server.node_identity_probe  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: object) -> None:
        return

    def _authorized(self) -> bool:
        supplied = self.headers.get("X-Benchmark-Token", "")
        return bool(supplied) and hmac.compare_digest(supplied, self.expected_token)

    def _send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if not self._authorized():
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "invalid benchmark token"})
            return
        path = urlsplit(self.path).path
        if path == "/health":
            self._send_json(
                HTTPStatus.OK,
                {"status": "ok", "workload_version": WORKLOAD_VERSION},
            )
            return
        if path == "/events":
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "close")
            self.end_headers()
            for event_id in range(1, SSE_EVENT_COUNT + 1):
                self.wfile.write(
                    f"id: {event_id}\ndata: event-{event_id}\n\n".encode()
                )
                self.wfile.flush()
                time.sleep(SSE_INTERVAL_SECONDS)
            self.close_connection = True
            return
        if path == "/resource":
            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    resource_future = executor.submit(run_resource_probe)
                    database_future = executor.submit(self.database_probe)
                    payload = resource_future.result()
                    payload.update(database_future.result())
                payload["pressure_mode"] = "concurrent-web-worker-database"
                payload["node_identity"] = self.node_identity_probe()
            except OSError as error:
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": type(error).__name__},
                )
                return
            self._send_json(HTTPStatus.OK, payload)
            return
        if path.startswith("/egress/"):
            status, payload = run_egress_probe(path.removeprefix("/egress/"))
            self._send_json(status, payload)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown workload path"})


class RuntimeWorkloadServer(ThreadingHTTPServer):
    benchmark_token: str
    database_probe: Callable[[], dict[str, int | float]]
    node_identity_probe: Callable[[], dict[str, str]]


def create_runtime_workload_server(
    host: str,
    port: int,
    *,
    token: str,
    database_probe: Callable[[], dict[str, int | float]] | None = None,
    node_identity_probe: Callable[[], dict[str, str]] | None = None,
) -> RuntimeWorkloadServer:
    if not token:
        raise ValueError("runtime benchmark token is required")
    server = RuntimeWorkloadServer((host, port), RuntimeWorkloadHandler)
    server.benchmark_token = token
    database_url = os.environ.get("RUNTIME_BENCHMARK_DATABASE_URL", "")
    if database_probe is None and not database_url:
        server.server_close()
        raise ValueError("RUNTIME_BENCHMARK_DATABASE_URL is required")
    server.database_probe = database_probe or (
        lambda: run_database_restore_probe(database_url)
    )
    candidate_identifier = os.environ.get("RUNTIME_BENCHMARK_CANDIDATE", "")
    if node_identity_probe is None and candidate_identifier not in HONG_KONG_METADATA_REGIONS:
        server.server_close()
        raise ValueError("RUNTIME_BENCHMARK_CANDIDATE is required")
    server.node_identity_probe = node_identity_probe or (
        lambda: run_node_identity_probe(candidate_identifier)
    )
    return server


def main() -> None:
    token = os.environ.get("RUNTIME_BENCHMARK_TOKEN", "")
    host = os.environ.get("RUNTIME_BENCHMARK_HOST", "0.0.0.0")
    port = int(os.environ.get("RUNTIME_BENCHMARK_PORT", "8080"))
    server = create_runtime_workload_server(host, port, token=token)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
