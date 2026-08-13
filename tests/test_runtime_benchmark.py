from __future__ import annotations

import json
import threading
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from ai_intel_agent import cli
from ai_intel_agent.runtime_benchmark import (
    HttpRuntimeProbeClient,
    PricingObservation,
    RuntimeBenchmarkConfigurationError,
    compare_hong_kong_runtime_results,
    load_runtime_benchmark_configuration,
    run_hong_kong_runtime_probe,
)
from ai_intel_agent.runtime_workload import create_runtime_workload_server

runner = CliRunner()


def _workload_transport(request: httpx.Request) -> httpx.Response:
    assert request.url.host is not None and request.url.host.endswith(".example")
    assert "authorization" not in request.headers
    if request.url.path == "/health":
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "workload_version": "hong-kong-runtime-workload-2026-08-13.v1",
            },
        )
    if request.url.path == "/events":
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text="id: 1\ndata: first\n\nid: 2\ndata: second\n\nid: 3\ndata: third\n\n",
        )
    if request.url.path.startswith("/egress/"):
        return httpx.Response(
            200,
            json={
                "reachable": True,
                "status_code": 200,
                "latency_ms": 12,
                "target_origin": "https://fixed.example",
            },
        )
    if request.url.path == "/resource":
        return httpx.Response(
            200,
            json={
                "vcpus": 2,
                "memory_total_bytes": 4 * 1024**3,
                "cpu_sha256_ms": 80,
                "disk_write_mib_per_second": 180,
                "disk_read_mib_per_second": 420,
                "memory_probe_mib": 128,
                "database_dump_ms": 40,
                "database_restore_ms": 55,
                "database_rows_restored": 10000,
                "pressure_mode": "concurrent-web-worker-database",
                "node_identity": {
                    "candidate_identifier": (
                        "tencent-lighthouse-hk"
                        if request.url.host == "benchmark.example"
                        else request.url.host.removesuffix(".example")
                    ),
                    "region": {
                        "benchmark": "ap-hongkong",
                        "tencent-lighthouse-hk": "ap-hongkong",
                        "aws-lightsail-hk": "ap-east-1",
                        "alibaba-swas-hk": "cn-hongkong",
                    }[request.url.host.removesuffix(".example")],
                    "node_id": f"node-{request.url.host}",
                },
            },
        )
    raise AssertionError(f"unexpected workload request: {request.url}")


def test_probe_records_every_fixed_dimension_without_model_credentials(
    tmp_path: Path,
) -> None:
    output = tmp_path / "tencent-lighthouse-hk.json"
    configuration = load_runtime_benchmark_configuration()
    with httpx.Client(transport=httpx.MockTransport(_workload_transport)) as http_client:
        result = run_hong_kong_runtime_probe(
            output,
            candidate_identifier="tencent-lighthouse-hk",
            target_url="https://benchmark.example",
            observer="fixed-mainland-observer",
            pricing=PricingObservation(
                monthly_cost_usd=Decimal("13.20"),
                observed_at=date(2026, 8, 13),
                source="https://cloud.tencent.com/document/product/1207/73452/",
            ),
            workload_image_sha256="a" * 64,
            database_image_sha256="b" * 64,
            client=HttpRuntimeProbeClient(
                "https://benchmark.example",
                configuration=configuration,
                http_client=http_client,
            ),
            configuration=configuration,
            now=lambda: datetime(2026, 8, 13, 4, 0, tzinfo=UTC),
        )

    stored = json.loads(output.read_text(encoding="utf-8"))
    assert stored["benchmark_version"] == configuration.version
    assert stored["protocol_sha256"] == configuration.protocol_sha256
    assert stored["candidate"]["identifier"] == "tencent-lighthouse-hk"
    assert stored["observer"] == "fixed-mainland-observer"
    assert stored["pricing"]["monthly_cost_usd"] == "13.20"
    assert stored["pricing"]["observed_at"] == "2026-08-13"
    assert stored["target_origin"] == "https://benchmark.example"
    assert stored["workload_image_sha256"] == "a" * 64
    assert stored["database_image_sha256"] == "b" * 64
    assert result == stored

    categories = {measurement["category"] for measurement in stored["measurements"]}
    assert categories == {
        "network",
        "SSE",
        "source egress",
        "model API",
        "OAuth",
        "resource",
        "cost",
    }
    assert len([item for item in stored["measurements"] if item["category"] == "network"]) == 3
    assert len([item for item in stored["measurements"] if item["category"] == "SSE"]) == 3
    assert all(item["passed"] for item in stored["measurements"])
    resource = next(
        item for item in stored["measurements"] if item["category"] == "resource"
    )
    assert resource["database_restore_ms"] == 55
    assert resource["database_rows_restored"] == 10000


def _write_candidate_result(
    tmp_path: Path,
    candidate_identifier: str,
    monthly_cost: str,
    network_latency_ms: float,
) -> Path:
    configuration = load_runtime_benchmark_configuration()
    output = tmp_path / f"{candidate_identifier}.json"
    with httpx.Client(transport=httpx.MockTransport(_workload_transport)) as http_client:
        run_hong_kong_runtime_probe(
            output,
            candidate_identifier=candidate_identifier,
            target_url=f"https://{candidate_identifier}.example",
            observer="fixed-mainland-observer",
            pricing=PricingObservation(
                monthly_cost_usd=Decimal(monthly_cost),
                observed_at=date(2026, 8, 13),
                source=configuration.candidate(candidate_identifier).pricing_sources[0],
            ),
            workload_image_sha256="a" * 64,
            database_image_sha256="b" * 64,
            client=HttpRuntimeProbeClient(
                f"https://{candidate_identifier}.example",
                configuration=configuration,
                http_client=http_client,
            ),
            configuration=configuration,
            now=lambda: datetime(2026, 8, 13, 4, 0, tzinfo=UTC),
        )
    payload = json.loads(output.read_text(encoding="utf-8"))
    for measurement in payload["measurements"]:
        if measurement["category"] == "network":
            measurement["latency_ms"] = network_latency_ms
        elif measurement["category"] == "SSE":
            measurement["first_event_ms"] = network_latency_ms + 10
            measurement["completion_ms"] = network_latency_ms + 50
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output


def test_compare_requires_complete_same_protocol_evidence_and_recommends_by_gates(
    tmp_path: Path,
) -> None:
    inputs = [
        _write_candidate_result(tmp_path, "tencent-lighthouse-hk", "13.20", 80),
        _write_candidate_result(tmp_path, "aws-lightsail-hk", "24.00", 120),
        _write_candidate_result(tmp_path, "alibaba-swas-hk", "18.00", 90),
    ]
    output = tmp_path / "hong-kong-runtime-benchmark.md"

    comparison = compare_hong_kong_runtime_results(inputs, output)

    report = output.read_text(encoding="utf-8")
    assert comparison["recommendation"] == "tencent-lighthouse-hk"
    assert "# Hong Kong Runtime Benchmark" in report
    assert "`tencent-lighthouse-hk`" in report
    assert "`aws-lightsail-hk`" in report
    assert "`alibaba-swas-hk`" in report
    assert "fixed-mainland-observer" in report
    assert "Network" in report
    assert "SSE" in report
    assert "Source egress" in report
    assert "Model API" in report
    assert "OAuth" in report
    assert "Resource" in report
    assert "Cost" in report
    assert "current cloud prices as permanent facts" in report
    assert "Recommendation: `tencent-lighthouse-hk`" in report
    assert "No opaque aggregate score" in report
    assert "aaaaaaaaaaaaaaaa" in report

    with pytest.raises(
        RuntimeBenchmarkConfigurationError,
        match="exactly one result for each configured candidate",
    ):
        compare_hong_kong_runtime_results(inputs[:2], tmp_path / "incomplete.md")

    mismatched = json.loads(inputs[-1].read_text(encoding="utf-8"))
    mismatched["workload_image_sha256"] = "b" * 64
    inputs[-1].write_text(
        json.dumps(mismatched, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    with pytest.raises(
        RuntimeBenchmarkConfigurationError,
        match="same workload image SHA-256",
    ):
        compare_hong_kong_runtime_results(inputs, tmp_path / "mixed-images.md")


def test_compare_rejects_substituted_probes_nodes_windows_and_stale_prices(
    tmp_path: Path,
) -> None:
    inputs = [
        _write_candidate_result(tmp_path, "tencent-lighthouse-hk", "13.20", 80),
        _write_candidate_result(tmp_path, "aws-lightsail-hk", "24.00", 120),
        _write_candidate_result(tmp_path, "alibaba-swas-hk", "18.00", 90),
    ]

    substituted = json.loads(inputs[0].read_text(encoding="utf-8"))
    measurement = next(
        item
        for item in substituted["measurements"]
        if item["probe"] == "source-github-releases"
    )
    measurement["probe"] = "source-arxiv-api"
    inputs[0].write_text(
        json.dumps(substituted, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    with pytest.raises(RuntimeBenchmarkConfigurationError, match="exactly once"):
        compare_hong_kong_runtime_results(inputs, tmp_path / "substituted.md")

    inputs[0] = _write_candidate_result(
        tmp_path, "tencent-lighthouse-hk", "13.20", 80
    )
    repeated_node = json.loads(inputs[1].read_text(encoding="utf-8"))
    repeated_node["target_origin"] = "https://tencent-lighthouse-hk.example"
    inputs[1].write_text(
        json.dumps(repeated_node, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    with pytest.raises(RuntimeBenchmarkConfigurationError, match="distinct candidate"):
        compare_hong_kong_runtime_results(inputs, tmp_path / "same-node.md")

    inputs[1] = _write_candidate_result(tmp_path, "aws-lightsail-hk", "24.00", 120)
    late = json.loads(inputs[2].read_text(encoding="utf-8"))
    late["run_at"] = "2026-08-14T08:00:00+00:00"
    next(
        item for item in late["measurements"] if item["category"] == "cost"
    )["price_age_days"] = 1
    inputs[2].write_text(
        json.dumps(late, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    with pytest.raises(RuntimeBenchmarkConfigurationError, match="observation window"):
        compare_hong_kong_runtime_results(
            inputs,
            tmp_path / "wide-window.md",
            now=lambda: datetime(2026, 8, 15, tzinfo=UTC),
        )

    inputs[2] = _write_candidate_result(tmp_path, "alibaba-swas-hk", "18.00", 90)
    with pytest.raises(RuntimeBenchmarkConfigurationError, match="stale"):
        compare_hong_kong_runtime_results(
            inputs,
            tmp_path / "stale-price.md",
            now=lambda: datetime(2026, 9, 20, tzinfo=UTC),
        )

    contradictory = json.loads(inputs[0].read_text(encoding="utf-8"))
    cost = next(
        item for item in contradictory["measurements"] if item["category"] == "cost"
    )
    cost["monthly_cost_usd"] = "0.01"
    inputs[0].write_text(
        json.dumps(contradictory, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    with pytest.raises(RuntimeBenchmarkConfigurationError, match="contradicts"):
        compare_hong_kong_runtime_results(inputs, tmp_path / "contradictory-cost.md")


def test_probe_requires_candidate_official_price_evidence(tmp_path: Path) -> None:
    configuration = load_runtime_benchmark_configuration()
    with httpx.Client(transport=httpx.MockTransport(_workload_transport)) as http_client:
        client = HttpRuntimeProbeClient(
            "https://benchmark.example",
            configuration=configuration,
            http_client=http_client,
        )
        with pytest.raises(RuntimeBenchmarkConfigurationError, match="official sources"):
            run_hong_kong_runtime_probe(
                tmp_path / "unofficial.json",
                candidate_identifier="tencent-lighthouse-hk",
                target_url="https://benchmark.example",
                observer="fixed-mainland-observer",
                pricing=PricingObservation(
                    monthly_cost_usd=Decimal("13.20"),
                    observed_at=date(2026, 8, 13),
                    source="https://example.com/pricing",
                ),
                workload_image_sha256="a" * 64,
                database_image_sha256="b" * 64,
                client=client,
                configuration=configuration,
                now=lambda: datetime(2026, 8, 13, 4, 0, tzinfo=UTC),
            )


def test_cli_exposes_probe_and_compare_commands(monkeypatch, tmp_path: Path) -> None:
    probe_output = tmp_path / "probe.json"
    report_output = tmp_path / "report.md"
    observed: dict[str, object] = {}

    class FakeClient:
        target_origin = "https://tencent.example"

        def __init__(self, target_url, *, configuration, workload_token):
            observed["target_url"] = target_url
            observed["workload_token"] = workload_token

        def close(self) -> None:
            observed["closed"] = True

    def fake_probe(output, **kwargs):
        observed["probe_output"] = output
        observed["probe_kwargs"] = kwargs
        output.write_text("{}\n", encoding="utf-8")
        return {"passed": True, "measurements": []}

    def fake_compare(inputs, output, **kwargs):
        observed["compare_inputs"] = inputs
        output.write_text("# report\n", encoding="utf-8")
        return {"recommendation": "tencent-lighthouse-hk"}

    monkeypatch.setattr(cli, "HttpRuntimeProbeClient", FakeClient)
    monkeypatch.setattr(cli, "run_hong_kong_runtime_probe", fake_probe)
    monkeypatch.setattr(cli, "compare_hong_kong_runtime_results", fake_compare)
    monkeypatch.setenv("RUNTIME_BENCHMARK_TOKEN", "environment-only-secret")

    probe_result = runner.invoke(
        cli.app,
        [
            "benchmark-runtime",
            "probe",
            "--candidate",
            "tencent-lighthouse-hk",
            "--target-url",
            "https://tencent.example",
            "--observer",
            "fixed-mainland-observer",
            "--monthly-cost-usd",
            "13.20",
            "--price-observed-at",
            "2026-08-13",
            "--price-source",
            "https://cloud.tencent.com/document/product/1207/73452/",
            "--workload-image-sha256",
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "--database-image-sha256",
            "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "--output",
            str(probe_output),
        ],
    )
    compare_result = runner.invoke(
        cli.app,
        [
            "benchmark-runtime",
            "compare",
            "--input",
            str(probe_output),
            "--input",
            str(probe_output),
            "--input",
            str(probe_output),
            "--output",
            str(report_output),
        ],
    )

    assert probe_result.exit_code == 0, probe_result.output
    assert compare_result.exit_code == 0, compare_result.output
    assert observed["workload_token"] == "environment-only-secret"
    assert observed["probe_output"] == probe_output
    assert observed["closed"] is True
    assert observed["compare_inputs"] == [probe_output, probe_output, probe_output]
    assert "Captured fixed Hong Kong runtime probes" in probe_result.output
    assert "Recommended Hong Kong runtime: tencent-lighthouse-hk" in compare_result.output


def test_representative_workload_http_boundary_is_fixed_and_token_protected() -> None:
    server = create_runtime_workload_server(
        "127.0.0.1",
        0,
        token="temporary-secret",
        database_probe=lambda: {
            "database_dump_ms": 40,
            "database_restore_ms": 55,
            "database_rows_restored": 10000,
        },
        node_identity_probe=lambda: {
            "candidate_identifier": "tencent-lighthouse-hk",
            "region": "ap-hongkong",
            "node_id": "lhins-test",
        },
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        with httpx.Client(
            base_url=f"http://{host}:{port}", timeout=20, trust_env=False
        ) as client:
            assert client.get("/health").status_code == 403
            headers = {"X-Benchmark-Token": "temporary-secret"}
            health = client.get("/health", headers=headers)
            events = client.get("/events", headers=headers)
            resource = client.get("/resource", headers=headers)
            unknown = client.get("/egress/not-allowlisted", headers=headers)

        assert health.json() == {
            "status": "ok",
            "workload_version": "hong-kong-runtime-workload-2026-08-13.v1",
        }
        assert events.headers["content-type"].startswith("text/event-stream")
        assert events.text.count("data:") == 3
        resource_payload = resource.json()
        assert resource_payload["vcpus"] >= 1
        assert resource_payload["memory_total_bytes"] > 0
        assert resource_payload["cpu_sha256_ms"] > 0
        assert resource_payload["disk_write_mib_per_second"] > 0
        assert resource_payload["disk_read_mib_per_second"] > 0
        assert resource_payload["memory_probe_mib"] == 128
        assert resource_payload["database_dump_ms"] == 40
        assert resource_payload["database_restore_ms"] == 55
        assert resource_payload["database_rows_restored"] == 10000
        assert resource_payload["pressure_mode"] == "concurrent-web-worker-database"
        assert resource_payload["node_identity"] == {
            "candidate_identifier": "tencent-lighthouse-hk",
            "region": "ap-hongkong",
            "node_id": "lhins-test",
        }
        assert unknown.status_code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
