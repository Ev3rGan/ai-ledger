from __future__ import annotations

import json
import os
from collections.abc import Iterator, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from pg0 import Pg0
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from ai_intel_agent.accepted_knowledge import (
    APPROVED_EMBEDDING_MODEL_ID,
    APPROVED_EMBEDDING_REVISION,
    APPROVED_EMBEDDING_SHA256,
    APPROVED_PROFILE_ID,
    APPROVED_PROFILE_SHA256,
    APPROVED_RERANKER_MODEL_ID,
    APPROVED_RERANKER_REVISION,
    APPROVED_RERANKER_SHA256,
    AcceptedKnowledgeConfigurationError,
    AcceptedKnowledgeIndexer,
    AcceptedKnowledgeRetrieval,
    ApprovedRetrievalBackends,
    ChunkSource,
    FastEmbedEmbeddingBackend,
    FastEmbedRerankerBackend,
    RetrievalBackendFault,
    RetrievalFilters,
    RetrievalHealthSnapshot,
    RetrievalModelConfiguration,
    RetrievalQuery,
    RetrievalRuntimeStageStatus,
    _run_git_snapshot_check,
    build_document_chunks,
    load_accepted_knowledge_profile,
    record_retrieval_backend_startup_state,
    retrieval_health_snapshot,
    validate_approved_model_artifacts,
)
from ai_intel_agent.cli import app
from ai_intel_agent.domain import DigestState, StoryReviewState, Topic
from ai_intel_agent.editorial import DigestPublicationContract
from ai_intel_agent.persistence import (
    CandidateRecord,
    ClaimRecord,
    DigestRecord,
    DigestStoryRecord,
    DigestWithdrawalRecord,
    DocumentVersionRecord,
    EvidenceSpanRecord,
    RetrievalChunkRecord,
    RetrievalIndexRecord,
    RetrievalRuntimeStateRecord,
    StoryPresentationRecord,
    StoryRecord,
    create_database_engine,
    database_url_for_alembic_config,
    upgrade_database,
)
from ai_intel_agent.research import ResearchEvidenceSet
from ai_intel_agent.web import create_app
from alembic import command


def _id(name: str):
    return uuid5(NAMESPACE_URL, f"m4-hybrid-retrieval:{name}")


runner = CliRunner()


def _upgrade_database_to(database_url: str, revision: str) -> None:
    project_root = Path(__file__).resolve().parents[1]
    configuration = Config(str(project_root / "alembic.ini"))
    configuration.set_main_option(
        "sqlalchemy.url",
        database_url_for_alembic_config(database_url),
    )
    command.upgrade(configuration, revision)


@pytest.fixture
def m4_database_url() -> Iterator[str]:
    name = f"ai_intel_m4_{_id(os.urandom(8).hex).hex}"
    data_root = os.getenv("PG0_TEST_DATA_ROOT")
    data_dir = str(Path(data_root) / name) if data_root else None
    server = Pg0(name=name, data_dir=data_dir)
    server.start()
    try:
        upgrade_database(server.uri)
        yield server.uri
    finally:
        server.drop()


class FakeEmbeddingBackend:
    def embed_documents(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        return tuple(self._vector(index + 1) for index, _ in enumerate(texts))

    def embed_query(self, text: str) -> tuple[float, ...]:
        return self._vector(1)

    @staticmethod
    def _vector(seed: int) -> tuple[float, ...]:
        return tuple([1.0 if index == seed % 384 else 0.0 for index in range(384)])


class SemanticEmbeddingBackend:
    def embed_documents(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        return tuple(
            self._vector(0 if "跨语言部署" in text else index + 1)
            for index, text in enumerate(texts)
        )

    def embed_query(self, text: str) -> tuple[float, ...]:
        return self._vector(0)

    @staticmethod
    def _vector(coordinate: int) -> tuple[float, ...]:
        return tuple(1.0 if index == coordinate else 0.0 for index in range(384))


class RecordingReranker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def rerank(self, query: str, documents: Sequence[str]) -> tuple[float, ...]:
        copied = tuple(documents)
        self.calls.append((query, copied))
        return tuple(
            100.0 if "跨语言部署" in text else float(index) for index, text in enumerate(copied)
        )


class UnavailableEmbeddingBackend:
    def embed_documents(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        raise RuntimeError("forced embedding outage")

    def embed_query(self, text: str) -> tuple[float, ...]:
        raise RuntimeError("forced embedding outage")


class FailingReranker:
    def rerank(self, query: str, documents: Sequence[str]) -> tuple[float, ...]:
        raise RuntimeError("forced reranker failure")


class RecordingAcceptedKnowledgeOperation:
    def __init__(self, retrieval: AcceptedKnowledgeRetrieval) -> None:
        self._retrieval = retrieval
        self.queries: list[RetrievalQuery] = []

    def retrieve(self, query: RetrievalQuery):
        self.queries.append(query)
        return self._retrieval.retrieve(query)


class CitingFirstResearchProvider:
    def __init__(self) -> None:
        self.calls: list[ResearchEvidenceSet] = []

    def stream(self, evidence_set: ResearchEvidenceSet):
        self.calls.append(evidence_set)
        evidence = evidence_set.evidence[0]
        yield json.dumps(
            {
                "answer": "共享 Hybrid Retrieval 找到了跨语言部署证据。",
                "citations": [
                    {
                        "story_id": str(evidence.story_id),
                        "claim_id": str(evidence.claim_id),
                        "evidence_span_id": str(evidence.evidence_span_id),
                    }
                ],
            },
            ensure_ascii=False,
        )


class FakeFastEmbedEmbeddingModel:
    def __init__(self) -> None:
        self.passage_calls: list[tuple[tuple[str, ...], int]] = []
        self.query_calls: list[tuple[tuple[str, ...], int]] = []
        self.model = _FakeFastEmbedRuntimeModel()

    def passage_embed(self, texts: Sequence[str], *, batch_size: int):
        copied = tuple(texts)
        self.passage_calls.append((copied, batch_size))
        return (tuple(2.0 if index == 0 else 0.0 for index in range(384)) for _ in copied)

    def query_embed(self, texts: Sequence[str], *, batch_size: int):
        copied = tuple(texts)
        self.query_calls.append((copied, batch_size))
        return (tuple(3.0 if index == 1 else 0.0 for index in range(384)) for _ in copied)


class _FakeEncoding:
    def __init__(self, text: str) -> None:
        self.ids = list(range(len(text.split())))
        self.offsets: list[tuple[int, int]] = []
        cursor = 0
        for token in text.split():
            start = text.index(token, cursor)
            end = start + len(token)
            self.offsets.append((start, end))
            cursor = end


class _FakeTokenizer:
    def encode(self, text: str, *, add_special_tokens: bool = True) -> _FakeEncoding:
        del add_special_tokens
        return _FakeEncoding(text)


class _FakeFastEmbedRuntimeModel:
    def __init__(self) -> None:
        self.tokenizer = _FakeTokenizer()


class DuplicateDominatingEmbeddingBackend:
    def embed_documents(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        return tuple(self._vector(0 if "dominant" in text else 1) for text in texts)

    def embed_query(self, text: str) -> tuple[float, ...]:
        del text
        return self._vector(0)

    @staticmethod
    def _vector(coordinate: int) -> tuple[float, ...]:
        return tuple(1.0 if index == coordinate else 0.0 for index in range(384))


class FakeFastEmbedRerankerModel:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...], int]] = []

    def rerank(self, query: str, documents: Sequence[str], *, batch_size: int):
        copied = tuple(documents)
        self.calls.append((query, copied, batch_size))
        return (float(index) for index, _ in enumerate(copied))


def _persist_knowledge_story(
    database_url: str,
    *,
    identity: str,
    body: str,
    publisher: str = "Fixture Publisher",
    review_state: StoryReviewState = StoryReviewState.ACCEPTED,
    public: bool = True,
    published_at: datetime | None = None,
    occurred_at: datetime | None = None,
    primary_topic: Topic = Topic.MODELS,
    digest_state: DigestState = DigestState.PUBLISHED,
    source_url: str | None = None,
    title: str | None = None,
    headline: str | None = None,
) -> tuple[object, object, object, object]:
    candidate_id = _id(f"{identity}:candidate")
    document_id = _id(f"{identity}:document")
    story_id = _id(f"{identity}:story")
    claim_id = _id(f"{identity}:claim")
    evidence_id = _id(f"{identity}:evidence")
    digest_id = _id(f"{identity}:digest")
    evidence_text = body
    observed_at = published_at or datetime(2026, 8, 20, 4, tzinfo=UTC)
    story_occurred_at = occurred_at or observed_at
    document_source_url = source_url or f"https://{identity}.example/articles/{identity}"
    document_title = title or f"Fixture {identity}"
    engine = create_database_engine(database_url)
    try:
        with Session(engine) as session, session.begin():
            session.execute(
                insert(CandidateRecord).values(
                    id=candidate_id,
                    title=f"Fixture {identity}",
                    canonical_url=document_source_url,
                    publisher=publisher,
                    discovered_at=observed_at,
                )
            )
            session.execute(
                insert(DocumentVersionRecord).values(
                    id=document_id,
                    candidate_id=candidate_id,
                    source_url=document_source_url,
                    title=document_title,
                    body=body,
                    content_hash=sha256(body.encode()).hexdigest(),
                    observed_at=observed_at,
                    published_at=observed_at,
                    published_at_raw=observed_at.isoformat(),
                    updated_at=None,
                    updated_at_raw=None,
                )
            )
            session.execute(
                insert(StoryRecord).values(
                    id=story_id,
                    primary_document_version_id=document_id,
                    stable_key=f"m4:{identity}",
                    headline=headline or f"Fixture Story {identity}",
                    occurred_at=story_occurred_at,
                    review_state=review_state.value,
                )
            )
            session.execute(
                insert(StoryPresentationRecord).values(
                    story_id=story_id,
                    summary=f"Reader summary for the accepted fixture Story named {identity}.",
                    why_it_matters=f"Why the accepted fixture Story named {identity} matters to readers.",
                    primary_topic=primary_topic.value,
                    secondary_topics=[],
                )
            )
            session.execute(
                insert(ClaimRecord).values(
                    id=claim_id,
                    story_id=story_id,
                    position=0,
                    text=f"Claim for {identity}: {body}",
                )
            )
            session.execute(
                insert(EvidenceSpanRecord).values(
                    id=evidence_id,
                    claim_id=claim_id,
                    document_version_id=document_id,
                    exact_text=evidence_text,
                    start_offset=0,
                    end_offset=len(evidence_text),
                    text_hash=sha256(evidence_text.encode()).hexdigest(),
                    role="primary",
                    relation="supports",
                )
            )
            session.execute(
                insert(DigestRecord).values(
                    id=digest_id,
                    stable_key=f"m4-digest:{identity}",
                    publication_date=observed_at.date(),
                    state=DigestState.DRAFT.value,
                    published_at=None,
                    introduction=None,
                    publication_contract=DigestPublicationContract.LEGACY_FIXTURE.value,
                    digest_plan_id=None,
                )
            )
            if public:
                session.execute(
                    insert(DigestStoryRecord).values(
                        digest_id=digest_id,
                        story_id=story_id,
                        position=0,
                    )
                )
                if digest_state is DigestState.PUBLISHED:
                    session.execute(
                        update(DigestRecord)
                        .where(DigestRecord.id == digest_id)
                        .values(state=DigestState.PUBLISHED.value, published_at=observed_at)
                    )
    finally:
        engine.dispose()
    return document_id, story_id, claim_id, evidence_id


def _domain_snapshot(database_url: str) -> tuple[tuple[object, ...], ...]:
    engine = create_database_engine(database_url)
    try:
        with Session(engine) as session:
            return tuple(
                tuple(session.execute(select(*record.__table__.columns)).all())
                for record in (
                    DocumentVersionRecord,
                    StoryRecord,
                    ClaimRecord,
                    EvidenceSpanRecord,
                )
            )
    finally:
        engine.dispose()


def test_runtime_profile_pins_the_approved_models_and_rebuild_contract() -> None:
    profile = load_accepted_knowledge_profile()

    assert profile.schema_version == "accepted-knowledge-retrieval.v1"
    assert profile.profile_id == "accepted-knowledge-minilm-mmarco-2026-08-19.v1"
    assert profile.embedding.model_id == (
        "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    )
    assert profile.embedding.source_repository == (
        "qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q"
    )
    assert profile.embedding.revision == "faf4aa4225822f3bc6376869cb1164e8e3feedd0"
    assert profile.embedding.artifact_sha256 == (
        "634d0f66c29dc934c8fa72b8a4fe91dd4d420a22f1d82a241058d4316e659a99"
    )
    assert profile.embedding.pooling == "mean"
    assert profile.embedding.normalization == "l2"
    assert profile.embedding.token_limit == 128
    assert profile.embedding.dimensions == 384
    assert profile.embedding.distance_metric == "cosine"
    assert profile.embedding.runtime_version == "fastembed==0.8.0"
    assert profile.embedding.rebuild_lifecycle == "new-profile-then-atomic-activate"

    assert profile.reranker.model_id == "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"
    assert profile.reranker.source_repository == profile.reranker.model_id
    assert profile.reranker.revision == "1427fd652930e4ba29e8149678df786c240d8825"
    assert profile.reranker.artifact_path == "onnx/model_quint8_avx2.onnx"
    assert profile.reranker.artifact_sha256 == (
        "6c2513767fb63d008a4377bef7a7a3555433d9436342bb53e35a3a72ffc52d4b"
    )
    assert profile.reranker.quantization == "avx2-uint8"
    assert profile.reranker.runtime_version == "fastembed==0.8.0"
    assert profile.retrieval.rerank_depth == 8
    assert profile.retrieval.final_top_k == 5
    assert profile.entity_policy == "technical-identifiers-v1"
    assert profile.fallback_channels == ("lexical", "exact_entity")


def test_model_artifact_gate_is_exact_offline_and_requires_avx2(tmp_path: Path) -> None:
    embedding_dir = tmp_path / "embedding"
    reranker_dir = tmp_path / "reranker"
    embedding_dir.mkdir(parents=True)
    (reranker_dir / "onnx").mkdir(parents=True)
    embedding_artifact = embedding_dir / "model_optimized.onnx"
    reranker_artifact = reranker_dir / "onnx" / "model_quint8_avx2.onnx"
    embedding_artifact.write_bytes(b"fixture embedding artifact")
    reranker_artifact.write_bytes(b"fixture reranker artifact")
    configuration = RetrievalModelConfiguration.from_environment(
        {
            "AI_INTEL_EMBEDDING_MODEL_DIR": str(embedding_dir),
            "AI_INTEL_RERANKER_MODEL_DIR": str(reranker_dir),
            "AI_INTEL_RETRIEVAL_THREADS": "3",
        }
    )
    verified_snapshots: list[tuple[Path, str, str, tuple[str, ...]]] = []

    def verify_snapshot(
        model_dir: Path,
        source_repository: str,
        revision: str,
        metadata_files: tuple[str, ...],
    ) -> None:
        verified_snapshots.append((model_dir, source_repository, revision, metadata_files))

    with pytest.raises(AcceptedKnowledgeConfigurationError, match="SHA-256"):
        validate_approved_model_artifacts(
            configuration,
            runtime_version="0.8.0",
            cpu_features=frozenset({"avx2"}),
            snapshot_verifier=verify_snapshot,
        )

    digests = {
        embedding_artifact: APPROVED_EMBEDDING_SHA256,
        reranker_artifact: APPROVED_RERANKER_SHA256,
    }
    check = validate_approved_model_artifacts(
        configuration,
        runtime_version="0.8.0",
        cpu_features=frozenset({"avx2"}),
        artifact_digest=lambda path: digests[path],
        snapshot_verifier=verify_snapshot,
    )

    assert configuration.threads == 3
    assert check.ready is True
    assert check.runtime_version == "fastembed==0.8.0"
    assert check.embedding.artifact_path == embedding_artifact
    assert check.embedding.artifact_sha256 == APPROVED_EMBEDDING_SHA256
    assert check.reranker.artifact_path == reranker_artifact
    assert check.reranker.artifact_sha256 == APPROVED_RERANKER_SHA256
    assert check.reranker.cpu_feature == "avx2"
    assert verified_snapshots[-2:] == [
        (
            embedding_dir,
            "qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q",
            APPROVED_EMBEDDING_REVISION,
            (
                "config.json",
                "special_tokens_map.json",
                "tokenizer.json",
                "tokenizer_config.json",
            ),
        ),
        (
            reranker_dir,
            APPROVED_RERANKER_MODEL_ID,
            APPROVED_RERANKER_REVISION,
            (
                "config.json",
                "special_tokens_map.json",
                "tokenizer.json",
                "tokenizer_config.json",
            ),
        ),
    ]
    with pytest.raises(AcceptedKnowledgeConfigurationError, match="AVX2"):
        validate_approved_model_artifacts(
            configuration,
            runtime_version="0.8.0",
            cpu_features=frozenset(),
            artifact_digest=lambda path: digests[path],
            snapshot_verifier=verify_snapshot,
        )
    with pytest.raises(AcceptedKnowledgeConfigurationError, match="runtime"):
        validate_approved_model_artifacts(
            configuration,
            runtime_version="0.8.1",
            cpu_features=frozenset({"avx2"}),
            artifact_digest=lambda path: digests[path],
            snapshot_verifier=verify_snapshot,
        )

    def reject_snapshot(
        model_dir: Path,
        source_repository: str,
        revision: str,
        metadata_files: tuple[str, ...],
    ) -> None:
        del model_dir, source_repository, revision, metadata_files
        raise AcceptedKnowledgeConfigurationError("snapshot revision mismatch")

    with pytest.raises(AcceptedKnowledgeConfigurationError, match="snapshot revision"):
        validate_approved_model_artifacts(
            configuration,
            runtime_version="0.8.0",
            cpu_features=frozenset({"avx2"}),
            artifact_digest=lambda path: digests[path],
            snapshot_verifier=reject_snapshot,
        )


def test_git_snapshot_checks_scope_safe_directory_to_the_model_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[tuple[str, ...]] = []

    def run(command: Sequence[str], **kwargs: object):
        del kwargs
        copied = tuple(command)
        commands.append(copied)
        return type("Completed", (), {"returncode": 0, "stdout": "identity\n"})()

    monkeypatch.setattr("ai_intel_agent.accepted_knowledge.subprocess.run", run)

    assert _run_git_snapshot_check(tmp_path, "rev-parse", "HEAD") == "identity"
    assert commands == [
        (
            "git",
            "-c",
            f"safe.directory={tmp_path.resolve()}",
            "-C",
            str(tmp_path.resolve()),
            "rev-parse",
            "HEAD",
        )
    ]


def test_fastembed_adapters_enforce_normalization_token_limit_and_fixed_rerank_batch() -> None:
    embedding_model = FakeFastEmbedEmbeddingModel()
    reranker_model = FakeFastEmbedRerankerModel()
    embedding = FastEmbedEmbeddingBackend(embedding_model)
    reranker = FastEmbedRerankerBackend(reranker_model)
    long_query = " ".join(f"query-token-{index}" for index in range(180))

    document_vectors = embedding.embed_documents(("first document", "second document"))
    query_vector = embedding.embed_query(long_query)
    scores = reranker.rerank("fixed query", tuple(f"document {index}" for index in range(8)))

    assert document_vectors[0][0] == 1.0
    assert query_vector[1] == 1.0
    assert len(embedding_model.query_calls[0][0][0].split()) == 128
    assert embedding_model.passage_calls == [(("first document", "second document"), 32)]
    assert scores == tuple(float(index) for index in range(8))
    assert reranker_model.calls == [
        ("fixed query", tuple(f"document {index}" for index in range(8)), 8)
    ]
    with pytest.raises(ValueError, match="token limit"):
        embedding.embed_documents((long_query,))


def test_production_bundle_prepares_exact_sha_hybrid_acceptance_without_model_substitution() -> (
    None
):
    project_root = Path(__file__).resolve().parents[1]
    dockerfile = (project_root / "deploy/m1/production.Dockerfile").read_text(encoding="utf-8")
    compose = (project_root / "deploy/m1/production.compose.yml").read_text(encoding="utf-8")
    release_environment = (project_root / "deploy/m1/release.env.example").read_text(
        encoding="utf-8"
    )
    operator = (project_root / "deploy/m1/operate.sh").read_text(encoding="utf-8")

    assert "--extra retrieval" in dockerfile
    assert "apt-get install --yes --no-install-recommends git" in dockerfile
    assert "AI_INTEL_EMBEDDING_MODEL_DIR" in compose
    assert "AI_INTEL_RERANKER_MODEL_DIR" in compose
    assert "AI_INTEL_RETRIEVAL_THREADS" in compose
    assert compose.count(":/opt/ai-ledger/models/embedding:ro") == 2
    assert compose.count(":/opt/ai-ledger/models/reranker:ro") == 2
    assert 'command: ["operator", "retrieval", "index", "--complete", "--production"]' in compose
    assert all(
        f"{key}=" in release_environment
        for key in (
            "AI_INTEL_EMBEDDING_MODEL_DIR",
            "AI_INTEL_RERANKER_MODEL_DIR",
            "AI_INTEL_RETRIEVAL_THREADS",
        )
    )
    assert '"retrieval-rebuild")' in operator
    assert '"accept-retrieval")' in operator
    assert "operator retrieval artifacts" in operator
    assert "http://127.0.0.1:8000/health/ready" in operator
    assert "operator retrieval status --production --require-hybrid" in operator
    assert '"p50_ms"' in operator and '"p95_ms"' in operator
    assert 'service_rss_output="$(docker top "$web_container" -eo pid,rss)"' in operator
    assert "NR > 1 { total += $2; rows += 1 }" in operator
    assert "if (rows == 0) exit 1" in operator
    assert 'validate_release "$previous_release"' in operator
    assert 'validate_image_revision "$previous_release"' in operator


def test_chunks_are_type_and_token_aware_without_crossing_document_versions() -> None:
    profile = load_accepted_knowledge_profile()
    release_body = " ".join(f"release-token-{index}" for index in range(110))
    paper_body = " ".join(f"paper-token-{index}" for index in range(110))
    release_id = uuid5(NAMESPACE_URL, "m4-chunk:release")
    paper_id = uuid5(NAMESPACE_URL, "m4-chunk:paper")

    release_chunks = build_document_chunks(
        ChunkSource(
            document_version_id=release_id,
            document_type="release",
            language="en",
            text=release_body,
        ),
        profile,
    )
    paper_chunks = build_document_chunks(
        ChunkSource(
            document_version_id=paper_id,
            document_type="paper",
            language="en",
            text=paper_body,
        ),
        profile,
    )

    assert len(release_chunks) == 2
    assert len(paper_chunks) == 1
    assert {chunk.document_version_id for chunk in release_chunks} == {release_id}
    assert {chunk.document_version_id for chunk in paper_chunks} == {paper_id}
    for chunks, body, document_type in (
        (release_chunks, release_body, "release"),
        (paper_chunks, paper_body, "paper"),
    ):
        window = profile.chunking.document_types[document_type]
        assert chunks[0].start_offset == 0
        assert chunks[-1].end_offset == len(body)
        assert all(chunk.text == body[chunk.start_offset : chunk.end_offset] for chunk in chunks)
        assert all(chunk.token_count <= window.max_tokens for chunk in chunks)
        assert tuple(chunk.ordinal for chunk in chunks) == tuple(range(len(chunks)))


@pytest.mark.postgres
def test_incremental_and_complete_rebuilds_are_profile_isolated_and_domain_immutable(
    m4_database_url: str,
) -> None:
    first_document, *_ = _persist_knowledge_story(
        m4_database_url,
        identity="first",
        body="Qwen3-Coder-480B-A35B-Instruct ships a multilingual coding model.",
    )
    before_first_build = _domain_snapshot(m4_database_url)
    engine = create_database_engine(m4_database_url)
    try:
        indexer = AcceptedKnowledgeIndexer(engine, embedding=FakeEmbeddingBackend())
        first_build = indexer.rebuild()
        repeated_incremental = indexer.incremental()
    finally:
        engine.dispose()

    assert first_build.documents_indexed == 1
    assert first_build.chunks_created == 1
    assert repeated_incremental.index_id == first_build.index_id
    assert repeated_incremental.documents_indexed == 0
    assert repeated_incremental.chunks_created == 0
    assert _domain_snapshot(m4_database_url) == before_first_build

    second_document, *_ = _persist_knowledge_story(
        m4_database_url,
        identity="second",
        body="MiniLM represents bilingual product announcements in one semantic space.",
    )
    before_incremental = _domain_snapshot(m4_database_url)
    engine = create_database_engine(m4_database_url)
    try:
        indexer = AcceptedKnowledgeIndexer(engine, embedding=FakeEmbeddingBackend())
        incremental = indexer.incremental()
        second_build = indexer.rebuild()
        with Session(engine) as session:
            indexes = tuple(
                session.scalars(
                    select(RetrievalIndexRecord).order_by(RetrievalIndexRecord.created_at)
                )
            )
            active_index_ids = tuple(index.id for index in indexes if index.state == "active")
            active_document_ids = set(
                session.scalars(
                    select(RetrievalChunkRecord.document_version_id).where(
                        RetrievalChunkRecord.index_id == second_build.index_id
                    )
                )
            )
            active_profile_count = session.scalar(
                select(func.count(func.distinct(RetrievalChunkRecord.index_id))).where(
                    RetrievalChunkRecord.index_id == second_build.index_id
                )
            )
    finally:
        engine.dispose()

    assert incremental.index_id == first_build.index_id
    assert incremental.documents_indexed == 1
    assert second_build.index_id != first_build.index_id
    assert active_index_ids == (second_build.index_id,)
    assert {index.state for index in indexes} == {"active", "retired"}
    assert active_document_ids == {first_document, second_document}
    assert active_profile_count == 1
    assert _domain_snapshot(m4_database_url) == before_incremental


@pytest.mark.postgres
def test_indexer_applies_document_type_windows_only_to_public_accepted_knowledge(
    m4_database_url: str,
) -> None:
    release_body = " ".join(f"release-token-{index}" for index in range(110))
    paper_body = " ".join(f"paper-token-{index}" for index in range(110))
    release_document, *_ = _persist_knowledge_story(
        m4_database_url,
        identity="typed-release",
        body=release_body,
        source_url="https://vendor.example/releases/v4.2",
        title="Version 4.2 release notes",
    )
    paper_document, *_ = _persist_knowledge_story(
        m4_database_url,
        identity="typed-paper",
        body=paper_body,
        source_url="https://arxiv.org/abs/2608.00001",
        title="A retrieval research paper",
    )
    private_document, *_ = _persist_knowledge_story(
        m4_database_url,
        identity="typed-private",
        body="private-document must not enter the accepted-knowledge index",
        public=False,
    )
    engine = create_database_engine(m4_database_url)
    try:
        build = AcceptedKnowledgeIndexer(engine, embedding=FakeEmbeddingBackend()).rebuild()
        with Session(engine) as session:
            chunks = tuple(
                session.scalars(
                    select(RetrievalChunkRecord)
                    .where(RetrievalChunkRecord.index_id == build.index_id)
                    .order_by(
                        RetrievalChunkRecord.document_version_id,
                        RetrievalChunkRecord.ordinal,
                    )
                )
            )
    finally:
        engine.dispose()

    chunks_by_document = {
        document_id: tuple(chunk for chunk in chunks if chunk.document_version_id == document_id)
        for document_id in (release_document, paper_document, private_document)
    }
    assert tuple(chunk.document_type for chunk in chunks_by_document[release_document]) == (
        "release",
        "release",
    )
    assert tuple(chunk.document_type for chunk in chunks_by_document[paper_document]) == ("paper",)
    assert chunks_by_document[private_document] == ()


@pytest.mark.postgres
def test_retrieval_never_mixes_an_incompatible_active_profile(
    m4_database_url: str,
) -> None:
    _, story_id, _, _ = _persist_knowledge_story(
        m4_database_url,
        identity="profile-mismatch",
        body="profile-mismatch CUDA-12.8 remains available through accepted FTS",
    )
    engine = create_database_engine(m4_database_url)
    try:
        build = AcceptedKnowledgeIndexer(engine, embedding=FakeEmbeddingBackend()).rebuild()
        with Session(engine) as session, session.begin():
            session.execute(
                update(RetrievalIndexRecord)
                .where(RetrievalIndexRecord.id == build.index_id)
                .values(profile_id="incompatible-profile", profile_sha256="0" * 64)
            )
        result = AcceptedKnowledgeRetrieval(
            engine,
            embedding=FakeEmbeddingBackend(),
        ).retrieve(RetrievalQuery(text="profile-mismatch CUDA-12.8"))
    finally:
        engine.dispose()

    assert result.hits[0].story_id == story_id
    assert result.trace.semantic == ()
    assert result.trace.entity == ()
    assert all(candidate.chunk_id is None for candidate in result.trace.lexical)
    assert ("index", "profile-incompatible") in {
        (fault.stage, fault.code) for fault in result.trace.faults
    }


@pytest.mark.postgres
def test_required_embedding_rebuild_rolls_back_without_replacing_active_generation(
    m4_database_url: str,
) -> None:
    _persist_knowledge_story(
        m4_database_url,
        identity="required-embedding",
        body="required-embedding keeps the active semantic generation intact",
    )
    engine = create_database_engine(m4_database_url)
    try:
        active = AcceptedKnowledgeIndexer(
            engine,
            embedding=FakeEmbeddingBackend(),
        ).rebuild()
        with pytest.raises(
            AcceptedKnowledgeConfigurationError,
            match="required Embeddings",
        ):
            AcceptedKnowledgeIndexer(
                engine,
                embedding=UnavailableEmbeddingBackend(),
                require_embeddings=True,
            ).rebuild()
        with Session(engine) as session:
            indexes = tuple(session.scalars(select(RetrievalIndexRecord)))
    finally:
        engine.dispose()

    assert tuple(index.id for index in indexes) == (active.index_id,)
    assert indexes[0].state == "active"
    assert indexes[0].embeddings_indexed > 0


@pytest.mark.postgres
def test_partial_active_generation_skips_semantic_and_keeps_full_fallback_corpus(
    m4_database_url: str,
) -> None:
    _persist_knowledge_story(
        m4_database_url,
        identity="embedded-before-fault",
        body="An older accepted document has a complete semantic vector.",
    )
    engine = create_database_engine(m4_database_url)
    try:
        AcceptedKnowledgeIndexer(engine, embedding=FakeEmbeddingBackend()).rebuild()
        _, _, _, fallback_evidence_id = _persist_knowledge_story(
            m4_database_url,
            identity="fallback-after-fault",
            body="pgvector fallback-after-fault remains lexically available.",
        )
        incremental = AcceptedKnowledgeIndexer(
            engine,
            embedding=UnavailableEmbeddingBackend(),
        ).incremental()
        result = AcceptedKnowledgeRetrieval(
            engine,
            embedding=FakeEmbeddingBackend(),
        ).retrieve(RetrievalQuery(text="pgvector fallback-after-fault"))
    finally:
        engine.dispose()

    assert incremental.fault_code == "embedding-unavailable"
    assert result.trace.semantic == ()
    assert fallback_evidence_id in {
        candidate.evidence_span_id for candidate in result.trace.lexical
    }
    assert fallback_evidence_id in {candidate.evidence_span_id for candidate in result.trace.entity}
    assert ("embedding", "embedding-index-incomplete") in {
        (fault.stage, fault.code) for fault in result.trace.faults
    }


@pytest.mark.postgres
def test_hybrid_retrieval_recovers_an_fts_miss_and_records_every_ranking_stage(
    m4_database_url: str,
) -> None:
    _, target_story_id, target_claim_id, target_evidence_id = _persist_knowledge_story(
        m4_database_url,
        identity="semantic-target",
        body="该系统让中文与英文产品公告在跨语言部署场景下保持语义一致。",
    )
    for index in range(9):
        _persist_knowledge_story(
            m4_database_url,
            identity=f"semantic-distractor-{index}",
            body=f"Unrelated retrieval fixture number {index} discusses storage policy.",
        )
    embedding = SemanticEmbeddingBackend()
    reranker = RecordingReranker()
    engine = create_database_engine(m4_database_url)
    try:
        AcceptedKnowledgeIndexer(engine, embedding=embedding).rebuild()
        result = AcceptedKnowledgeRetrieval(
            engine,
            embedding=embedding,
            reranker=reranker,
        ).retrieve(RetrievalQuery(text="cross language deployment"))
    finally:
        engine.dispose()

    assert target_evidence_id not in {
        candidate.evidence_span_id for candidate in result.trace.lexical
    }
    assert target_evidence_id in {candidate.evidence_span_id for candidate in result.trace.semantic}
    assert [candidate.stage for candidate in result.trace.lexical] == ["lexical"] * len(
        result.trace.lexical
    )
    assert [candidate.stage for candidate in result.trace.semantic] == ["semantic"] * len(
        result.trace.semantic
    )
    assert [candidate.stage for candidate in result.trace.entity] == ["entity"] * len(
        result.trace.entity
    )
    assert [candidate.stage for candidate in result.trace.fusion] == ["fusion"] * len(
        result.trace.fusion
    )
    assert [candidate.stage for candidate in result.trace.final] == ["final"] * len(
        result.trace.final
    )
    assert len(reranker.calls) == 1
    assert len(reranker.calls[0][1]) == 8
    assert len(result.hits) == 5
    target = result.hits[0]
    assert (target.story_id, target.claim_id, target.evidence_span_id) == (
        target_story_id,
        target_claim_id,
        target_evidence_id,
    )
    assert all(hit.chunk_id != hit.evidence_span_id for hit in result.hits)


@pytest.mark.postgres
def test_exact_technical_entity_and_all_supported_filters_share_one_boundary(
    m4_database_url: str,
) -> None:
    published_at = datetime(2026, 8, 19, 8, tzinfo=UTC)
    occurred_at = datetime(2026, 8, 19, 9, tzinfo=UTC)
    _, target_story_id, _, target_evidence_id = _persist_knowledge_story(
        m4_database_url,
        identity="entity-filter-target",
        body="The release requires CUDA-12.8 for its accelerated inference path.",
        publisher="NVIDIA Technical Blog",
        published_at=published_at,
        occurred_at=occurred_at,
        primary_topic=Topic.INDUSTRY_AND_INFRASTRUCTURE,
    )
    _persist_knowledge_story(
        m4_database_url,
        identity="entity-filter-distractor",
        body="A separate CUDA-12.8 note belongs to another publisher and topic.",
        publisher="Distractor Publisher",
        published_at=datetime(2026, 8, 18, 8, tzinfo=UTC),
        occurred_at=datetime(2026, 8, 18, 9, tzinfo=UTC),
        primary_topic=Topic.RESEARCH,
    )
    embedding = FakeEmbeddingBackend()
    engine = create_database_engine(m4_database_url)
    try:
        AcceptedKnowledgeIndexer(engine, embedding=embedding).rebuild()
        retrieval = AcceptedKnowledgeRetrieval(engine, embedding=embedding)
        matching = retrieval.retrieve(
            RetrievalQuery(
                text="CUDA-12.8",
                filters=RetrievalFilters(
                    publisher="NVIDIA Technical Blog",
                    topic=Topic.INDUSTRY_AND_INFRASTRUCTURE,
                    publication_date=date(2026, 8, 19),
                    occurred_from=datetime(2026, 8, 19, 8, tzinfo=UTC),
                    occurred_to=datetime(2026, 8, 20, 0, tzinfo=UTC),
                ),
            )
        )
        mismatches = (
            RetrievalFilters(publisher="Wrong Publisher"),
            RetrievalFilters(topic=Topic.RESEARCH, publisher="NVIDIA Technical Blog"),
            RetrievalFilters(
                publication_date=date(2026, 8, 18),
                publisher="NVIDIA Technical Blog",
            ),
            RetrievalFilters(
                occurred_from=datetime(2026, 8, 20, tzinfo=UTC),
                publisher="NVIDIA Technical Blog",
            ),
            RetrievalFilters(
                occurred_to=datetime(2026, 8, 19, 8, tzinfo=UTC),
                publisher="NVIDIA Technical Blog",
            ),
        )
        mismatch_results = tuple(
            retrieval.retrieve(RetrievalQuery(text="CUDA-12.8", filters=filters))
            for filters in mismatches
        )
    finally:
        engine.dispose()

    assert tuple(hit.story_id for hit in matching.hits) == (target_story_id,)
    assert target_evidence_id in {candidate.evidence_span_id for candidate in matching.trace.entity}
    assert all(result.hits == () for result in mismatch_results)


@pytest.mark.postgres
def test_model_faults_preserve_fused_order_and_resolvable_public_citations(
    m4_database_url: str,
) -> None:
    for index in range(3):
        _persist_knowledge_story(
            m4_database_url,
            identity=f"fallback-{index}",
            body=f"CUDA-12.8 fallback Evidence fixture number {index}.",
        )
    embedding = UnavailableEmbeddingBackend()
    engine = create_database_engine(m4_database_url)
    try:
        build = AcceptedKnowledgeIndexer(engine, embedding=embedding).rebuild()
        result = AcceptedKnowledgeRetrieval(
            engine,
            embedding=embedding,
            reranker=FailingReranker(),
        ).retrieve(RetrievalQuery(text="CUDA-12.8 fallback"))
        with Session(engine) as session:
            states = {
                state.stage: state for state in session.scalars(select(RetrievalRuntimeStateRecord))
            }
            resolved = tuple(
                (
                    session.get(StoryRecord, hit.story_id),
                    session.get(ClaimRecord, hit.claim_id),
                    session.get(EvidenceSpanRecord, hit.evidence_span_id),
                )
                for hit in result.hits
            )
    finally:
        engine.dispose()

    assert build.fault_code == "embedding-unavailable"
    assert build.embeddings_created == 0
    assert result.hits
    assert result.trace.lexical
    assert all(candidate.chunk_id is not None for candidate in result.trace.lexical)
    assert tuple(hit.evidence_span_id for hit in result.hits) == tuple(
        candidate.evidence_span_id for candidate in result.trace.fusion[:5]
    )
    assert {(fault.stage, fault.code) for fault in result.trace.faults} == {
        ("embedding", "embedding-unavailable"),
        ("reranker", "reranker-failed"),
    }
    assert states["embedding"].state == "unavailable"
    assert states["embedding"].fault_code == "embedding-unavailable"
    assert states["reranker"].state == "degraded"
    assert states["reranker"].fault_code == "reranker-failed"
    assert all(
        story is not None and claim is not None and evidence is not None
        for story, claim, evidence in resolved
    )
    assert all(hit.chunk_id != hit.evidence_span_id for hit in result.hits)


@pytest.mark.postgres
def test_only_accepted_still_public_knowledge_can_support_retrieval(
    m4_database_url: str,
) -> None:
    _, accepted_story_id, _, _ = _persist_knowledge_story(
        m4_database_url,
        identity="visibility-accepted",
        body="visibility-guard evidence remains public and accepted",
    )
    _persist_knowledge_story(
        m4_database_url,
        identity="visibility-rejected",
        body="visibility-guard evidence was rejected",
        review_state=StoryReviewState.REJECTED,
        public=False,
    )
    _persist_knowledge_story(
        m4_database_url,
        identity="visibility-draft",
        body="visibility-guard evidence belongs only to a draft Digest",
        digest_state=DigestState.DRAFT,
    )
    _persist_knowledge_story(
        m4_database_url,
        identity="visibility-private",
        body="visibility-guard evidence has never been publicly published",
        public=False,
    )
    _, withdrawn_story_id, _, _ = _persist_knowledge_story(
        m4_database_url,
        identity="visibility-withdrawn",
        body="visibility-guard evidence was later withdrawn",
    )
    engine = create_database_engine(m4_database_url)
    try:
        with Session(engine) as session, session.begin():
            withdrawn_digest_id = session.scalar(
                select(DigestRecord.id)
                .join(
                    DigestStoryRecord,
                    DigestStoryRecord.digest_id == DigestRecord.id,
                )
                .where(DigestStoryRecord.story_id == withdrawn_story_id)
            )
            session.add(
                DigestWithdrawalRecord(
                    digest_id=withdrawn_digest_id,
                    actor_identifier="m4-test-operator",
                    reason="Withdrawn fixture must no longer support public answers.",
                    withdrawn_at=datetime(2026, 8, 21, tzinfo=UTC),
                )
            )
        result = AcceptedKnowledgeRetrieval(engine).retrieve(
            RetrievalQuery(text="visibility-guard")
        )
    finally:
        engine.dispose()

    assert tuple(hit.story_id for hit in result.hits) == (accepted_story_id,)


@pytest.mark.postgres
def test_browse_and_research_use_the_same_hybrid_retrieval_operation(
    m4_database_url: str,
) -> None:
    _, target_story_id, _, target_evidence_id = _persist_knowledge_story(
        m4_database_url,
        identity="shared-operation-target",
        body="中文公告说明跨语言部署已经通过一致性验证。",
    )
    for index in range(2):
        _persist_knowledge_story(
            m4_database_url,
            identity=f"shared-operation-distractor-{index}",
            body=f"Unrelated fixture {index} discusses a storage maintenance window.",
        )
    embedding = SemanticEmbeddingBackend()
    reranker = RecordingReranker()
    provider = CitingFirstResearchProvider()
    engine = create_database_engine(m4_database_url)
    try:
        AcceptedKnowledgeIndexer(engine, embedding=embedding).rebuild()
        operation = RecordingAcceptedKnowledgeOperation(
            AcceptedKnowledgeRetrieval(
                engine,
                embedding=embedding,
                reranker=reranker,
            )
        )
        with TestClient(
            create_app(
                m4_database_url,
                research_provider=provider,
                accepted_knowledge_retrieval=operation,
            )
        ) as client:
            browse = client.get("/browse", params={"q": "cross language deployment"})
            research = client.post(
                "/research/answer",
                json={"question": "cross language deployment"},
            )
    finally:
        engine.dispose()

    assert browse.status_code == 200
    assert "Fixture Story shared-operation-target" in browse.text
    assert research.status_code == 200
    assert "answer.delta" in research.text
    assert str(target_evidence_id) in research.text
    assert [query.text for query in operation.queries] == [
        "cross language deployment",
        "cross language deployment",
    ]
    assert len(provider.calls) == 1
    assert provider.calls[0].evidence[0].story_id == target_story_id


def test_hybrid_health_requires_a_complete_current_exact_model_generation() -> None:
    active_id = _id("health-active-index")
    observed_at = datetime(2026, 8, 21, 8, tzinfo=UTC)
    stages = (
        RetrievalRuntimeStageStatus(
            stage="index",
            index_id=active_id,
            state="ready",
            model_id=None,
            revision=None,
            artifact_sha256=None,
            fault_code=None,
            updated_at=observed_at,
        ),
        RetrievalRuntimeStageStatus(
            stage="embedding",
            index_id=active_id,
            state="ready",
            model_id=APPROVED_EMBEDDING_MODEL_ID,
            revision=APPROVED_EMBEDDING_REVISION,
            artifact_sha256=APPROVED_EMBEDDING_SHA256,
            fault_code=None,
            updated_at=observed_at,
        ),
        RetrievalRuntimeStageStatus(
            stage="reranker",
            index_id=active_id,
            state="ready",
            model_id=APPROVED_RERANKER_MODEL_ID,
            revision=APPROVED_RERANKER_REVISION,
            artifact_sha256=APPROVED_RERANKER_SHA256,
            fault_code=None,
            updated_at=observed_at,
        ),
    )
    ready = RetrievalHealthSnapshot(
        active_index_id=active_id,
        profile_id=APPROVED_PROFILE_ID,
        profile_sha256=APPROVED_PROFILE_SHA256,
        documents_indexed=1,
        chunks_indexed=2,
        embeddings_indexed=2,
        index_fault_code=None,
        stages=stages,
    )

    assert ready.hybrid_ready is True
    assert replace(ready, embeddings_indexed=1).hybrid_ready is False
    assert replace(ready, profile_sha256="0" * 64).hybrid_ready is False
    assert replace(ready, index_fault_code="embedding-unavailable").hybrid_ready is False
    stale_reranker = replace(stages[-1], index_id=_id("health-retired-index"))
    assert replace(ready, stages=stages[:-1] + (stale_reranker,)).hybrid_ready is False


@pytest.mark.postgres
def test_startup_model_faults_replace_stale_ready_runtime_state(
    m4_database_url: str,
) -> None:
    _persist_knowledge_story(
        m4_database_url,
        identity="startup-fault-state",
        body="CUDA startup fault state is accepted public Evidence.",
    )
    embedding = FakeEmbeddingBackend()
    engine = create_database_engine(m4_database_url)
    try:
        AcceptedKnowledgeIndexer(engine, embedding=embedding).rebuild()
        AcceptedKnowledgeRetrieval(
            engine,
            embedding=embedding,
            reranker=RecordingReranker(),
        ).retrieve(RetrievalQuery(text="CUDA startup fault state"))
        assert retrieval_health_snapshot(engine).hybrid_ready is True

        record_retrieval_backend_startup_state(
            engine,
            ApprovedRetrievalBackends(
                embedding=None,
                reranker=None,
                faults=(
                    RetrievalBackendFault("embedding", "embedding-unavailable"),
                    RetrievalBackendFault("reranker", "reranker-unavailable"),
                ),
            ),
        )
        snapshot = retrieval_health_snapshot(engine)
    finally:
        engine.dispose()

    assert snapshot.hybrid_ready is False
    assert {
        (stage.stage, stage.index_id, stage.state, stage.fault_code)
        for stage in snapshot.stages
        if stage.stage in {"embedding", "reranker"}
    } == {
        ("embedding", snapshot.active_index_id, "unavailable", "embedding-unavailable"),
        ("reranker", snapshot.active_index_id, "unavailable", "reranker-unavailable"),
    }


def test_migration_uses_generation_scoped_exact_vector_scans() -> None:
    project_root = Path(__file__).resolve().parents[1]
    migration = (project_root / "alembic/versions/0011_m4_hybrid_retrieval.py").read_text(
        encoding="utf-8"
    )

    assert "USING hnsw" not in migration
    assert '"ix_retrieval_chunks_index_id"' in migration


@pytest.mark.postgres
def test_active_chunk_fts_keeps_the_accepted_projection_lexical_boundary(
    m4_database_url: str,
) -> None:
    _, _, _, evidence_id = _persist_knowledge_story(
        m4_database_url,
        identity="lexical-projection",
        body="The accepted evidence deliberately omits the headline-only search term.",
        headline="Zygomorphic runtime announcement",
    )
    engine = create_database_engine(m4_database_url)
    try:
        AcceptedKnowledgeIndexer(engine, embedding=FakeEmbeddingBackend()).rebuild()
        result = AcceptedKnowledgeRetrieval(engine).retrieve(RetrievalQuery(text="Zygomorphic"))
    finally:
        engine.dispose()

    assert evidence_id in {candidate.evidence_span_id for candidate in result.trace.lexical}
    assert tuple(hit.evidence_span_id for hit in result.hits) == (evidence_id,)


@pytest.mark.postgres
def test_semantic_channel_collapses_overlapping_chunks_before_candidate_limit(
    m4_database_url: str,
) -> None:
    _, _, _, dominant_evidence_id = _persist_knowledge_story(
        m4_database_url,
        identity="dominant-evidence",
        body=" ".join("dominant" for _ in range(1100)),
    )
    _, _, _, secondary_evidence_id = _persist_knowledge_story(
        m4_database_url,
        identity="secondary-evidence",
        body="A secondary accepted semantic target remains eligible after Evidence collapse.",
    )
    embedding = DuplicateDominatingEmbeddingBackend()
    engine = create_database_engine(m4_database_url)
    try:
        AcceptedKnowledgeIndexer(engine, embedding=embedding).rebuild()
        result = AcceptedKnowledgeRetrieval(engine, embedding=embedding).retrieve(
            RetrievalQuery(text="semantic-only-query")
        )
    finally:
        engine.dispose()

    semantic_evidence_ids = tuple(candidate.evidence_span_id for candidate in result.trace.semantic)
    assert semantic_evidence_ids.count(dominant_evidence_id) == 1
    assert secondary_evidence_id in semantic_evidence_ids


@pytest.mark.postgres
def test_plain_technical_names_enter_the_exact_entity_channel(
    m4_database_url: str,
) -> None:
    _, _, _, evidence_id = _persist_knowledge_story(
        m4_database_url,
        identity="plain-entities",
        body="CUDA uses AVX while pgvector and fastembed serve the retrieval runtime.",
    )
    engine = create_database_engine(m4_database_url)
    try:
        AcceptedKnowledgeIndexer(engine, embedding=None).rebuild()
        retrieval = AcceptedKnowledgeRetrieval(engine)
        results = tuple(
            retrieval.retrieve(RetrievalQuery(text=entity))
            for entity in ("CUDA", "AVX", "pgvector", "fastembed")
        )
    finally:
        engine.dispose()

    assert all(
        evidence_id in {candidate.evidence_span_id for candidate in result.trace.entity}
        for result in results
    )


@pytest.mark.postgres
def test_blank_browse_catalog_is_not_capped_by_evidence_candidate_depth(
    m4_database_url: str,
) -> None:
    story_ids = {
        _persist_knowledge_story(
            m4_database_url,
            identity=f"catalog-{index}",
            body=f"Accepted Browse catalog Evidence {index}.",
        )[1]
        for index in range(13)
    }
    engine = create_database_engine(m4_database_url)
    try:
        result = AcceptedKnowledgeRetrieval(engine).retrieve(RetrievalQuery(text=""))
    finally:
        engine.dispose()

    assert set(result.matching_story_ids) == story_ids


@pytest.mark.postgres
def test_operator_cli_rebuilds_model_free_and_exposes_degraded_health(
    m4_database_url: str,
) -> None:
    _persist_knowledge_story(
        m4_database_url,
        identity="operator-index",
        body="operator-index CUDA-12.8 evidence remains available without models",
    )
    environment = {"AI_INTEL_DATABASE_URL": m4_database_url}

    rebuilt = runner.invoke(
        app,
        ["operator", "retrieval", "index", "--complete"],
        env=environment,
    )
    queried = runner.invoke(
        app,
        ["operator", "retrieval", "query", "operator-index CUDA-12.8"],
        env=environment,
    )
    status = runner.invoke(
        app,
        ["operator", "retrieval", "status"],
        env=environment,
    )
    strict_status = runner.invoke(
        app,
        ["operator", "retrieval", "status", "--require-hybrid"],
        env=environment,
    )

    assert rebuilt.exit_code == 0, rebuilt.output
    rebuilt_payload = json.loads(rebuilt.output)
    assert rebuilt_payload["mode"] == "complete"
    assert rebuilt_payload["documents_indexed"] == 1
    assert rebuilt_payload["embeddings_created"] == 0
    assert rebuilt_payload["fault_code"] == "embedding-unavailable"
    assert queried.exit_code == 0, queried.output
    query_payload = json.loads(queried.output)
    assert query_payload["hits"]
    assert set(query_payload["trace"]) == {
        "lexical",
        "semantic",
        "entity",
        "fusion",
        "final",
        "faults",
    }
    assert query_payload["trace"]["entity"]
    assert status.exit_code == 0, status.output
    status_payload = json.loads(status.output)
    assert status_payload["profile_id"] == ("accepted-knowledge-minilm-mmarco-2026-08-19.v1")
    assert status_payload["hybrid_ready"] is False
    assert strict_status.exit_code != 0
    assert "Hybrid Retrieval is not ready" in strict_status.output


@pytest.mark.postgres
def test_0010_to_0011_rehearsal_preserves_predecessor_state_and_builds_fallback_index() -> None:
    name = f"ai_intel_m4_rehearsal_{_id(os.urandom(8).hex).hex}"
    data_root = os.getenv("PG0_TEST_DATA_ROOT")
    data_dir = str(Path(data_root) / name) if data_root else None
    server = Pg0(name=name, data_dir=data_dir)
    server.start()
    try:
        _upgrade_database_to(server.uri, "0010")
        _, story_id, claim_id, evidence_id = _persist_knowledge_story(
            server.uri,
            identity="migration-predecessor",
            body="migration-predecessor CUDA-12.8 accepted Evidence",
        )
        before = _domain_snapshot(server.uri)

        _upgrade_database_to(server.uri, "head")
        engine = create_database_engine(server.uri)
        try:
            build = AcceptedKnowledgeIndexer(engine, embedding=None).rebuild()
            result = AcceptedKnowledgeRetrieval(engine).retrieve(
                RetrievalQuery(text="migration-predecessor CUDA-12.8")
            )
        finally:
            engine.dispose()

        assert _domain_snapshot(server.uri) == before
        assert build.documents_indexed == 1
        assert build.embeddings_created == 0
        assert build.fault_code == "embedding-unavailable"
        assert (
            result.hits[0].story_id,
            result.hits[0].claim_id,
            result.hits[0].evidence_span_id,
        ) == (story_id, claim_id, evidence_id)
    finally:
        server.drop()
