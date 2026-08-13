import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from importlib.resources import files
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ai_intel_agent.cli import app
from ai_intel_agent.retrieval_calibration import (
    CandidateMeasurement,
    EmbeddingCandidate,
    RerankerCandidate,
    RetrievalMetrics,
    build_retrieval_chunks,
    compare_retrieval_profiles,
    load_retrieval_candidate_configuration,
    load_retrieval_corpus,
    load_retrieval_profile,
    measure_retrieval_recall,
    run_retrieval_calibration,
    select_retrieval_profile_candidate,
)

_CONCEPT_TERMS = (
    (
        "pgvector",
        "postgresql",
        "关系数据库",
        "全文检索",
    ),
    (
        "mixture-of-experts",
        "混合专家",
        "only part of the total parameters",
        "参数总量中的一部分",
    ),
    (
        "resource indicator",
        "资源指示符",
        "protected resource",
        "受保护资源",
    ),
    (
        "rebuildable retrieval chunk",
        "检索 chunk",
        "not evidence",
        "才能支持 claim",
    ),
)


def _concept_vector(text: str) -> tuple[float, ...]:
    normalized = text.casefold()
    return tuple(
        float(sum(term in normalized for term in concept_terms))
        for concept_terms in _CONCEPT_TERMS
    )


class FakeEmbeddingModel:
    def __init__(self, candidate: EmbeddingCandidate) -> None:
        self.model_id = candidate.model_id
        self.dimensions = candidate.dimensions

    def embed_passages(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        return tuple(self._padded_vector(text) for text in texts)

    def embed_queries(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        return tuple(self._padded_vector(text) for text in texts)

    def _padded_vector(self, text: str) -> tuple[float, ...]:
        concepts = _concept_vector(text)
        return concepts + (0.0,) * (self.dimensions - len(concepts))

    def close(self) -> None:
        return None


class FakeRerankerModel:
    def rerank(self, query: str, documents: Sequence[str]) -> tuple[float, ...]:
        query_vector = _concept_vector(query)
        return tuple(
            sum(
                query_value * document_value
                for query_value, document_value in zip(
                    query_vector, _concept_vector(document), strict=True
                )
            )
            for document in documents
        )

    def close(self) -> None:
        return None


class FakeCalibrationRuntime:
    name = "fake-cpu-runtime"

    def __init__(self, *, threads: int) -> None:
        self.threads = threads
        self.logical_cpu_count = 8

    def create_embedding(self, candidate: EmbeddingCandidate) -> FakeEmbeddingModel:
        return FakeEmbeddingModel(candidate)

    def create_reranker(self, candidate: RerankerCandidate) -> FakeRerankerModel:
        assert candidate.model_id is not None
        return FakeRerankerModel()

    def rss_bytes(self) -> int:
        return 256 * 1024 * 1024


def test_cli_exposes_standalone_retrieval_calibration_command() -> None:
    result = CliRunner().invoke(app, ["calibrate-retrieval", "--help"])

    assert result.exit_code == 0
    assert "versioned Retrieval Profile" in result.stdout


def test_fixed_corpus_and_candidate_matrix_are_versioned_and_domain_safe() -> None:
    corpus = load_retrieval_corpus()
    configuration = load_retrieval_candidate_configuration()

    assert corpus.schema_version == "retrieval-calibration-corpus.v1"
    assert corpus.version == "retrieval-calibration-2026-08-13.v1"
    assert len(corpus.documents) >= 8
    assert corpus.review_state == "human-approved"
    assert corpus.approved_by == "Ev3rGan"
    assert corpus.approved_at == datetime.fromisoformat("2026-08-13T14:25:22+08:00")
    assert corpus.approved_fixtures_sha256 == corpus.fixtures_sha256
    assert all(document.document_type != "story" for document in corpus.documents)
    assert {group for query in corpus.queries for group in query.metric_groups} == {
        "cross_language",
        "exact_entity",
        "evidence_span",
    }
    assert all(
        span.text == document.text[span.start : span.end]
        for document in corpus.documents
        for span in document.evidence_spans
    )
    assert all(
        span.exact_text_sha256 == sha256(span.text.encode("utf-8")).hexdigest()
        and span.claim_id
        and span.evidence_role in {"Primary", "Independent", "Secondary", "Community"}
        and span.locator_type == "character-offset"
        for document in corpus.documents
        for span in document.evidence_spans
    )

    assert configuration.schema_version == "retrieval-candidates.v1"
    assert len(configuration.embedding_candidates) == 2
    assert {candidate.pooling for candidate in configuration.embedding_candidates} == {"mean"}
    assert all(candidate.source_url.startswith("https://") for candidate in configuration.embedding_candidates)
    assert {candidate.provider for candidate in configuration.reranker_candidates} == {
        "fastembed",
        "none",
    }
    assert len(configuration.chunk_profiles) >= 2
    assert len(configuration.fusion_profiles) >= 2
    assert configuration.selection_policy.ranking_order[4:6] == (
        "declared_model_size_gb_asc",
        "index_throughput_desc",
    )


def test_standalone_calibration_reports_metrics_and_exports_loadable_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "ai_intel_agent.cli.FastEmbedCalibrationRuntime",
        FakeCalibrationRuntime,
    )
    corpus = load_retrieval_corpus()
    approved_corpus = replace(
        corpus,
        review_state="human-approved",
        approved_by="test-administrator",
        approved_at=datetime(2026, 8, 13, tzinfo=UTC),
        approved_fixtures_sha256=corpus.fixtures_sha256,
    )
    monkeypatch.setattr(
        "ai_intel_agent.cli.load_retrieval_corpus",
        lambda: approved_corpus,
    )
    report_path = tmp_path / "retrieval-calibration.md"
    profile_path = tmp_path / "retrieval-profile.v1.json"

    result = CliRunner().invoke(
        app,
        [
            "calibrate-retrieval",
            "--output",
            str(report_path),
            "--profile-output",
            str(profile_path),
        ],
    )

    assert result.exit_code == 0, (
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}\n{result.exception!r}"
    )
    assert "Calibrated 16 Retrieval Profile candidates" in result.stdout
    report = report_path.read_text(encoding="utf-8")
    assert "Cross-language retrieval Recall@5" in report
    assert "Exact technical-Entity retrieval Recall@5" in report
    assert "Evidence Span Recall@5" in report
    assert "CPU resources" in report
    assert "Index throughput" in report
    assert "P95 query latency" in report
    assert "Corpus SHA-256" in report
    assert "Chunks remain rebuildable retrieval artifacts" in report

    profile = load_retrieval_profile(profile_path)
    configuration = load_retrieval_candidate_configuration()
    assert profile.schema_version == "retrieval-profile.v1"
    assert profile.calibration.corpus_review_state == "human-approved"
    assert profile.calibration.corpus_fixtures_sha256 == corpus.fixtures_sha256
    assert profile.calibration.corpus_approved_by == "test-administrator"
    selected_id = (
        f"{profile.embedding.candidate_id}__{profile.reranker.candidate_id}__"
        f"{profile.chunking.profile_id}__{profile.fusion.profile_id}"
    )
    identity = f"{configuration.sha256}|{corpus.sha256}|{selected_id}"
    expected_suffix = sha256(identity.encode()).hexdigest()[:12]
    assert profile.profile_id == f"retrieval-profile-2026-08-13.v1-{expected_suffix}"
    assert profile.embedding.model_id
    assert profile.embedding.dimensions in {384, 768}
    assert profile.embedding.pooling == "mean"
    assert profile.chunking.strategy == "type-aware-character-window"
    assert profile.retrieval.final_top_k == 5
    assert set(profile.fusion.weights) == {"lexical", "semantic", "exact_entity"}


def test_cli_fails_before_runtime_for_a_corpus_without_exact_human_approval(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_created = False

    def create_runtime(*, threads: int) -> FakeCalibrationRuntime:
        nonlocal runtime_created
        runtime_created = True
        return FakeCalibrationRuntime(threads=threads)

    monkeypatch.setattr(
        "ai_intel_agent.cli.FastEmbedCalibrationRuntime",
        create_runtime,
    )
    corpus = load_retrieval_corpus()
    awaiting_corpus = replace(
        corpus,
        review_state="awaiting-human-approval",
        approved_by=None,
        approved_at=None,
        approved_fixtures_sha256=None,
    )
    monkeypatch.setattr(
        "ai_intel_agent.cli.load_retrieval_corpus",
        lambda: awaiting_corpus,
    )

    result = CliRunner().invoke(
        app,
        [
            "calibrate-retrieval",
            "--output",
            str(tmp_path / "report.md"),
            "--profile-output",
            str(tmp_path / "profile.json"),
        ],
    )

    assert result.exit_code != 0
    assert corpus.fixtures_sha256 in result.output
    assert "human approval" in result.output
    assert runtime_created is False


def test_packaged_profile_matches_current_versioned_inputs() -> None:
    profile = load_retrieval_profile()
    corpus = load_retrieval_corpus()
    configuration = load_retrieval_candidate_configuration()

    assert profile.calibration.corpus_version == corpus.version
    assert profile.calibration.corpus_sha256 == corpus.sha256
    assert profile.calibration.corpus_review_state == corpus.review_state
    assert profile.calibration.corpus_fixtures_sha256 == corpus.fixtures_sha256
    assert profile.calibration.candidate_configuration_version == configuration.version
    assert profile.calibration.candidate_configuration_sha256 == configuration.sha256


def test_chunk_profiles_stay_inside_each_document_version() -> None:
    corpus = load_retrieval_corpus()
    documents = {document.document_version_id: document for document in corpus.documents}

    for profile in load_retrieval_candidate_configuration().chunk_profiles:
        chunks = build_retrieval_chunks(corpus.documents, profile)

        assert chunks
        for chunk in chunks:
            document = documents[chunk.document_version_id]
            assert chunk.text == document.text[chunk.start : chunk.end]
            assert all(
                span.identifier in chunk.evidence_span_ids
                for span in document.evidence_spans
                if chunk.start <= span.start and span.end <= chunk.end
            )


def test_exact_entity_recall_requires_the_entity_in_the_retrieved_chunk() -> None:
    corpus = load_retrieval_corpus()
    profile = load_retrieval_candidate_configuration().chunk_profiles[0]
    chunks = build_retrieval_chunks(corpus.documents, profile)
    results: list[tuple[int, ...]] = []

    for query in corpus.queries:
        candidates = [
            (index, chunk)
            for index, chunk in enumerate(chunks)
            if chunk.document_version_id == query.expected_document_version_id
        ]
        if "exact_entity" in query.metric_groups:
            assert query.expected_entity is not None
            chosen = next(
                index
                for index, chunk in candidates
                if query.expected_entity.casefold() not in chunk.text.casefold()
            )
        else:
            chosen = candidates[0][0]
        results.append((chosen,))

    metrics = measure_retrieval_recall(
        corpus.queries,
        chunks,
        tuple(results),
        final_top_k=1,
    )

    assert metrics.exact_entity_recall_at_k == 0.0


def test_calibration_scores_are_reproducible_for_the_same_versioned_inputs(
    tmp_path: Path,
) -> None:
    corpus = load_retrieval_corpus()
    corpus = replace(
        corpus,
        review_state="human-approved",
        approved_by="test-administrator",
        approved_at=datetime(2026, 8, 13, tzinfo=UTC),
        approved_fixtures_sha256=corpus.fixtures_sha256,
    )
    configuration = load_retrieval_candidate_configuration()
    run_at = datetime(2026, 8, 13, 12, tzinfo=UTC)

    first = run_retrieval_calibration(
        tmp_path / "first.md",
        tmp_path / "first.json",
        runtime=FakeCalibrationRuntime(threads=configuration.runtime.threads),
        corpus=corpus,
        configuration=configuration,
        now=run_at,
    )
    second = run_retrieval_calibration(
        tmp_path / "second.md",
        tmp_path / "second.json",
        runtime=FakeCalibrationRuntime(threads=configuration.runtime.threads),
        corpus=corpus,
        configuration=configuration,
        now=run_at,
    )

    assert tuple(item.metrics for item in first.measurements) == tuple(
        item.metrics for item in second.measurements
    )
    assert first.selected.identifier == second.selected.identifier
    assert first.profile.profile_id == second.profile.profile_id


def test_two_loadable_retrieval_profiles_can_be_compared(tmp_path: Path) -> None:
    payload = json.loads(
        files("ai_intel_agent")
        .joinpath("data", "retrieval_profile.v1.json")
        .read_text(encoding="utf-8")
    )
    changed = json.loads(json.dumps(payload))
    changed["profile_id"] = "retrieval-profile-comparison-fixture"
    changed["fusion"]["weights"] = {
        "lexical": 0.3,
        "semantic": 0.5,
        "exact_entity": 0.2,
    }
    changed["calibration"]["metrics"]["cross_language_recall_at_k"] = 0.75
    left_path = tmp_path / "left.json"
    right_path = tmp_path / "right.json"
    left_path.write_text(json.dumps(payload), encoding="utf-8")
    right_path.write_text(json.dumps(changed), encoding="utf-8")

    comparison = compare_retrieval_profiles(
        load_retrieval_profile(left_path),
        load_retrieval_profile(right_path),
    )

    assert comparison.changed_components == ("fusion",)
    assert comparison.cross_language_recall_delta == pytest.approx(-0.25)
    assert comparison.exact_entity_recall_delta == 0.0
    assert comparison.evidence_span_recall_delta == 0.0


def test_failed_gate_cannot_be_hidden_by_a_higher_mean_recall() -> None:
    failing = CandidateMeasurement(
        identifier="high-average-but-failed",
        embedding_candidate_id="embedding-a",
        reranker_candidate_id="reranker-a",
        chunk_profile_id="chunks-a",
        fusion_profile_id="fusion-a",
        metrics=RetrievalMetrics(1.0, 0.99, 1.0),
        passed_selection_gates=False,
        declared_model_size_gb=0.5,
        index_throughput_chunks_per_second=100.0,
        median_query_latency_ms=1.0,
        p95_query_latency_ms=1.0,
    )
    eligible = CandidateMeasurement(
        identifier="eligible",
        embedding_candidate_id="embedding-b",
        reranker_candidate_id="reranker-b",
        chunk_profile_id="chunks-b",
        fusion_profile_id="fusion-b",
        metrics=RetrievalMetrics(0.75, 1.0, 0.75),
        passed_selection_gates=True,
        declared_model_size_gb=1.0,
        index_throughput_chunks_per_second=10.0,
        median_query_latency_ms=10.0,
        p95_query_latency_ms=10.0,
    )

    assert select_retrieval_profile_candidate((failing, eligible)) == eligible


def test_quality_tie_prefers_median_latency_before_p95_tail() -> None:
    lower_median = CandidateMeasurement(
        identifier="lower-median",
        embedding_candidate_id="embedding-a",
        reranker_candidate_id="reranker-a",
        chunk_profile_id="chunks-a",
        fusion_profile_id="fusion-a",
        metrics=RetrievalMetrics(1.0, 1.0, 1.0),
        passed_selection_gates=True,
        declared_model_size_gb=1.0,
        index_throughput_chunks_per_second=10.0,
        median_query_latency_ms=8.0,
        p95_query_latency_ms=15.0,
    )
    lower_p95 = replace(
        lower_median,
        identifier="lower-p95",
        median_query_latency_ms=10.0,
        p95_query_latency_ms=12.0,
    )

    assert select_retrieval_profile_candidate((lower_p95, lower_median)) == lower_median


def test_quality_tie_prefers_smaller_model_before_latency_noise() -> None:
    smaller = CandidateMeasurement(
        identifier="smaller",
        embedding_candidate_id="embedding-small",
        reranker_candidate_id="reranker",
        chunk_profile_id="chunks",
        fusion_profile_id="fusion",
        metrics=RetrievalMetrics(1.0, 1.0, 1.0),
        passed_selection_gates=True,
        declared_model_size_gb=1.25,
        index_throughput_chunks_per_second=40.0,
        median_query_latency_ms=900.0,
        p95_query_latency_ms=1000.0,
    )
    larger_but_noisily_faster = replace(
        smaller,
        identifier="larger",
        declared_model_size_gb=2.0,
        median_query_latency_ms=650.0,
        p95_query_latency_ms=800.0,
    )

    assert (
        select_retrieval_profile_candidate((larger_but_noisily_faster, smaller))
        == smaller
    )
