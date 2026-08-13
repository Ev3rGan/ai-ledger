from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

import httpx

CONFIGURATION_PATH = (
    Path(__file__).parent / "data" / "hong_kong_runtime_benchmark.v2.json"
)


class RuntimeBenchmarkConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class RuntimeCandidate:
    identifier: str
    provider: str
    region: str
    metadata_region: str
    minimum_vcpus: int
    minimum_memory_gib: int
    pricing_sources: tuple[str, ...]


@dataclass(frozen=True)
class PricingObservation:
    monthly_cost_usd: Decimal
    observed_at: date
    source: str

    def validate(
        self,
        *,
        candidate: RuntimeCandidate,
        observed_by: datetime,
        configuration: RuntimeBenchmarkConfiguration,
    ) -> int:
        if not self.monthly_cost_usd.is_finite() or self.monthly_cost_usd < 0:
            raise RuntimeBenchmarkConfigurationError(
                "monthly cost must be a finite non-negative value"
            )
        if self.observed_at > observed_by.date():
            raise RuntimeBenchmarkConfigurationError("price observation cannot be in the future")
        if self.source not in candidate.pricing_sources:
            raise RuntimeBenchmarkConfigurationError(
                f"price source for {candidate.identifier} must be one of its configured official sources"
            )
        price_age_days = (observed_by.date() - self.observed_at).days
        if price_age_days > configuration.maximum_pricing_age_days:
            raise RuntimeBenchmarkConfigurationError("price observation is stale")
        return price_age_days


@dataclass(frozen=True)
class EgressProbe:
    identifier: str
    category: Literal["source egress", "model API", "OAuth"]
    path: str


@dataclass(frozen=True)
class RuntimeBenchmarkConfiguration:
    version: str
    workload_version: str
    attempts: int
    timeout_seconds: float
    sse_events: int
    maximum_monthly_cost_usd: Decimal
    maximum_pricing_age_days: int
    maximum_observation_window_hours: int
    candidates: tuple[RuntimeCandidate, ...]
    egress_probes: tuple[EgressProbe, ...]
    protocol_sha256: str

    def candidate(self, identifier: str) -> RuntimeCandidate:
        try:
            return next(item for item in self.candidates if item.identifier == identifier)
        except StopIteration as error:
            known = ", ".join(item.identifier for item in self.candidates)
            raise RuntimeBenchmarkConfigurationError(
                f"unknown Hong Kong runtime candidate {identifier!r}; expected one of: {known}"
            ) from error


def load_runtime_benchmark_configuration(
    path: Path = CONFIGURATION_PATH,
) -> RuntimeBenchmarkConfiguration:
    raw = path.read_bytes()
    try:
        payload = json.loads(raw)
        candidates = tuple(
            RuntimeCandidate(
                identifier=item["identifier"],
                provider=item["provider"],
                region=item["region"],
                metadata_region=item["metadata_region"],
                minimum_vcpus=int(item["minimum_vcpus"]),
                minimum_memory_gib=int(item["minimum_memory_gib"]),
                pricing_sources=tuple(item["pricing_sources"]),
            )
            for item in payload["candidates"]
        )
        egress_probes = tuple(
            EgressProbe(
                identifier=item["identifier"],
                category=item["category"],
                path=item["path"],
            )
            for item in payload["egress_probes"]
        )
        maximum_cost = Decimal(payload["maximum_monthly_cost_usd"])
    except (KeyError, TypeError, ValueError, InvalidOperation) as error:
        raise RuntimeBenchmarkConfigurationError(
            f"invalid runtime benchmark configuration: {error}"
        ) from error

    if len(candidates) != 3 or len({item.identifier for item in candidates}) != 3:
        raise RuntimeBenchmarkConfigurationError(
            "runtime benchmark must define exactly three distinct Hong Kong candidates"
        )
    if int(payload["attempts"]) < 2:
        raise RuntimeBenchmarkConfigurationError("runtime benchmark requires repeated attempts")

    return RuntimeBenchmarkConfiguration(
        version=payload["version"],
        workload_version=payload["workload_version"],
        attempts=int(payload["attempts"]),
        timeout_seconds=float(payload["timeout_seconds"]),
        sse_events=int(payload["sse_events"]),
        maximum_monthly_cost_usd=maximum_cost,
        maximum_pricing_age_days=int(payload["maximum_pricing_age_days"]),
        maximum_observation_window_hours=int(payload["maximum_observation_window_hours"]),
        candidates=candidates,
        egress_probes=egress_probes,
        protocol_sha256=hashlib.sha256(raw).hexdigest(),
    )


def _target_origin(target_url: str) -> str:
    parsed = urlsplit(target_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeBenchmarkConfigurationError(
            "target URL must be an absolute HTTP or HTTPS URL"
        )
    if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeBenchmarkConfigurationError(
            "remote benchmark workloads must use HTTPS"
        )
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _normalize_image_sha256(value: str, label: str) -> str:
    normalized = value.removeprefix("sha256:").lower()
    if re.fullmatch(r"[0-9a-f]{64}", normalized) is None:
        raise RuntimeBenchmarkConfigurationError(
            f"{label} image SHA-256 must contain exactly 64 hexadecimal characters"
        )
    return normalized


class HttpRuntimeProbeClient:
    def __init__(
        self,
        target_url: str,
        *,
        configuration: RuntimeBenchmarkConfiguration,
        http_client: httpx.Client | None = None,
        workload_token: str | None = None,
    ) -> None:
        self.target_origin = _target_origin(target_url)
        self.configuration = configuration
        self._owns_client = http_client is None
        if http_client is None:
            headers = {"User-Agent": "ai-ledger-hong-kong-runtime-benchmark/0.1"}
            if workload_token:
                headers["X-Benchmark-Token"] = workload_token
            self.http_client = httpx.Client(
                timeout=configuration.timeout_seconds,
                follow_redirects=False,
                headers=headers,
            )
        else:
            self.http_client = http_client

    def close(self) -> None:
        if self._owns_client:
            self.http_client.close()

    def network(self, attempt: int) -> dict[str, object]:
        started = perf_counter()
        try:
            response = self.http_client.get(f"{self.target_origin}/health")
            latency_ms = round((perf_counter() - started) * 1000, 3)
            payload = response.json()
            passed = (
                response.status_code == 200
                and payload.get("status") == "ok"
                and payload.get("workload_version") == self.configuration.workload_version
            )
            return {
                "category": "network",
                "probe": "public-workload-health",
                "attempt": attempt,
                "passed": passed,
                "latency_ms": latency_ms,
                "status_code": response.status_code,
                "workload_version": payload.get("workload_version"),
                "error": None if passed else "unexpected workload health response",
            }
        except (httpx.HTTPError, ValueError) as error:
            return {
                "category": "network",
                "probe": "public-workload-health",
                "attempt": attempt,
                "passed": False,
                "latency_ms": round((perf_counter() - started) * 1000, 3),
                "status_code": None,
                "error": type(error).__name__,
            }

    def sse(self, attempt: int) -> dict[str, object]:
        started = perf_counter()
        first_event_ms: float | None = None
        event_count = 0
        try:
            with self.http_client.stream("GET", f"{self.target_origin}/events") as response:
                content_type = response.headers.get("content-type", "").lower()
                for line in response.iter_lines():
                    if line.startswith("data:"):
                        event_count += 1
                        if first_event_ms is None:
                            first_event_ms = round((perf_counter() - started) * 1000, 3)
            completion_ms = round((perf_counter() - started) * 1000, 3)
            passed = (
                response.status_code == 200
                and "text/event-stream" in content_type
                and event_count == self.configuration.sse_events
            )
            return {
                "category": "SSE",
                "probe": "public-event-stream",
                "attempt": attempt,
                "passed": passed,
                "status_code": response.status_code,
                "events_received": event_count,
                "first_event_ms": first_event_ms,
                "completion_ms": completion_ms,
                "error": None if passed else "incomplete or invalid SSE stream",
            }
        except httpx.HTTPError as error:
            return {
                "category": "SSE",
                "probe": "public-event-stream",
                "attempt": attempt,
                "passed": False,
                "status_code": None,
                "events_received": event_count,
                "first_event_ms": first_event_ms,
                "completion_ms": round((perf_counter() - started) * 1000, 3),
                "error": type(error).__name__,
            }

    def egress(self, probe: EgressProbe, attempt: int) -> dict[str, object]:
        started = perf_counter()
        try:
            response = self.http_client.get(f"{self.target_origin}{probe.path}")
            controller_latency_ms = round((perf_counter() - started) * 1000, 3)
            payload = response.json()
            passed = response.status_code == 200 and payload.get("reachable") is True
            return {
                "category": probe.category,
                "probe": probe.identifier,
                "attempt": attempt,
                "passed": passed,
                "controller_latency_ms": controller_latency_ms,
                "node_egress_latency_ms": payload.get("latency_ms"),
                "target_origin": payload.get("target_origin"),
                "status_code": payload.get("status_code"),
                "error": None if passed else payload.get("error", "node egress probe failed"),
            }
        except (httpx.HTTPError, ValueError) as error:
            return {
                "category": probe.category,
                "probe": probe.identifier,
                "attempt": attempt,
                "passed": False,
                "controller_latency_ms": round((perf_counter() - started) * 1000, 3),
                "node_egress_latency_ms": None,
                "target_origin": None,
                "status_code": None,
                "error": type(error).__name__,
            }

    def resource(self, candidate: RuntimeCandidate) -> dict[str, object]:
        started = perf_counter()
        try:
            response = self.http_client.get(f"{self.target_origin}/resource")
            payload = response.json()
            memory_gib = float(payload["memory_total_bytes"]) / 1024**3
            passed = (
                response.status_code == 200
                and int(payload["vcpus"]) >= candidate.minimum_vcpus
                and memory_gib >= candidate.minimum_memory_gib * 0.90
                and float(payload["disk_write_mib_per_second"]) > 0
                and float(payload["disk_read_mib_per_second"]) > 0
                and float(payload["database_dump_ms"]) > 0
                and float(payload["database_restore_ms"]) > 0
                and int(payload["database_rows_restored"]) == 10000
                and payload["pressure_mode"] == "concurrent-web-worker-database"
                and payload["node_identity"]["candidate_identifier"]
                == candidate.identifier
                and payload["node_identity"]["region"] == candidate.metadata_region
                and bool(payload["node_identity"]["node_id"])
            )
            return {
                "category": "resource",
                "probe": "representative-container-workload",
                "attempt": 1,
                "passed": passed,
                "controller_latency_ms": round((perf_counter() - started) * 1000, 3),
                "vcpus": int(payload["vcpus"]),
                "memory_total_bytes": int(payload["memory_total_bytes"]),
                "cpu_sha256_ms": float(payload["cpu_sha256_ms"]),
                "disk_write_mib_per_second": float(payload["disk_write_mib_per_second"]),
                "disk_read_mib_per_second": float(payload["disk_read_mib_per_second"]),
                "memory_probe_mib": int(payload["memory_probe_mib"]),
                "database_dump_ms": float(payload["database_dump_ms"]),
                "database_restore_ms": float(payload["database_restore_ms"]),
                "database_rows_restored": int(payload["database_rows_restored"]),
                "pressure_mode": str(payload["pressure_mode"]),
                "node_identity": {
                    "candidate_identifier": str(
                        payload["node_identity"]["candidate_identifier"]
                    ),
                    "region": str(payload["node_identity"]["region"]),
                    "node_id": str(payload["node_identity"]["node_id"]),
                },
                "error": None if passed else "candidate does not satisfy the fixed resource gate",
            }
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as error:
            return {
                "category": "resource",
                "probe": "representative-container-workload",
                "attempt": 1,
                "passed": False,
                "controller_latency_ms": round((perf_counter() - started) * 1000, 3),
                "error": type(error).__name__,
            }


def run_hong_kong_runtime_probe(
    output: Path,
    *,
    candidate_identifier: str,
    target_url: str,
    observer: str,
    pricing: PricingObservation,
    workload_image_sha256: str,
    database_image_sha256: str,
    client: HttpRuntimeProbeClient,
    configuration: RuntimeBenchmarkConfiguration | None = None,
    now: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    configuration = configuration or load_runtime_benchmark_configuration()
    candidate = configuration.candidate(candidate_identifier)
    run_at = (now or (lambda: datetime.now(UTC)))()
    if run_at.tzinfo is None:
        raise RuntimeBenchmarkConfigurationError("benchmark clock must be timezone-aware")
    if not observer.strip():
        raise RuntimeBenchmarkConfigurationError("observer label is required")
    if _target_origin(target_url) != client.target_origin:
        raise RuntimeBenchmarkConfigurationError(
            "probe client target does not match the requested target URL"
        )
    normalized_image_sha256 = _normalize_image_sha256(workload_image_sha256, "workload")
    normalized_database_image_sha256 = _normalize_image_sha256(
        database_image_sha256, "database"
    )
    pricing_age_days = pricing.validate(
        candidate=candidate,
        observed_by=run_at,
        configuration=configuration,
    )

    measurements: list[dict[str, object]] = []
    for attempt in range(1, configuration.attempts + 1):
        measurements.append(client.network(attempt))
        measurements.append(client.sse(attempt))
    for probe in configuration.egress_probes:
        for attempt in range(1, configuration.attempts + 1):
            measurements.append(client.egress(probe, attempt))
    measurements.append(client.resource(candidate))

    cost_passed = (
        pricing.monthly_cost_usd <= configuration.maximum_monthly_cost_usd
    )
    measurements.append(
        {
            "category": "cost",
            "probe": "observed-monthly-price",
            "attempt": 1,
            "passed": cost_passed,
            "monthly_cost_usd": format(pricing.monthly_cost_usd, "f"),
            "price_age_days": pricing_age_days,
            "maximum_monthly_cost_usd": format(
                configuration.maximum_monthly_cost_usd, "f"
            ),
            "error": None if cost_passed else "price is stale or exceeds the MVP budget",
        }
    )

    result: dict[str, object] = {
        "benchmark_version": configuration.version,
        "protocol_sha256": configuration.protocol_sha256,
        "workload_version": configuration.workload_version,
        "workload_image_sha256": normalized_image_sha256,
        "database_image_sha256": normalized_database_image_sha256,
        "run_at": run_at.isoformat(),
        "observer": observer.strip(),
        "target_origin": client.target_origin,
        "candidate": {
            "identifier": candidate.identifier,
            "provider": candidate.provider,
            "region": candidate.region,
            "minimum_vcpus": candidate.minimum_vcpus,
            "minimum_memory_gib": candidate.minimum_memory_gib,
            "pricing_sources": list(candidate.pricing_sources),
        },
        "pricing": {
            "monthly_cost_usd": format(pricing.monthly_cost_usd, "f"),
            "observed_at": pricing.observed_at.isoformat(),
            "source": pricing.source,
            "warning": "Observed comparison input; cloud prices are not permanent facts.",
        },
        "measurements": measurements,
        "passed": all(bool(item["passed"]) for item in measurements),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result


def _median(values: list[float]) -> float | None:
    return median(values) if values else None


def _measurements_for(
    result: dict[str, object], category: str
) -> list[dict[str, object]]:
    measurements = result.get("measurements")
    if not isinstance(measurements, list):
        raise RuntimeBenchmarkConfigurationError("runtime result has no measurement list")
    return [item for item in measurements if item.get("category") == category]


def _require_complete_result(
    result: dict[str, object],
    configuration: RuntimeBenchmarkConfiguration,
    *,
    comparison_at: datetime,
) -> datetime:
    if result.get("benchmark_version") != configuration.version:
        raise RuntimeBenchmarkConfigurationError("runtime result benchmark version mismatch")
    if result.get("protocol_sha256") != configuration.protocol_sha256:
        raise RuntimeBenchmarkConfigurationError("runtime result protocol SHA-256 mismatch")
    if result.get("workload_version") != configuration.workload_version:
        raise RuntimeBenchmarkConfigurationError("runtime result workload version mismatch")
    if re.fullmatch(r"[0-9a-f]{64}", str(result.get("workload_image_sha256", ""))) is None:
        raise RuntimeBenchmarkConfigurationError("runtime result has no valid workload image SHA-256")
    if re.fullmatch(r"[0-9a-f]{64}", str(result.get("database_image_sha256", ""))) is None:
        raise RuntimeBenchmarkConfigurationError("runtime result has no valid database image SHA-256")

    measurements = result.get("measurements")
    if not isinstance(measurements, list) or not all(
        isinstance(item, dict) for item in measurements
    ):
        raise RuntimeBenchmarkConfigurationError("runtime result has invalid measurements")
    if not all(type(item.get("passed")) is bool for item in measurements):
        raise RuntimeBenchmarkConfigurationError(
            "runtime result measurement passed values must be booleans"
        )
    expected_measurements = [
        ("network", "public-workload-health", attempt)
        for attempt in range(1, configuration.attempts + 1)
    ] + [
        ("SSE", "public-event-stream", attempt)
        for attempt in range(1, configuration.attempts + 1)
    ]
    for probe in configuration.egress_probes:
        expected_measurements.extend(
            (probe.category, probe.identifier, attempt)
            for attempt in range(1, configuration.attempts + 1)
        )
    expected_measurements.extend(
        [
            ("resource", "representative-container-workload", 1),
            ("cost", "observed-monthly-price", 1),
        ]
    )
    actual_measurements = Counter(
        (str(item.get("category")), str(item.get("probe")), item.get("attempt"))
        for item in measurements
    )
    if actual_measurements != Counter(expected_measurements):
        raise RuntimeBenchmarkConfigurationError(
            "runtime result does not contain every fixed probe and attempt exactly once"
        )

    try:
        run_at = datetime.fromisoformat(str(result["run_at"])).astimezone(UTC)
        candidate_identifier = str(result["candidate"]["identifier"])
        candidate = configuration.candidate(candidate_identifier)
        if (
            result["candidate"]["provider"] != candidate.provider
            or result["candidate"]["region"] != candidate.region
        ):
            raise RuntimeBenchmarkConfigurationError(
                "runtime result candidate metadata does not match the protocol"
            )
        pricing_payload = result["pricing"]
        pricing = PricingObservation(
            monthly_cost_usd=Decimal(str(pricing_payload["monthly_cost_usd"])),
            observed_at=date.fromisoformat(str(pricing_payload["observed_at"])),
            source=str(pricing_payload["source"]),
        )
    except (KeyError, TypeError, ValueError, InvalidOperation) as error:
        raise RuntimeBenchmarkConfigurationError(
            f"runtime result has invalid run or pricing evidence: {error}"
        ) from error
    if run_at > comparison_at:
        raise RuntimeBenchmarkConfigurationError("runtime result was captured in the future")
    pricing.validate(
        candidate=candidate,
        observed_by=run_at,
        configuration=configuration,
    )
    pricing_age_days = pricing.validate(
        candidate=candidate,
        observed_by=comparison_at,
        configuration=configuration,
    )
    cost = _measurements_for(result, "cost")[0]
    try:
        measured_cost = Decimal(str(cost["monthly_cost_usd"]))
        measured_price_age_days = int(cost["price_age_days"])
    except (KeyError, ValueError, InvalidOperation) as error:
        raise RuntimeBenchmarkConfigurationError(
            "runtime result cost measurement is invalid"
        ) from error
    if (
        measured_cost != pricing.monthly_cost_usd
        or measured_price_age_days != (run_at.date() - pricing.observed_at).days
    ):
        raise RuntimeBenchmarkConfigurationError(
            "runtime result cost measurement contradicts its pricing evidence"
        )
    cost_should_pass = (
        pricing.monthly_cost_usd <= configuration.maximum_monthly_cost_usd
        and pricing_age_days <= configuration.maximum_pricing_age_days
    )
    if bool(cost.get("passed")) != cost_should_pass:
        raise RuntimeBenchmarkConfigurationError(
            "runtime result cost gate does not match current price evidence"
        )
    resource = _measurements_for(result, "resource")[0]
    try:
        node_identity = resource["node_identity"]
        identity_matches = (
            node_identity["candidate_identifier"] == candidate.identifier
            and node_identity["region"] == candidate.metadata_region
            and bool(node_identity["node_id"])
        )
    except (KeyError, TypeError) as error:
        raise RuntimeBenchmarkConfigurationError(
            "runtime result has invalid provider node identity evidence"
        ) from error
    if not identity_matches:
        raise RuntimeBenchmarkConfigurationError(
            "runtime result provider node identity does not match its candidate"
        )
    return run_at


def _format_metric(value: float | None, suffix: str = "") -> str:
    return "n/a" if value is None else f"{value:.1f}{suffix}"


def compare_hong_kong_runtime_results(
    inputs: list[Path] | tuple[Path, ...],
    output: Path,
    *,
    configuration: RuntimeBenchmarkConfiguration | None = None,
    now: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    configuration = configuration or load_runtime_benchmark_configuration()
    comparison_at = (now or (lambda: datetime.now(UTC)))()
    if comparison_at.tzinfo is None:
        raise RuntimeBenchmarkConfigurationError("comparison clock must be timezone-aware")
    comparison_at = comparison_at.astimezone(UTC)
    results: list[dict[str, object]] = []
    run_times: list[datetime] = []
    for path in inputs:
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeBenchmarkConfigurationError(
                f"cannot read runtime result {path}: {error}"
            ) from error
        if not isinstance(result, dict):
            raise RuntimeBenchmarkConfigurationError(f"runtime result {path} is not an object")
        run_times.append(
            _require_complete_result(
                result,
                configuration,
                comparison_at=comparison_at,
            )
        )
        result["_evidence_path"] = str(path)
        results.append(result)

    identifiers = [str(result["candidate"]["identifier"]) for result in results]
    expected_identifiers = {candidate.identifier for candidate in configuration.candidates}
    if len(results) != len(expected_identifiers) or set(identifiers) != expected_identifiers:
        raise RuntimeBenchmarkConfigurationError(
            "comparison requires exactly one result for each configured candidate"
        )
    observers = {str(result.get("observer", "")) for result in results}
    if len(observers) != 1 or not next(iter(observers)):
        raise RuntimeBenchmarkConfigurationError(
            "comparison results must use one fixed non-empty observer label"
        )
    target_origins = {str(result.get("target_origin", "")) for result in results}
    if len(target_origins) != len(results) or "" in target_origins:
        raise RuntimeBenchmarkConfigurationError(
            "comparison results must come from distinct candidate target origins"
        )
    node_ids = {
        str(_measurements_for(result, "resource")[0]["node_identity"]["node_id"])
        for result in results
    }
    if len(node_ids) != len(results):
        raise RuntimeBenchmarkConfigurationError(
            "comparison results must come from distinct provider node identities"
        )
    observation_window_hours = (
        max(run_times) - min(run_times)
    ).total_seconds() / 3600
    if observation_window_hours > configuration.maximum_observation_window_hours:
        raise RuntimeBenchmarkConfigurationError(
            "comparison results exceed the fixed observation window"
        )
    workload_images = {str(result["workload_image_sha256"]) for result in results}
    if len(workload_images) != 1:
        raise RuntimeBenchmarkConfigurationError(
            "comparison results must use the same workload image SHA-256"
        )
    database_images = {str(result["database_image_sha256"]) for result in results}
    if len(database_images) != 1:
        raise RuntimeBenchmarkConfigurationError(
            "comparison results must use the same database image SHA-256"
        )

    summaries: list[dict[str, object]] = []
    for result in results:
        network = _measurements_for(result, "network")
        sse = _measurements_for(result, "SSE")
        source = _measurements_for(result, "source egress")
        model = _measurements_for(result, "model API")
        oauth = _measurements_for(result, "OAuth")
        resource = _measurements_for(result, "resource")[0]
        all_measurements = result["measurements"]
        summary = {
            "identifier": result["candidate"]["identifier"],
            "provider": result["candidate"]["provider"],
            "eligible": all(bool(item.get("passed")) for item in all_measurements),
            "network_passes": sum(bool(item["passed"]) for item in network),
            "network_total": len(network),
            "network_median_ms": _median(
                [float(item["latency_ms"]) for item in network if item.get("latency_ms") is not None]
            ),
            "sse_passes": sum(bool(item["passed"]) for item in sse),
            "sse_total": len(sse),
            "sse_first_event_median_ms": _median(
                [
                    float(item["first_event_ms"])
                    for item in sse
                    if item.get("first_event_ms") is not None
                ]
            ),
            "source_passes": sum(bool(item["passed"]) for item in source),
            "source_total": len(source),
            "model_passes": sum(bool(item["passed"]) for item in model),
            "model_total": len(model),
            "oauth_passes": sum(bool(item["passed"]) for item in oauth),
            "oauth_total": len(oauth),
            "vcpus": resource.get("vcpus"),
            "memory_gib": (
                float(resource["memory_total_bytes"]) / 1024**3
                if resource.get("memory_total_bytes") is not None
                else None
            ),
            "cpu_ms": resource.get("cpu_sha256_ms"),
            "disk_write_mib_per_second": resource.get("disk_write_mib_per_second"),
            "disk_read_mib_per_second": resource.get("disk_read_mib_per_second"),
            "database_restore_ms": resource.get("database_restore_ms"),
            "database_rows_restored": resource.get("database_rows_restored"),
            "metadata_region": resource["node_identity"]["region"],
            "node_id": resource["node_identity"]["node_id"],
            "monthly_cost_usd": Decimal(
                str(result["pricing"]["monthly_cost_usd"])
            ),
            "pricing": result["pricing"],
            "evidence_path": result["_evidence_path"],
            "target_origin": result["target_origin"],
        }
        summaries.append(summary)

    eligible = [summary for summary in summaries if summary["eligible"]]
    eligible.sort(
        key=lambda item: (
            float(item["network_median_ms"] or float("inf")),
            float(item["sse_first_event_median_ms"] or float("inf")),
            float(item["cpu_ms"] or float("inf")),
            item["monthly_cost_usd"],
            item["identifier"],
        )
    )
    recommendation = str(eligible[0]["identifier"]) if eligible else None

    ordered = sorted(summaries, key=lambda item: str(item["identifier"]))
    lines = [
        "# Hong Kong Runtime Benchmark",
        "",
        f"- Benchmark version: `{configuration.version}`",
        f"- Protocol SHA-256: `{configuration.protocol_sha256}`",
        f"- Representative workload: `{configuration.workload_version}`",
        f"- Workload image SHA-256: `{next(iter(workload_images))}`",
        f"- Database image SHA-256: `{next(iter(database_images))}`",
        f"- Fixed observer: `{next(iter(observers))}`",
        f"- Candidate results: {len(results)}",
        (
            "- Scope guard: This benchmark does not deploy the MVP, run acquisition or Research, "
            "perform OAuth login, or run database migrations."
        ),
        "- Price warning: observations are dated inputs; do not treat current cloud prices as permanent facts.",
        "",
        "## Probe coverage",
        "",
        (
            "Network and SSE are observed from the one fixed controller against each public "
            "workload. Source egress, Model API, OAuth, and Resource probes execute inside the "
            "same versioned container workload on each Hong Kong node. Model probes perform no "
            "inference and use no provider keys."
        ),
        "",
        "| Candidate | Eligible | Network | SSE | Source egress | Model API | OAuth | Resource | Cost |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in ordered:
        resource_text = (
            f"{item['vcpus']} vCPU / {_format_metric(item['memory_gib'], ' GiB')}; "
            f"CPU {_format_metric(item['cpu_ms'], ' ms')}; "
            f"disk W/R {_format_metric(item['disk_write_mib_per_second'])}/"
            f"{_format_metric(item['disk_read_mib_per_second'], ' MiB/s')}; "
            f"PG restore {_format_metric(item['database_restore_ms'], ' ms')} "
            f"({item['database_rows_restored']} rows)"
        )
        lines.append(
            f"| `{item['identifier']}` | {'PASS' if item['eligible'] else 'FAIL'} | "
            f"{item['network_passes']}/{item['network_total']}; "
            f"median {_format_metric(item['network_median_ms'], ' ms')} | "
            f"{item['sse_passes']}/{item['sse_total']}; first event "
            f"{_format_metric(item['sse_first_event_median_ms'], ' ms')} | "
            f"{item['source_passes']}/{item['source_total']} | "
            f"{item['model_passes']}/{item['model_total']} | "
            f"{item['oauth_passes']}/{item['oauth_total']} | {resource_text} | "
            f"${item['monthly_cost_usd']}/month |"
        )

    lines.extend(
        [
            "",
            "## Decision",
            "",
            (
                f"- Recommendation: `{recommendation}`"
                if recommendation
                else "- Recommendation: none; no candidate passed every fixed gate."
            ),
            (
                "- Eligibility: every repeated Network, SSE, Source egress, Model API, OAuth, "
                "Resource, and Cost measurement must pass."
            ),
            (
                "- Ranking among eligible candidates: lowest median Network latency, then lowest "
                "median SSE first-event latency, then CPU workload latency, monthly cost, and "
                "stable identifier."
            ),
            "- No opaque aggregate score is used; the report preserves every gate and tie-breaker.",
            "",
            "## Evidence",
            "",
        ]
    )
    for item in ordered:
        pricing = item["pricing"]
        lines.append(
            f"- `{item['identifier']}`: target `{item['target_origin']}`; artifact "
            f"`{item['evidence_path']}`; price observed {pricing['observed_at']} from "
            f"[{pricing['source']}]({pricing['source']}); provider metadata "
            f"`{item['metadata_region']}/{item['node_id']}`."
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "benchmark_version": configuration.version,
        "protocol_sha256": configuration.protocol_sha256,
        "observer": next(iter(observers)),
        "workload_image_sha256": next(iter(workload_images)),
        "database_image_sha256": next(iter(database_images)),
        "recommendation": recommendation,
        "summaries": ordered,
    }
