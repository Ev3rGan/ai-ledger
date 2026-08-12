from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from typer.testing import CliRunner

from ai_intel_agent import cli
from ai_intel_agent.extraction_benchmark import (
    BENCHMARK_VERSION,
    CORPUS,
    EXTRACTION_PATHS,
    BenchmarkCredentials,
    ExtractionAttempt,
    ExtractionFailure,
    ExtractionPayload,
    FirecrawlAdapter,
    PlaywrightTrafilaturaAdapter,
    ProviderUsageSnapshot,
    TavilyAdapter,
    _body_completeness_ratio,
    _metadata_is_accurate,
    _noise_ratio,
    _provider_request_weight,
    run_benchmark_with_adapters,
)

runner = CliRunner()

CORPUS_CATEGORIES = (
    "Official English announcements",
    "Chinese technical media",
    "Dynamic product pages",
    "Technical docs and changelogs",
    "Research papers and PDFs",
    "Repositories and releases",
)

BENCHMARK_DIMENSIONS = (
    "Body extraction",
    "Body completeness",
    "Metadata",
    "Noise",
    "Provenance anchoring",
    "Repeatability",
    "Reliability",
    "Latency",
    "Cost",
)


def test_benchmark_credentials_do_not_expose_secrets_in_repr() -> None:
    credentials = BenchmarkCredentials.from_environment(
        {
            "FIRECRAWL_API_KEY": "firecrawl-secret",
            "TAVILY_API_KEY": "tavily-secret",
        }
    )

    assert "firecrawl-secret" not in repr(credentials)
    assert "tavily-secret" not in repr(credentials)


class FakeAdapter:
    def __init__(self, path_name: str, managed: bool) -> None:
        self.path_name = path_name
        self.managed = managed
        self.calls: list[tuple[str, int]] = []
        self._usage_reads = 0

    async def extract(self, entry, attempt: int) -> ExtractionPayload:
        self.calls.append((entry.identifier, attempt))
        title = f"{entry.publisher} benchmark document"
        if self.path_name == "HTTP plus Trafilatura" and entry == CORPUS[0]:
            title = "Self asserted incorrect title"
        body = " ".join(
            [
                title,
                entry.identifier,
                entry.publisher,
                "fixed benchmark article body with exact source wording and technical details",
            ]
            * 20
        )
        return ExtractionPayload(
            body=body,
            title=title,
            description="Fixed metadata description for the benchmark fixture.",
            canonical_url=entry.url,
            published_at="2026-08-12",
            source_text=None if self.managed else body,
            final_url=entry.url,
            http_status=200,
            provider_credits=Decimal(1) if self.managed else Decimal(0),
        )

    async def usage_snapshot(self) -> ProviderUsageSnapshot | None:
        self._usage_reads += 1
        if not self.managed:
            return None
        if self.path_name == "Firecrawl":
            value = Decimal(1000 if self._usage_reads == 1 else 880)
            return ProviderUsageSnapshot(value=value, direction="remaining")
        return ProviderUsageSnapshot(value=Decimal(50), direction="cumulative")


def test_live_benchmark_runs_every_url_path_twice_and_generates_report(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "document-extraction-benchmark.md"
    adapters = tuple(FakeAdapter(path.name, path.kind == "managed") for path in EXTRACTION_PATHS)

    benchmark = run_benchmark_with_adapters(
        output_path,
        adapters=adapters,
        attempts=2,
        concurrency=32,
        now=lambda: datetime(2026, 8, 12, 12, 0, tzinfo=UTC),
    )

    report = output_path.read_text(encoding="utf-8")
    assert len(benchmark.corpus) == 60
    assert len(benchmark.measurements) == 240
    undisputed_measurements = [
        measurement
        for measurement in benchmark.measurements
        if measurement.url_identifier != CORPUS[0].identifier
    ]
    assert all(measurement.body_complete for measurement in undisputed_measurements)
    assert all(
        measurement.body_completeness_ratio == 1.0 for measurement in undisputed_measurements
    )
    disputed_local_metadata = [
        measurement
        for measurement in benchmark.measurements
        if measurement.url_identifier == CORPUS[0].identifier
        and measurement.extraction_path in {"HTTP plus Trafilatura", "Playwright plus Trafilatura"}
    ]
    assert disputed_local_metadata
    assert not any(measurement.metadata_accurate for measurement in disputed_local_metadata)
    assert sum(len(adapter.calls) for adapter in adapters) == 480
    assert all(len(adapter.calls) == 120 for adapter in adapters)

    assert "# Document Extraction Benchmark" in report
    assert f"- Benchmark version: `{BENCHMARK_VERSION}`" in report
    assert "- Evaluation mode: live" in report
    assert "- Corpus URLs: 60" in report
    assert "- Attempts per URL and path: 2" in report
    assert "- Path-by-URL measurements: 240" in report
    assert "- Scope guard: This command does not activate production Source Definitions" in report
    assert "Rewritten or synthesized text remains ineligible for Evidence" in report
    assert "Raw extracted bodies are not written to this report" in report
    assert "Body completeness" in report
    assert "weighted attribution" in report
    assert "documented schedule; account delta lagged at 0.00" in report
    assert (
        "| `Tavily` | documented schedule; account delta lagged at 0.00 | weighted attribution "
        "by successful provider responses | 50.00 | 50.00 | 48.00" in report
    )

    assert all(f"`{path.name}`" in report for path in EXTRACTION_PATHS)
    assert all(category in report for category in CORPUS_CATEGORIES)
    assert all(dimension in report for dimension in BENCHMARK_DIMENSIONS)

    corpus_rows = [
        line
        for line in report.splitlines()
        if line.startswith("| ") and line.count("|") >= 5 and "https://" in line
    ]
    assert len(corpus_rows) == 60

    measurement_rows = [line for line in report.splitlines() if line.startswith("| measurement-")]
    assert len(measurement_rows) == 240
    assert all("fixed benchmark article body" not in line for line in measurement_rows)

    recommendation_lines = [
        line
        for line in report.splitlines()
        if line.startswith("- Managed fallback recommendation:")
    ]
    assert len(recommendation_lines) == 1
    assert "Firecrawl" in recommendation_lines[0]


def test_body_completeness_is_recall_against_local_reference() -> None:
    reference = " ".join(f"token-{index}" for index in range(100))
    long_prefix = " ".join(f"prefix-{index}" for index in range(12_000))

    assert _body_completeness_ratio(reference, reference) == 1.0
    assert _body_completeness_ratio(f"{long_prefix} {reference}", reference) == 1.0
    assert _body_completeness_ratio(" ".join(reference.split()[:30]), reference) < 0.70


def test_noise_ratio_recognizes_chinese_boilerplate() -> None:
    body = "隐私政策\n登录\n注册\n关注我们\n正文信息"

    assert _noise_ratio(body) > 0.50


def test_metadata_accuracy_requires_local_title_and_canonical_agreement() -> None:
    accurate = ExtractionPayload(
        body="Source body " * 40,
        title="Model 4.2 release notes",
        canonical_url="https://example.com/releases/model-4-2/?source=benchmark",
        published_at="2026-08-12",
    )
    invented_title = ExtractionPayload(
        body=accurate.body,
        title="Invented provider summary",
        canonical_url=accurate.canonical_url,
    )
    wrong_page = ExtractionPayload(
        body=accurate.body,
        title=accurate.title,
        canonical_url="https://example.com/pricing",
    )
    wrong_date = ExtractionPayload(
        body=accurate.body,
        title=accurate.title,
        canonical_url=accurate.canonical_url,
        published_at="2025-01-01",
    )
    references = {
        "reference_titles": ("Model 4.2 release notes | Example",),
        "reference_urls": ("https://example.com/releases/model-4-2",),
        "reference_dates": ("2026-08-12T09:00:00Z",),
        "reference_source": "Model 4.2 release notes. Detailed changes follow.",
    }

    assert _metadata_is_accurate(accurate, **references)
    assert not _metadata_is_accurate(invented_title, **references)
    assert not _metadata_is_accurate(wrong_page, **references)
    assert not _metadata_is_accurate(wrong_date, **references)


def test_provider_request_weights_preserve_variable_credit_costs() -> None:
    firecrawl_attempt = ExtractionAttempt(
        url_identifier=CORPUS[0].identifier,
        category=CORPUS[0].category,
        extraction_path="Firecrawl",
        attempt=1,
        latency_ms=1,
        payload=ExtractionPayload(body="body", provider_credits=Decimal(5)),
        error=None,
        http_status=200,
    )
    tavily_attempt = ExtractionAttempt(
        url_identifier=CORPUS[0].identifier,
        category=CORPUS[0].category,
        extraction_path="Tavily",
        attempt=1,
        latency_ms=1,
        payload=ExtractionPayload(body="body", provider_credits=Decimal(2)),
        error=None,
        http_status=200,
    )

    assert _provider_request_weight("Firecrawl", firecrawl_attempt) == Decimal(5)
    assert _provider_request_weight("Tavily", tavily_attempt) == Decimal(1)


def test_playwright_rejects_http_error_pages_before_extraction() -> None:
    class FakePage:
        url = CORPUS[0].url

        async def route(self, *args, **kwargs) -> None:
            pass

        async def goto(self, *args, **kwargs):
            return SimpleNamespace(status=404)

        async def close(self) -> None:
            pass

    class FakeContext:
        async def new_page(self) -> FakePage:
            return FakePage()

    async def exercise() -> None:
        adapter = PlaywrightTrafilaturaAdapter(
            FakeContext(),
            concurrency=1,
            timeout_seconds=1,
        )
        with pytest.raises(ExtractionFailure, match="HTTP 404"):
            await adapter.extract(CORPUS[0], 1)

    asyncio.run(exercise())


def test_cli_invokes_live_benchmark_runner(monkeypatch, tmp_path: Path) -> None:
    output_path = tmp_path / "document-extraction-benchmark.md"
    observed: dict[str, object] = {}

    def fake_run(output: Path, *, attempts: int, concurrency: int, progress):
        observed.update(
            output=output,
            attempts=attempts,
            concurrency=concurrency,
            progress=progress,
        )
        output.write_text("# live benchmark\n", encoding="utf-8")
        return SimpleNamespace(corpus=CORPUS, extraction_paths=EXTRACTION_PATHS)

    monkeypatch.setattr(cli, "run_document_extraction_benchmark", fake_run)

    result = runner.invoke(
        cli.app,
        [
            "benchmark-extraction",
            "--output",
            str(output_path),
            "--attempts",
            "2",
            "--concurrency",
            "7",
        ],
    )

    assert result.exit_code == 0, result.output
    assert observed["output"] == output_path
    assert observed["attempts"] == 2
    assert observed["concurrency"] == 7
    assert callable(observed["progress"])
    assert "Benchmarked 60 fixed corpus URLs across 4 extraction paths" in result.output


def test_firecrawl_adapter_sends_v2_scrape_request_and_parses_metadata() -> None:
    async def exercise() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url == "https://api.firecrawl.dev/v2/scrape"
            assert request.headers["Authorization"] == "Bearer fc-test"
            payload = json.loads(request.content)
            assert payload["formats"] == ["markdown"]
            assert payload["onlyMainContent"] is True
            assert payload["maxAge"] == 0
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "markdown": "Source body " * 40,
                        "metadata": {
                            "title": "Source title",
                            "description": "Source description",
                            "sourceURL": CORPUS[0].url,
                            "publishedTime": "2026-08-12",
                            "statusCode": 200,
                        },
                    },
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = FirecrawlAdapter(client, "fc-test", concurrency=1)
            result = await adapter.extract(CORPUS[0], 1)

        assert result.title == "Source title"
        assert result.canonical_url == CORPUS[0].url
        assert result.http_status == 200
        assert len(result.body) > 200

    asyncio.run(exercise())


def test_tavily_adapter_sends_extract_request_and_parses_result() -> None:
    async def exercise() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url == "https://api.tavily.com/extract"
            assert request.headers["Authorization"] == "Bearer tvly-test"
            payload = json.loads(request.content)
            assert payload["urls"] == CORPUS[0].url
            assert payload["extract_depth"] == "advanced"
            assert payload["include_usage"] is True
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "url": CORPUS[0].url,
                            "raw_content": "Source body " * 40,
                            "title": "Source title",
                        }
                    ],
                    "failed_results": [],
                    "usage": {"credits": 2},
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = TavilyAdapter(client, "tvly-test", concurrency=1)
            result = await adapter.extract(CORPUS[0], 1)

        assert result.title == "Source title"
        assert result.canonical_url == CORPUS[0].url
        assert result.provider_credits == Decimal(2)
        assert len(result.body) > 200

    asyncio.run(exercise())
