from __future__ import annotations

import asyncio
import hashlib
import math
import os
import re
from collections import Counter, defaultdict
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from itertools import pairwise
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Literal, Protocol
from urllib.parse import urljoin, urlsplit

import httpx
from dotenv import load_dotenv
from playwright.async_api import BrowserContext, async_playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from trafilatura import extract, extract_metadata, html2txt

from ai_intel_agent.extraction_corpus import CORPUS, CorpusUrl

BENCHMARK_VERSION = "document-extraction-benchmark-2026-08-12.v4-live"
USER_AGENT = "ai-ledger-document-extraction-benchmark/0.1"
BODY_MIN_CHARS = 200
BODY_COMPLETENESS_MIN_RATIO = 0.70
LOW_NOISE_MAX_RATIO = 0.20
PROVENANCE_MIN_RATIO = 0.70
REPEATABILITY_MIN_SIMILARITY = 0.90
MANAGED_MIN_COMPLETE_PAGES = 48
MANAGED_MIN_ANCHORED_PAGES = 30
MANAGED_MIN_RELIABLE_PAGES = 48
MAX_CAPTURE_CHARS = 500_000
MAX_SHINGLE_SAMPLES = 5_000
FIRECRAWL_USD_PER_CREDIT = Decimal("0.0032")
TAVILY_USD_PER_CREDIT = Decimal("0.008")

ProgressCallback = Callable[[int, int, str], None]


class BenchmarkConfigurationError(ValueError):
    pass


class ExtractionFailure(RuntimeError):
    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status


@dataclass(frozen=True)
class ExtractionPath:
    name: str
    kind: Literal["local", "local browser", "managed"]
    description: str
    credit_schedule: Literal["none", "firecrawl", "tavily"]
    usd_per_credit: Decimal


@dataclass(frozen=True)
class BenchmarkCredentials:
    firecrawl_api_key: str = field(repr=False)
    tavily_api_key: str = field(repr=False)

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> BenchmarkCredentials:
        if environment is None:
            load_dotenv()
            environment = os.environ
        firecrawl_api_key = environment.get("FIRECRAWL_API_KEY", "").strip()
        tavily_api_key = environment.get("TAVILY_API_KEY", "").strip()
        missing = [
            name
            for name, value in (
                ("FIRECRAWL_API_KEY", firecrawl_api_key),
                ("TAVILY_API_KEY", tavily_api_key),
            )
            if not value
        ]
        if missing:
            raise BenchmarkConfigurationError(
                "live extraction benchmark requires: " + ", ".join(missing)
            )
        return cls(
            firecrawl_api_key=firecrawl_api_key,
            tavily_api_key=tavily_api_key,
        )


@dataclass(frozen=True)
class ExtractionPayload:
    body: str
    title: str | None = None
    description: str | None = None
    canonical_url: str | None = None
    published_at: str | None = None
    source_text: str | None = None
    final_url: str | None = None
    http_status: int | None = None
    provider_credits: Decimal = Decimal(0)


@dataclass(frozen=True)
class ProviderUsageSnapshot:
    value: Decimal
    direction: Literal["remaining", "cumulative"]


@dataclass(frozen=True)
class ExtractionAttempt:
    url_identifier: str
    category: str
    extraction_path: str
    attempt: int
    latency_ms: int
    payload: ExtractionPayload | None
    error: str | None
    http_status: int | None


@dataclass(frozen=True)
class UrlPathMeasurement:
    identifier: str
    url_identifier: str
    category: str
    extraction_path: str
    attempts_succeeded: int
    attempts_total: int
    body_extracted: bool
    body_chars: int
    body_hash: str | None
    body_completeness_ratio: float
    body_complete: bool
    metadata_fields: int
    metadata_accurate: bool
    noise_ratio: float
    low_noise: bool
    provenance_ratio: float
    provenance_anchored: bool
    repeatability_similarity: float
    repeatable: bool
    reliable: bool
    latency_ms: int
    provider_credits: Decimal
    estimated_cost_usd: Decimal
    error: str | None


@dataclass(frozen=True)
class CategoryPathResult:
    category: str
    extraction_path: str
    body_successes: int
    body_complete_pages: int
    metadata_successes: int
    low_noise_pages: int
    anchored_pages: int
    repeatable_pages: int
    reliable_pages: int
    p50_latency_ms: int
    p95_latency_ms: int
    cost_per_1000_url_attempts_usd: Decimal


@dataclass(frozen=True)
class PathSummary:
    extraction_path: str
    body_successes: int
    body_complete_pages: int
    metadata_successes: int
    low_noise_pages: int
    anchored_pages: int
    repeatable_pages: int
    reliable_pages: int
    p50_latency_ms: int
    p95_latency_ms: int
    observed_credits: Decimal
    observed_cost_usd: Decimal
    cost_per_1000_url_attempts_usd: Decimal


@dataclass(frozen=True)
class ProviderCostObservation:
    extraction_path: str
    credits_before: Decimal | None
    credits_after: Decimal | None
    credits_used: Decimal
    usage_basis: str
    allocation_basis: str
    usd_per_credit: Decimal
    estimated_cost_usd: Decimal


@dataclass(frozen=True)
class DocumentExtractionBenchmark:
    version: str
    started_at: datetime
    completed_at: datetime
    attempts_per_url_path: int
    corpus: tuple[CorpusUrl, ...]
    extraction_paths: tuple[ExtractionPath, ...]
    measurements: tuple[UrlPathMeasurement, ...]
    results: tuple[CategoryPathResult, ...]
    path_summaries: tuple[PathSummary, ...]
    provider_costs: tuple[ProviderCostObservation, ...]
    managed_fallback_recommendation: str | None
    recommendation_reason: str


class ExtractionAdapter(Protocol):
    path_name: str

    async def extract(self, entry: CorpusUrl, attempt: int) -> ExtractionPayload: ...

    async def usage_snapshot(self) -> ProviderUsageSnapshot | None: ...


EXTRACTION_PATHS = (
    ExtractionPath(
        name="HTTP plus Trafilatura",
        kind="local",
        description=(
            "Direct HTTP fetch followed by local Trafilatura body and metadata extraction."
        ),
        credit_schedule="none",
        usd_per_credit=Decimal(0),
    ),
    ExtractionPath(
        name="Playwright plus Trafilatura",
        kind="local browser",
        description=("Chromium-rendered HTML followed by the same local Trafilatura extraction."),
        credit_schedule="none",
        usd_per_credit=Decimal(0),
    ),
    ExtractionPath(
        name="Firecrawl",
        kind="managed",
        description="Firecrawl v2 Scrape with main-content Markdown and fresh acquisition.",
        credit_schedule="firecrawl",
        usd_per_credit=FIRECRAWL_USD_PER_CREDIT,
    ),
    ExtractionPath(
        name="Tavily",
        kind="managed",
        description="Tavily Extract with advanced-depth Markdown output.",
        credit_schedule="tavily",
        usd_per_credit=TAVILY_USD_PER_CREDIT,
    ),
)


class HttpTrafilaturaAdapter:
    path_name = "HTTP plus Trafilatura"

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        concurrency: int,
    ) -> None:
        self._client = client
        self.concurrency = concurrency
        self._semaphore = asyncio.Semaphore(concurrency)

    async def extract(self, entry: CorpusUrl, attempt: int) -> ExtractionPayload:
        del attempt
        async with self._semaphore:
            response = await _request_with_retries(self._client, "GET", entry.url)
            content_type = response.headers.get("content-type", "").lower()
            if not _is_text_document(content_type):
                raise ExtractionFailure(
                    f"unsupported content type: {content_type or 'unknown'}",
                    http_status=response.status_code,
                )
            return _extract_html_payload(
                response.text,
                final_url=str(response.url),
                http_status=response.status_code,
            )

    async def usage_snapshot(self) -> ProviderUsageSnapshot | None:
        return None


class PlaywrightTrafilaturaAdapter:
    path_name = "Playwright plus Trafilatura"

    def __init__(
        self,
        context: BrowserContext,
        *,
        concurrency: int,
        timeout_seconds: float,
    ) -> None:
        self._context = context
        self.concurrency = concurrency
        self._semaphore = asyncio.Semaphore(concurrency)
        self._timeout_ms = int(timeout_seconds * 1000)

    async def extract(self, entry: CorpusUrl, attempt: int) -> ExtractionPayload:
        del attempt
        async with self._semaphore:
            page = await self._context.new_page()
            try:
                await page.route("**/*", _block_heavy_browser_resources)
                response = await page.goto(
                    entry.url,
                    wait_until="domcontentloaded",
                    timeout=self._timeout_ms,
                )
                if response is not None and response.status >= 400:
                    raise ExtractionFailure(
                        f"HTTP {response.status}: browser navigation returned an error page",
                        http_status=response.status,
                    )
                try:
                    await page.wait_for_function(
                        "document.body && document.body.innerText.trim().length >= 200",
                        timeout=min(self._timeout_ms, 5000),
                    )
                except PlaywrightTimeoutError:
                    pass
                html = await page.content()
                return _extract_html_payload(
                    html,
                    final_url=page.url,
                    http_status=response.status if response is not None else None,
                )
            except PlaywrightError as error:
                raise ExtractionFailure(_safe_error(error)) from error
            finally:
                await page.close()

    async def usage_snapshot(self) -> ProviderUsageSnapshot | None:
        return None


class FirecrawlAdapter:
    path_name = "Firecrawl"

    def __init__(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        *,
        concurrency: int,
    ) -> None:
        self._client = client
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self.concurrency = concurrency
        self._semaphore = asyncio.Semaphore(concurrency)
        self._rate_limiter = _IntervalRateLimiter(1.25)

    async def extract(self, entry: CorpusUrl, attempt: int) -> ExtractionPayload:
        del attempt
        async with self._semaphore:
            await self._rate_limiter.wait()
            response = await _request_with_retries(
                self._client,
                "POST",
                "https://api.firecrawl.dev/v2/scrape",
                headers=self._headers,
                json={
                    "url": entry.url,
                    "formats": ["markdown"],
                    "onlyMainContent": True,
                    "maxAge": 0,
                    "parsers": ["pdf"],
                    "proxy": "auto",
                    "storeInCache": False,
                    "timeout": 30_000,
                },
            )
            payload = _json_object(response)
            if payload.get("success") is not True:
                raise ExtractionFailure(
                    _api_error(payload, "Firecrawl returned success=false"),
                    http_status=response.status_code,
                )
            data = payload.get("data")
            if not isinstance(data, dict):
                raise ExtractionFailure("Firecrawl response omitted data")
            body = _string(data.get("markdown"))
            if not body:
                raise ExtractionFailure("Firecrawl response omitted markdown")
            metadata = data.get("metadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            reported_url = _first_string(metadata, "sourceURL", "url")
            return ExtractionPayload(
                body=body[:MAX_CAPTURE_CHARS],
                title=_first_string(metadata, "title", "ogTitle"),
                description=_first_string(metadata, "description", "ogDescription"),
                canonical_url=reported_url,
                published_at=_first_string(
                    metadata,
                    "publishedTime",
                    "published_time",
                    "date",
                ),
                final_url=reported_url or entry.url,
                http_status=_integer(metadata.get("statusCode")) or response.status_code,
                provider_credits=_firecrawl_credit_estimate(metadata),
            )

    async def usage_snapshot(self) -> ProviderUsageSnapshot | None:
        response = await _request_with_retries(
            self._client,
            "GET",
            "https://api.firecrawl.dev/v2/team/credit-usage",
            headers=self._headers,
        )
        payload = _json_object(response)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ExtractionFailure("Firecrawl credit response omitted data")
        remaining = _decimal(data.get("remainingCredits"))
        if remaining is None:
            raise ExtractionFailure("Firecrawl credit response omitted remainingCredits")
        return ProviderUsageSnapshot(value=remaining, direction="remaining")


class TavilyAdapter:
    path_name = "Tavily"

    def __init__(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        *,
        concurrency: int,
    ) -> None:
        self._client = client
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self.concurrency = concurrency
        self._semaphore = asyncio.Semaphore(concurrency)

    async def extract(self, entry: CorpusUrl, attempt: int) -> ExtractionPayload:
        del attempt
        async with self._semaphore:
            response = await _request_with_retries(
                self._client,
                "POST",
                "https://api.tavily.com/extract",
                headers=self._headers,
                json={
                    "urls": entry.url,
                    "extract_depth": "advanced",
                    "include_images": False,
                    "include_favicon": False,
                    "format": "markdown",
                    "timeout": 30,
                    "include_usage": True,
                },
            )
            payload = _json_object(response)
            results = payload.get("results")
            if not isinstance(results, list) or not results:
                raise ExtractionFailure(
                    _api_error(payload, "Tavily response contained no successful result"),
                    http_status=response.status_code,
                )
            result = results[0]
            if not isinstance(result, dict):
                raise ExtractionFailure("Tavily result was not an object")
            body = _string(result.get("raw_content"))
            if not body:
                raise ExtractionFailure("Tavily result omitted raw_content")
            usage = payload.get("usage")
            usage = usage if isinstance(usage, dict) else {}
            reported_url = _first_string(result, "url")
            return ExtractionPayload(
                body=body[:MAX_CAPTURE_CHARS],
                title=_first_string(result, "title"),
                description=_first_string(result, "description"),
                canonical_url=reported_url,
                published_at=_first_string(result, "published_date", "published_at"),
                final_url=reported_url or entry.url,
                http_status=response.status_code,
                provider_credits=_decimal(usage.get("credits")) or Decimal(0),
            )

    async def usage_snapshot(self) -> ProviderUsageSnapshot | None:
        response = await _request_with_retries(
            self._client,
            "GET",
            "https://api.tavily.com/usage",
            headers=self._headers,
        )
        payload = _json_object(response)
        key_usage = payload.get("key")
        if not isinstance(key_usage, dict):
            raise ExtractionFailure("Tavily usage response omitted key usage")
        used = _decimal(key_usage.get("extract_usage"))
        if used is None:
            raise ExtractionFailure("Tavily usage response omitted extract_usage")
        return ProviderUsageSnapshot(value=used, direction="cumulative")


def run_document_extraction_benchmark(
    output_path: Path,
    *,
    attempts: int = 2,
    concurrency: int = 12,
    progress: ProgressCallback | None = None,
) -> DocumentExtractionBenchmark:
    credentials = BenchmarkCredentials.from_environment()
    return asyncio.run(
        _run_live_benchmark(
            output_path,
            credentials=credentials,
            attempts=attempts,
            concurrency=concurrency,
            progress=progress,
        )
    )


def run_benchmark_with_adapters(
    output_path: Path,
    *,
    adapters: Sequence[ExtractionAdapter],
    attempts: int = 2,
    concurrency: int = 12,
    progress: ProgressCallback | None = None,
    now: Callable[[], datetime] | None = None,
    corpus: tuple[CorpusUrl, ...] = CORPUS,
) -> DocumentExtractionBenchmark:
    return asyncio.run(
        _run_with_adapters(
            output_path,
            adapters=adapters,
            attempts=attempts,
            concurrency=concurrency,
            progress=progress,
            now=now or _utc_now,
            corpus=corpus,
        )
    )


async def _run_live_benchmark(
    output_path: Path,
    *,
    credentials: BenchmarkCredentials,
    attempts: int,
    concurrency: int,
    progress: ProgressCallback | None,
) -> DocumentExtractionBenchmark:
    async with _default_live_adapters(credentials) as adapters:
        return await _run_with_adapters(
            output_path,
            adapters=adapters,
            attempts=attempts,
            concurrency=concurrency,
            progress=progress,
            now=_utc_now,
            corpus=CORPUS,
        )


@asynccontextmanager
async def _default_live_adapters(
    credentials: BenchmarkCredentials,
) -> AsyncIterator[tuple[ExtractionAdapter, ...]]:
    timeout = httpx.Timeout(45.0, connect=15.0)
    limits = httpx.Limits(max_connections=24, max_keepalive_connections=12)
    async with (
        httpx.AsyncClient(
            timeout=timeout,
            limits=limits,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client,
        async_playwright() as playwright,
    ):
        try:
            browser = await playwright.chromium.launch(headless=True)
        except PlaywrightError as error:
            raise BenchmarkConfigurationError(
                "Chromium is unavailable; run `playwright install chromium`"
            ) from error
        context = await browser.new_context(
            user_agent=USER_AGENT,
            locale="en-US",
            ignore_https_errors=False,
        )
        try:
            yield (
                HttpTrafilaturaAdapter(client, concurrency=8),
                PlaywrightTrafilaturaAdapter(
                    context,
                    concurrency=3,
                    timeout_seconds=30,
                ),
                FirecrawlAdapter(
                    client,
                    credentials.firecrawl_api_key,
                    concurrency=2,
                ),
                TavilyAdapter(
                    client,
                    credentials.tavily_api_key,
                    concurrency=3,
                ),
            )
        finally:
            await context.close()
            await browser.close()


async def _run_with_adapters(
    output_path: Path,
    *,
    adapters: Sequence[ExtractionAdapter],
    attempts: int,
    concurrency: int,
    progress: ProgressCallback | None,
    now: Callable[[], datetime],
    corpus: tuple[CorpusUrl, ...],
) -> DocumentExtractionBenchmark:
    adapters = tuple(adapters)
    _validate_configuration(corpus, adapters, attempts, concurrency)
    started_at = now()
    usage_before = await _usage_snapshots(adapters)
    global_semaphore = asyncio.Semaphore(concurrency)
    path_semaphores = {
        adapter.path_name: asyncio.Semaphore(
            max(1, min(concurrency, getattr(adapter, "concurrency", concurrency)))
        )
        for adapter in adapters
    }
    completed = 0
    total = len(corpus) * len(adapters) * attempts
    observations: list[ExtractionAttempt] = []

    async def execute(
        adapter: ExtractionAdapter,
        entry: CorpusUrl,
        attempt_number: int,
    ) -> ExtractionAttempt:
        nonlocal completed
        payload: ExtractionPayload | None = None
        error_message: str | None = None
        http_status: int | None = None
        async with path_semaphores[adapter.path_name], global_semaphore:
            started = perf_counter()
            try:
                payload = await adapter.extract(entry, attempt_number)
                http_status = payload.http_status
            except ExtractionFailure as error:
                error_message = _safe_error(error)
                http_status = error.http_status
            except (
                httpx.HTTPError,
                PlaywrightError,
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
            ) as error:
                detail = _safe_error(error)
                error_message = (
                    f"{type(error).__name__}: {detail}" if detail else type(error).__name__
                )
            latency_ms = max(1, round((perf_counter() - started) * 1000))
        completed += 1
        if progress is not None:
            progress(
                completed,
                total,
                f"{adapter.path_name}: {entry.identifier} attempt {attempt_number}",
            )
        return ExtractionAttempt(
            url_identifier=entry.identifier,
            category=entry.category,
            extraction_path=adapter.path_name,
            attempt=attempt_number,
            latency_ms=latency_ms,
            payload=payload,
            error=error_message,
            http_status=http_status,
        )

    for attempt_number in range(1, attempts + 1):
        round_results = await asyncio.gather(
            *(execute(adapter, entry, attempt_number) for adapter in adapters for entry in corpus)
        )
        observations.extend(round_results)

    usage_after = await _usage_snapshots(adapters)
    provider_costs = _provider_cost_observations(
        adapters,
        usage_before,
        usage_after,
        observations,
    )
    measurements = _build_measurements(
        corpus,
        observations,
        attempts,
        provider_costs,
    )
    category_results = _build_category_results(corpus, measurements, attempts)
    path_summaries = _build_path_summaries(
        corpus,
        measurements,
        attempts,
        provider_costs,
    )
    recommendation, recommendation_reason = _managed_fallback_recommendation(path_summaries)
    benchmark = DocumentExtractionBenchmark(
        version=BENCHMARK_VERSION,
        started_at=started_at,
        completed_at=now(),
        attempts_per_url_path=attempts,
        corpus=corpus,
        extraction_paths=EXTRACTION_PATHS,
        measurements=measurements,
        results=category_results,
        path_summaries=path_summaries,
        provider_costs=provider_costs,
        managed_fallback_recommendation=recommendation,
        recommendation_reason=recommendation_reason,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(_render_markdown(benchmark), encoding="utf-8")
    return benchmark


async def _usage_snapshots(
    adapters: Sequence[ExtractionAdapter],
) -> dict[str, ProviderUsageSnapshot | None]:
    async def read(adapter: ExtractionAdapter) -> ProviderUsageSnapshot | None:
        try:
            return await adapter.usage_snapshot()
        except (ExtractionFailure, httpx.HTTPError, ValueError):
            return None

    snapshots = await asyncio.gather(*(read(adapter) for adapter in adapters))
    return {
        adapter.path_name: snapshot for adapter, snapshot in zip(adapters, snapshots, strict=True)
    }


def _provider_cost_observations(
    adapters: Sequence[ExtractionAdapter],
    before: Mapping[str, ProviderUsageSnapshot | None],
    after: Mapping[str, ProviderUsageSnapshot | None],
    observations: Sequence[ExtractionAttempt],
) -> tuple[ProviderCostObservation, ...]:
    costs: list[ProviderCostObservation] = []
    for adapter in adapters:
        path_name = adapter.path_name
        before_snapshot = before.get(path_name)
        after_snapshot = after.get(path_name)
        account_delta: Decimal | None = None
        if (
            before_snapshot is not None
            and after_snapshot is not None
            and before_snapshot.direction == after_snapshot.direction
        ):
            if before_snapshot.direction == "remaining":
                account_delta = max(Decimal(0), before_snapshot.value - after_snapshot.value)
            else:
                account_delta = max(Decimal(0), after_snapshot.value - before_snapshot.value)
        documented_estimate = _documented_credit_estimate(path_name, observations)
        if account_delta is None:
            credits_used = documented_estimate
            basis = "documented schedule estimate"
        elif account_delta < documented_estimate:
            credits_used = documented_estimate
            basis = f"documented schedule; account delta lagged at {account_delta:.2f}"
        else:
            credits_used = account_delta
            basis = "provider account delta"
        unit_rate = _usd_per_credit(path_name)
        costs.append(
            ProviderCostObservation(
                extraction_path=path_name,
                credits_before=before_snapshot.value if before_snapshot else None,
                credits_after=after_snapshot.value if after_snapshot else None,
                credits_used=credits_used,
                usage_basis=basis,
                allocation_basis=_cost_allocation_basis(path_name),
                usd_per_credit=unit_rate,
                estimated_cost_usd=credits_used * unit_rate,
            )
        )
    return tuple(costs)


def _build_measurements(
    corpus: tuple[CorpusUrl, ...],
    observations: Sequence[ExtractionAttempt],
    attempts_per_path: int,
    provider_costs: Sequence[ProviderCostObservation],
) -> tuple[UrlPathMeasurement, ...]:
    observations_by_pair: dict[tuple[str, str], list[ExtractionAttempt]] = defaultdict(list)
    for observation in observations:
        observations_by_pair[(observation.url_identifier, observation.extraction_path)].append(
            observation
        )

    local_path_names = {path.name for path in EXTRACTION_PATHS if path.kind != "managed"}
    reference_source_by_url: dict[str, str] = {}
    reference_body_by_url: dict[str, str] = {}
    for entry in corpus:
        local_payloads = [
            observation.payload
            for observation in observations
            if observation.url_identifier == entry.identifier
            and observation.extraction_path in local_path_names
            and observation.payload is not None
        ]
        source_candidates = [
            payload.source_text for payload in local_payloads if payload.source_text
        ]
        if source_candidates:
            reference_source_by_url[entry.identifier] = max(source_candidates, key=len)
        body_candidates = [
            observation.payload.body
            for observation in observations
            if observation.url_identifier == entry.identifier
            and observation.extraction_path in local_path_names
            and _attempt_has_body(observation)
            and observation.payload is not None
        ]
        if body_candidates:
            reference_body_by_url[entry.identifier] = max(
                body_candidates,
                key=lambda body: len(_normalize_text(body)),
            )

    credits_by_path = {cost.extraction_path: cost.credits_used for cost in provider_costs}
    request_weights_by_path = {
        path.name: sum(
            (
                _provider_request_weight(path.name, observation)
                for observation in observations
                if observation.extraction_path == path.name
            ),
            Decimal(0),
        )
        for path in EXTRACTION_PATHS
    }
    corpus_by_identifier = {entry.identifier: entry for entry in corpus}
    measurements: list[UrlPathMeasurement] = []
    for path in EXTRACTION_PATHS:
        for entry in corpus:
            pair_observations = sorted(
                observations_by_pair[(entry.identifier, path.name)],
                key=lambda observation: observation.attempt,
            )
            successful = [
                observation for observation in pair_observations if _attempt_has_body(observation)
            ]
            returned_payloads = [
                observation.payload
                for observation in pair_observations
                if observation.error is None and observation.payload is not None
            ]
            first_payload = (
                successful[0].payload
                if successful
                else returned_payloads[0]
                if returned_payloads
                else None
            )
            assert first_payload is not None or not successful
            bodies = [
                observation.payload.body
                for observation in successful
                if observation.payload is not None
            ]
            noise_ratio = (
                sum(_noise_ratio(body) for body in bodies) / len(bodies) if bodies else 1.0
            )
            reference_source = reference_source_by_url.get(entry.identifier, "")
            reference_body = reference_body_by_url.get(entry.identifier, "")
            completeness_ratios = [
                _body_completeness_ratio(body, reference_body) for body in bodies
            ]
            completeness_ratio = min(completeness_ratios) if completeness_ratios else 0.0
            provenance_ratios = [_provenance_ratio(body, reference_source) for body in bodies]
            provenance_ratio = (
                sum(provenance_ratios) / len(provenance_ratios) if provenance_ratios else 0.0
            )
            repeatability = _repeatability_similarity(bodies, attempts_per_path)
            metadata_fields = _metadata_field_count(first_payload)
            metadata_reference_path_names = (
                local_path_names if path.kind == "managed" else local_path_names - {path.name}
            )
            metadata_reference_payloads = [
                observation.payload
                for reference_path_name in metadata_reference_path_names
                for observation in observations_by_pair[(entry.identifier, reference_path_name)]
                if observation.error is None and observation.payload is not None
            ]
            metadata_reference_sources = [
                payload.source_text
                for payload in metadata_reference_payloads
                if payload.source_text
            ]
            metadata_reference_source = (
                max(metadata_reference_sources, key=len) if metadata_reference_sources else ""
            )
            metadata_accurate = _metadata_is_accurate(
                first_payload,
                reference_titles=tuple(
                    dict.fromkeys(
                        payload.title for payload in metadata_reference_payloads if payload.title
                    )
                ),
                reference_urls=tuple(
                    dict.fromkeys(
                        [entry.url]
                        + [
                            url
                            for payload in metadata_reference_payloads
                            for url in (payload.final_url, payload.canonical_url)
                            if url
                        ]
                    )
                ),
                reference_dates=tuple(
                    dict.fromkeys(
                        payload.published_at
                        for payload in metadata_reference_payloads
                        if payload.published_at
                    )
                ),
                reference_source=metadata_reference_source,
            )
            path_request_weight = request_weights_by_path[path.name]
            pair_request_weight = sum(
                (
                    _provider_request_weight(path.name, observation)
                    for observation in pair_observations
                ),
                Decimal(0),
            )
            credits = credits_by_path.get(path.name, Decimal(0))
            allocated_credits = (
                credits * pair_request_weight / path_request_weight
                if path_request_weight
                else Decimal(0)
            )
            errors = list(
                dict.fromkeys(
                    observation.error for observation in pair_observations if observation.error
                )
            )
            below_threshold = len(returned_payloads) - len(successful)
            if below_threshold:
                errors.append(
                    f"{below_threshold} attempt(s) below {BODY_MIN_CHARS}-character threshold"
                )
            representative_body = first_payload.body if first_payload is not None else ""
            normalized_representative = _normalize_text(representative_body)
            measurements.append(
                UrlPathMeasurement(
                    identifier=f"measurement-{entry.identifier}-{_slug(path.name)}",
                    url_identifier=entry.identifier,
                    category=corpus_by_identifier[entry.identifier].category,
                    extraction_path=path.name,
                    attempts_succeeded=len(successful),
                    attempts_total=attempts_per_path,
                    body_extracted=bool(successful),
                    body_chars=len(representative_body),
                    body_hash=(
                        hashlib.sha256(normalized_representative.encode("utf-8")).hexdigest()[:16]
                        if normalized_representative
                        else None
                    ),
                    body_completeness_ratio=completeness_ratio,
                    body_complete=(
                        bool(reference_body)
                        and bool(bodies)
                        and completeness_ratio >= BODY_COMPLETENESS_MIN_RATIO
                    ),
                    metadata_fields=metadata_fields,
                    metadata_accurate=metadata_accurate,
                    noise_ratio=noise_ratio,
                    low_noise=bool(bodies) and noise_ratio <= LOW_NOISE_MAX_RATIO,
                    provenance_ratio=provenance_ratio,
                    provenance_anchored=(
                        bool(reference_source)
                        and bool(bodies)
                        and provenance_ratio >= PROVENANCE_MIN_RATIO
                    ),
                    repeatability_similarity=repeatability,
                    repeatable=(
                        len(successful) == attempts_per_path
                        and repeatability >= REPEATABILITY_MIN_SIMILARITY
                    ),
                    reliable=len(successful) == attempts_per_path,
                    latency_ms=round(
                        median(observation.latency_ms for observation in pair_observations)
                    ),
                    provider_credits=allocated_credits,
                    estimated_cost_usd=(allocated_credits * _usd_per_credit(path.name)),
                    error="; ".join(errors) if errors else None,
                )
            )
    expected = len(corpus) * len(EXTRACTION_PATHS)
    if len(measurements) != expected:
        raise ValueError(f"expected {expected} measurements, found {len(measurements)}")
    return tuple(measurements)


def _build_category_results(
    corpus: tuple[CorpusUrl, ...],
    measurements: Sequence[UrlPathMeasurement],
    attempts_per_path: int,
) -> tuple[CategoryPathResult, ...]:
    results: list[CategoryPathResult] = []
    for category in _category_order(corpus):
        for path in EXTRACTION_PATHS:
            group = [
                measurement
                for measurement in measurements
                if measurement.category == category and measurement.extraction_path == path.name
            ]
            latencies = [measurement.latency_ms for measurement in group]
            total_cost = sum(
                (measurement.estimated_cost_usd for measurement in group),
                Decimal(0),
            )
            denominator = Decimal(len(group) * attempts_per_path)
            results.append(
                CategoryPathResult(
                    category=category,
                    extraction_path=path.name,
                    body_successes=sum(item.body_extracted for item in group),
                    body_complete_pages=sum(item.body_complete for item in group),
                    metadata_successes=sum(item.metadata_accurate for item in group),
                    low_noise_pages=sum(item.low_noise for item in group),
                    anchored_pages=sum(item.provenance_anchored for item in group),
                    repeatable_pages=sum(item.repeatable for item in group),
                    reliable_pages=sum(item.reliable for item in group),
                    p50_latency_ms=_percentile(latencies, 0.50),
                    p95_latency_ms=_percentile(latencies, 0.95),
                    cost_per_1000_url_attempts_usd=(
                        total_cost * Decimal(1000) / denominator if denominator else Decimal(0)
                    ),
                )
            )
    return tuple(results)


def _build_path_summaries(
    corpus: tuple[CorpusUrl, ...],
    measurements: Sequence[UrlPathMeasurement],
    attempts_per_path: int,
    provider_costs: Sequence[ProviderCostObservation],
) -> tuple[PathSummary, ...]:
    cost_by_path = {cost.extraction_path: cost for cost in provider_costs}
    summaries: list[PathSummary] = []
    for path in EXTRACTION_PATHS:
        group = [item for item in measurements if item.extraction_path == path.name]
        cost = cost_by_path[path.name]
        denominator = Decimal(len(corpus) * attempts_per_path)
        summaries.append(
            PathSummary(
                extraction_path=path.name,
                body_successes=sum(item.body_extracted for item in group),
                body_complete_pages=sum(item.body_complete for item in group),
                metadata_successes=sum(item.metadata_accurate for item in group),
                low_noise_pages=sum(item.low_noise for item in group),
                anchored_pages=sum(item.provenance_anchored for item in group),
                repeatable_pages=sum(item.repeatable for item in group),
                reliable_pages=sum(item.reliable for item in group),
                p50_latency_ms=_percentile([item.latency_ms for item in group], 0.50),
                p95_latency_ms=_percentile([item.latency_ms for item in group], 0.95),
                observed_credits=cost.credits_used,
                observed_cost_usd=cost.estimated_cost_usd,
                cost_per_1000_url_attempts_usd=(
                    cost.estimated_cost_usd * Decimal(1000) / denominator
                    if denominator
                    else Decimal(0)
                ),
            )
        )
    return tuple(summaries)


def _managed_fallback_recommendation(
    summaries: Sequence[PathSummary],
) -> tuple[str | None, str]:
    managed_names = {path.name for path in EXTRACTION_PATHS if path.kind == "managed"}
    candidates = [
        summary
        for summary in summaries
        if summary.extraction_path in managed_names
        and summary.body_complete_pages >= MANAGED_MIN_COMPLETE_PAGES
        and summary.anchored_pages >= MANAGED_MIN_ANCHORED_PAGES
        and summary.reliable_pages >= MANAGED_MIN_RELIABLE_PAGES
    ]
    if not candidates:
        return (
            None,
            (
                "No managed path met the provisional fallback gate of body completeness on "
                f"{MANAGED_MIN_COMPLETE_PAGES}/60 URLs, local provenance anchoring on "
                f"{MANAGED_MIN_ANCHORED_PAGES}/60, and reliability on "
                f"{MANAGED_MIN_RELIABLE_PAGES}/60, so this run recommends no managed fallback."
            ),
        )

    def score(summary: PathSummary) -> tuple[float, Decimal]:
        quality = (
            summary.body_successes
            + summary.body_complete_pages
            + summary.metadata_successes
            + summary.low_noise_pages
            + (2 * summary.anchored_pages)
            + summary.repeatable_pages
            + summary.reliable_pages
        )
        return float(quality), -summary.observed_cost_usd

    chosen = max(candidates, key=score)
    return (
        chosen.extraction_path,
        (
            f"{chosen.extraction_path} ranked highest among managed paths in this live run: "
            f"body extracted {chosen.body_successes}/60, body complete "
            f"{chosen.body_complete_pages}/60, metadata {chosen.metadata_successes}/60, "
            f"low-noise {chosen.low_noise_pages}/60, locally anchorable "
            f"{chosen.anchored_pages}/60, repeatable {chosen.repeatable_pages}/60, and "
            f"reliable {chosen.reliable_pages}/60. This is a benchmark recommendation only; "
            "production activation remains out of scope."
        ),
    )


def _render_markdown(benchmark: DocumentExtractionBenchmark) -> str:
    recommendation = benchmark.managed_fallback_recommendation or "none"
    lines = [
        "# Document Extraction Benchmark",
        "",
        f"- Benchmark version: `{benchmark.version}`",
        "- Parent Spec: #1",
        "- Implementation ticket: #4",
        "- Evaluation mode: live",
        f"- Started at: `{benchmark.started_at.isoformat()}`",
        f"- Completed at: `{benchmark.completed_at.isoformat()}`",
        f"- Corpus URLs: {len(benchmark.corpus)}",
        f"- Corpus categories: {len(_category_order(benchmark.corpus))}",
        "- URLs per category: 10",
        f"- Attempts per URL and path: {benchmark.attempts_per_url_path}",
        f"- Path-by-URL measurements: {len(benchmark.measurements)}",
        f"- Managed fallback recommendation: `{recommendation}`",
        (
            "- Scope guard: This command does not activate production Source Definitions, "
            "integrate Collection Runs, freeze provisional Q144 thresholds, or create Evidence."
        ),
        "",
        (
            "Rewritten or synthesized text remains ineligible for Evidence. Managed output is "
            "scored only against locally acquired source text, and any future Evidence Span "
            "still requires exact source text, offsets, and hashes on an immutable Document "
            "Version. Raw extracted bodies are not written to this report."
        ),
        "",
        "## Methodology",
        "",
        (
            f"- Body extraction: at least {BODY_MIN_CHARS} normalized characters in a successful "
            "attempt."
        ),
        (
            "- Body completeness: minimum successful-attempt recall of up to 5,000 "
            "deterministically selected exact eight-token shingles from the longest locally "
            f"extracted body, with a pass threshold of {BODY_COMPLETENESS_MIN_RATIO:.0%}."
        ),
        (
            "- Metadata: each path is checked against independent local acquisition: the title "
            "must agree with another local path's title or source text, the canonical URL must "
            "match a locally observed document URL, and any reported date must agree with an "
            "independently observed date. Publication date remains the third completeness field."
        ),
        (f"- Noise: deterministic boilerplate-line ratio at or below {LOW_NOISE_MAX_RATIO:.0%}."),
        (
            "- Provenance anchoring: up to 5,000 deterministically selected exact eight-token "
            "body shingles found in the longest locally acquired source view, with a pass "
            f"threshold of {PROVENANCE_MIN_RATIO:.0%}."
        ),
        (
            "- Repeatability: minimum bidirectional five-token-shingle containment across "
            "independent rounds, with a pass threshold of "
            f"{REPEATABILITY_MIN_SIMILARITY:.0%}."
        ),
        "- Reliability: every configured attempt returned an extractable body.",
        "- Latency: local wall-clock duration, reported as p50 and p95.",
        (
            "- Cost: provider account-credit deltas when available, otherwise response-level "
            "estimates. Category and URL costs use weighted attribution from response credit "
            "weights; USD values use documented reference rates, not an invoice."
        ),
        (
            "- Provider references (captured 2026-08-12): "
            "[Firecrawl Scrape](https://docs.firecrawl.dev/api-reference/endpoint/scrape), "
            "[Firecrawl pricing](https://www.firecrawl.dev/pricing), "
            "[Tavily Extract](https://docs.tavily.com/documentation/api-reference/endpoint/extract), "
            "and [Tavily credits](https://docs.tavily.com/documentation/api-credits)."
        ),
        (
            "- Managed fallback gate: at least "
            f"{MANAGED_MIN_COMPLETE_PAGES}/60 complete body extractions, "
            f"{MANAGED_MIN_ANCHORED_PAGES}/60 locally anchorable pages, and "
            f"{MANAGED_MIN_RELIABLE_PAGES}/60 reliable pages."
        ),
        "",
        "## Extraction paths",
        "",
        "| Path | Kind | Live configuration |",
        "| --- | --- | --- |",
    ]
    for path in benchmark.extraction_paths:
        lines.append(f"| `{path.name}` | {path.kind} | {path.description} |")

    lines.extend(
        [
            "",
            "## Managed fallback recommendation",
            "",
            f"- Chosen managed path: `{recommendation}`",
            f"- Rationale: {benchmark.recommendation_reason}",
            "- Guardrail: at most one managed fallback may advance from this benchmark.",
            (
                "- Evidence guardrail: a managed extraction may assist acquisition but cannot "
                "itself become Evidence without exact local anchoring."
            ),
            "",
            "## Aggregate comparison",
            "",
            (
                "| Path | Body extraction | Body completeness | Metadata | Noise | "
                "Provenance anchoring | Repeatability | Reliability | Latency | Cost |"
            ),
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
        ]
    )
    for summary in benchmark.path_summaries:
        lines.append(
            "| "
            f"`{summary.extraction_path}` | "
            f"{summary.body_successes}/60 | "
            f"{summary.body_complete_pages}/60 | "
            f"{summary.metadata_successes}/60 | "
            f"{summary.low_noise_pages}/60 low-noise | "
            f"{summary.anchored_pages}/60 | "
            f"{summary.repeatable_pages}/60 | "
            f"{summary.reliable_pages}/60 | "
            f"p50 {summary.p50_latency_ms} ms / p95 {summary.p95_latency_ms} ms | "
            f"{summary.observed_credits:.2f} credits / "
            f"${summary.observed_cost_usd:.4f} run / "
            f"${summary.cost_per_1000_url_attempts_usd:.4f} per 1k URL attempts |"
        )

    lines.extend(
        [
            "",
            "## Provider cost observation",
            "",
            (
                "| Path | Usage basis | Category attribution | Credits before | Credits after | "
                "Credits used | Reference USD/credit | Estimated run cost |"
            ),
            "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for cost in benchmark.provider_costs:
        lines.append(
            f"| `{cost.extraction_path}` | {cost.usage_basis} | {cost.allocation_basis} | "
            f"{_decimal_cell(cost.credits_before)} | "
            f"{_decimal_cell(cost.credits_after)} | "
            f"{cost.credits_used:.2f} | "
            f"${cost.usd_per_credit:.4f} | ${cost.estimated_cost_usd:.4f} |"
        )

    lines.extend(
        [
            "",
            "## Category comparison",
            "",
            (
                "| Category | Path | Body extraction | Body completeness | Metadata | Noise | "
                "Provenance anchoring | Repeatability | Reliability | Latency | Cost |"
            ),
            "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: |",
        ]
    )
    for result in benchmark.results:
        lines.append(
            f"| {result.category} | `{result.extraction_path}` | "
            f"{result.body_successes}/10 | {result.body_complete_pages}/10 | "
            f"{result.metadata_successes}/10 | "
            f"{result.low_noise_pages}/10 low-noise | {result.anchored_pages}/10 | "
            f"{result.repeatable_pages}/10 | {result.reliable_pages}/10 | "
            f"p50 {result.p50_latency_ms} ms / p95 {result.p95_latency_ms} ms | "
            f"${result.cost_per_1000_url_attempts_usd:.4f} per 1k URL attempts |"
        )

    lines.extend(
        [
            "",
            "## Fixed 60-URL corpus",
            "",
            "| ID | Category | Publisher | Shape | URL |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for entry in benchmark.corpus:
        lines.append(
            f"| {entry.identifier} | {entry.category} | {entry.publisher} | "
            f"{entry.document_shape} | {entry.url} |"
        )

    lines.extend(
        [
            "",
            "## Path-by-URL measurements",
            "",
            (
                "| Measurement ID | URL ID | Category | Path | Attempts | Body extraction | "
                "Body completeness | Metadata | Noise | Provenance anchoring | Repeatability | "
                "Reliability | Latency | Cost | Error |"
            ),
            (
                "| --- | --- | --- | --- | ---: | --- | --- | --- | --- | --- | --- | --- | "
                "---: | ---: | --- |"
            ),
        ]
    )
    for measurement in benchmark.measurements:
        body_cell = (
            f"yes ({measurement.body_chars} chars; `{measurement.body_hash}`)"
            if measurement.body_extracted
            else (
                f"no ({measurement.body_chars} chars; `{measurement.body_hash}`)"
                if measurement.body_hash
                else "no"
            )
        )
        lines.append(
            f"| {measurement.identifier} | {measurement.url_identifier} | "
            f"{measurement.category} | `{measurement.extraction_path}` | "
            f"{measurement.attempts_succeeded}/{measurement.attempts_total} | "
            f"{body_cell} | {measurement.body_completeness_ratio:.1%}; "
            f"{_yes_no(measurement.body_complete)} | {measurement.metadata_fields}/3; "
            f"{_yes_no(measurement.metadata_accurate)} | "
            f"{measurement.noise_ratio:.1%}; {_yes_no(measurement.low_noise)} | "
            f"{measurement.provenance_ratio:.1%}; "
            f"{_yes_no(measurement.provenance_anchored)} | "
            f"{measurement.repeatability_similarity:.1%}; "
            f"{_yes_no(measurement.repeatable)} | "
            f"{_yes_no(measurement.reliable)} | {measurement.latency_ms} ms | "
            f"{measurement.provider_credits:.3f} credits / "
            f"${measurement.estimated_cost_usd:.5f} | "
            f"{_table_text(measurement.error or '')} |"
        )

    lines.extend(
        [
            "",
            "## Interpretation guardrails",
            "",
            "- Prefer official feeds and APIs before any page-body extraction path.",
            "- Prefer HTTP plus Trafilatura for accessible static documents.",
            (
                "- Use Playwright plus Trafilatura only for fixed dynamic sources where an "
                "official feed or API is unavailable."
            ),
            (
                "- Treat the managed recommendation as provisional benchmark evidence, not a "
                "production Source Definition or frozen product threshold."
            ),
        ]
    )
    return "\n".join(lines) + "\n"


async def _request_with_retries(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    **kwargs: object,
) -> httpx.Response:
    response: httpx.Response | None = None
    for retry in range(3):
        response = await client.request(method, url, **kwargs)
        if response.status_code not in {408, 425, 429, 500, 502, 503, 504}:
            break
        if retry < 2:
            retry_after = _retry_after_seconds(response, retry)
            await asyncio.sleep(retry_after)
    assert response is not None
    if response.is_error:
        payload: object
        try:
            payload = response.json()
        except ValueError:
            payload = None
        message = (
            _api_error(payload, response.reason_phrase)
            if isinstance(payload, dict)
            else response.reason_phrase
        )
        raise ExtractionFailure(
            f"HTTP {response.status_code}: {message}",
            http_status=response.status_code,
        )
    return response


class _CanonicalUrlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.canonical_url: str | None = None
        self.open_graph_url: str | None = None

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = {name.lower(): value for name, value in attrs if value is not None}
        if tag.lower() == "link" and "canonical" in attributes.get("rel", "").lower().split():
            self.canonical_url = self.canonical_url or attributes.get("href")
        if tag.lower() == "meta" and attributes.get("property", "").lower() == "og:url":
            self.open_graph_url = self.open_graph_url or attributes.get("content")


def _canonical_url_from_html(html: str, final_url: str) -> str | None:
    parser = _CanonicalUrlParser()
    parser.feed(html)
    candidate = parser.canonical_url or parser.open_graph_url
    if not candidate:
        return None
    resolved = urljoin(final_url, candidate)
    parsed = urlsplit(resolved)
    return resolved if parsed.scheme in {"http", "https"} and parsed.hostname else None


def _extract_html_payload(
    html: str,
    *,
    final_url: str,
    http_status: int | None,
) -> ExtractionPayload:
    body = extract(
        html,
        url=final_url,
        output_format="markdown",
        include_comments=False,
        include_tables=True,
        favor_recall=True,
    )
    if not body:
        raise ExtractionFailure(
            "Trafilatura did not extract a body",
            http_status=http_status,
        )
    metadata = extract_metadata(html, default_url=final_url).as_dict()
    source_text = html2txt(html) or body
    return ExtractionPayload(
        body=body[:MAX_CAPTURE_CHARS],
        title=_first_string(metadata, "title"),
        description=_first_string(metadata, "description"),
        canonical_url=_canonical_url_from_html(html, final_url),
        published_at=_first_string(metadata, "date"),
        source_text=source_text[:MAX_CAPTURE_CHARS],
        final_url=final_url,
        http_status=http_status,
    )


async def _block_heavy_browser_resources(route) -> None:
    if route.request.resource_type in {"image", "media", "font"}:
        await route.abort()
    else:
        await route.continue_()


def _validate_configuration(
    corpus: tuple[CorpusUrl, ...],
    adapters: Sequence[ExtractionAdapter],
    attempts: int,
    concurrency: int,
) -> None:
    if attempts < 2:
        raise BenchmarkConfigurationError(
            "at least two attempts are required to measure repeatability"
        )
    if concurrency < 1:
        raise BenchmarkConfigurationError("concurrency must be at least one")
    if len(corpus) != 60:
        raise BenchmarkConfigurationError(
            f"document extraction corpus must contain 60 URLs, found {len(corpus)}"
        )
    if len({entry.identifier for entry in corpus}) != len(corpus):
        raise BenchmarkConfigurationError("corpus identifiers must be unique")
    if len({entry.url for entry in corpus}) != len(corpus):
        raise BenchmarkConfigurationError("corpus URLs must be unique")
    categories = Counter(entry.category for entry in corpus)
    if len(categories) != 6 or any(count != 10 for count in categories.values()):
        raise BenchmarkConfigurationError(
            "the fixed corpus must contain six categories with ten URLs each"
        )
    expected_paths = {path.name for path in EXTRACTION_PATHS}
    actual_paths = {adapter.path_name for adapter in adapters}
    if actual_paths != expected_paths or len(adapters) != len(EXTRACTION_PATHS):
        raise BenchmarkConfigurationError(
            f"adapter coverage mismatch; expected={sorted(expected_paths)}, "
            f"actual={sorted(actual_paths)}"
        )


def _attempt_has_body(observation: ExtractionAttempt) -> bool:
    return (
        observation.error is None
        and observation.payload is not None
        and len(_normalize_text(observation.payload.body)) >= BODY_MIN_CHARS
    )


def _metadata_field_count(payload: ExtractionPayload | None) -> int:
    if payload is None:
        return 0
    return sum(
        bool(value) for value in (payload.title, payload.canonical_url, payload.published_at)
    )


def _metadata_is_accurate(
    payload: ExtractionPayload | None,
    *,
    reference_titles: Sequence[str],
    reference_urls: Sequence[str],
    reference_dates: Sequence[str],
    reference_source: str,
) -> bool:
    if payload is None or not payload.title or not payload.canonical_url:
        return False
    title_accurate = _title_matches_reference(
        payload.title,
        reference_titles=reference_titles,
        reference_source=reference_source,
    )
    url_accurate = any(
        _same_document_url(payload.canonical_url, reference_url) for reference_url in reference_urls
    )
    date_accurate = True
    if payload.published_at:
        normalized_date = _normalize_date(payload.published_at)
        normalized_reference_dates = {
            normalized for value in reference_dates if (normalized := _normalize_date(value))
        }
        date_accurate = bool(normalized_date and normalized_date in normalized_reference_dates)
    return title_accurate and url_accurate and date_accurate


def _title_matches_reference(
    title: str,
    *,
    reference_titles: Sequence[str],
    reference_source: str,
) -> bool:
    candidate = _normalize_metadata_text(title)
    if not candidate:
        return False
    references = [_normalize_metadata_text(value) for value in reference_titles]
    for reference in references:
        if not reference:
            continue
        if candidate == reference:
            return True
        if min(len(candidate), len(reference)) >= 8 and (
            candidate in reference or reference in candidate
        ):
            return True
        candidate_tokens = set(candidate.split())
        reference_tokens = set(reference.split())
        if candidate_tokens and reference_tokens:
            overlap = len(candidate_tokens & reference_tokens) / min(
                len(candidate_tokens), len(reference_tokens)
            )
            if overlap >= 0.80:
                return True
    normalized_source = _normalize_metadata_text(reference_source)
    return len(candidate) >= 8 and candidate in normalized_source


def _normalize_metadata_text(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+|[\u3400-\u9fff]", value.lower()))


def _normalize_date(value: str) -> str:
    normalized = value.strip().lower()
    date_match = re.search(
        r"(?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)",
        normalized,
    )
    if date_match:
        year, month, day = date_match.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"
    return _normalize_metadata_text(normalized)


def _same_document_url(left: str, right: str) -> bool:
    left_parts = urlsplit(left)
    right_parts = urlsplit(right)
    left_host = (left_parts.hostname or "").lower().removeprefix("www.")
    right_host = (right_parts.hostname or "").lower().removeprefix("www.")
    left_path = re.sub(r"/{2,}", "/", left_parts.path).rstrip("/") or "/"
    right_path = re.sub(r"/{2,}", "/", right_parts.path).rstrip("/") or "/"
    return bool(left_host and right_host and left_host == right_host and left_path == right_path)


def _noise_ratio(body: str) -> float:
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if not lines:
        return 1.0
    markers = (
        "accept cookies",
        "cookie policy",
        "privacy policy",
        "terms of service",
        "sign in",
        "log in",
        "subscribe",
        "skip to content",
        "main menu",
        "advertisement",
        "all rights reserved",
        "隐私政策",
        "登录",
        "注册",
        "关注我们",
    )
    noisy_chars = 0
    seen: Counter[str] = Counter()
    for line in lines:
        normalized = _normalize_text(line)
        seen[normalized] += 1
        is_marker = len(line) <= 160 and any(marker in normalized for marker in markers)
        is_link_only = bool(re.fullmatch(r"(?:\[[^]]+\]\([^)]+\)\s*)+", line))
        if is_marker or is_link_only:
            noisy_chars += len(line)
    noisy_chars += sum(len(line) * (count - 1) for line, count in seen.items() if count > 1)
    total_chars = sum(len(line) for line in lines)
    return min(1.0, noisy_chars / max(1, total_chars))


def _provenance_ratio(body: str, source_text: str) -> float:
    return _sampled_shingle_containment(
        candidate=source_text,
        reference=body,
        width=8,
    )


def _body_completeness_ratio(body: str, reference_body: str) -> float:
    return _sampled_shingle_containment(
        candidate=body,
        reference=reference_body,
        width=8,
    )


def _repeatability_similarity(bodies: Sequence[str], attempts: int) -> float:
    if len(bodies) != attempts or attempts < 2:
        return 0.0
    similarities: list[float] = []
    for left, right in pairwise(bodies):
        left_in_right = _sampled_shingle_containment(
            candidate=right,
            reference=left,
            width=5,
        )
        right_in_left = _sampled_shingle_containment(
            candidate=left,
            reference=right,
            width=5,
        )
        similarities.append(min(left_in_right, right_in_left))
    return min(similarities)


def _sampled_shingle_containment(
    *,
    candidate: str,
    reference: str,
    width: int,
) -> float:
    reference_shingles = _sampled_shingles(reference, width=width)
    candidate_tokens = _tokens(candidate)
    if not reference_shingles or len(candidate_tokens) < width:
        return 0.0
    matched: set[tuple[str, ...]] = set()
    for index in range(len(candidate_tokens) - width + 1):
        shingle = tuple(candidate_tokens[index : index + width])
        if shingle in reference_shingles:
            matched.add(shingle)
            if len(matched) == len(reference_shingles):
                return 1.0
    return len(matched) / len(reference_shingles)


def _sampled_shingles(value: str, *, width: int) -> set[tuple[str, ...]]:
    tokens = _tokens(value)
    if len(tokens) < width:
        return set()
    total = len(tokens) - width + 1
    if total <= MAX_SHINGLE_SAMPLES:
        indices = range(total)
    else:
        indices = (
            index * (total - 1) // (MAX_SHINGLE_SAMPLES - 1) for index in range(MAX_SHINGLE_SAMPLES)
        )
    return {tuple(tokens[index : index + width]) for index in indices}


def _tokens(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+|[\u3400-\u9fff]", value.lower())


def _percentile(values: Sequence[int], quantile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[index]


def _category_order(corpus: tuple[CorpusUrl, ...]) -> list[str]:
    return list(dict.fromkeys(entry.category for entry in corpus))


def _is_text_document(content_type: str) -> bool:
    return any(marker in content_type for marker in ("text/", "html", "xml", "rss", "atom"))


def _json_object(response: httpx.Response) -> dict[str, object]:
    try:
        payload = response.json()
    except ValueError as error:
        raise ExtractionFailure(
            "provider returned invalid JSON",
            http_status=response.status_code,
        ) from error
    if not isinstance(payload, dict):
        raise ExtractionFailure(
            "provider returned a non-object JSON response",
            http_status=response.status_code,
        )
    return payload


def _api_error(payload: Mapping[str, object], default: str) -> str:
    error = payload.get("error")
    if isinstance(error, str) and error:
        return _safe_error(error)
    detail = payload.get("detail")
    if isinstance(detail, str) and detail:
        return _safe_error(detail)
    if isinstance(detail, dict):
        nested = detail.get("error")
        if isinstance(nested, str) and nested:
            return _safe_error(nested)
    failed = payload.get("failed_results")
    if isinstance(failed, list) and failed:
        return _safe_error(str(failed[0]))
    return default


def _firecrawl_credit_estimate(metadata: Mapping[str, object]) -> Decimal:
    explicit = _decimal(metadata.get("creditsUsed"))
    if explicit is not None:
        return explicit
    pages = _decimal(metadata.get("numPages"))
    proxy = (_string(metadata.get("proxyUsed")) or "").lower()
    base = max(Decimal(1), pages or Decimal(1))
    return max(base, Decimal(5)) if proxy == "enhanced" else base


def _retry_after_seconds(response: httpx.Response, retry: int) -> float:
    value = response.headers.get("retry-after")
    if value:
        try:
            return min(15.0, max(0.5, float(value)))
        except ValueError:
            pass
    match = re.search(r"retry after\s+(\d+(?:\.\d+)?)s", response.text, re.IGNORECASE)
    if match:
        return min(60.0, max(0.5, float(match.group(1))))
    return float(2**retry)


def _first_string(mapping: Mapping[str, object], *keys: str) -> str | None:
    for key in keys:
        value = _string(mapping.get(key))
        if value:
            return value
    return None


def _string(value: object) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _usd_per_credit(path_name: str) -> Decimal:
    return _extraction_path(path_name).usd_per_credit


def _extraction_path(path_name: str) -> ExtractionPath:
    try:
        return next(path for path in EXTRACTION_PATHS if path.name == path_name)
    except StopIteration as error:
        raise ValueError(f"unknown extraction path: {path_name}") from error


def _provider_request_weight(
    path_name: str,
    observation: ExtractionAttempt,
) -> Decimal:
    path = _extraction_path(path_name)
    if observation.error is not None or observation.payload is None:
        return Decimal(0)
    if path.credit_schedule == "firecrawl":
        return max(Decimal(1), observation.payload.provider_credits)
    if path.credit_schedule == "tavily":
        return Decimal(1)
    return Decimal(0)


def _cost_allocation_basis(path_name: str) -> str:
    schedule = _extraction_path(path_name).credit_schedule
    if schedule == "firecrawl":
        return "weighted attribution by response-level credit estimates"
    if schedule == "tavily":
        return "weighted attribution by successful provider responses"
    return "not applicable"


def _documented_credit_estimate(
    path_name: str,
    observations: Sequence[ExtractionAttempt],
) -> Decimal:
    path = _extraction_path(path_name)
    path_observations = [
        observation for observation in observations if observation.extraction_path == path_name
    ]
    if path.credit_schedule == "tavily":
        successful_provider_responses = sum(
            observation.error is None and observation.payload is not None
            for observation in path_observations
        )
        return Decimal(math.ceil(successful_provider_responses / 5) * 2)
    if path.credit_schedule == "firecrawl":
        return sum(
            (_provider_request_weight(path_name, observation) for observation in path_observations),
            Decimal(0),
        )
    return Decimal(0)


def _normalize_text(value: str) -> str:
    return " ".join(value.lower().split())


def _safe_error(error: object) -> str:
    value = _table_text(str(error))
    value = re.sub(r"https?://\S+", "<url>", value)
    value = re.sub(r",?\s*resets at .*$", "", value, flags=re.IGNORECASE)
    return value[:220]


def _table_text(value: str) -> str:
    return value.replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()


def _decimal_cell(value: Decimal | None) -> str:
    return f"{value:.2f}" if value is not None else "n/a"


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _utc_now() -> datetime:
    return datetime.now(UTC)


class _IntervalRateLimiter:
    def __init__(self, interval_seconds: float) -> None:
        self._interval_seconds = interval_seconds
        self._next_allowed = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            current = perf_counter()
            delay = max(0.0, self._next_allowed - current)
            if delay:
                await asyncio.sleep(delay)
            self._next_allowed = max(current, self._next_allowed) + self._interval_seconds
