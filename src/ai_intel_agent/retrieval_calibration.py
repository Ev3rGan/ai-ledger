from __future__ import annotations

import gc
import json
import math
import os
import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from importlib.resources import files
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Any, Literal, Protocol

MetricGroup = Literal["cross_language", "exact_entity", "evidence_span"]
RerankerProvider = Literal["fastembed", "none"]

CORPUS_RESOURCE = "retrieval_calibration_corpus.v1.json"
CANDIDATE_RESOURCE = "retrieval_candidates.v1.json"
DEFAULT_PROFILE_RESOURCE = "retrieval_profile.v1.json"
PROFILE_SERIES = "retrieval-profile-2026-08-13.v1"
SELECTION_RANKING_ORDER = (
    "worst_recall_desc",
    "mean_recall_desc",
    "evidence_span_recall_desc",
    "cross_language_recall_desc",
    "declared_model_size_gb_asc",
    "index_throughput_desc",
    "median_query_latency_ms_asc",
    "p95_query_latency_ms_asc",
    "peak_rss_mb_asc",
    "stable_candidate_id_asc",
)


class RetrievalCalibrationConfigurationError(ValueError):
    """Raised when versioned retrieval calibration input is invalid."""


@dataclass(frozen=True)
class EvidenceSpanFixture:
    identifier: str
    text: str
    start: int
    end: int


@dataclass(frozen=True)
class EntityFixture:
    canonical_name: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class RetrievalDocumentFixture:
    document_version_id: str
    document_type: str
    language: str
    text: str
    entities: tuple[EntityFixture, ...]
    evidence_spans: tuple[EvidenceSpanFixture, ...]


@dataclass(frozen=True)
class RetrievalQueryFixture:
    identifier: str
    text: str
    language: str
    metric_groups: tuple[MetricGroup, ...]
    expected_document_version_id: str
    expected_evidence_span_ids: tuple[str, ...]
    expected_entity: str | None


@dataclass(frozen=True)
class RetrievalCalibrationCorpus:
    schema_version: str
    version: str
    description: str
    documents: tuple[RetrievalDocumentFixture, ...]
    queries: tuple[RetrievalQueryFixture, ...]
    sha256: str


@dataclass(frozen=True)
class CalibrationRuntimeConfiguration:
    provider: str
    version: str
    execution_provider: str
    threads: int
    source_url: str


@dataclass(frozen=True)
class EmbeddingCandidate:
    identifier: str
    provider: str
    model_id: str
    dimensions: int
    pooling: str
    model_size_gb: float
    license: str
    source_url: str


@dataclass(frozen=True)
class RerankerCandidate:
    identifier: str
    provider: RerankerProvider
    model_id: str | None
    model_size_gb: float
    license: str
    source_url: str | None


@dataclass(frozen=True)
class ChunkWindow:
    max_characters: int
    overlap_characters: int


@dataclass(frozen=True)
class ChunkProfile:
    identifier: str
    strategy: str
    document_types: dict[str, ChunkWindow]


@dataclass(frozen=True)
class FusionProfile:
    identifier: str
    method: str
    rrf_k: int
    weights: dict[str, float]
    lexical_candidate_count: int
    semantic_candidate_count: int
    exact_entity_candidate_count: int
    fused_candidate_count: int
    rerank_depth: int
    final_top_k: int


@dataclass(frozen=True)
class RetrievalSelectionPolicy:
    cross_language_recall_at_k_minimum: float
    exact_entity_recall_at_k_minimum: float
    evidence_span_recall_at_k_minimum: float
    ranking_order: tuple[str, ...]


@dataclass(frozen=True)
class RetrievalCandidateConfiguration:
    schema_version: str
    version: str
    runtime: CalibrationRuntimeConfiguration
    embedding_candidates: tuple[EmbeddingCandidate, ...]
    reranker_candidates: tuple[RerankerCandidate, ...]
    chunk_profiles: tuple[ChunkProfile, ...]
    fusion_profiles: tuple[FusionProfile, ...]
    selection_policy: RetrievalSelectionPolicy
    sha256: str


@dataclass(frozen=True)
class RetrievalChunk:
    identifier: str
    document_version_id: str
    document_type: str
    language: str
    start: int
    end: int
    text: str
    evidence_span_ids: tuple[str, ...]


@dataclass(frozen=True)
class RetrievalMetrics:
    cross_language_recall_at_k: float
    exact_entity_recall_at_k: float
    evidence_span_recall_at_k: float

    @property
    def mean_recall(self) -> float:
        return (
            self.cross_language_recall_at_k
            + self.exact_entity_recall_at_k
            + self.evidence_span_recall_at_k
        ) / 3

    @property
    def worst_recall(self) -> float:
        return min(
            self.cross_language_recall_at_k,
            self.exact_entity_recall_at_k,
            self.evidence_span_recall_at_k,
        )


@dataclass(frozen=True)
class CandidateMeasurement:
    identifier: str
    embedding_candidate_id: str
    reranker_candidate_id: str
    chunk_profile_id: str
    fusion_profile_id: str
    metrics: RetrievalMetrics
    passed_selection_gates: bool
    declared_model_size_gb: float
    index_throughput_chunks_per_second: float
    median_query_latency_ms: float
    p95_query_latency_ms: float
    peak_rss_mb: float


@dataclass(frozen=True)
class ModelResourceObservation:
    role: str
    candidate_id: str
    model_id: str
    declared_model_size_gb: float
    load_latency_ms: float
    rss_after_load_mb: float


@dataclass(frozen=True)
class ProfileEmbedding:
    candidate_id: str
    provider: str
    model_id: str
    dimensions: int
    pooling: str
    source_url: str


@dataclass(frozen=True)
class ProfileReranker:
    candidate_id: str
    provider: str
    model_id: str | None
    source_url: str | None


@dataclass(frozen=True)
class ProfileChunking:
    profile_id: str
    strategy: str
    document_types: dict[str, ChunkWindow]


@dataclass(frozen=True)
class ProfileRetrieval:
    lexical_candidate_count: int
    semantic_candidate_count: int
    exact_entity_candidate_count: int
    fused_candidate_count: int
    rerank_depth: int
    final_top_k: int


@dataclass(frozen=True)
class ProfileFusion:
    profile_id: str
    method: str
    rrf_k: int
    weights: dict[str, float]


@dataclass(frozen=True)
class ProfileCalibration:
    generated_at: datetime
    corpus_version: str
    corpus_sha256: str
    candidate_configuration_version: str
    candidate_configuration_sha256: str
    runtime: str
    logical_cpu_count: int
    configured_threads: int
    metrics: RetrievalMetrics
    declared_model_size_gb: float
    index_throughput_chunks_per_second: float
    median_query_latency_ms: float
    p95_query_latency_ms: float
    peak_rss_mb: float


@dataclass(frozen=True)
class RetrievalProfile:
    schema_version: str
    profile_id: str
    embedding: ProfileEmbedding
    reranker: ProfileReranker
    chunking: ProfileChunking
    retrieval: ProfileRetrieval
    fusion: ProfileFusion
    selection_policy: RetrievalSelectionPolicy
    calibration: ProfileCalibration


@dataclass(frozen=True)
class RetrievalCalibration:
    corpus: RetrievalCalibrationCorpus
    configuration: RetrievalCandidateConfiguration
    runtime_name: str
    logical_cpu_count: int
    measurements: tuple[CandidateMeasurement, ...]
    resource_observations: tuple[ModelResourceObservation, ...]
    selected: CandidateMeasurement
    profile: RetrievalProfile


class EmbeddingModel(Protocol):
    model_id: str
    dimensions: int

    def embed_passages(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]: ...

    def embed_queries(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]: ...

    def close(self) -> None: ...


class RerankerModel(Protocol):
    def rerank(self, query: str, documents: Sequence[str]) -> tuple[float, ...]: ...

    def close(self) -> None: ...


class CalibrationRuntime(Protocol):
    name: str
    threads: int
    logical_cpu_count: int

    def create_embedding(self, candidate: EmbeddingCandidate) -> EmbeddingModel: ...

    def create_reranker(self, candidate: RerankerCandidate) -> RerankerModel: ...

    def rss_bytes(self) -> int: ...


class _FastEmbedEmbeddingModel:
    def __init__(self, model: Any, candidate: EmbeddingCandidate) -> None:
        self._model = model
        self.model_id = candidate.model_id
        self.dimensions = candidate.dimensions

    def embed_passages(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        return tuple(
            tuple(float(value) for value in vector)
            for vector in self._model.passage_embed(texts, batch_size=32)
        )

    def embed_queries(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        return tuple(
            tuple(float(value) for value in vector)
            for vector in self._model.query_embed(texts, batch_size=16)
        )

    def close(self) -> None:
        return None


class _FastEmbedRerankerModel:
    def __init__(self, model: Any) -> None:
        self._model = model

    def rerank(self, query: str, documents: Sequence[str]) -> tuple[float, ...]:
        return tuple(float(value) for value in self._model.rerank(query, documents, batch_size=8))

    def close(self) -> None:
        return None


class FastEmbedCalibrationRuntime:
    """CPU-only boundary for optional ONNX Embedding and Reranker candidates."""

    def __init__(self, *, threads: int) -> None:
        try:
            import psutil
            from fastembed import TextEmbedding  # noqa: F401
            from fastembed.rerank.cross_encoder import TextCrossEncoder  # noqa: F401
        except ImportError as error:
            raise RetrievalCalibrationConfigurationError(
                "Retrieval calibration dependencies are missing; run uv sync --extra retrieval"
            ) from error
        try:
            runtime_version = package_version("fastembed")
        except PackageNotFoundError as error:
            raise RetrievalCalibrationConfigurationError(
                "FastEmbed package metadata is unavailable"
            ) from error
        if not runtime_version.startswith("0.8."):
            raise RetrievalCalibrationConfigurationError(
                f"Expected FastEmbed 0.8.x, found {runtime_version}"
            )
        self.name = f"fastembed-{runtime_version}-onnx-cpu"
        self.threads = threads
        self.logical_cpu_count = psutil.cpu_count(logical=True) or os.cpu_count() or 1
        self._process = psutil.Process()

    def create_embedding(self, candidate: EmbeddingCandidate) -> EmbeddingModel:
        from fastembed import TextEmbedding

        model = TextEmbedding(
            model_name=candidate.model_id,
            threads=self.threads,
            providers=["CPUExecutionProvider"],
            cuda=False,
        )
        return _FastEmbedEmbeddingModel(model, candidate)

    def create_reranker(self, candidate: RerankerCandidate) -> RerankerModel:
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        if candidate.model_id is None:
            raise RetrievalCalibrationConfigurationError("Cannot load the no-Reranker control")
        model = TextCrossEncoder(
            model_name=candidate.model_id,
            threads=self.threads,
            providers=["CPUExecutionProvider"],
            cuda=False,
        )
        return _FastEmbedRerankerModel(model)

    def rss_bytes(self) -> int:
        return int(self._process.memory_info().rss)


def load_retrieval_corpus() -> RetrievalCalibrationCorpus:
    raw, digest = _load_json_resource(CORPUS_RESOURCE)
    if _required_text(raw, "schema_version") != "retrieval-calibration-corpus.v1":
        raise RetrievalCalibrationConfigurationError("Unsupported retrieval corpus schema")

    documents_raw = _required_list(raw, "documents")
    documents = tuple(_parse_document(item) for item in documents_raw)
    queries = tuple(_parse_query(item) for item in _required_list(raw, "queries"))
    corpus = RetrievalCalibrationCorpus(
        schema_version=_required_text(raw, "schema_version"),
        version=_required_text(raw, "corpus_version"),
        description=_required_text(raw, "description"),
        documents=documents,
        queries=queries,
        sha256=digest,
    )
    _validate_corpus(corpus)
    return corpus


def load_retrieval_candidate_configuration() -> RetrievalCandidateConfiguration:
    raw, digest = _load_json_resource(CANDIDATE_RESOURCE)
    if _required_text(raw, "schema_version") != "retrieval-candidates.v1":
        raise RetrievalCalibrationConfigurationError("Unsupported retrieval candidate schema")

    runtime_raw = _required_dict(raw, "runtime")
    runtime = CalibrationRuntimeConfiguration(
        provider=_required_text(runtime_raw, "provider"),
        version=_required_text(runtime_raw, "version"),
        execution_provider=_required_text(runtime_raw, "execution_provider"),
        threads=_required_int(runtime_raw, "threads"),
        source_url=_required_https_url(runtime_raw, "source_url"),
    )
    embeddings = tuple(
        EmbeddingCandidate(
            identifier=_required_text(item, "candidate_id"),
            provider=_required_text(item, "provider"),
            model_id=_required_text(item, "model_id"),
            dimensions=_required_int(item, "dimensions"),
            pooling=_required_text(item, "pooling"),
            model_size_gb=_required_float(item, "model_size_gb"),
            license=_required_text(item, "license"),
            source_url=_required_https_url(item, "source_url"),
        )
        for item in _required_list(raw, "embedding_candidates")
    )
    rerankers = tuple(_parse_reranker(item) for item in _required_list(raw, "reranker_candidates"))
    chunk_profiles = tuple(
        _parse_chunk_profile(item) for item in _required_list(raw, "chunk_profiles")
    )
    fusion_profiles = tuple(
        _parse_fusion_profile(item) for item in _required_list(raw, "fusion_profiles")
    )
    selection_raw = _required_dict(raw, "selection_policy")
    configuration = RetrievalCandidateConfiguration(
        schema_version=_required_text(raw, "schema_version"),
        version=_required_text(raw, "candidate_configuration_version"),
        runtime=runtime,
        embedding_candidates=embeddings,
        reranker_candidates=rerankers,
        chunk_profiles=chunk_profiles,
        fusion_profiles=fusion_profiles,
        selection_policy=RetrievalSelectionPolicy(
            cross_language_recall_at_k_minimum=_required_float(
                selection_raw, "cross_language_recall_at_k_minimum"
            ),
            exact_entity_recall_at_k_minimum=_required_float(
                selection_raw, "exact_entity_recall_at_k_minimum"
            ),
            evidence_span_recall_at_k_minimum=_required_float(
                selection_raw, "evidence_span_recall_at_k_minimum"
            ),
            ranking_order=tuple(_required_text_list(selection_raw, "ranking_order")),
        ),
        sha256=digest,
    )
    _validate_candidate_configuration(configuration)
    return configuration


def run_retrieval_calibration(
    report_output: Path,
    profile_output: Path,
    *,
    runtime: CalibrationRuntime,
    corpus: RetrievalCalibrationCorpus | None = None,
    configuration: RetrievalCandidateConfiguration | None = None,
    now: datetime | None = None,
    progress: Callable[[int, int, str], None] | None = None,
) -> RetrievalCalibration:
    """Evaluate the fixed corpus and export a report plus one loadable Retrieval Profile."""

    corpus = corpus or load_retrieval_corpus()
    configuration = configuration or load_retrieval_candidate_configuration()
    if runtime.threads != configuration.runtime.threads:
        raise RetrievalCalibrationConfigurationError(
            "Calibration runtime thread count does not match the versioned configuration"
        )
    chunk_sets = {
        profile.identifier: build_retrieval_chunks(corpus.documents, profile)
        for profile in configuration.chunk_profiles
    }
    reranker_models: dict[str, RerankerModel] = {}
    resources: list[ModelResourceObservation] = []
    peak_rss_bytes = runtime.rss_bytes()
    try:
        for candidate in configuration.reranker_candidates:
            if candidate.provider == "none":
                continue
            started = perf_counter()
            model = runtime.create_reranker(candidate)
            load_latency_ms = (perf_counter() - started) * 1000
            reranker_models[candidate.identifier] = model
            peak_rss_bytes = max(peak_rss_bytes, runtime.rss_bytes())
            resources.append(
                ModelResourceObservation(
                    role="Reranker",
                    candidate_id=candidate.identifier,
                    model_id=candidate.model_id or "none",
                    declared_model_size_gb=candidate.model_size_gb,
                    load_latency_ms=load_latency_ms,
                    rss_after_load_mb=runtime.rss_bytes() / (1024 * 1024),
                )
            )

        measurements: list[CandidateMeasurement] = []
        total = (
            len(configuration.embedding_candidates)
            * len(configuration.chunk_profiles)
            * len(configuration.fusion_profiles)
            * len(configuration.reranker_candidates)
        )
        completed = 0
        for embedding_candidate in configuration.embedding_candidates:
            started = perf_counter()
            embedding_model = runtime.create_embedding(embedding_candidate)
            load_latency_ms = (perf_counter() - started) * 1000
            peak_rss_bytes = max(peak_rss_bytes, runtime.rss_bytes())
            resources.append(
                ModelResourceObservation(
                    role="Embedding",
                    candidate_id=embedding_candidate.identifier,
                    model_id=embedding_candidate.model_id,
                    declared_model_size_gb=embedding_candidate.model_size_gb,
                    load_latency_ms=load_latency_ms,
                    rss_after_load_mb=runtime.rss_bytes() / (1024 * 1024),
                )
            )
            try:
                query_vectors, query_embedding_latencies = _embed_queries(
                    embedding_model,
                    corpus.queries,
                    embedding_candidate,
                )
                for chunk_profile in configuration.chunk_profiles:
                    chunks = chunk_sets[chunk_profile.identifier]
                    index_started = perf_counter()
                    chunk_vectors = embedding_model.embed_passages(
                        tuple(chunk.text for chunk in chunks)
                    )
                    index_seconds = max(perf_counter() - index_started, 1e-9)
                    _validate_vectors(
                        chunk_vectors,
                        expected_count=len(chunks),
                        expected_dimensions=embedding_candidate.dimensions,
                        label=embedding_candidate.identifier,
                    )
                    throughput = len(chunks) / index_seconds
                    peak_rss_bytes = max(peak_rss_bytes, runtime.rss_bytes())
                    for fusion_profile in configuration.fusion_profiles:
                        base_results, base_latencies = _retrieve_without_reranking(
                            corpus,
                            chunks,
                            chunk_vectors,
                            query_vectors,
                            query_embedding_latencies,
                            fusion_profile,
                        )
                        for reranker_candidate in configuration.reranker_candidates:
                            results = base_results
                            latencies = base_latencies
                            if reranker_candidate.provider != "none":
                                results, latencies = _rerank_results(
                                    corpus.queries,
                                    chunks,
                                    base_results,
                                    base_latencies,
                                    fusion_profile,
                                    reranker_models[reranker_candidate.identifier],
                                )
                                peak_rss_bytes = max(peak_rss_bytes, runtime.rss_bytes())
                            metrics = _measure_recall(
                                corpus.queries,
                                chunks,
                                results,
                                final_top_k=fusion_profile.final_top_k,
                            )
                            candidate_id = _candidate_measurement_id(
                                embedding_candidate,
                                reranker_candidate,
                                chunk_profile,
                                fusion_profile,
                            )
                            measurement = CandidateMeasurement(
                                identifier=candidate_id,
                                embedding_candidate_id=embedding_candidate.identifier,
                                reranker_candidate_id=reranker_candidate.identifier,
                                chunk_profile_id=chunk_profile.identifier,
                                fusion_profile_id=fusion_profile.identifier,
                                metrics=metrics,
                                passed_selection_gates=_passes_selection_gates(
                                    metrics, configuration.selection_policy
                                ),
                                declared_model_size_gb=(
                                    embedding_candidate.model_size_gb
                                    + reranker_candidate.model_size_gb
                                ),
                                index_throughput_chunks_per_second=throughput,
                                median_query_latency_ms=median(latencies),
                                p95_query_latency_ms=_percentile(latencies, 0.95),
                                peak_rss_mb=peak_rss_bytes / (1024 * 1024),
                            )
                            measurements.append(measurement)
                            completed += 1
                            if progress is not None:
                                progress(completed, total, candidate_id)
            finally:
                embedding_model.close()
                del embedding_model
                gc.collect()
    finally:
        for model in reranker_models.values():
            model.close()
        reranker_models.clear()
        gc.collect()

    selected = select_retrieval_profile_candidate(tuple(measurements))
    generated_at = now or datetime.now(UTC)
    if generated_at.tzinfo is None:
        raise RetrievalCalibrationConfigurationError("Calibration timestamp must be timezone-aware")
    profile = _build_retrieval_profile(
        selected,
        corpus=corpus,
        configuration=configuration,
        runtime=runtime,
        generated_at=generated_at,
    )
    calibration = RetrievalCalibration(
        corpus=corpus,
        configuration=configuration,
        runtime_name=runtime.name,
        logical_cpu_count=runtime.logical_cpu_count,
        measurements=tuple(measurements),
        resource_observations=tuple(resources),
        selected=selected,
        profile=profile,
    )
    report_output.parent.mkdir(parents=True, exist_ok=True)
    profile_output.parent.mkdir(parents=True, exist_ok=True)
    report_output.write_text(render_retrieval_calibration_report(calibration), encoding="utf-8")
    profile_output.write_text(
        json.dumps(_profile_to_dict(profile), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return calibration


def load_retrieval_profile(path: Path | None = None) -> RetrievalProfile:
    if path is None:
        try:
            raw_bytes = files("ai_intel_agent").joinpath("data", DEFAULT_PROFILE_RESOURCE).read_bytes()
        except FileNotFoundError as error:
            raise RetrievalCalibrationConfigurationError(
                "The packaged Retrieval Profile has not been generated"
            ) from error
    else:
        try:
            raw_bytes = path.read_bytes()
        except OSError as error:
            raise RetrievalCalibrationConfigurationError(
                f"Cannot read Retrieval Profile: {path}"
            ) from error
    try:
        payload = json.loads(raw_bytes)
    except json.JSONDecodeError as error:
        raise RetrievalCalibrationConfigurationError("Retrieval Profile is not valid JSON") from error
    raw = _as_dict(payload, "Retrieval Profile")
    if _required_text(raw, "schema_version") != "retrieval-profile.v1":
        raise RetrievalCalibrationConfigurationError("Unsupported Retrieval Profile schema")

    embedding_raw = _required_dict(raw, "embedding")
    reranker_raw = _required_dict(raw, "reranker")
    chunking_raw = _required_dict(raw, "chunking")
    retrieval_raw = _required_dict(raw, "retrieval")
    fusion_raw = _required_dict(raw, "fusion")
    selection_raw = _required_dict(raw, "selection_policy")
    calibration_raw = _required_dict(raw, "calibration")
    metrics_raw = _required_dict(calibration_raw, "metrics")
    model_id = reranker_raw.get("model_id")
    if model_id is not None and not isinstance(model_id, str):
        raise RetrievalCalibrationConfigurationError("Reranker model_id must be text or null")
    reranker_source_url = reranker_raw.get("source_url")
    if reranker_source_url is not None and (
        not isinstance(reranker_source_url, str)
        or not reranker_source_url.startswith("https://")
    ):
        raise RetrievalCalibrationConfigurationError(
            "Reranker source_url must be an HTTPS URL or null"
        )
    profile = RetrievalProfile(
        schema_version=_required_text(raw, "schema_version"),
        profile_id=_required_text(raw, "profile_id"),
        embedding=ProfileEmbedding(
            candidate_id=_required_text(embedding_raw, "candidate_id"),
            provider=_required_text(embedding_raw, "provider"),
            model_id=_required_text(embedding_raw, "model_id"),
            dimensions=_required_int(embedding_raw, "dimensions"),
            pooling=_required_text(embedding_raw, "pooling"),
            source_url=_required_https_url(embedding_raw, "source_url"),
        ),
        reranker=ProfileReranker(
            candidate_id=_required_text(reranker_raw, "candidate_id"),
            provider=_required_text(reranker_raw, "provider"),
            model_id=model_id,
            source_url=reranker_source_url,
        ),
        chunking=ProfileChunking(
            profile_id=_required_text(chunking_raw, "profile_id"),
            strategy=_required_text(chunking_raw, "strategy"),
            document_types=_parse_profile_chunk_windows(chunking_raw),
        ),
        retrieval=ProfileRetrieval(
            lexical_candidate_count=_required_int(retrieval_raw, "lexical_candidate_count"),
            semantic_candidate_count=_required_int(retrieval_raw, "semantic_candidate_count"),
            exact_entity_candidate_count=_required_int(
                retrieval_raw, "exact_entity_candidate_count"
            ),
            fused_candidate_count=_required_int(retrieval_raw, "fused_candidate_count"),
            rerank_depth=_required_int(retrieval_raw, "rerank_depth"),
            final_top_k=_required_int(retrieval_raw, "final_top_k"),
        ),
        fusion=ProfileFusion(
            profile_id=_required_text(fusion_raw, "profile_id"),
            method=_required_text(fusion_raw, "method"),
            rrf_k=_required_int(fusion_raw, "rrf_k"),
            weights={
                name: _number(value, f"fusion weight {name}")
                for name, value in _required_dict(fusion_raw, "weights").items()
            },
        ),
        selection_policy=RetrievalSelectionPolicy(
            cross_language_recall_at_k_minimum=_required_float(
                selection_raw, "cross_language_recall_at_k_minimum"
            ),
            exact_entity_recall_at_k_minimum=_required_float(
                selection_raw, "exact_entity_recall_at_k_minimum"
            ),
            evidence_span_recall_at_k_minimum=_required_float(
                selection_raw, "evidence_span_recall_at_k_minimum"
            ),
            ranking_order=tuple(_required_text_list(selection_raw, "ranking_order")),
        ),
        calibration=ProfileCalibration(
            generated_at=_parse_datetime(_required_text(calibration_raw, "generated_at")),
            corpus_version=_required_text(calibration_raw, "corpus_version"),
            corpus_sha256=_required_sha256(calibration_raw, "corpus_sha256"),
            candidate_configuration_version=_required_text(
                calibration_raw, "candidate_configuration_version"
            ),
            candidate_configuration_sha256=_required_sha256(
                calibration_raw, "candidate_configuration_sha256"
            ),
            runtime=_required_text(calibration_raw, "runtime"),
            logical_cpu_count=_required_int(calibration_raw, "logical_cpu_count"),
            configured_threads=_required_int(calibration_raw, "configured_threads"),
            metrics=RetrievalMetrics(
                cross_language_recall_at_k=_required_float(
                    metrics_raw, "cross_language_recall_at_k"
                ),
                exact_entity_recall_at_k=_required_float(
                    metrics_raw, "exact_entity_recall_at_k"
                ),
                evidence_span_recall_at_k=_required_float(
                    metrics_raw, "evidence_span_recall_at_k"
                ),
            ),
            declared_model_size_gb=_required_float(
                calibration_raw, "declared_model_size_gb"
            ),
            index_throughput_chunks_per_second=_required_float(
                calibration_raw, "index_throughput_chunks_per_second"
            ),
            median_query_latency_ms=_required_float(
                calibration_raw, "median_query_latency_ms"
            ),
            p95_query_latency_ms=_required_float(calibration_raw, "p95_query_latency_ms"),
            peak_rss_mb=_required_float(calibration_raw, "peak_rss_mb"),
        ),
    )
    _validate_loaded_profile(profile)
    return profile


def build_retrieval_chunks(
    documents: Sequence[RetrievalDocumentFixture],
    profile: ChunkProfile,
) -> tuple[RetrievalChunk, ...]:
    chunks: list[RetrievalChunk] = []
    for document in documents:
        try:
            window = profile.document_types[document.document_type]
        except KeyError as error:
            raise RetrievalCalibrationConfigurationError(
                f"Chunk profile {profile.identifier} has no policy for {document.document_type}"
            ) from error
        start = 0
        while start < len(document.text):
            end = min(start + window.max_characters, len(document.text))
            if end < len(document.text):
                end = _prefer_text_boundary(document.text, start, end)
            chunk_text = document.text[start:end]
            if chunk_text:
                chunks.append(
                    RetrievalChunk(
                        identifier=f"{document.document_version_id}:{start}:{end}",
                        document_version_id=document.document_version_id,
                        document_type=document.document_type,
                        language=document.language,
                        start=start,
                        end=end,
                        text=chunk_text,
                        evidence_span_ids=tuple(
                            span.identifier
                            for span in document.evidence_spans
                            if start <= span.start and span.end <= end
                        ),
                    )
                )
            if end >= len(document.text):
                break
            next_start = max(end - window.overlap_characters, start + 1)
            while next_start < len(document.text) and document.text[next_start].isspace():
                next_start += 1
            start = next_start
    return tuple(chunks)


def select_retrieval_profile_candidate(
    measurements: tuple[CandidateMeasurement, ...],
) -> CandidateMeasurement:
    eligible = [item for item in measurements if item.passed_selection_gates]
    if not eligible:
        if not measurements:
            raise RetrievalCalibrationConfigurationError(
                "No Retrieval Profile candidates were measured"
            )
        best = max(measurements, key=lambda item: item.metrics.mean_recall)
        raise RetrievalCalibrationConfigurationError(
            "No Retrieval Profile candidate passed every recall gate; "
            f"best={best.identifier} "
            f"cross-language={best.metrics.cross_language_recall_at_k:.3f} "
            f"exact-entity={best.metrics.exact_entity_recall_at_k:.3f} "
            f"evidence-span={best.metrics.evidence_span_recall_at_k:.3f}"
        )
    ordered = sorted(eligible, key=lambda item: item.identifier)
    return max(
        ordered,
        key=lambda item: (
            item.metrics.worst_recall,
            item.metrics.mean_recall,
            item.metrics.evidence_span_recall_at_k,
            item.metrics.cross_language_recall_at_k,
            -item.declared_model_size_gb,
            item.index_throughput_chunks_per_second,
            -item.median_query_latency_ms,
            -item.p95_query_latency_ms,
            -item.peak_rss_mb,
        ),
    )


def render_retrieval_calibration_report(calibration: RetrievalCalibration) -> str:
    selected = calibration.selected
    profile = calibration.profile
    lines = [
        "# Multilingual Retrieval Profile calibration",
        "",
        "- Parent Spec: #1",
        "- Implementation ticket: #6",
        f"- Corpus version: `{calibration.corpus.version}`",
        f"- Corpus SHA-256: `{calibration.corpus.sha256}`",
        f"- Candidate configuration: `{calibration.configuration.version}`",
        f"- Candidate configuration SHA-256: `{calibration.configuration.sha256}`",
        f"- Runtime: `{calibration.runtime_name}`",
        f"- Generated at: `{profile.calibration.generated_at.isoformat()}`",
        f"- Candidate combinations: {len(calibration.measurements)}",
        "",
        "## Selected Retrieval Profile",
        "",
        f"- Profile ID: `{profile.profile_id}`",
        (
            f"- Embedding: `{profile.embedding.model_id}` ({profile.embedding.dimensions} "
            f"dimensions, {profile.embedding.pooling} pooling)"
        ),
        f"- Reranker: `{profile.reranker.model_id or 'none'}`",
        f"- Chunk profile: `{profile.chunking.profile_id}`",
        f"- Fusion profile: `{profile.fusion.profile_id}`",
        (
            f"- Cross-language retrieval Recall@{profile.retrieval.final_top_k}: "
            f"{selected.metrics.cross_language_recall_at_k:.1%}"
        ),
        (
            f"- Exact technical-Entity retrieval Recall@{profile.retrieval.final_top_k}: "
            f"{selected.metrics.exact_entity_recall_at_k:.1%}"
        ),
        (
        f"- Evidence Span Recall@{profile.retrieval.final_top_k}: "
            f"{selected.metrics.evidence_span_recall_at_k:.1%}"
        ),
        f"- Declared model size: {selected.declared_model_size_gb:.2f} GiB",
        f"- Index throughput: {selected.index_throughput_chunks_per_second:.1f} Chunks/s",
        f"- Median query latency: {selected.median_query_latency_ms:.2f} ms",
        f"- P95 query latency: {selected.p95_query_latency_ms:.2f} ms",
        f"- Peak process RSS: {selected.peak_rss_mb:.1f} MiB",
        "",
        (
            "Selection is fail-closed: each recall threshold must pass before worst-category and "
            "mean recall are compared. Quality ties prefer smaller declared model size, higher "
            "index throughput, lower median and P95 query latency, lower RSS, then stable ID."
        ),
        "",
        "## Candidate results",
        "",
        (
            "| Candidate | Cross-language | Exact Entity | Evidence Span | Gates | Model GiB | "
            "Chunks/s | P50 ms | P95 ms | RSS MiB |"
        ),
        "| --- | ---: | ---: | ---: | :---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in calibration.measurements:
        lines.append(
            f"| `{item.identifier}` | {item.metrics.cross_language_recall_at_k:.1%} | "
            f"{item.metrics.exact_entity_recall_at_k:.1%} | "
            f"{item.metrics.evidence_span_recall_at_k:.1%} | "
            f"{'PASS' if item.passed_selection_gates else 'FAIL'} | "
            f"{item.declared_model_size_gb:.2f} | "
            f"{item.index_throughput_chunks_per_second:.1f} | "
            f"{item.median_query_latency_ms:.2f} | {item.p95_query_latency_ms:.2f} | "
            f"{item.peak_rss_mb:.1f} |"
        )
    lines.extend(
        [
            "",
            "## CPU resources",
            "",
            f"- Logical CPU count: {calibration.logical_cpu_count}",
            f"- Configured ONNX threads: {calibration.configuration.runtime.threads}",
            "",
            "| Role | Candidate | Model | Declared size GiB | Load ms | RSS after load MiB |",
            "| --- | --- | --- | ---: | ---: | ---: |",
        ]
    )
    for item in calibration.resource_observations:
        lines.append(
            f"| {item.role} | `{item.candidate_id}` | `{item.model_id}` | "
            f"{item.declared_model_size_gb:.2f} | {item.load_latency_ms:.2f} | "
            f"{item.rss_after_load_mb:.1f} |"
        )
    lines.extend(
        [
            "",
            (
                "RSS values are calibration-process working sets. ONNX allocations can be retained "
                "between candidate phases, so declared model size and an isolated deployment "
                "measurement remain necessary for capacity planning."
            ),
            "",
            "## Versioned candidates",
            "",
            (
                f"- Runtime: [{calibration.configuration.runtime.provider} "
                f"{calibration.configuration.runtime.version}]"
                f"({calibration.configuration.runtime.source_url}) with "
                f"`{calibration.configuration.runtime.execution_provider}`."
            ),
            "",
            "| Role | Candidate | Model | Pooling | License | Source |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for candidate in calibration.configuration.embedding_candidates:
        lines.append(
            f"| Embedding | `{candidate.identifier}` | `{candidate.model_id}` | "
            f"{candidate.pooling} | {candidate.license} | "
            f"[model card]({candidate.source_url}) |"
        )
    for candidate in calibration.configuration.reranker_candidates:
        source = f"[model card]({candidate.source_url})" if candidate.source_url else "n/a"
        lines.append(
            f"| Reranker | `{candidate.identifier}` | `{candidate.model_id or 'none'}` | "
            f"n/a | {candidate.license} | {source} |"
        )
    lines.extend(
        [
            "",
            "## Scope and interpretation",
            "",
            "- The fixed corpus is synthetic and project-specific; these results are not a general model leaderboard.",
            (
                "- Chunks remain rebuildable retrieval artifacts and are never treated as Evidence. "
                "Evidence Span recall requires a retrieved Chunk to contain the exact anchored span."
            ),
            (
                "- The command does not connect to the application database, Browse, or Research "
                "behavior and does not introduce a vector database."
            ),
            (
                "- Chunk sizes, candidate counts, fusion weights, reranking depth, and thresholds "
                "are temporary, versioned calibration outputs that can be replaced by a later run."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def _embed_queries(
    model: EmbeddingModel,
    queries: Sequence[RetrievalQueryFixture],
    candidate: EmbeddingCandidate,
) -> tuple[tuple[tuple[float, ...], ...], tuple[float, ...]]:
    vectors: list[tuple[float, ...]] = []
    latencies: list[float] = []
    for query in queries:
        started = perf_counter()
        result = model.embed_queries((query.text,))
        latencies.append((perf_counter() - started) * 1000)
        _validate_vectors(
            result,
            expected_count=1,
            expected_dimensions=candidate.dimensions,
            label=candidate.identifier,
        )
        vectors.append(result[0])
    return tuple(vectors), tuple(latencies)


def _retrieve_without_reranking(
    corpus: RetrievalCalibrationCorpus,
    chunks: tuple[RetrievalChunk, ...],
    chunk_vectors: tuple[tuple[float, ...], ...],
    query_vectors: tuple[tuple[float, ...], ...],
    query_embedding_latencies: tuple[float, ...],
    fusion: FusionProfile,
) -> tuple[tuple[tuple[int, ...], ...], tuple[float, ...]]:
    results: list[tuple[int, ...]] = []
    latencies: list[float] = []
    for query, query_vector, embedding_latency in zip(
        corpus.queries,
        query_vectors,
        query_embedding_latencies,
        strict=True,
    ):
        started = perf_counter()
        lexical = _lexical_ranking(query.text, chunks)[: fusion.lexical_candidate_count]
        semantic = _semantic_ranking(query_vector, chunk_vectors)[: fusion.semantic_candidate_count]
        exact_entity = _exact_entity_ranking(query.text, chunks, corpus.documents)[
            : fusion.exact_entity_candidate_count
        ]
        fused = _weighted_reciprocal_rank_fusion(
            {
                "lexical": lexical,
                "semantic": semantic,
                "exact_entity": exact_entity,
            },
            fusion,
        )[: fusion.fused_candidate_count]
        latencies.append(embedding_latency + (perf_counter() - started) * 1000)
        results.append(fused)
    return tuple(results), tuple(latencies)


def _rerank_results(
    queries: Sequence[RetrievalQueryFixture],
    chunks: tuple[RetrievalChunk, ...],
    base_results: tuple[tuple[int, ...], ...],
    base_latencies: tuple[float, ...],
    fusion: FusionProfile,
    model: RerankerModel,
) -> tuple[tuple[tuple[int, ...], ...], tuple[float, ...]]:
    results: list[tuple[int, ...]] = []
    latencies: list[float] = []
    for query, base, base_latency in zip(queries, base_results, base_latencies, strict=True):
        candidate_indexes = base[: fusion.rerank_depth]
        candidate_texts = tuple(chunks[index].text for index in candidate_indexes)
        started = perf_counter()
        scores = model.rerank(query.text, candidate_texts)
        rerank_latency = (perf_counter() - started) * 1000
        if len(scores) != len(candidate_indexes) or any(not math.isfinite(score) for score in scores):
            raise RetrievalCalibrationConfigurationError(
                "Reranker returned an invalid number of finite scores"
            )
        prior_rank = {chunk_index: rank for rank, chunk_index in enumerate(candidate_indexes)}
        ordered = tuple(
            sorted(
                candidate_indexes,
                key=lambda chunk_index: (
                    -scores[prior_rank[chunk_index]],
                    prior_rank[chunk_index],
                    chunks[chunk_index].identifier,
                ),
            )
        )
        results.append(ordered)
        latencies.append(base_latency + rerank_latency)
    return tuple(results), tuple(latencies)


def _measure_recall(
    queries: Sequence[RetrievalQueryFixture],
    chunks: tuple[RetrievalChunk, ...],
    results: tuple[tuple[int, ...], ...],
    *,
    final_top_k: int,
) -> RetrievalMetrics:
    group_hits: dict[str, list[bool]] = {
        "cross_language": [],
        "exact_entity": [],
        "evidence_span": [],
    }
    for query, result in zip(queries, results, strict=True):
        retrieved = tuple(chunks[index] for index in result[:final_top_k])
        document_hit = any(
            chunk.document_version_id == query.expected_document_version_id
            for chunk in retrieved
        )
        if "cross_language" in query.metric_groups:
            group_hits["cross_language"].append(document_hit)
        if "exact_entity" in query.metric_groups:
            group_hits["exact_entity"].append(document_hit)
        if "evidence_span" in query.metric_groups:
            expected = set(query.expected_evidence_span_ids)
            group_hits["evidence_span"].append(
                any(expected.intersection(chunk.evidence_span_ids) for chunk in retrieved)
            )
    if any(not values for values in group_hits.values()):
        raise RetrievalCalibrationConfigurationError("Every retrieval metric needs corpus cases")
    return RetrievalMetrics(
        cross_language_recall_at_k=_recall(group_hits["cross_language"]),
        exact_entity_recall_at_k=_recall(group_hits["exact_entity"]),
        evidence_span_recall_at_k=_recall(group_hits["evidence_span"]),
    )


def _lexical_ranking(query: str, chunks: Sequence[RetrievalChunk]) -> tuple[int, ...]:
    query_terms = _retrieval_terms(query)
    chunk_terms = tuple(_retrieval_terms(chunk.text) for chunk in chunks)
    if not query_terms:
        return ()
    document_frequency = Counter(
        term for terms in chunk_terms for term in set(terms) if term in set(query_terms)
    )
    average_length = sum(len(terms) for terms in chunk_terms) / max(len(chunk_terms), 1)
    scores: list[tuple[int, float]] = []
    for index, terms in enumerate(chunk_terms):
        frequencies = Counter(terms)
        score = 0.0
        for term in query_terms:
            frequency = frequencies[term]
            if not frequency:
                continue
            inverse_document_frequency = math.log(
                1 + (len(chunks) - document_frequency[term] + 0.5) / (document_frequency[term] + 0.5)
            )
            denominator = frequency + 1.2 * (
                1 - 0.75 + 0.75 * len(terms) / max(average_length, 1)
            )
            score += inverse_document_frequency * frequency * 2.2 / denominator
        if score > 0:
            scores.append((index, score))
    return tuple(
        index
        for index, _ in sorted(
            scores,
            key=lambda item: (-item[1], chunks[item[0]].identifier),
        )
    )


def _semantic_ranking(
    query_vector: tuple[float, ...],
    chunk_vectors: tuple[tuple[float, ...], ...],
) -> tuple[int, ...]:
    scored = tuple(
        (index, _cosine_similarity(query_vector, vector))
        for index, vector in enumerate(chunk_vectors)
    )
    return tuple(index for index, _ in sorted(scored, key=lambda item: (-item[1], item[0])))


def _exact_entity_ranking(
    query: str,
    chunks: Sequence[RetrievalChunk],
    documents: Sequence[RetrievalDocumentFixture],
) -> tuple[int, ...]:
    normalized_query = _normalize_entity_text(query)
    matched: list[tuple[str, ...]] = []
    for document in documents:
        for entity in document.entities:
            names = (entity.canonical_name, *entity.aliases)
            if any(_normalize_entity_text(name) in normalized_query for name in names):
                matched.append(tuple(_normalize_entity_text(name) for name in names))
    scores: list[tuple[int, int]] = []
    for index, chunk in enumerate(chunks):
        normalized_chunk = _normalize_entity_text(chunk.text)
        score = sum(any(name in normalized_chunk for name in names) for names in matched)
        if score:
            scores.append((index, score))
    return tuple(
        index
        for index, _ in sorted(
            scores,
            key=lambda item: (-item[1], chunks[item[0]].identifier),
        )
    )


def _weighted_reciprocal_rank_fusion(
    rankings: dict[str, tuple[int, ...]],
    profile: FusionProfile,
) -> tuple[int, ...]:
    scores: dict[int, float] = {}
    for signal, ranking in rankings.items():
        weight = profile.weights[signal]
        for rank, chunk_index in enumerate(ranking, start=1):
            scores[chunk_index] = scores.get(chunk_index, 0.0) + weight / (profile.rrf_k + rank)
    return tuple(index for index, _ in sorted(scores.items(), key=lambda item: (-item[1], item[0])))


def _build_retrieval_profile(
    selected: CandidateMeasurement,
    *,
    corpus: RetrievalCalibrationCorpus,
    configuration: RetrievalCandidateConfiguration,
    runtime: CalibrationRuntime,
    generated_at: datetime,
) -> RetrievalProfile:
    embedding = next(
        item
        for item in configuration.embedding_candidates
        if item.identifier == selected.embedding_candidate_id
    )
    reranker = next(
        item
        for item in configuration.reranker_candidates
        if item.identifier == selected.reranker_candidate_id
    )
    chunking = next(
        item for item in configuration.chunk_profiles if item.identifier == selected.chunk_profile_id
    )
    fusion = next(
        item for item in configuration.fusion_profiles if item.identifier == selected.fusion_profile_id
    )
    identity_payload = f"{configuration.sha256}|{corpus.sha256}|{selected.identifier}"
    profile_id = f"{PROFILE_SERIES}-{sha256(identity_payload.encode()).hexdigest()[:12]}"
    return RetrievalProfile(
        schema_version="retrieval-profile.v1",
        profile_id=profile_id,
        embedding=ProfileEmbedding(
            candidate_id=embedding.identifier,
            provider=embedding.provider,
            model_id=embedding.model_id,
            dimensions=embedding.dimensions,
            pooling=embedding.pooling,
            source_url=embedding.source_url,
        ),
        reranker=ProfileReranker(
            candidate_id=reranker.identifier,
            provider=reranker.provider,
            model_id=reranker.model_id,
            source_url=reranker.source_url,
        ),
        chunking=ProfileChunking(
            profile_id=chunking.identifier,
            strategy=chunking.strategy,
            document_types=chunking.document_types,
        ),
        retrieval=ProfileRetrieval(
            lexical_candidate_count=fusion.lexical_candidate_count,
            semantic_candidate_count=fusion.semantic_candidate_count,
            exact_entity_candidate_count=fusion.exact_entity_candidate_count,
            fused_candidate_count=fusion.fused_candidate_count,
            rerank_depth=fusion.rerank_depth,
            final_top_k=fusion.final_top_k,
        ),
        fusion=ProfileFusion(
            profile_id=fusion.identifier,
            method=fusion.method,
            rrf_k=fusion.rrf_k,
            weights=fusion.weights,
        ),
        selection_policy=configuration.selection_policy,
        calibration=ProfileCalibration(
            generated_at=generated_at.astimezone(UTC),
            corpus_version=corpus.version,
            corpus_sha256=corpus.sha256,
            candidate_configuration_version=configuration.version,
            candidate_configuration_sha256=configuration.sha256,
            runtime=runtime.name,
            logical_cpu_count=runtime.logical_cpu_count,
            configured_threads=runtime.threads,
            metrics=selected.metrics,
            declared_model_size_gb=selected.declared_model_size_gb,
            index_throughput_chunks_per_second=selected.index_throughput_chunks_per_second,
            median_query_latency_ms=selected.median_query_latency_ms,
            p95_query_latency_ms=selected.p95_query_latency_ms,
            peak_rss_mb=selected.peak_rss_mb,
        ),
    )


def _profile_to_dict(profile: RetrievalProfile) -> dict[str, object]:
    return {
        "schema_version": profile.schema_version,
        "profile_id": profile.profile_id,
        "temporary_calibration_values": True,
        "embedding": {
            "candidate_id": profile.embedding.candidate_id,
            "provider": profile.embedding.provider,
            "model_id": profile.embedding.model_id,
            "dimensions": profile.embedding.dimensions,
            "pooling": profile.embedding.pooling,
            "source_url": profile.embedding.source_url,
        },
        "reranker": {
            "candidate_id": profile.reranker.candidate_id,
            "provider": profile.reranker.provider,
            "model_id": profile.reranker.model_id,
            "source_url": profile.reranker.source_url,
        },
        "chunking": {
            "profile_id": profile.chunking.profile_id,
            "strategy": profile.chunking.strategy,
            "document_types": {
                document_type: {
                    "max_characters": window.max_characters,
                    "overlap_characters": window.overlap_characters,
                }
                for document_type, window in sorted(profile.chunking.document_types.items())
            },
        },
        "retrieval": {
            "lexical_candidate_count": profile.retrieval.lexical_candidate_count,
            "semantic_candidate_count": profile.retrieval.semantic_candidate_count,
            "exact_entity_candidate_count": profile.retrieval.exact_entity_candidate_count,
            "fused_candidate_count": profile.retrieval.fused_candidate_count,
            "rerank_depth": profile.retrieval.rerank_depth,
            "final_top_k": profile.retrieval.final_top_k,
        },
        "fusion": {
            "profile_id": profile.fusion.profile_id,
            "method": profile.fusion.method,
            "rrf_k": profile.fusion.rrf_k,
            "weights": profile.fusion.weights,
        },
        "selection_policy": {
            "cross_language_recall_at_k_minimum": (
                profile.selection_policy.cross_language_recall_at_k_minimum
            ),
            "exact_entity_recall_at_k_minimum": (
                profile.selection_policy.exact_entity_recall_at_k_minimum
            ),
            "evidence_span_recall_at_k_minimum": (
                profile.selection_policy.evidence_span_recall_at_k_minimum
            ),
            "ranking_order": list(profile.selection_policy.ranking_order),
        },
        "calibration": {
            "generated_at": profile.calibration.generated_at.isoformat(),
            "corpus_version": profile.calibration.corpus_version,
            "corpus_sha256": profile.calibration.corpus_sha256,
            "candidate_configuration_version": (
                profile.calibration.candidate_configuration_version
            ),
            "candidate_configuration_sha256": (
                profile.calibration.candidate_configuration_sha256
            ),
            "runtime": profile.calibration.runtime,
            "logical_cpu_count": profile.calibration.logical_cpu_count,
            "configured_threads": profile.calibration.configured_threads,
            "metrics": {
                "cross_language_recall_at_k": (
                    profile.calibration.metrics.cross_language_recall_at_k
                ),
                "exact_entity_recall_at_k": (
                    profile.calibration.metrics.exact_entity_recall_at_k
                ),
                "evidence_span_recall_at_k": (
                    profile.calibration.metrics.evidence_span_recall_at_k
                ),
            },
            "declared_model_size_gb": profile.calibration.declared_model_size_gb,
            "index_throughput_chunks_per_second": (
                profile.calibration.index_throughput_chunks_per_second
            ),
            "median_query_latency_ms": profile.calibration.median_query_latency_ms,
            "p95_query_latency_ms": profile.calibration.p95_query_latency_ms,
            "peak_rss_mb": profile.calibration.peak_rss_mb,
        },
    }


def _parse_profile_chunk_windows(raw: dict[str, object]) -> dict[str, ChunkWindow]:
    document_types = _required_dict(raw, "document_types")
    return {
        document_type: ChunkWindow(
            max_characters=_required_int(window, "max_characters"),
            overlap_characters=_required_int(window, "overlap_characters"),
        )
        for document_type, value in document_types.items()
        for window in (_as_dict(value, f"Chunk window for {document_type}"),)
    }


def _validate_loaded_profile(profile: RetrievalProfile) -> None:
    if profile.embedding.dimensions < 1:
        raise RetrievalCalibrationConfigurationError("Profile Embedding dimensions must be positive")
    if profile.embedding.pooling not in {"mean", "cls"}:
        raise RetrievalCalibrationConfigurationError("Profile Embedding pooling is unsupported")
    if profile.reranker.provider not in {"fastembed", "none"}:
        raise RetrievalCalibrationConfigurationError("Profile has an unsupported Reranker provider")
    if profile.reranker.provider == "fastembed" and not profile.reranker.model_id:
        raise RetrievalCalibrationConfigurationError("Profile FastEmbed Reranker requires a model_id")
    if profile.reranker.provider == "none" and profile.reranker.model_id is not None:
        raise RetrievalCalibrationConfigurationError("Profile no-Reranker control cannot name a model")
    if set(profile.fusion.weights) != {"lexical", "semantic", "exact_entity"}:
        raise RetrievalCalibrationConfigurationError("Profile fusion weights are incomplete")
    if abs(sum(profile.fusion.weights.values()) - 1.0) > 1e-9:
        raise RetrievalCalibrationConfigurationError("Profile fusion weights must sum to one")
    if profile.selection_policy.ranking_order != SELECTION_RANKING_ORDER:
        raise RetrievalCalibrationConfigurationError(
            "Profile selection ranking_order is unsupported"
        )
    if not profile.retrieval.final_top_k <= profile.retrieval.rerank_depth:
        raise RetrievalCalibrationConfigurationError("Profile retrieval depths are invalid")
    metrics = (
        profile.calibration.metrics.cross_language_recall_at_k,
        profile.calibration.metrics.exact_entity_recall_at_k,
        profile.calibration.metrics.evidence_span_recall_at_k,
    )
    if any(not 0 <= value <= 1 for value in metrics):
        raise RetrievalCalibrationConfigurationError("Profile metrics must be between zero and one")
    if profile.calibration.declared_model_size_gb < 0:
        raise RetrievalCalibrationConfigurationError(
            "Profile declared model size cannot be negative"
        )


def _candidate_measurement_id(
    embedding: EmbeddingCandidate,
    reranker: RerankerCandidate,
    chunking: ChunkProfile,
    fusion: FusionProfile,
) -> str:
    return (
        f"{embedding.identifier}__{reranker.identifier}__"
        f"{chunking.identifier}__{fusion.identifier}"
    )


def _passes_selection_gates(
    metrics: RetrievalMetrics,
    policy: RetrievalSelectionPolicy,
) -> bool:
    return (
        metrics.cross_language_recall_at_k >= policy.cross_language_recall_at_k_minimum
        and metrics.exact_entity_recall_at_k >= policy.exact_entity_recall_at_k_minimum
        and metrics.evidence_span_recall_at_k >= policy.evidence_span_recall_at_k_minimum
    )


def _validate_vectors(
    vectors: tuple[tuple[float, ...], ...],
    *,
    expected_count: int,
    expected_dimensions: int,
    label: str,
) -> None:
    if len(vectors) != expected_count:
        raise RetrievalCalibrationConfigurationError(
            f"Embedding candidate {label} returned {len(vectors)} vectors, expected {expected_count}"
        )
    if any(len(vector) != expected_dimensions for vector in vectors):
        raise RetrievalCalibrationConfigurationError(
            f"Embedding candidate {label} returned an unexpected dimension"
        )
    if any(not math.isfinite(value) for vector in vectors for value in vector):
        raise RetrievalCalibrationConfigurationError(
            f"Embedding candidate {label} returned a non-finite value"
        )


def _prefer_text_boundary(text: str, start: int, proposed_end: int) -> int:
    minimum = start + max((proposed_end - start) * 3 // 5, 1)
    for index in range(proposed_end - 1, minimum - 1, -1):
        if text[index] in "。！？.!?\n":
            return index + 1
    return proposed_end


def _retrieval_terms(value: str) -> tuple[str, ...]:
    normalized = value.casefold()
    latin = re.findall(r"[a-z0-9]+(?:[-._][a-z0-9]+)*", normalized)
    cjk_runs = re.findall(r"[\u3400-\u9fff]+", normalized)
    cjk: list[str] = []
    for run in cjk_runs:
        cjk.extend(run)
        cjk.extend(run[index : index + 2] for index in range(len(run) - 1))
    return tuple(latin + cjk)


def _normalize_entity_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _cosine_similarity(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True)) / (left_norm * right_norm)


def _recall(hits: Sequence[bool]) -> float:
    return sum(hits) / len(hits)


def _percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        raise RetrievalCalibrationConfigurationError("Cannot calculate a percentile without values")
    ordered = sorted(values)
    index = max(math.ceil(quantile * len(ordered)) - 1, 0)
    return ordered[index]


def _parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise RetrievalCalibrationConfigurationError("Profile timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise RetrievalCalibrationConfigurationError("Profile timestamp must include a timezone")
    return parsed


def _required_sha256(mapping: dict[str, object], key: str) -> str:
    value = _required_text(mapping, key)
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise RetrievalCalibrationConfigurationError(f"{key} must be a lowercase SHA-256")
    return value


def _parse_document(item: object) -> RetrievalDocumentFixture:
    raw = _as_dict(item, "document")
    text = _required_text(raw, "text")
    entities = tuple(
        EntityFixture(
            canonical_name=_required_text(entity, "canonical_name"),
            aliases=tuple(_required_text_list(entity, "aliases")),
        )
        for entity in _required_list(raw, "entities")
    )
    spans: list[EvidenceSpanFixture] = []
    for span_item in _required_list(raw, "evidence_spans"):
        span = _as_dict(span_item, "evidence span")
        span_text = _required_text(span, "text")
        if text.count(span_text) != 1:
            raise RetrievalCalibrationConfigurationError(
                "Evidence Span text must occur exactly once in its Document Version"
            )
        start = text.index(span_text)
        spans.append(
            EvidenceSpanFixture(
                identifier=_required_text(span, "evidence_span_id"),
                text=span_text,
                start=start,
                end=start + len(span_text),
            )
        )
    return RetrievalDocumentFixture(
        document_version_id=_required_text(raw, "document_version_id"),
        document_type=_required_text(raw, "document_type"),
        language=_required_text(raw, "language"),
        text=text,
        entities=entities,
        evidence_spans=tuple(spans),
    )


def _parse_query(item: object) -> RetrievalQueryFixture:
    raw = _as_dict(item, "query")
    groups = tuple(_required_text_list(raw, "metric_groups"))
    allowed_groups = {"cross_language", "exact_entity", "evidence_span"}
    if not groups or not set(groups) <= allowed_groups:
        raise RetrievalCalibrationConfigurationError("Query has unsupported metric groups")
    expected_entity = raw.get("expected_entity")
    if expected_entity is not None and not isinstance(expected_entity, str):
        raise RetrievalCalibrationConfigurationError("expected_entity must be text or null")
    return RetrievalQueryFixture(
        identifier=_required_text(raw, "query_id"),
        text=_required_text(raw, "text"),
        language=_required_text(raw, "language"),
        metric_groups=groups,  # type: ignore[arg-type]
        expected_document_version_id=_required_text(raw, "expected_document_version_id"),
        expected_evidence_span_ids=tuple(
            _required_text_list(raw, "expected_evidence_span_ids")
        ),
        expected_entity=expected_entity,
    )


def _parse_reranker(item: object) -> RerankerCandidate:
    raw = _as_dict(item, "reranker candidate")
    provider = _required_text(raw, "provider")
    if provider not in {"fastembed", "none"}:
        raise RetrievalCalibrationConfigurationError("Unsupported Reranker provider")
    model_id = raw.get("model_id")
    if model_id is not None and not isinstance(model_id, str):
        raise RetrievalCalibrationConfigurationError("Reranker model_id must be text or null")
    source_url = raw.get("source_url")
    if source_url is not None and (
        not isinstance(source_url, str) or not source_url.startswith("https://")
    ):
        raise RetrievalCalibrationConfigurationError(
            "Reranker source_url must be an HTTPS URL or null"
        )
    return RerankerCandidate(
        identifier=_required_text(raw, "candidate_id"),
        provider=provider,  # type: ignore[arg-type]
        model_id=model_id,
        model_size_gb=_required_float(raw, "model_size_gb"),
        license=_required_text(raw, "license"),
        source_url=source_url,
    )


def _parse_chunk_profile(item: object) -> ChunkProfile:
    raw = _as_dict(item, "Chunk profile")
    document_types_raw = _required_dict(raw, "document_types")
    document_types = {
        document_type: ChunkWindow(
            max_characters=_required_int(window, "max_characters"),
            overlap_characters=_required_int(window, "overlap_characters"),
        )
        for document_type, value in document_types_raw.items()
        for window in (_as_dict(value, f"Chunk window for {document_type}"),)
    }
    return ChunkProfile(
        identifier=_required_text(raw, "profile_id"),
        strategy=_required_text(raw, "strategy"),
        document_types=document_types,
    )


def _parse_fusion_profile(item: object) -> FusionProfile:
    raw = _as_dict(item, "fusion profile")
    weights_raw = _required_dict(raw, "weights")
    weights = {name: _number(value, f"fusion weight {name}") for name, value in weights_raw.items()}
    return FusionProfile(
        identifier=_required_text(raw, "profile_id"),
        method=_required_text(raw, "method"),
        rrf_k=_required_int(raw, "rrf_k"),
        weights=weights,
        lexical_candidate_count=_required_int(raw, "lexical_candidate_count"),
        semantic_candidate_count=_required_int(raw, "semantic_candidate_count"),
        exact_entity_candidate_count=_required_int(raw, "exact_entity_candidate_count"),
        fused_candidate_count=_required_int(raw, "fused_candidate_count"),
        rerank_depth=_required_int(raw, "rerank_depth"),
        final_top_k=_required_int(raw, "final_top_k"),
    )


def _validate_corpus(corpus: RetrievalCalibrationCorpus) -> None:
    document_ids = [item.document_version_id for item in corpus.documents]
    query_ids = [item.identifier for item in corpus.queries]
    span_owners = {
        span.identifier: document.document_version_id
        for document in corpus.documents
        for span in document.evidence_spans
    }
    if len(document_ids) != len(set(document_ids)) or len(query_ids) != len(set(query_ids)):
        raise RetrievalCalibrationConfigurationError("Corpus identifiers must be unique")
    if len(span_owners) != sum(len(item.evidence_spans) for item in corpus.documents):
        raise RetrievalCalibrationConfigurationError("Evidence Span identifiers must be unique")
    documents = {item.document_version_id: item for item in corpus.documents}
    for query in corpus.queries:
        document = documents.get(query.expected_document_version_id)
        if document is None:
            raise RetrievalCalibrationConfigurationError("Query references an unknown Document Version")
        if any(span_owners.get(span_id) != document.document_version_id for span_id in query.expected_evidence_span_ids):
            raise RetrievalCalibrationConfigurationError("Query Evidence Span belongs to another Document Version")
        if "cross_language" in query.metric_groups and query.language == document.language:
            raise RetrievalCalibrationConfigurationError("Cross-language query must cross languages")
        if "exact_entity" in query.metric_groups and (
            query.expected_entity is None
            or query.expected_entity.casefold() not in query.text.casefold()
        ):
            raise RetrievalCalibrationConfigurationError(
                "Exact-Entity query must contain its gold Entity"
            )


def _validate_candidate_configuration(configuration: RetrievalCandidateConfiguration) -> None:
    groups = (
        configuration.embedding_candidates,
        configuration.reranker_candidates,
        configuration.chunk_profiles,
        configuration.fusion_profiles,
    )
    if any(len(items) < 2 for items in groups):
        raise RetrievalCalibrationConfigurationError("Every calibration dimension needs two candidates")
    for items in groups:
        identifiers = [item.identifier for item in items]
        if len(identifiers) != len(set(identifiers)):
            raise RetrievalCalibrationConfigurationError("Candidate identifiers must be unique")
    if configuration.runtime.threads < 1:
        raise RetrievalCalibrationConfigurationError("CPU thread count must be positive")
    for candidate in configuration.embedding_candidates:
        if candidate.dimensions < 1 or candidate.model_size_gb <= 0:
            raise RetrievalCalibrationConfigurationError("Embedding dimensions and size must be positive")
        if candidate.pooling not in {"mean", "cls"}:
            raise RetrievalCalibrationConfigurationError("Embedding pooling is unsupported")
    for candidate in configuration.reranker_candidates:
        if candidate.provider == "fastembed" and not candidate.model_id:
            raise RetrievalCalibrationConfigurationError("FastEmbed Reranker requires a model_id")
        if candidate.provider == "fastembed" and candidate.source_url is None:
            raise RetrievalCalibrationConfigurationError("FastEmbed Reranker requires a source_url")
        if candidate.provider == "none" and candidate.model_id is not None:
            raise RetrievalCalibrationConfigurationError("No-Reranker control cannot name a model")
    for profile in configuration.chunk_profiles:
        if profile.strategy != "type-aware-character-window" or not profile.document_types:
            raise RetrievalCalibrationConfigurationError("Unsupported or empty Chunk strategy")
        for window in profile.document_types.values():
            if not 0 <= window.overlap_characters < window.max_characters:
                raise RetrievalCalibrationConfigurationError("Chunk overlap must be smaller than its window")
    expected_weights = {"lexical", "semantic", "exact_entity"}
    for profile in configuration.fusion_profiles:
        counts = (
            profile.rrf_k,
            profile.lexical_candidate_count,
            profile.semantic_candidate_count,
            profile.exact_entity_candidate_count,
            profile.fused_candidate_count,
            profile.rerank_depth,
            profile.final_top_k,
        )
        if profile.method != "weighted-reciprocal-rank-fusion":
            raise RetrievalCalibrationConfigurationError("Unsupported fusion method")
        if set(profile.weights) != expected_weights or any(value < 0 for value in profile.weights.values()):
            raise RetrievalCalibrationConfigurationError("Fusion weights must cover three non-negative signals")
        if abs(sum(profile.weights.values()) - 1.0) > 1e-9 or any(value < 1 for value in counts):
            raise RetrievalCalibrationConfigurationError("Fusion weights or candidate counts are invalid")
        if not profile.final_top_k <= profile.rerank_depth <= profile.fused_candidate_count:
            raise RetrievalCalibrationConfigurationError("Fusion depths must be monotonically bounded")
    thresholds = (
        configuration.selection_policy.cross_language_recall_at_k_minimum,
        configuration.selection_policy.exact_entity_recall_at_k_minimum,
        configuration.selection_policy.evidence_span_recall_at_k_minimum,
    )
    if any(not 0 <= value <= 1 for value in thresholds):
        raise RetrievalCalibrationConfigurationError("Selection thresholds must be between zero and one")
    if configuration.selection_policy.ranking_order != SELECTION_RANKING_ORDER:
        raise RetrievalCalibrationConfigurationError(
            "Selection ranking_order does not match the v1 calibration policy"
        )


def _load_json_resource(name: str) -> tuple[dict[str, object], str]:
    raw_bytes = files("ai_intel_agent").joinpath("data", name).read_bytes()
    try:
        payload = json.loads(raw_bytes)
    except json.JSONDecodeError as error:
        raise RetrievalCalibrationConfigurationError(f"Invalid JSON in {name}") from error
    if not isinstance(payload, dict):
        raise RetrievalCalibrationConfigurationError(f"{name} must contain a JSON object")
    return payload, sha256(raw_bytes).hexdigest()


def _as_dict(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise RetrievalCalibrationConfigurationError(f"{label} must be an object")
    return value


def _required_dict(mapping: dict[str, object], key: str) -> dict[str, object]:
    return _as_dict(mapping.get(key), key)


def _required_list(mapping: dict[str, object], key: str) -> list[object]:
    value = mapping.get(key)
    if not isinstance(value, list):
        raise RetrievalCalibrationConfigurationError(f"{key} must be a list")
    return value


def _required_text(mapping: dict[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RetrievalCalibrationConfigurationError(f"{key} must be non-empty text")
    return value


def _required_https_url(mapping: dict[str, object], key: str) -> str:
    value = _required_text(mapping, key)
    if not value.startswith("https://"):
        raise RetrievalCalibrationConfigurationError(f"{key} must be an HTTPS URL")
    return value


def _required_text_list(mapping: dict[str, object], key: str) -> list[str]:
    values = _required_list(mapping, key)
    if not all(isinstance(value, str) and value.strip() for value in values):
        raise RetrievalCalibrationConfigurationError(f"{key} must contain non-empty text")
    return values  # type: ignore[return-value]


def _required_int(mapping: dict[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise RetrievalCalibrationConfigurationError(f"{key} must be an integer")
    return value


def _required_float(mapping: dict[str, object], key: str) -> float:
    return _number(mapping.get(key), key)


def _number(value: object, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise RetrievalCalibrationConfigurationError(f"{label} must be numeric")
    return float(value)
