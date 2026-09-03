from __future__ import annotations

import json
import math
import re
import subprocess
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime
from hashlib import sha256
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from importlib.resources import files
from pathlib import Path
from threading import Timer, local
from time import monotonic
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import and_, func, literal, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, aliased

from ai_intel_agent.domain import (
    AuditAction,
    AuditSubjectType,
    DigestState,
    EvidenceRelation,
    EvidenceRole,
    StoryReviewState,
    Topic,
)
from ai_intel_agent.persistence import (
    AuditEventRecord,
    CandidateRecord,
    ClaimRecord,
    DatabaseAcquisitionDeadlineExceeded,
    DigestRecord,
    DigestStoryRecord,
    DocumentVersionRecord,
    EvidenceSpanRecord,
    RetrievalChunkEntityRecord,
    RetrievalChunkRecord,
    RetrievalIndexRecord,
    RetrievalRuntimeStateRecord,
    SourceSpecificRecordRecord,
    StoryPresentationRecord,
    StoryRecord,
    reserve_database_acquisition_budget,
)
from ai_intel_agent.publication import (
    PublicPublicationRepository,
    bounded_public_evidence_excerpt,
)

PROFILE_RESOURCE = "accepted_knowledge_retrieval.v1.json"
PROFILE_SCHEMA_VERSION = "accepted-knowledge-retrieval.v1"
APPROVED_PROFILE_ID = "accepted-knowledge-minilm-mmarco-2026-08-19.v1"
APPROVED_PROFILE_SHA256 = "b27e5caac923c1982a5dfda6e1bdc509ba829d2a1e1e9498fb327bda9f2b709a"
APPROVED_EMBEDDING_MODEL_ID = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
APPROVED_EMBEDDING_SOURCE_REPOSITORY = "qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q"
APPROVED_EMBEDDING_REVISION = "faf4aa4225822f3bc6376869cb1164e8e3feedd0"
APPROVED_EMBEDDING_SHA256 = "634d0f66c29dc934c8fa72b8a4fe91dd4d420a22f1d82a241058d4316e659a99"
APPROVED_RERANKER_MODEL_ID = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"
APPROVED_RERANKER_SOURCE_REPOSITORY = APPROVED_RERANKER_MODEL_ID
APPROVED_RERANKER_REVISION = "1427fd652930e4ba29e8149678df786c240d8825"
APPROVED_RERANKER_SHA256 = "6c2513767fb63d008a4377bef7a7a3555433d9436342bb53e35a3a72ffc52d4b"
APPROVED_RUNTIME_VERSION = "fastembed==0.8.0"
APPROVED_MODEL_METADATA_FILES = (
    "config.json",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
)
APPROVED_ENTITY_POLICY = "technical-identifiers-v1"
KNOWN_TECHNICAL_ENTITIES = frozenset(
    {
        "avx",
        "cuda",
        "fastembed",
        "minilm",
        "mmarco",
        "onnx",
        "pgvector",
        "postgresql",
    }
)
TOKEN = re.compile(
    r"[\u3400-\u9fff]|[0-9A-Za-z]+(?:[._/+:-][0-9A-Za-z]+)*|[^\s]",
    flags=re.UNICODE,
)
CHINESE_CHARACTER = re.compile(r"[\u3400-\u9fff]")
QUERY_TERM = re.compile(r"[0-9A-Za-z][0-9A-Za-z._+-]*|[\u3400-\u9fff]{2,}")
GIT_LFS_POINTER = re.compile(
    rb"version https://git-lfs.github.com/spec/v1\n"
    rb"oid sha256:([0-9a-f]{64})\n"
    rb"size (0|[1-9][0-9]*)\n"
)
GENERIC_QUESTION_TERMS = frozenset(
    {"发生了什么", "是什么", "怎么样", "有什么新消息", "有什么更新", "有哪些变化", "如何"}
)
QUESTION_PREFIXES = ("请问", "关于", "的")
QUESTION_SUFFIXES = ("是多少", "是什么", "怎么样", "如何", "多少", "了吗", "吗", "呢")
RETRIEVAL_TIME_SEMANTICS = frozenset(
    {
        "event",
        "source-publication",
        "discovery",
        "editorial",
        "digest-publication",
    }
)


class AcceptedKnowledgeConfigurationError(ValueError):
    pass


class AcceptedKnowledgeDeadlineExceeded(TimeoutError):
    pass


class _DeadlineAwareOnnxSession:
    """Inject one caller-owned RunOptions into FastEmbed's synchronous session call."""

    def __init__(self, session: Any) -> None:
        self._session = session
        self._context = local()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    def run(
        self,
        output_names: Any,
        input_feed: Any,
        run_options: Any = None,
    ) -> Any:
        active_run_options = getattr(self._context, "run_options", None)
        return self._session.run(
            output_names,
            input_feed,
            active_run_options if active_run_options is not None else run_options,
        )

    def invoke(self, run_options: Any, operation: Callable[[], Any]) -> Any:
        missing = object()
        previous = getattr(self._context, "run_options", missing)
        self._context.run_options = run_options
        try:
            return operation()
        finally:
            if previous is missing:
                del self._context.run_options
            else:
                self._context.run_options = previous


def _deadline_aware_fastembed_session(model: Any) -> _DeadlineAwareOnnxSession | None:
    runtime_model = getattr(model, "model", None)
    session = getattr(runtime_model, "model", None)
    if isinstance(session, _DeadlineAwareOnnxSession):
        return session
    if runtime_model is None or not callable(getattr(session, "run", None)):
        return None
    wrapped = _DeadlineAwareOnnxSession(session)
    runtime_model.model = wrapped
    return wrapped


def _run_fastembed_query_with_deadline(
    operation: Callable[[], Any],
    *,
    session: _DeadlineAwareOnnxSession | None,
    timeout_seconds: float | None,
) -> Any:
    if timeout_seconds is None:
        return operation()
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("FastEmbed query timeout_seconds must be positive and finite")
    if session is None:
        raise AcceptedKnowledgeConfigurationError(
            "FastEmbed ONNX session is unavailable for deadline-aware query inference"
        )
    try:
        from onnxruntime import RunOptions
    except ImportError as error:
        raise AcceptedKnowledgeConfigurationError(
            "ONNX Runtime is unavailable for deadline-aware query inference"
        ) from error

    run_options = RunOptions()
    watchdog = Timer(
        timeout_seconds,
        setattr,
        args=(run_options, "terminate", True),
    )
    watchdog.name = f"accepted-knowledge-onnx-deadline-{id(run_options):x}"
    watchdog.daemon = True
    result: Any = None
    failure: Exception | None = None
    watchdog.start()
    try:
        result = session.invoke(run_options, operation)
    except Exception as error:  # noqa: BLE001 - map only watchdog termination below.
        failure = error
    finally:
        watchdog.cancel()
        watchdog.join()
    if run_options.terminate:
        raise AcceptedKnowledgeDeadlineExceeded(
            "Accepted-knowledge query inference exceeded its deadline"
        ) from failure
    if failure is not None:
        raise failure
    return result


@dataclass(frozen=True)
class RetrievalModelConfiguration:
    embedding_model_dir: Path
    reranker_model_dir: Path
    threads: int

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> RetrievalModelConfiguration:
        embedding_raw = environ.get("AI_INTEL_EMBEDDING_MODEL_DIR", "").strip()
        reranker_raw = environ.get("AI_INTEL_RERANKER_MODEL_DIR", "").strip()
        if not embedding_raw or not reranker_raw:
            raise AcceptedKnowledgeConfigurationError(
                "AI_INTEL_EMBEDDING_MODEL_DIR and AI_INTEL_RERANKER_MODEL_DIR are required"
            )
        embedding_model_dir = Path(embedding_raw)
        reranker_model_dir = Path(reranker_raw)
        if not embedding_model_dir.is_absolute() or not reranker_model_dir.is_absolute():
            raise AcceptedKnowledgeConfigurationError(
                "Retrieval model directories must be absolute"
            )
        threads_raw = environ.get("AI_INTEL_RETRIEVAL_THREADS", "2").strip()
        try:
            threads = int(threads_raw)
        except ValueError as error:
            raise AcceptedKnowledgeConfigurationError(
                "AI_INTEL_RETRIEVAL_THREADS must be an integer"
            ) from error
        if not 1 <= threads <= 64:
            raise AcceptedKnowledgeConfigurationError(
                "AI_INTEL_RETRIEVAL_THREADS must be between 1 and 64"
            )
        return cls(
            embedding_model_dir=embedding_model_dir,
            reranker_model_dir=reranker_model_dir,
            threads=threads,
        )


@dataclass(frozen=True)
class ApprovedModelArtifact:
    role: str
    model_id: str
    revision: str
    artifact_path: Path
    artifact_sha256: str
    cpu_feature: str | None


@dataclass(frozen=True)
class ApprovedModelArtifactCheck:
    ready: bool
    runtime_version: str
    embedding: ApprovedModelArtifact
    reranker: ApprovedModelArtifact


@dataclass(frozen=True)
class RetrievalBackendFault:
    stage: str
    code: str


@dataclass(frozen=True)
class ApprovedRetrievalBackends:
    embedding: EmbeddingBackend | None
    reranker: RerankerBackend | None
    faults: tuple[RetrievalBackendFault, ...]


@dataclass(frozen=True)
class RetrievalRuntimeStageStatus:
    stage: str
    index_id: UUID | None
    state: str
    model_id: str | None
    revision: str | None
    artifact_sha256: str | None
    fault_code: str | None
    updated_at: datetime


@dataclass(frozen=True)
class RetrievalHealthSnapshot:
    active_index_id: UUID | None
    profile_id: str | None
    profile_sha256: str | None
    documents_indexed: int
    chunks_indexed: int
    embeddings_indexed: int
    index_fault_code: str | None
    stages: tuple[RetrievalRuntimeStageStatus, ...]
    documents_pending_index: int = 0

    @property
    def hybrid_ready(self) -> bool:
        stages = {stage.stage: stage for stage in self.stages}
        index = stages.get("index")
        embedding = stages.get("embedding")
        reranker = stages.get("reranker")
        return (
            self.active_index_id is not None
            and self.profile_id == APPROVED_PROFILE_ID
            and self.profile_sha256 == APPROVED_PROFILE_SHA256
            and self.chunks_indexed > 0
            and self.embeddings_indexed == self.chunks_indexed
            and self.index_fault_code is None
            and self.documents_pending_index == 0
            and index is not None
            and index.index_id == self.active_index_id
            and index.state == "ready"
            and index.fault_code is None
            and embedding is not None
            and embedding.index_id == self.active_index_id
            and embedding.state == "ready"
            and embedding.model_id == APPROVED_EMBEDDING_MODEL_ID
            and embedding.revision == APPROVED_EMBEDDING_REVISION
            and embedding.artifact_sha256 == APPROVED_EMBEDDING_SHA256
            and embedding.fault_code is None
            and reranker is not None
            and reranker.index_id == self.active_index_id
            and reranker.state == "ready"
            and reranker.model_id == APPROVED_RERANKER_MODEL_ID
            and reranker.revision == APPROVED_RERANKER_REVISION
            and reranker.artifact_sha256 == APPROVED_RERANKER_SHA256
            and reranker.fault_code is None
        )


@dataclass(frozen=True)
class EmbeddingProfile:
    provider: str
    model_id: str
    source_repository: str
    revision: str
    artifact_path: str
    artifact_sha256: str
    pooling: str
    normalization: str
    token_limit: int
    dimensions: int
    distance_metric: str
    runtime_version: str
    rebuild_lifecycle: str
    metadata_files: tuple[str, ...]


@dataclass(frozen=True)
class RerankerProfile:
    provider: str
    model_id: str
    source_repository: str
    revision: str
    artifact_path: str
    artifact_sha256: str
    quantization: str
    required_cpu_feature: str
    runtime_version: str
    metadata_files: tuple[str, ...]


@dataclass(frozen=True)
class ChunkWindow:
    max_tokens: int
    overlap_tokens: int


@dataclass(frozen=True)
class ChunkingProfile:
    strategy: str
    tokenizer: str
    document_types: dict[str, ChunkWindow]


@dataclass(frozen=True)
class RetrievalLimits:
    lexical_candidates: int
    semantic_candidates: int
    entity_candidates: int
    rerank_depth: int
    final_top_k: int


@dataclass(frozen=True)
class FusionProfile:
    method: str
    rrf_k: int
    weights: dict[str, float]


@dataclass(frozen=True)
class AcceptedKnowledgeProfile:
    schema_version: str
    profile_id: str
    embedding: EmbeddingProfile
    reranker: RerankerProfile
    chunking: ChunkingProfile
    retrieval: RetrievalLimits
    fusion: FusionProfile
    entity_policy: str
    fallback_channels: tuple[str, ...]


@dataclass(frozen=True)
class ChunkSource:
    document_version_id: UUID
    document_type: str
    language: str
    text: str


@dataclass(frozen=True)
class KnowledgeChunk:
    id: UUID
    document_version_id: UUID
    document_type: str
    language: str
    ordinal: int
    start_offset: int
    end_offset: int
    token_count: int
    text: str


class EmbeddingBackend(Protocol):
    def embed_documents(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]: ...

    def embed_query(
        self,
        text: str,
        *,
        timeout_seconds: float | None = None,
    ) -> tuple[float, ...]: ...


class RerankerBackend(Protocol):
    def rerank(
        self,
        query: str,
        documents: Sequence[str],
        *,
        timeout_seconds: float | None = None,
    ) -> tuple[float, ...]: ...


class FastEmbedEmbeddingBackend:
    """Apply the approved MiniLM input and output contract to one FastEmbed model."""

    def __init__(
        self,
        model: Any,
        *,
        profile: AcceptedKnowledgeProfile | None = None,
    ) -> None:
        self._model = model
        self._profile = profile or load_accepted_knowledge_profile()
        _validate_approved_profile(self._profile)
        self._deadline_session = _deadline_aware_fastembed_session(model)

    def embed_documents(
        self,
        texts: Sequence[str],
    ) -> tuple[tuple[float, ...], ...]:
        prepared = tuple(self._prepare_document(text) for text in texts)
        return tuple(
            _normalized_embedding(vector, self._profile.embedding.dimensions)
            for vector in self._model.passage_embed(prepared, batch_size=32)
        )

    def embed_query(
        self,
        text: str,
        *,
        timeout_seconds: float | None = None,
    ) -> tuple[float, ...]:
        prepared = self._prepare_query(text)
        vectors = _run_fastembed_query_with_deadline(
            lambda: tuple(self._model.query_embed((prepared,), batch_size=1)),
            session=self._deadline_session,
            timeout_seconds=timeout_seconds,
        )
        if len(vectors) != 1:
            raise ValueError("FastEmbed query Embedding returned an invalid count")
        return _normalized_embedding(vectors[0], self._profile.embedding.dimensions)

    def _tokenizer(self) -> Any:
        runtime_model = getattr(self._model, "model", self._model)
        tokenizer = getattr(runtime_model, "tokenizer", None)
        if tokenizer is None or not callable(getattr(tokenizer, "encode", None)):
            raise ValueError("Approved MiniLM tokenizer is unavailable")
        return tokenizer

    def _prepare_document(self, text: str) -> str:
        encoding = self._tokenizer().encode(text, add_special_tokens=True)
        if len(encoding.ids) > self._profile.embedding.token_limit:
            raise ValueError("Chunk exceeds the approved MiniLM token limit")
        return text

    def _prepare_query(self, text: str) -> str:
        tokenizer = self._tokenizer()
        encoding = tokenizer.encode(text, add_special_tokens=True)
        token_limit = self._profile.embedding.token_limit
        if len(encoding.ids) <= token_limit:
            return text
        offsets = tuple(encoding.offsets[:token_limit])
        end_offset = max((end for _, end in offsets), default=0)
        while end_offset > 0:
            prepared = text[:end_offset]
            if len(tokenizer.encode(prepared, add_special_tokens=True).ids) <= token_limit:
                return prepared
            end_offset = max(
                (end for _, end in offsets if end < end_offset),
                default=0,
            )
        raise ValueError("Query cannot satisfy the approved MiniLM token limit")


class FastEmbedRerankerBackend:
    """Run only the approved fused-top-eight mMARCO scoring boundary."""

    def __init__(
        self,
        model: Any,
        *,
        profile: AcceptedKnowledgeProfile | None = None,
    ) -> None:
        self._model = model
        self._profile = profile or load_accepted_knowledge_profile()
        _validate_approved_profile(self._profile)
        self._deadline_session = _deadline_aware_fastembed_session(model)

    def rerank(
        self,
        query: str,
        documents: Sequence[str],
        *,
        timeout_seconds: float | None = None,
    ) -> tuple[float, ...]:
        if len(documents) > self._profile.retrieval.rerank_depth:
            raise ValueError("Reranker input exceeds the approved fused top eight")
        scores = _run_fastembed_query_with_deadline(
            lambda: tuple(
                float(value)
                for value in self._model.rerank(query, tuple(documents), batch_size=8)
            ),
            session=self._deadline_session,
            timeout_seconds=timeout_seconds,
        )
        if len(scores) != len(documents) or any(not math.isfinite(score) for score in scores):
            raise ValueError("FastEmbed Reranker returned invalid scores")
        return scores


@dataclass(frozen=True)
class IndexBuildResult:
    index_id: UUID
    profile_id: str
    documents_indexed: int
    chunks_created: int
    embeddings_created: int
    fault_code: str | None


@dataclass(frozen=True)
class RetrievalFilters:
    publisher: str | None = None
    topic: Topic | None = None
    publication_date: date | None = None
    occurred_from: datetime | None = None
    occurred_to: datetime | None = None
    time_semantics: tuple[str, ...] = ()
    time_from: datetime | None = None
    time_to: datetime | None = None

    def __post_init__(self) -> None:
        for label, value in (
            ("occurred_from", self.occurred_from),
            ("occurred_to", self.occurred_to),
            ("time_from", self.time_from),
            ("time_to", self.time_to),
        ):
            if value is not None and value.utcoffset() is None:
                raise ValueError(f"{label} must include a timezone")
        if (
            self.occurred_from is not None
            and self.occurred_to is not None
            and self.occurred_from >= self.occurred_to
        ):
            raise ValueError("occurred_from must be earlier than occurred_to")
        if (
            self.time_from is not None
            and self.time_to is not None
            and self.time_from >= self.time_to
        ):
            raise ValueError("time_from must be earlier than time_to")
        if (
            len(set(self.time_semantics)) != len(self.time_semantics)
            or any(value not in RETRIEVAL_TIME_SEMANTICS for value in self.time_semantics)
        ):
            raise ValueError("time_semantics contains an unsupported or duplicate value")
        if (self.time_from is not None or self.time_to is not None) and not (
            self.time_semantics
        ):
            raise ValueError("time_semantics is required for a semantic time range")


@dataclass(frozen=True)
class RetrievalQuery:
    text: str
    filters: RetrievalFilters = field(default_factory=RetrievalFilters)
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.timeout_seconds is not None and (
            not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0
        ):
            raise ValueError("Retrieval timeout_seconds must be positive and finite")


@dataclass(frozen=True)
class RetrievalFault:
    stage: str
    code: str


@dataclass(frozen=True)
class RetrievalStageCandidate:
    stage: str
    evidence_span_id: UUID
    rank: int
    score: float
    chunk_id: UUID | None


@dataclass(frozen=True)
class RetrievalTrace:
    lexical: tuple[RetrievalStageCandidate, ...]
    semantic: tuple[RetrievalStageCandidate, ...]
    entity: tuple[RetrievalStageCandidate, ...]
    fusion: tuple[RetrievalStageCandidate, ...]
    final: tuple[RetrievalStageCandidate, ...]
    faults: tuple[RetrievalFault, ...]


@dataclass(frozen=True)
class AcceptedKnowledgeHit:
    story_id: UUID
    story_stable_key: str
    story_headline: str
    claim_id: UUID
    claim_text: str
    evidence_span_id: UUID
    exact_text: str
    chunk_id: UUID | None


@dataclass(frozen=True)
class AcceptedKnowledgeResult:
    query: RetrievalQuery
    hits: tuple[AcceptedKnowledgeHit, ...]
    matching_story_ids: tuple[UUID, ...]
    trace: RetrievalTrace


class AcceptedKnowledgeOperation(Protocol):
    def retrieve(self, query: RetrievalQuery) -> AcceptedKnowledgeResult: ...


@dataclass(frozen=True)
class _RetrievalCandidate:
    story_id: UUID
    story_stable_key: str
    story_headline: str
    claim_id: UUID
    claim_text: str
    evidence_span_id: UUID
    exact_text: str
    chunk_id: UUID | None
    chunk_text: str | None
    score: float

    @property
    def reranker_text(self) -> str:
        return "\n".join(
            value
            for value in (
                self.story_headline,
                self.claim_text,
                self.exact_text,
                self.chunk_text,
            )
            if value
        )

    def public_hit(self) -> AcceptedKnowledgeHit:
        return AcceptedKnowledgeHit(
            story_id=self.story_id,
            story_stable_key=self.story_stable_key,
            story_headline=self.story_headline,
            claim_id=self.claim_id,
            claim_text=self.claim_text,
            evidence_span_id=self.evidence_span_id,
            exact_text=bounded_public_evidence_excerpt(self.exact_text),
            chunk_id=self.chunk_id,
        )


class AcceptedKnowledgeIndexer:
    """Build one active, profile-isolated retrieval generation at a time."""

    def __init__(
        self,
        engine: Engine,
        *,
        embedding: EmbeddingBackend | None,
        profile: AcceptedKnowledgeProfile | None = None,
        clock: Callable[[], datetime] | None = None,
        require_embeddings: bool = False,
    ) -> None:
        self._engine = engine
        self._embedding = embedding
        self._profile = profile or load_accepted_knowledge_profile()
        _validate_approved_profile(self._profile)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._require_embeddings = require_embeddings
        self._profile_definition = asdict(self._profile)
        self._profile_sha256 = _profile_sha256(self._profile)

    def rebuild(self) -> IndexBuildResult:
        index_id = uuid4()
        created_at = self._clock()
        with Session(self._engine) as session, session.begin():
            previous = session.scalar(
                select(RetrievalIndexRecord)
                .where(RetrievalIndexRecord.state == "active")
                .with_for_update()
            )
            index = RetrievalIndexRecord(
                id=index_id,
                profile_id=self._profile.profile_id,
                profile_sha256=self._profile_sha256,
                profile_definition=self._profile_definition,
                state="building",
                created_at=created_at,
                completed_at=None,
                documents_indexed=0,
                chunks_indexed=0,
                embeddings_indexed=0,
                fault_code=None,
            )
            session.add(index)
            session.flush()
            result = self._index_missing_documents(session, index)
            self._require_complete_embeddings(index, result)
            if previous is not None:
                previous.state = "retired"
                session.flush([previous])
            index.state = "active"
            index.completed_at = self._clock()
            session.flush([index])
            self._record_index_states(session, index, result.fault_code)
            return result

    def incremental(self) -> IndexBuildResult:
        with Session(self._engine) as session:
            active_id = session.scalar(
                select(RetrievalIndexRecord.id).where(RetrievalIndexRecord.state == "active")
            )
        if active_id is None:
            return self.rebuild()
        with Session(self._engine) as session, session.begin():
            index = session.scalar(
                select(RetrievalIndexRecord)
                .where(RetrievalIndexRecord.id == active_id)
                .with_for_update()
            )
            if index is None or index.state != "active":
                raise AcceptedKnowledgeConfigurationError("Active retrieval index changed")
            if (
                index.profile_id != self._profile.profile_id
                or index.profile_sha256 != self._profile_sha256
            ):
                raise AcceptedKnowledgeConfigurationError(
                    "Incremental indexing cannot mix Retrieval Profiles"
                )
            result = self._index_missing_documents(session, index)
            self._require_complete_embeddings(index, result)
            index.completed_at = self._clock()
            self._record_index_states(session, index, result.fault_code)
            return result

    def _require_complete_embeddings(
        self,
        index: RetrievalIndexRecord,
        result: IndexBuildResult,
    ) -> None:
        if self._require_embeddings and (
            result.fault_code is not None or index.embeddings_indexed != index.chunks_indexed
        ):
            raise AcceptedKnowledgeConfigurationError(
                "Production Retrieval rebuild did not create all required Embeddings"
            )

    def _index_missing_documents(
        self,
        session: Session,
        index: RetrievalIndexRecord,
    ) -> IndexBuildResult:
        indexed_document_ids = set(
            session.scalars(
                select(RetrievalChunkRecord.document_version_id).where(
                    RetrievalChunkRecord.index_id == index.id
                )
            )
        )
        record_kind = (
            select(SourceSpecificRecordRecord.record_kind)
            .where(
                SourceSpecificRecordRecord.document_version_id == DocumentVersionRecord.id,
                SourceSpecificRecordRecord.evidence_eligible.is_(True),
            )
            .order_by(SourceSpecificRecordRecord.id)
            .limit(1)
            .scalar_subquery()
        )
        statement = (
            select(DocumentVersionRecord, record_kind.label("record_kind"))
            .join(
                EvidenceSpanRecord,
                EvidenceSpanRecord.document_version_id == DocumentVersionRecord.id,
            )
            .join(ClaimRecord, ClaimRecord.id == EvidenceSpanRecord.claim_id)
            .join(StoryRecord, StoryRecord.id == ClaimRecord.story_id)
            .where(*_accepted_evidence_conditions())
            .distinct()
            .order_by(DocumentVersionRecord.id)
        )
        if indexed_document_ids:
            statement = statement.where(DocumentVersionRecord.id.not_in(indexed_document_ids))
        documents = tuple(session.execute(statement))
        chunks = tuple(
            chunk
            for document, record_kind_value in documents
            for chunk in build_document_chunks(
                ChunkSource(
                    document_version_id=document.id,
                    document_type=_document_type(document, record_kind_value),
                    language=("zh" if CHINESE_CHARACTER.search(document.body) else "en"),
                    text=document.body,
                ),
                self._profile,
            )
        )
        vectors, fault_code = self._embed_chunks(chunks)
        embeddings_created = 0
        for chunk, vector in zip(chunks, vectors, strict=True):
            persisted_chunk_id = uuid5(
                NAMESPACE_URL,
                f"ai-intel-agent:retrieval-index-chunk:{index.id}:{chunk.id}",
            )
            chunk_record = RetrievalChunkRecord(
                id=persisted_chunk_id,
                index_id=index.id,
                document_version_id=chunk.document_version_id,
                document_type=chunk.document_type,
                language=chunk.language,
                ordinal=chunk.ordinal,
                start_offset=chunk.start_offset,
                end_offset=chunk.end_offset,
                token_count=chunk.token_count,
                text=chunk.text,
                text_hash=sha256(chunk.text.encode()).hexdigest(),
                embedding=list(vector) if vector is not None else None,
            )
            session.add(chunk_record)
            session.flush([chunk_record])
            if vector is not None:
                embeddings_created += 1
            for display_name, normalized_name in _technical_entities(chunk.text):
                session.add(
                    RetrievalChunkEntityRecord(
                        chunk_id=persisted_chunk_id,
                        normalized_name=normalized_name,
                        display_name=display_name,
                        entity_type="technical-identifier",
                    )
                )
        index.documents_indexed += len(documents)
        index.chunks_indexed += len(chunks)
        index.embeddings_indexed += embeddings_created
        index.fault_code = (
            "embedding-unavailable"
            if fault_code is not None or index.embeddings_indexed != index.chunks_indexed
            else None
        )
        return IndexBuildResult(
            index_id=index.id,
            profile_id=index.profile_id,
            documents_indexed=len(documents),
            chunks_created=len(chunks),
            embeddings_created=embeddings_created,
            fault_code=index.fault_code,
        )

    def _embed_chunks(
        self,
        chunks: tuple[KnowledgeChunk, ...],
    ) -> tuple[tuple[tuple[float, ...] | None, ...], str | None]:
        if self._embedding is None:
            return tuple(None for _ in chunks), "embedding-unavailable"
        if not chunks:
            return (), None
        try:
            raw_vectors = self._embedding.embed_documents(tuple(chunk.text for chunk in chunks))
            if len(raw_vectors) != len(chunks):
                raise ValueError("Embedding count does not match Chunk count")
            vectors = tuple(
                _normalized_embedding(vector, self._profile.embedding.dimensions)
                for vector in raw_vectors
            )
        except Exception:  # noqa: BLE001 - indexing preserves model-free Chunks on model faults.
            return tuple(None for _ in chunks), "embedding-unavailable"
        return tuple(vectors), None

    def _record_index_states(
        self,
        session: Session,
        index: RetrievalIndexRecord,
        fault_code: str | None,
    ) -> None:
        observed_at = self._clock()
        embedding_ready = (
            fault_code is None
            and index.chunks_indexed > 0
            and index.embeddings_indexed == index.chunks_indexed
        )
        rows = (
            {
                "stage": "index",
                "index_id": index.id,
                "state": "ready",
                "model_id": None,
                "revision": None,
                "artifact_sha256": None,
                "fault_code": None,
                "fault_detail": None,
                "updated_at": observed_at,
            },
            {
                "stage": "embedding",
                "index_id": index.id,
                "state": "ready" if embedding_ready else "unavailable",
                "model_id": self._profile.embedding.model_id,
                "revision": self._profile.embedding.revision,
                "artifact_sha256": self._profile.embedding.artifact_sha256,
                "fault_code": None if embedding_ready else "embedding-unavailable",
                "fault_detail": (
                    None
                    if embedding_ready
                    else "The active generation does not contain complete semantic vectors"
                ),
                "updated_at": observed_at,
            },
            {
                "stage": "reranker",
                "index_id": index.id,
                "state": "unavailable",
                "model_id": self._profile.reranker.model_id,
                "revision": self._profile.reranker.revision,
                "artifact_sha256": self._profile.reranker.artifact_sha256,
                "fault_code": "reranker-not-probed",
                "fault_detail": "The approved Reranker has not succeeded on this generation",
                "updated_at": observed_at,
            },
        )
        for row in rows:
            session.execute(
                insert(RetrievalRuntimeStateRecord)
                .values(**row)
                .on_conflict_do_update(
                    index_elements=[RetrievalRuntimeStateRecord.stage],
                    set_={key: value for key, value in row.items() if key != "stage"},
                )
            )


class AcceptedKnowledgeRetrieval:
    """Retrieve public accepted Evidence through deterministic hybrid ranking."""

    def __init__(
        self,
        engine: Engine,
        *,
        embedding: EmbeddingBackend | None = None,
        reranker: RerankerBackend | None = None,
        profile: AcceptedKnowledgeProfile | None = None,
        clock: Callable[[], datetime] | None = None,
        timer: Callable[[], float] = monotonic,
    ) -> None:
        self._engine = engine
        self._embedding = embedding
        self._reranker = reranker
        self._profile = profile or load_accepted_knowledge_profile()
        _validate_approved_profile(self._profile)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._timer = timer

    def retrieve(self, query: RetrievalQuery) -> AcceptedKnowledgeResult:
        deadline = (
            self._timer() + query.timeout_seconds
            if query.timeout_seconds is not None
            else None
        )
        try:
            return self._retrieve(query, deadline=deadline)
        except AcceptedKnowledgeDeadlineExceeded:
            raise
        except Exception as error:
            if deadline is not None and self._timer() >= deadline:
                raise AcceptedKnowledgeDeadlineExceeded(
                    "Accepted-knowledge retrieval exceeded its deadline"
                ) from error
            raise

    def _retrieve(
        self,
        query: RetrievalQuery,
        *,
        deadline: float | None,
    ) -> AcceptedKnowledgeResult:
        self._check_deadline(deadline)
        query_text = _fts_query_text(query.text)
        faults: list[RetrievalFault] = []
        embedding_state: tuple[str, str | None, str | None] | None = None
        with Session(self._engine) as session:
            active_index = self._scalar_with_deadline(
                session,
                select(RetrievalIndexRecord).where(
                    RetrievalIndexRecord.state == "active"
                ),
                deadline,
            )
            active_index_id = (
                active_index.id
                if active_index is not None
                and active_index.profile_id == self._profile.profile_id
                and active_index.profile_sha256 == _profile_sha256(self._profile)
                else None
            )
            documents_pending_index = (
                self._scalar_with_deadline(
                    session,
                    _documents_pending_index_statement(active_index_id),
                    deadline,
                )
                if active_index_id is not None
                else 0
            )
            index_projection_current = documents_pending_index == 0
            if active_index_id is not None and not index_projection_current:
                faults.append(RetrievalFault("index", "index-stale"))
            if active_index is not None and active_index_id is None:
                faults.append(RetrievalFault("index", "profile-incompatible"))
            lexical = self._lexical_candidates(
                session,
                active_index_id,
                query_text,
                query.filters,
                deadline,
            )
            lexical_story_ids = self._matching_lexical_story_ids(
                session,
                query_text,
                query.filters,
                deadline,
            )
            semantic: tuple[_RetrievalCandidate, ...] = ()
            semantic_index_ready = (
                active_index_id is not None
                and index_projection_current
                and active_index is not None
                and active_index.fault_code is None
                and active_index.chunks_indexed > 0
                and active_index.embeddings_indexed == active_index.chunks_indexed
            )
            if semantic_index_ready and self._embedding is not None and query.text.strip():
                try:
                    self._check_deadline(deadline)
                    query_vector = _normalized_embedding(
                        self._embedding.embed_query(
                            query.text,
                            timeout_seconds=self._remaining_seconds(deadline),
                        ),
                        self._profile.embedding.dimensions,
                    )
                    self._check_deadline(deadline)
                    semantic = self._semantic_candidates(
                        session,
                        active_index_id,
                        query_vector,
                        query.filters,
                        deadline,
                    )
                    embedding_state = ("ready", None, None)
                except AcceptedKnowledgeDeadlineExceeded:
                    raise
                except Exception:  # noqa: BLE001 - fallback remains an available product path.
                    faults.append(RetrievalFault("embedding", "embedding-unavailable"))
                    embedding_state = (
                        "unavailable",
                        "embedding-unavailable",
                        "Semantic query embedding or exact vector scan failed",
                    )
            elif query.text.strip():
                fault_code = (
                    "embedding-index-stale"
                    if active_index_id is not None and not index_projection_current
                    else "embedding-index-incomplete"
                    if active_index_id is not None
                    and active_index is not None
                    and active_index.embeddings_indexed > 0
                    and active_index.embeddings_indexed != active_index.chunks_indexed
                    else "embedding-unavailable"
                )
                faults.append(RetrievalFault("embedding", fault_code))
                embedding_state = (
                    "unavailable",
                    fault_code,
                    (
                        "The active generation does not cover all accepted published documents"
                        if fault_code == "embedding-index-stale"
                        else "The active generation does not contain complete semantic vectors"
                        if fault_code == "embedding-index-incomplete"
                        else "The approved Embedding runtime is unavailable"
                    ),
                )
            entity = (
                self._entity_candidates(
                    session,
                    active_index_id,
                    query.text,
                    query.filters,
                    deadline,
                )
                if active_index_id is not None
                and index_projection_current
                and query.text.strip()
                else ()
            )
            if not query.text.strip():
                lexical = self._catalog_candidates(session, query.filters, deadline)
                lexical_story_ids = self._catalog_story_ids(
                    session,
                    query.filters,
                    deadline,
                )

        rankings = {
            "lexical": lexical,
            "semantic": semantic,
            "exact_entity": entity,
        }
        fused = _fuse_candidates(rankings, self._profile)
        catalog_request = not query.text.strip()
        rerank_candidates = (
            fused
            if catalog_request
            else _rerank_candidate_pool(fused, rankings, self._profile)
        )
        final = (
            rerank_candidates
            if catalog_request
            else rerank_candidates[: self._profile.retrieval.final_top_k]
        )
        final_scores = {candidate.evidence_span_id: candidate.score for candidate in final}
        reranker_state: tuple[str, str | None, str | None] | None = None
        if rerank_candidates and self._reranker is not None and query.text.strip():
            try:
                self._check_deadline(deadline)
                scores = self._reranker.rerank(
                    query.text,
                    tuple(candidate.reranker_text for candidate in rerank_candidates),
                    timeout_seconds=self._remaining_seconds(deadline),
                )
                self._check_deadline(deadline)
                if len(scores) != len(rerank_candidates) or any(
                    not math.isfinite(score) for score in scores
                ):
                    raise ValueError("Reranker returned invalid scores")
                prior = {
                    candidate.evidence_span_id: rank
                    for rank, candidate in enumerate(rerank_candidates)
                }
                scores_by_id = {
                    candidate.evidence_span_id: score
                    for candidate, score in zip(rerank_candidates, scores, strict=True)
                }
                final = tuple(
                    sorted(
                        rerank_candidates,
                        key=lambda candidate: (
                            -scores_by_id[candidate.evidence_span_id],
                            prior[candidate.evidence_span_id],
                            str(candidate.evidence_span_id),
                        ),
                    )[: self._profile.retrieval.final_top_k]
                )
                final_scores = scores_by_id
                reranker_state = ("ready", None, None)
            except AcceptedKnowledgeDeadlineExceeded:
                raise
            except Exception:  # noqa: BLE001 - deterministic Fusion order is the contract.
                faults.append(RetrievalFault("reranker", "reranker-failed"))
                reranker_state = (
                    "degraded",
                    "reranker-failed",
                    "Reranker load or inference failed",
                )
        elif rerank_candidates and query.text.strip():
            faults.append(RetrievalFault("reranker", "reranker-unavailable"))
            reranker_state = (
                "unavailable",
                "reranker-unavailable",
                "Approved Reranker is not configured",
            )

        if embedding_state is not None:
            state, fault_code, fault_detail = embedding_state
            self._record_runtime_state(
                stage="embedding",
                index_id=active_index_id,
                state=state,
                model_id=self._profile.embedding.model_id,
                revision=self._profile.embedding.revision,
                artifact_sha256=self._profile.embedding.artifact_sha256,
                fault_code=fault_code,
                fault_detail=fault_detail,
                deadline=deadline,
            )
        if reranker_state is not None:
            state, fault_code, fault_detail = reranker_state
            self._record_runtime_state(
                stage="reranker",
                index_id=active_index_id,
                state=state,
                model_id=self._profile.reranker.model_id,
                revision=self._profile.reranker.revision,
                artifact_sha256=self._profile.reranker.artifact_sha256,
                fault_code=fault_code,
                fault_detail=fault_detail,
                deadline=deadline,
            )

        self._check_deadline(deadline)
        trace = RetrievalTrace(
            lexical=_stage_candidates("lexical", lexical),
            semantic=_stage_candidates("semantic", semantic),
            entity=_stage_candidates("entity", entity),
            fusion=_stage_candidates("fusion", fused),
            final=tuple(
                RetrievalStageCandidate(
                    stage="final",
                    evidence_span_id=candidate.evidence_span_id,
                    rank=rank,
                    score=float(final_scores[candidate.evidence_span_id]),
                    chunk_id=candidate.chunk_id,
                )
                for rank, candidate in enumerate(final, start=1)
            ),
            faults=tuple(faults),
        )
        return AcceptedKnowledgeResult(
            query=query,
            hits=tuple(candidate.public_hit() for candidate in final),
            matching_story_ids=_matching_story_ids(
                (final, lexical, semantic, entity),
                lexical_story_ids,
            ),
            trace=trace,
        )

    def _check_deadline(self, deadline: float | None) -> None:
        self._remaining_seconds(deadline)

    def _remaining_seconds(self, deadline: float | None) -> float | None:
        if deadline is None:
            return None
        remaining_seconds = deadline - self._timer()
        if remaining_seconds <= 0:
            raise AcceptedKnowledgeDeadlineExceeded(
                "Accepted-knowledge retrieval exceeded its deadline"
            )
        return remaining_seconds

    def _set_statement_deadline(
        self,
        session: Session,
        deadline: float | None,
    ) -> None:
        if deadline is None:
            return
        try:
            statement_seconds = reserve_database_acquisition_budget(
                self._engine,
                self._remaining_seconds(deadline),
            )
        except DatabaseAcquisitionDeadlineExceeded as error:
            raise AcceptedKnowledgeDeadlineExceeded(
                "Accepted-knowledge retrieval exceeded its deadline"
            ) from error
        assert statement_seconds is not None
        timeout_milliseconds = max(1, math.ceil(statement_seconds * 1000))
        session.execute(
            select(
                func.set_config(
                    "statement_timeout",
                    f"{timeout_milliseconds}ms",
                    True,
                )
            )
        )

    def _execute_with_deadline(
        self,
        session: Session,
        statement: Any,
        deadline: float | None,
    ) -> Any:
        self._set_statement_deadline(session, deadline)
        rows = session.execute(statement)
        self._check_deadline(deadline)
        return rows

    def _scalar_with_deadline(
        self,
        session: Session,
        statement: Any,
        deadline: float | None,
    ) -> Any:
        self._set_statement_deadline(session, deadline)
        value = session.scalar(statement)
        self._check_deadline(deadline)
        return value

    def _record_runtime_state(
        self,
        *,
        stage: str,
        index_id: UUID | None,
        state: str,
        model_id: str,
        revision: str,
        artifact_sha256: str,
        fault_code: str | None,
        fault_detail: str | None,
        deadline: float | None,
    ) -> None:
        values = {
            "stage": stage,
            "index_id": index_id,
            "state": state,
            "model_id": model_id,
            "revision": revision,
            "artifact_sha256": artifact_sha256,
            "fault_code": fault_code,
            "fault_detail": fault_detail,
            "updated_at": self._clock(),
        }
        try:
            with Session(self._engine) as session, session.begin():
                self._execute_with_deadline(
                    session,
                    insert(RetrievalRuntimeStateRecord)
                    .values(**values)
                    .on_conflict_do_update(
                        index_elements=[RetrievalRuntimeStateRecord.stage],
                        set_={key: value for key, value in values.items() if key != "stage"},
                    ),
                    deadline,
                )
        except Exception:  # noqa: BLE001 - telemetry cannot take Retrieval offline.
            return

    def _lexical_candidates(
        self,
        session: Session,
        index_id: UUID | None,
        query_text: str,
        filters: RetrievalFilters,
        deadline: float | None,
    ) -> tuple[_RetrievalCandidate, ...]:
        if not query_text:
            return ()
        indexed = (
            self._indexed_lexical_candidates(
                session,
                index_id,
                query_text,
                filters,
                deadline,
            )
            if index_id is not None
            else ()
        )
        primary_document = aliased(DocumentVersionRecord)
        primary_candidate = aliased(CandidateRecord)
        searchable_text = func.concat_ws(
            " ",
            StoryRecord.headline,
            StoryPresentationRecord.summary,
            StoryPresentationRecord.why_it_matters,
            ClaimRecord.text,
            EvidenceSpanRecord.exact_text,
        )
        search_vector = func.to_tsvector("simple", searchable_text)
        ts_query = func.websearch_to_tsquery("simple", query_text)
        score = func.ts_rank_cd(search_vector, ts_query)
        statement = (
            select(
                *_candidate_columns(),
                literal(None).label("chunk_id"),
                literal(None).label("chunk_text"),
                score.label("score"),
            )
            .join(ClaimRecord, ClaimRecord.story_id == StoryRecord.id)
            .join(EvidenceSpanRecord, EvidenceSpanRecord.claim_id == ClaimRecord.id)
            .join(
                primary_document,
                primary_document.id == StoryRecord.primary_document_version_id,
            )
            .join(primary_candidate, primary_candidate.id == primary_document.candidate_id)
            .outerjoin(
                StoryPresentationRecord,
                StoryPresentationRecord.story_id == StoryRecord.id,
            )
            .where(
                *_accepted_evidence_conditions(),
                search_vector.op("@@")(ts_query),
            )
            .order_by(
                score.desc(),
                StoryRecord.occurred_at.desc(),
                ClaimRecord.position,
                EvidenceSpanRecord.start_offset,
                EvidenceSpanRecord.id,
            )
            .limit(self._profile.retrieval.lexical_candidates)
        )
        statement = _apply_filters(
            statement,
            filters,
            primary_document=primary_document,
            primary_candidate=primary_candidate,
        )
        accepted_projection = _unique_candidates(
            self._execute_with_deadline(session, statement, deadline)
        )
        return _merge_lexical_candidates(
            indexed,
            accepted_projection,
            self._profile.retrieval.lexical_candidates,
        )

    def _indexed_lexical_candidates(
        self,
        session: Session,
        index_id: UUID,
        query_text: str,
        filters: RetrievalFilters,
        deadline: float | None,
    ) -> tuple[_RetrievalCandidate, ...]:
        primary_document = aliased(DocumentVersionRecord)
        primary_candidate = aliased(CandidateRecord)
        ts_query = func.websearch_to_tsquery("simple", query_text)
        score = func.ts_rank_cd(RetrievalChunkRecord.search_vector, ts_query)
        statement = (
            select(
                *_candidate_columns(),
                RetrievalChunkRecord.id.label("chunk_id"),
                RetrievalChunkRecord.text.label("chunk_text"),
                score.label("score"),
            )
            .select_from(RetrievalChunkRecord)
            .join(
                EvidenceSpanRecord,
                (EvidenceSpanRecord.document_version_id == RetrievalChunkRecord.document_version_id)
                & (EvidenceSpanRecord.start_offset < RetrievalChunkRecord.end_offset)
                & (EvidenceSpanRecord.end_offset > RetrievalChunkRecord.start_offset),
            )
            .join(ClaimRecord, ClaimRecord.id == EvidenceSpanRecord.claim_id)
            .join(StoryRecord, StoryRecord.id == ClaimRecord.story_id)
            .join(
                primary_document,
                primary_document.id == StoryRecord.primary_document_version_id,
            )
            .join(primary_candidate, primary_candidate.id == primary_document.candidate_id)
            .outerjoin(
                StoryPresentationRecord,
                StoryPresentationRecord.story_id == StoryRecord.id,
            )
            .where(
                *_accepted_evidence_conditions(),
                RetrievalChunkRecord.index_id == index_id,
                RetrievalChunkRecord.search_vector.op("@@")(ts_query),
            )
        )
        statement = _apply_filters(
            statement,
            filters,
            primary_document=primary_document,
            primary_candidate=primary_candidate,
        )
        statement = _deduplicated_candidate_statement(
            statement,
            evidence_order=(score.desc(), RetrievalChunkRecord.id),
            limit=self._profile.retrieval.lexical_candidates,
        )
        return _unique_candidates(
            self._execute_with_deadline(session, statement, deadline)
        )

    def _matching_lexical_story_ids(
        self,
        session: Session,
        query_text: str,
        filters: RetrievalFilters,
        deadline: float | None,
    ) -> tuple[UUID, ...]:
        if not query_text:
            return ()
        primary_document = aliased(DocumentVersionRecord)
        primary_candidate = aliased(CandidateRecord)
        searchable_text = func.concat_ws(
            " ",
            StoryRecord.headline,
            StoryPresentationRecord.summary,
            StoryPresentationRecord.why_it_matters,
            ClaimRecord.text,
            EvidenceSpanRecord.exact_text,
        )
        ts_query = func.websearch_to_tsquery("simple", query_text)
        statement = (
            select(
                StoryRecord.id.label("story_id"),
                StoryRecord.occurred_at.label("occurred_at"),
            )
            .join(ClaimRecord, ClaimRecord.story_id == StoryRecord.id)
            .join(EvidenceSpanRecord, EvidenceSpanRecord.claim_id == ClaimRecord.id)
            .join(
                primary_document,
                primary_document.id == StoryRecord.primary_document_version_id,
            )
            .join(primary_candidate, primary_candidate.id == primary_document.candidate_id)
            .outerjoin(
                StoryPresentationRecord,
                StoryPresentationRecord.story_id == StoryRecord.id,
            )
            .where(
                *_accepted_evidence_conditions(),
                func.to_tsvector("simple", searchable_text).op("@@")(ts_query),
            )
            .distinct()
            .order_by(StoryRecord.occurred_at.desc(), StoryRecord.id)
        )
        statement = _apply_filters(
            statement,
            filters,
            primary_document=primary_document,
            primary_candidate=primary_candidate,
        )
        return tuple(
            row.story_id
            for row in self._execute_with_deadline(session, statement, deadline)
        )

    def _semantic_candidates(
        self,
        session: Session,
        index_id: UUID,
        query_vector: tuple[float, ...],
        filters: RetrievalFilters,
        deadline: float | None,
    ) -> tuple[_RetrievalCandidate, ...]:
        primary_document = aliased(DocumentVersionRecord)
        primary_candidate = aliased(CandidateRecord)
        distance = RetrievalChunkRecord.embedding.cosine_distance(list(query_vector))
        score = 1.0 - distance
        statement = (
            select(
                *_candidate_columns(),
                RetrievalChunkRecord.id.label("chunk_id"),
                RetrievalChunkRecord.text.label("chunk_text"),
                score.label("score"),
            )
            .select_from(RetrievalChunkRecord)
            .join(
                EvidenceSpanRecord,
                (EvidenceSpanRecord.document_version_id == RetrievalChunkRecord.document_version_id)
                & (EvidenceSpanRecord.start_offset < RetrievalChunkRecord.end_offset)
                & (EvidenceSpanRecord.end_offset > RetrievalChunkRecord.start_offset),
            )
            .join(ClaimRecord, ClaimRecord.id == EvidenceSpanRecord.claim_id)
            .join(StoryRecord, StoryRecord.id == ClaimRecord.story_id)
            .join(
                primary_document,
                primary_document.id == StoryRecord.primary_document_version_id,
            )
            .join(primary_candidate, primary_candidate.id == primary_document.candidate_id)
            .outerjoin(
                StoryPresentationRecord,
                StoryPresentationRecord.story_id == StoryRecord.id,
            )
            .where(
                *_accepted_evidence_conditions(),
                RetrievalChunkRecord.index_id == index_id,
                RetrievalChunkRecord.embedding.is_not(None),
            )
        )
        statement = _apply_filters(
            statement,
            filters,
            primary_document=primary_document,
            primary_candidate=primary_candidate,
        )
        statement = _deduplicated_candidate_statement(
            statement,
            evidence_order=(distance, RetrievalChunkRecord.id),
            limit=self._profile.retrieval.semantic_candidates,
        )
        return _unique_candidates(
            self._execute_with_deadline(session, statement, deadline)
        )

    def _entity_candidates(
        self,
        session: Session,
        index_id: UUID,
        query_text: str,
        filters: RetrievalFilters,
        deadline: float | None,
    ) -> tuple[_RetrievalCandidate, ...]:
        query_entities = tuple(
            normalized_name for _, normalized_name in _technical_entities(query_text)
        )
        if not query_entities:
            return ()
        primary_document = aliased(DocumentVersionRecord)
        primary_candidate = aliased(CandidateRecord)
        score = func.length(RetrievalChunkEntityRecord.normalized_name)
        statement = (
            select(
                *_candidate_columns(),
                RetrievalChunkRecord.id.label("chunk_id"),
                RetrievalChunkRecord.text.label("chunk_text"),
                score.label("score"),
            )
            .select_from(RetrievalChunkEntityRecord)
            .join(
                RetrievalChunkRecord,
                RetrievalChunkRecord.id == RetrievalChunkEntityRecord.chunk_id,
            )
            .join(
                EvidenceSpanRecord,
                (EvidenceSpanRecord.document_version_id == RetrievalChunkRecord.document_version_id)
                & (EvidenceSpanRecord.start_offset < RetrievalChunkRecord.end_offset)
                & (EvidenceSpanRecord.end_offset > RetrievalChunkRecord.start_offset),
            )
            .join(ClaimRecord, ClaimRecord.id == EvidenceSpanRecord.claim_id)
            .join(StoryRecord, StoryRecord.id == ClaimRecord.story_id)
            .join(
                primary_document,
                primary_document.id == StoryRecord.primary_document_version_id,
            )
            .join(primary_candidate, primary_candidate.id == primary_document.candidate_id)
            .outerjoin(
                StoryPresentationRecord,
                StoryPresentationRecord.story_id == StoryRecord.id,
            )
            .where(
                *_accepted_evidence_conditions(),
                RetrievalChunkRecord.index_id == index_id,
                RetrievalChunkEntityRecord.normalized_name.in_(query_entities),
            )
        )
        statement = _apply_filters(
            statement,
            filters,
            primary_document=primary_document,
            primary_candidate=primary_candidate,
        )
        statement = _deduplicated_candidate_statement(
            statement,
            evidence_order=(
                score.desc(),
                RetrievalChunkEntityRecord.normalized_name,
                RetrievalChunkRecord.id,
            ),
            limit=self._profile.retrieval.entity_candidates,
        )
        return _unique_candidates(
            self._execute_with_deadline(session, statement, deadline)
        )

    def _catalog_candidates(
        self,
        session: Session,
        filters: RetrievalFilters,
        deadline: float | None,
    ) -> tuple[_RetrievalCandidate, ...]:
        primary_document = aliased(DocumentVersionRecord)
        primary_candidate = aliased(CandidateRecord)
        statement = (
            select(
                *_candidate_columns(),
                literal(None).label("chunk_id"),
                literal(None).label("chunk_text"),
                literal(0.0).label("score"),
            )
            .join(ClaimRecord, ClaimRecord.story_id == StoryRecord.id)
            .join(EvidenceSpanRecord, EvidenceSpanRecord.claim_id == ClaimRecord.id)
            .join(
                primary_document,
                primary_document.id == StoryRecord.primary_document_version_id,
            )
            .join(primary_candidate, primary_candidate.id == primary_document.candidate_id)
            .outerjoin(
                StoryPresentationRecord,
                StoryPresentationRecord.story_id == StoryRecord.id,
            )
            .where(*_accepted_evidence_conditions())
            .order_by(
                StoryRecord.occurred_at.desc(),
                StoryRecord.id,
                ClaimRecord.position,
                EvidenceSpanRecord.start_offset,
            )
            .limit(self._profile.retrieval.lexical_candidates)
        )
        statement = _apply_filters(
            statement,
            filters,
            primary_document=primary_document,
            primary_candidate=primary_candidate,
        )
        return _unique_candidates(
            self._execute_with_deadline(session, statement, deadline)
        )

    def _catalog_story_ids(
        self,
        session: Session,
        filters: RetrievalFilters,
        deadline: float | None,
    ) -> tuple[UUID, ...]:
        primary_document = aliased(DocumentVersionRecord)
        primary_candidate = aliased(CandidateRecord)
        statement = (
            select(
                StoryRecord.id.label("story_id"),
                StoryRecord.occurred_at.label("occurred_at"),
            )
            .join(ClaimRecord, ClaimRecord.story_id == StoryRecord.id)
            .join(EvidenceSpanRecord, EvidenceSpanRecord.claim_id == ClaimRecord.id)
            .join(
                primary_document,
                primary_document.id == StoryRecord.primary_document_version_id,
            )
            .join(primary_candidate, primary_candidate.id == primary_document.candidate_id)
            .outerjoin(
                StoryPresentationRecord,
                StoryPresentationRecord.story_id == StoryRecord.id,
            )
            .where(*_accepted_evidence_conditions())
            .distinct()
            .order_by(StoryRecord.occurred_at.desc(), StoryRecord.id)
        )
        statement = _apply_filters(
            statement,
            filters,
            primary_document=primary_document,
            primary_candidate=primary_candidate,
        )
        return tuple(
            row.story_id
            for row in self._execute_with_deadline(session, statement, deadline)
        )


def retrieval_health_snapshot(engine: Engine) -> RetrievalHealthSnapshot:
    with Session(engine) as session:
        active = session.scalar(
            select(RetrievalIndexRecord).where(RetrievalIndexRecord.state == "active")
        )
        states = tuple(
            RetrievalRuntimeStageStatus(
                stage=record.stage,
                index_id=record.index_id,
                state=record.state,
                model_id=record.model_id,
                revision=record.revision,
                artifact_sha256=record.artifact_sha256,
                fault_code=record.fault_code,
                updated_at=record.updated_at,
            )
            for record in session.scalars(
                select(RetrievalRuntimeStateRecord).order_by(RetrievalRuntimeStateRecord.stage)
            )
        )
        documents_pending_index = (
            session.scalar(_documents_pending_index_statement(active.id))
            if active is not None
            else 0
        )
    return RetrievalHealthSnapshot(
        active_index_id=active.id if active is not None else None,
        profile_id=active.profile_id if active is not None else None,
        profile_sha256=active.profile_sha256 if active is not None else None,
        documents_indexed=active.documents_indexed if active is not None else 0,
        chunks_indexed=active.chunks_indexed if active is not None else 0,
        embeddings_indexed=active.embeddings_indexed if active is not None else 0,
        index_fault_code=active.fault_code if active is not None else None,
        stages=states,
        documents_pending_index=documents_pending_index or 0,
    )


def record_retrieval_backend_startup_state(
    engine: Engine,
    backends: ApprovedRetrievalBackends,
    *,
    clock: Callable[[], datetime] | None = None,
) -> None:
    """Invalidate prior-process model readiness before the Web process can serve."""
    profile = load_accepted_knowledge_profile()
    observed_at = (clock or (lambda: datetime.now(UTC)))()
    faults = {fault.stage: fault.code for fault in backends.faults}
    with Session(engine) as session, session.begin():
        active = session.scalar(
            select(RetrievalIndexRecord).where(RetrievalIndexRecord.state == "active")
        )
        active_index_id = (
            active.id
            if active is not None
            and active.profile_id == profile.profile_id
            and active.profile_sha256 == _profile_sha256(profile)
            else None
        )
        for stage, backend, model in (
            ("embedding", backends.embedding, profile.embedding),
            ("reranker", backends.reranker, profile.reranker),
        ):
            fault_code = faults.get(stage)
            if fault_code is None:
                fault_code = (
                    f"{stage}-not-probed" if backend is not None else f"{stage}-unavailable"
                )
            values = {
                "stage": stage,
                "index_id": active_index_id,
                "state": "unavailable",
                "model_id": model.model_id,
                "revision": model.revision,
                "artifact_sha256": model.artifact_sha256,
                "fault_code": fault_code,
                "fault_detail": (
                    "The current Web process has not completed a successful inference"
                    if backend is not None and fault_code.endswith("-not-probed")
                    else "The approved model failed to load for the current Web process"
                ),
                "updated_at": observed_at,
            }
            session.execute(
                insert(RetrievalRuntimeStateRecord)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=[RetrievalRuntimeStateRecord.stage],
                    set_={key: value for key, value in values.items() if key != "stage"},
                )
            )


def _candidate_columns() -> tuple[Any, ...]:
    return (
        StoryRecord.id.label("story_id"),
        StoryRecord.stable_key.label("story_stable_key"),
        StoryRecord.headline.label("story_headline"),
        ClaimRecord.id.label("claim_id"),
        ClaimRecord.text.label("claim_text"),
        EvidenceSpanRecord.id.label("evidence_span_id"),
        EvidenceSpanRecord.exact_text.label("exact_text"),
    )


def _deduplicated_candidate_statement(
    statement: Any,
    *,
    evidence_order: tuple[Any, ...],
    limit: int,
) -> Any:
    ranked = statement.add_columns(
        func.row_number()
        .over(
            partition_by=EvidenceSpanRecord.id,
            order_by=evidence_order,
        )
        .label("evidence_rank")
    ).subquery()
    return (
        select(
            ranked.c.story_id,
            ranked.c.story_stable_key,
            ranked.c.story_headline,
            ranked.c.claim_id,
            ranked.c.claim_text,
            ranked.c.evidence_span_id,
            ranked.c.exact_text,
            ranked.c.chunk_id,
            ranked.c.chunk_text,
            ranked.c.score,
        )
        .where(ranked.c.evidence_rank == 1)
        .order_by(ranked.c.score.desc(), ranked.c.evidence_span_id)
        .limit(limit)
    )


def _matching_story_ids(
    rankings: tuple[tuple[_RetrievalCandidate, ...], ...],
    lexical_story_ids: tuple[UUID, ...],
) -> tuple[UUID, ...]:
    ranked = tuple(
        dict.fromkeys(candidate.story_id for candidates in rankings for candidate in candidates)
    )
    seen = set(ranked)
    return ranked + tuple(story_id for story_id in lexical_story_ids if story_id not in seen)


def _documents_pending_index_statement(index_id: UUID) -> Any:
    indexed_document = (
        select(RetrievalChunkRecord.id)
        .where(
            RetrievalChunkRecord.index_id == index_id,
            RetrievalChunkRecord.document_version_id == DocumentVersionRecord.id,
        )
        .exists()
    )
    pending_documents = (
        select(DocumentVersionRecord.id)
        .join(
            EvidenceSpanRecord,
            EvidenceSpanRecord.document_version_id == DocumentVersionRecord.id,
        )
        .join(ClaimRecord, ClaimRecord.id == EvidenceSpanRecord.claim_id)
        .join(StoryRecord, StoryRecord.id == ClaimRecord.story_id)
        .where(*_accepted_evidence_conditions(), ~indexed_document)
        .distinct()
        .subquery()
    )
    return select(func.count()).select_from(pending_documents)


def _accepted_evidence_conditions() -> tuple[Any, ...]:
    return (
        StoryRecord.review_state == StoryReviewState.ACCEPTED.value,
        EvidenceSpanRecord.relation == EvidenceRelation.SUPPORTS.value,
        EvidenceSpanRecord.role != EvidenceRole.COMMUNITY.value,
        PublicPublicationRepository.public_story_exists(StoryRecord.id),
    )


def _apply_filters(
    statement: Any,
    filters: RetrievalFilters,
    *,
    primary_document: Any,
    primary_candidate: Any,
) -> Any:
    if filters.publisher is not None and filters.publisher.strip():
        statement = statement.where(primary_candidate.publisher == filters.publisher.strip())
    if filters.topic is not None:
        statement = statement.where(StoryPresentationRecord.primary_topic == filters.topic.value)
    if filters.publication_date is not None:
        statement = statement.where(
            primary_document.published_at.is_not(None),
            func.date(primary_document.published_at) == filters.publication_date,
        )
    if filters.occurred_from is not None:
        statement = statement.where(StoryRecord.occurred_at >= filters.occurred_from)
    if filters.occurred_to is not None:
        statement = statement.where(StoryRecord.occurred_at < filters.occurred_to)
    if filters.time_semantics:
        source_publication_at = (
            select(DocumentVersionRecord.published_at)
            .where(DocumentVersionRecord.id == EvidenceSpanRecord.document_version_id)
            .correlate(EvidenceSpanRecord)
            .scalar_subquery()
        )
        discovery_at = (
            select(CandidateRecord.discovered_at)
            .select_from(DocumentVersionRecord)
            .join(CandidateRecord, CandidateRecord.id == DocumentVersionRecord.candidate_id)
            .where(DocumentVersionRecord.id == EvidenceSpanRecord.document_version_id)
            .correlate(EvidenceSpanRecord)
            .scalar_subquery()
        )
        editorial_at = (
            select(func.max(AuditEventRecord.occurred_at))
            .where(
                AuditEventRecord.subject_type == AuditSubjectType.STORY.value,
                AuditEventRecord.subject_id == StoryRecord.id,
                AuditEventRecord.action == AuditAction.STORY_ACCEPTED.value,
            )
            .correlate(StoryRecord)
            .scalar_subquery()
        )
        digest_publication_at = (
            select(func.max(DigestRecord.published_at))
            .select_from(DigestStoryRecord)
            .join(DigestRecord, DigestRecord.id == DigestStoryRecord.digest_id)
            .where(
                DigestStoryRecord.story_id == StoryRecord.id,
                DigestRecord.state == DigestState.PUBLISHED.value,
                DigestRecord.published_at.is_not(None),
            )
            .correlate(StoryRecord)
            .scalar_subquery()
        )
        timestamps = {
            "event": StoryRecord.occurred_at,
            "source-publication": source_publication_at,
            "discovery": discovery_at,
            "editorial": editorial_at,
            "digest-publication": digest_publication_at,
        }
        time_conditions = []
        for semantic in filters.time_semantics:
            timestamp = timestamps[semantic]
            bounds = []
            if filters.time_from is not None:
                bounds.append(timestamp >= filters.time_from)
            if filters.time_to is not None:
                bounds.append(timestamp < filters.time_to)
            if bounds:
                time_conditions.append(and_(*bounds))
        if time_conditions:
            statement = statement.where(or_(*time_conditions))
    return statement


def _unique_candidates(rows: Any) -> tuple[_RetrievalCandidate, ...]:
    candidates: list[_RetrievalCandidate] = []
    seen: set[UUID] = set()
    for row in rows:
        if row.evidence_span_id in seen:
            continue
        seen.add(row.evidence_span_id)
        candidates.append(
            _RetrievalCandidate(
                story_id=row.story_id,
                story_stable_key=row.story_stable_key,
                story_headline=row.story_headline,
                claim_id=row.claim_id,
                claim_text=row.claim_text,
                evidence_span_id=row.evidence_span_id,
                exact_text=row.exact_text,
                chunk_id=row.chunk_id,
                chunk_text=row.chunk_text,
                score=float(row.score),
            )
        )
    return tuple(candidates)


def _merge_lexical_candidates(
    indexed: tuple[_RetrievalCandidate, ...],
    accepted_projection: tuple[_RetrievalCandidate, ...],
    limit: int,
) -> tuple[_RetrievalCandidate, ...]:
    candidates: dict[UUID, tuple[_RetrievalCandidate, int, int]] = {}
    for source_order, source in enumerate((indexed, accepted_projection)):
        for source_rank, candidate in enumerate(source):
            current = candidates.get(candidate.evidence_span_id)
            if current is None:
                candidates[candidate.evidence_span_id] = (
                    candidate,
                    source_order,
                    source_rank,
                )
                continue
            preferred = (
                current
                if current[1] <= source_order
                else (
                    candidate,
                    source_order,
                    source_rank,
                )
            )
            candidates[candidate.evidence_span_id] = (
                replace(
                    preferred[0],
                    score=max(current[0].score, candidate.score),
                ),
                preferred[1],
                preferred[2],
            )
    return tuple(
        item[0]
        for item in sorted(
            candidates.values(),
            key=lambda item: (
                -item[0].score,
                item[1],
                item[2],
                str(item[0].evidence_span_id),
            ),
        )[:limit]
    )


def _fuse_candidates(
    rankings: dict[str, tuple[_RetrievalCandidate, ...]],
    profile: AcceptedKnowledgeProfile,
) -> tuple[_RetrievalCandidate, ...]:
    candidates: dict[UUID, _RetrievalCandidate] = {}
    scores: dict[UUID, float] = {}
    first_seen: dict[UUID, tuple[int, int]] = {}
    for channel_order, channel in enumerate(("lexical", "semantic", "exact_entity")):
        weight = profile.fusion.weights[channel]
        for rank, candidate in enumerate(rankings[channel], start=1):
            identifier = candidate.evidence_span_id
            scores[identifier] = scores.get(identifier, 0.0) + weight / (
                profile.fusion.rrf_k + rank
            )
            candidates.setdefault(identifier, candidate)
            first_seen.setdefault(identifier, (channel_order, rank))
    return tuple(
        replace(candidates[identifier], score=scores[identifier])
        for identifier in sorted(
            candidates,
            key=lambda identifier: (
                -scores[identifier],
                first_seen[identifier],
                str(identifier),
            ),
        )
    )


def _rerank_candidate_pool(
    fused: tuple[_RetrievalCandidate, ...],
    rankings: Mapping[str, tuple[_RetrievalCandidate, ...]],
    profile: AcceptedKnowledgeProfile,
) -> tuple[_RetrievalCandidate, ...]:
    selected = list(fused[: profile.retrieval.rerank_depth])
    protected_ids: set[UUID] = set()
    for channel in profile.fallback_channels:
        channel_candidates = rankings[channel]
        if not channel_candidates:
            continue
        candidate = channel_candidates[0]
        candidate_id = candidate.evidence_span_id
        protected_ids.add(candidate_id)
        if any(item.evidence_span_id == candidate_id for item in selected):
            continue
        if len(selected) < profile.retrieval.rerank_depth:
            selected.append(candidate)
            continue
        replacement = next(
            (
                index
                for index in range(len(selected) - 1, -1, -1)
                if selected[index].evidence_span_id not in protected_ids
            ),
            None,
        )
        if replacement is not None:
            selected[replacement] = candidate
    return tuple(selected)


def _stage_candidates(
    stage: str,
    candidates: tuple[_RetrievalCandidate, ...],
) -> tuple[RetrievalStageCandidate, ...]:
    return tuple(
        RetrievalStageCandidate(
            stage=stage,
            evidence_span_id=candidate.evidence_span_id,
            rank=rank,
            score=candidate.score,
            chunk_id=candidate.chunk_id,
        )
        for rank, candidate in enumerate(candidates, start=1)
    )


def _validate_vector(vector: Sequence[float], dimensions: int) -> None:
    if len(vector) != dimensions or any(not math.isfinite(value) for value in vector):
        raise ValueError("Embedding vector does not match the approved profile")


def _normalized_embedding(
    vector: Sequence[float],
    dimensions: int,
) -> tuple[float, ...]:
    converted = tuple(float(value) for value in vector)
    _validate_vector(converted, dimensions)
    norm = math.sqrt(sum(value * value for value in converted))
    if not math.isfinite(norm) or norm == 0.0:
        raise ValueError("Embedding vector cannot be L2-normalized")
    return tuple(value / norm for value in converted)


def _truncate_profile_tokens(text: str, token_limit: int) -> str:
    tokens = tuple(TOKEN.finditer(text))
    if len(tokens) <= token_limit:
        return text
    return text[: tokens[token_limit - 1].end()]


def _fts_query_text(question: str) -> str:
    terms = tuple(
        normalized
        for term in QUERY_TERM.findall(question)
        if (normalized := _normalized_query_term(term)) is not None
    )
    return " ".join(dict.fromkeys(terms))


def _normalized_query_term(term: str) -> str | None:
    if term in GENERIC_QUESTION_TERMS:
        return None
    if CHINESE_CHARACTER.search(term) is None:
        return term
    normalized = term
    for prefix in QUESTION_PREFIXES:
        if normalized.startswith(prefix) and len(normalized) > len(prefix):
            normalized = normalized.removeprefix(prefix)
            break
    for suffix in QUESTION_SUFFIXES:
        if normalized.endswith(suffix) and len(normalized) > len(suffix):
            normalized = normalized.removesuffix(suffix)
            break
    if not normalized or normalized in GENERIC_QUESTION_TERMS:
        return None
    return normalized


def build_document_chunks(
    source: ChunkSource,
    profile: AcceptedKnowledgeProfile,
) -> tuple[KnowledgeChunk, ...]:
    try:
        window = profile.chunking.document_types[source.document_type]
    except KeyError as error:
        raise AcceptedKnowledgeConfigurationError(
            f"No Chunk policy exists for Document type {source.document_type}"
        ) from error
    tokens = tuple(TOKEN.finditer(source.text))
    if not tokens:
        return ()
    chunks: list[KnowledgeChunk] = []
    first_token = 0
    while first_token < len(tokens):
        last_token = min(first_token + window.max_tokens, len(tokens))
        start_offset = tokens[first_token].start()
        end_offset = tokens[last_token - 1].end()
        chunks.append(
            KnowledgeChunk(
                id=uuid5(
                    NAMESPACE_URL,
                    "ai-intel-agent:retrieval-chunk:"
                    f"{profile.profile_id}:{source.document_version_id}:"
                    f"{start_offset}:{end_offset}",
                ),
                document_version_id=source.document_version_id,
                document_type=source.document_type,
                language=source.language,
                ordinal=len(chunks),
                start_offset=start_offset,
                end_offset=end_offset,
                token_count=last_token - first_token,
                text=source.text[start_offset:end_offset],
            )
        )
        if last_token == len(tokens):
            break
        first_token = last_token - window.overlap_tokens
    return tuple(chunks)


def _document_type(
    document: DocumentVersionRecord,
    record_kind: str | None,
) -> str:
    normalized_kind = (record_kind or "").casefold()
    if normalized_kind == "paper":
        return "paper"
    if normalized_kind in {
        "release-notes-section",
        "model-release",
        "software-release",
    }:
        return "release"
    if normalized_kind in {"documentation", "technical-documentation"}:
        return "documentation"
    identity = f"{document.source_url} {document.title}".casefold()
    if any(marker in identity for marker in ("arxiv.org", "/paper", " research paper")):
        return "paper"
    if any(
        marker in identity for marker in ("/release", "release notes", "release-notes", "changelog")
    ):
        return "release"
    if any(marker in identity for marker in ("/docs", "documentation", "readme")):
        return "documentation"
    return "technical_note"


def _technical_entities(text: str) -> tuple[tuple[str, str], ...]:
    entities: dict[str, str] = {}
    for match in TOKEN.finditer(text):
        display_name = match.group(0)
        if len(display_name) < 2:
            continue
        normalized_name = _normalize_entity(display_name)
        has_technical_punctuation = any(value in display_name for value in ".-_/+:")
        has_digit = any(value.isdigit() for value in display_name)
        has_mixed_case = any(value.islower() for value in display_name) and any(
            value.isupper() for value in display_name[1:]
        )
        is_uppercase_acronym = (
            display_name.isascii() and display_name.isalpha() and display_name.isupper()
        )
        is_known_technical_name = normalized_name in KNOWN_TECHNICAL_ENTITIES
        if not (
            has_technical_punctuation
            or has_digit
            or has_mixed_case
            or is_uppercase_acronym
            or is_known_technical_name
        ):
            continue
        if normalized_name:
            entities.setdefault(normalized_name, display_name)
    return tuple((display_name, name) for name, display_name in sorted(entities.items()))


def _normalize_entity(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).casefold().split())


def validate_approved_model_artifacts(
    configuration: RetrievalModelConfiguration,
    *,
    runtime_version: str | None = None,
    cpu_features: frozenset[str] | None = None,
    artifact_digest: Callable[[Path], str] | None = None,
    snapshot_verifier: Callable[[Path, str, str, tuple[str, ...]], None] | None = None,
) -> ApprovedModelArtifactCheck:
    profile = load_accepted_knowledge_profile()
    if runtime_version is None:
        try:
            runtime_version = package_version("fastembed")
        except PackageNotFoundError as error:
            raise AcceptedKnowledgeConfigurationError(
                "Approved FastEmbed runtime is not installed"
            ) from error
    expected_runtime = profile.embedding.runtime_version.removeprefix("fastembed==")
    if runtime_version != expected_runtime:
        raise AcceptedKnowledgeConfigurationError(
            "FastEmbed runtime does not match the approved Retrieval Profile"
        )
    digest = artifact_digest or _sha256_file
    verify_snapshot = snapshot_verifier or _verify_git_model_snapshot
    verify_snapshot(
        configuration.embedding_model_dir,
        profile.embedding.source_repository,
        profile.embedding.revision,
        profile.embedding.metadata_files,
    )
    embedding_path = _approved_artifact_path(
        configuration.embedding_model_dir,
        profile.embedding.artifact_path,
    )
    reranker_path = _approved_artifact_path(
        configuration.reranker_model_dir,
        profile.reranker.artifact_path,
    )
    _require_artifact_digest(
        embedding_path,
        profile.embedding.artifact_sha256,
        digest,
    )
    verify_snapshot(
        configuration.reranker_model_dir,
        profile.reranker.source_repository,
        profile.reranker.revision,
        profile.reranker.metadata_files,
    )
    features = cpu_features if cpu_features is not None else _detected_cpu_features()
    if profile.reranker.required_cpu_feature.casefold() not in {
        feature.casefold() for feature in features
    }:
        raise AcceptedKnowledgeConfigurationError(
            "Approved mMARCO AVX2 Reranker requires the AVX2 CPU feature"
        )
    _require_artifact_digest(
        reranker_path,
        profile.reranker.artifact_sha256,
        digest,
    )
    return ApprovedModelArtifactCheck(
        ready=True,
        runtime_version=profile.embedding.runtime_version,
        embedding=ApprovedModelArtifact(
            role="embedding",
            model_id=profile.embedding.model_id,
            revision=profile.embedding.revision,
            artifact_path=embedding_path,
            artifact_sha256=profile.embedding.artifact_sha256,
            cpu_feature=None,
        ),
        reranker=ApprovedModelArtifact(
            role="reranker",
            model_id=profile.reranker.model_id,
            revision=profile.reranker.revision,
            artifact_path=reranker_path,
            artifact_sha256=profile.reranker.artifact_sha256,
            cpu_feature=profile.reranker.required_cpu_feature,
        ),
    )


def load_approved_fastembed_backends(
    configuration: RetrievalModelConfiguration,
) -> ApprovedRetrievalBackends:
    """Load only the two pinned local artifacts; return model-free fallbacks on faults."""
    profile = load_accepted_knowledge_profile()
    try:
        runtime_version = package_version("fastembed")
    except PackageNotFoundError:
        return ApprovedRetrievalBackends(
            embedding=None,
            reranker=None,
            faults=(
                RetrievalBackendFault("embedding", "embedding-unavailable"),
                RetrievalBackendFault("reranker", "reranker-unavailable"),
            ),
        )
    expected_runtime = profile.embedding.runtime_version.removeprefix("fastembed==")
    if runtime_version != expected_runtime:
        return ApprovedRetrievalBackends(
            embedding=None,
            reranker=None,
            faults=(
                RetrievalBackendFault("embedding", "embedding-unavailable"),
                RetrievalBackendFault("reranker", "reranker-unavailable"),
            ),
        )

    faults: list[RetrievalBackendFault] = []
    embedding_backend: EmbeddingBackend | None = None
    reranker_backend: RerankerBackend | None = None
    try:
        _verify_git_model_snapshot(
            configuration.embedding_model_dir,
            profile.embedding.source_repository,
            profile.embedding.revision,
            profile.embedding.metadata_files,
        )
        embedding_path = _approved_artifact_path(
            configuration.embedding_model_dir,
            profile.embedding.artifact_path,
        )
        _require_artifact_digest(
            embedding_path,
            profile.embedding.artifact_sha256,
            _sha256_file,
        )
        from fastembed import TextEmbedding

        embedding_model = TextEmbedding(
            model_name=profile.embedding.model_id,
            threads=configuration.threads,
            providers=["CPUExecutionProvider"],
            cuda=False,
            local_files_only=True,
            specific_model_path=str(configuration.embedding_model_dir),
        )
        embedding_backend = FastEmbedEmbeddingBackend(
            embedding_model,
            profile=profile,
        )
    except Exception:  # noqa: BLE001 - the supported FTS + Entity fallback remains active.
        faults.append(RetrievalBackendFault("embedding", "embedding-unavailable"))

    try:
        if profile.reranker.required_cpu_feature.casefold() not in _detected_cpu_features():
            raise AcceptedKnowledgeConfigurationError("AVX2 is unavailable")
        _verify_git_model_snapshot(
            configuration.reranker_model_dir,
            profile.reranker.source_repository,
            profile.reranker.revision,
            profile.reranker.metadata_files,
        )
        reranker_path = _approved_artifact_path(
            configuration.reranker_model_dir,
            profile.reranker.artifact_path,
        )
        _require_artifact_digest(
            reranker_path,
            profile.reranker.artifact_sha256,
            _sha256_file,
        )
        from fastembed.common.model_description import ModelSource
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        registered = tuple(
            item
            for item in TextCrossEncoder.list_supported_models()
            if item["model"] == profile.reranker.model_id
        )
        if not registered:
            TextCrossEncoder.add_custom_model(
                model=profile.reranker.model_id,
                sources=ModelSource(hf=profile.reranker.model_id),
                model_file=profile.reranker.artifact_path,
            )
        elif len(registered) != 1 or registered[0]["model_file"] != profile.reranker.artifact_path:
            raise AcceptedKnowledgeConfigurationError(
                "FastEmbed registered an incompatible mMARCO artifact"
            )
        reranker_model = TextCrossEncoder(
            model_name=profile.reranker.model_id,
            threads=configuration.threads,
            providers=["CPUExecutionProvider"],
            cuda=False,
            local_files_only=True,
            specific_model_path=str(configuration.reranker_model_dir),
        )
        reranker_backend = FastEmbedRerankerBackend(
            reranker_model,
            profile=profile,
        )
    except Exception:  # noqa: BLE001 - Fusion order is the supported Reranker fallback.
        faults.append(RetrievalBackendFault("reranker", "reranker-unavailable"))
    return ApprovedRetrievalBackends(
        embedding=embedding_backend,
        reranker=reranker_backend,
        faults=tuple(faults),
    )


def _approved_artifact_path(model_dir: Path, relative_path: str) -> Path:
    if not model_dir.is_dir():
        raise AcceptedKnowledgeConfigurationError(
            f"Retrieval model directory is unavailable: {model_dir}"
        )
    resolved_root = model_dir.resolve()
    artifact_path = (resolved_root / relative_path).resolve()
    if artifact_path == resolved_root or resolved_root not in artifact_path.parents:
        raise AcceptedKnowledgeConfigurationError(
            "Retrieval artifact path escapes its configured model directory"
        )
    if not artifact_path.is_file():
        raise AcceptedKnowledgeConfigurationError(
            f"Approved Retrieval artifact is unavailable: {artifact_path}"
        )
    return artifact_path


def _verify_git_model_snapshot(
    model_dir: Path,
    source_repository: str,
    revision: str,
    metadata_files: tuple[str, ...],
) -> None:
    if not model_dir.is_dir():
        raise AcceptedKnowledgeConfigurationError(
            f"Retrieval model snapshot directory is unavailable: {model_dir}"
        )
    resolved_root = model_dir.resolve()
    top_level = Path(
        _run_git_snapshot_check(resolved_root, "rev-parse", "--show-toplevel")
    ).resolve()
    if top_level != resolved_root:
        raise AcceptedKnowledgeConfigurationError(
            "Retrieval model directory must be the root of its Git snapshot"
        )
    _reject_git_snapshot_lazy_fetch(resolved_root)
    observed_revision = _run_git_snapshot_check(
        resolved_root,
        "rev-parse",
        "--verify",
        "HEAD",
    ).casefold()
    if observed_revision != revision:
        raise AcceptedKnowledgeConfigurationError(
            "Retrieval model snapshot revision does not match the approved Profile"
        )
    observed_remote = _run_git_snapshot_check(
        resolved_root,
        "remote",
        "get-url",
        "origin",
    )
    expected_remote = f"https://huggingface.co/{source_repository}"
    if observed_remote.rstrip("/").removesuffix(".git") != expected_remote:
        raise AcceptedKnowledgeConfigurationError(
            "Retrieval model snapshot repository does not match the approved Profile"
        )
    for relative_path in metadata_files:
        _verify_git_snapshot_metadata(
            resolved_root,
            revision,
            relative_path,
        )


def _verify_git_snapshot_metadata(
    model_dir: Path,
    revision: str,
    relative_path: str,
) -> None:
    working_path = _approved_artifact_path(model_dir, relative_path)
    committed_content = _read_git_snapshot_blob(model_dir, revision, relative_path)
    pointer = _parse_git_lfs_pointer(committed_content)
    try:
        if pointer is not None:
            expected_sha256, expected_size = pointer
            matches = (
                working_path.stat().st_size == expected_size
                and _sha256_file(working_path) == expected_sha256
            )
        else:
            matches = working_path.read_bytes() == committed_content
    except OSError as error:
        raise AcceptedKnowledgeConfigurationError(
            "Retrieval model metadata could not be read"
        ) from error
    if not matches:
        raise AcceptedKnowledgeConfigurationError(
            "Retrieval model metadata differs from the approved Git snapshot"
        )


def _parse_git_lfs_pointer(content: bytes) -> tuple[str, int] | None:
    if not content.startswith(b"version https://git-lfs.github.com/spec/"):
        return None
    match = GIT_LFS_POINTER.fullmatch(content)
    if match is None:
        raise AcceptedKnowledgeConfigurationError(
            "Retrieval model metadata has a malformed Git LFS pointer"
        )
    return match.group(1).decode("ascii"), int(match.group(2))


def _read_git_snapshot_blob(
    model_dir: Path,
    revision: str,
    relative_path: str,
) -> bytes:
    resolved_root = model_dir.resolve()
    try:
        completed = subprocess.run(
            (
                "git",
                "--no-replace-objects",
                "-c",
                f"safe.directory={resolved_root}",
                "-C",
                str(resolved_root),
                "cat-file",
                "blob",
                f"{revision}:{relative_path}",
            ),
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise AcceptedKnowledgeConfigurationError(
            "Retrieval model Git snapshot could not be verified"
        ) from error
    if completed.returncode != 0:
        raise AcceptedKnowledgeConfigurationError(
            "Retrieval model Git snapshot does not match the approved revision"
        )
    return completed.stdout


def _reject_git_snapshot_lazy_fetch(model_dir: Path) -> None:
    resolved_root = model_dir.resolve()
    try:
        completed = subprocess.run(
            (
                "git",
                "--no-replace-objects",
                "-c",
                f"safe.directory={resolved_root}",
                "-C",
                str(resolved_root),
                "config",
                "--includes",
                "--get-regexp",
                r"^(extensions\.partial[Cc]lone|remote\..*\.promisor)$",
            ),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise AcceptedKnowledgeConfigurationError(
            "Retrieval model Git snapshot could not be verified"
        ) from error
    if completed.returncode == 0:
        raise AcceptedKnowledgeConfigurationError(
            "Retrieval model Git snapshot must not be a partial or promisor repository"
        )
    if completed.returncode != 1:
        raise AcceptedKnowledgeConfigurationError(
            "Retrieval model Git snapshot completeness could not be verified"
        )


def _run_git_snapshot_check(
    model_dir: Path,
    *arguments: str,
) -> str:
    resolved_root = model_dir.resolve()
    try:
        completed = subprocess.run(
            (
                "git",
                "-c",
                f"safe.directory={resolved_root}",
                "-C",
                str(resolved_root),
                *arguments,
            ),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise AcceptedKnowledgeConfigurationError(
            "Retrieval model Git snapshot could not be verified"
        ) from error
    if completed.returncode != 0:
        raise AcceptedKnowledgeConfigurationError(
            "Retrieval model Git snapshot does not match the approved revision"
        )
    output = completed.stdout.strip()
    if not output:
        raise AcceptedKnowledgeConfigurationError(
            "Retrieval model Git snapshot returned no identity"
        )
    return output


def _require_artifact_digest(
    artifact_path: Path,
    expected_sha256: str,
    digest: Callable[[Path], str],
) -> None:
    observed = digest(artifact_path).casefold()
    if observed != expected_sha256:
        raise AcceptedKnowledgeConfigurationError(
            f"Approved Retrieval artifact SHA-256 mismatch: {artifact_path}"
        )


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _detected_cpu_features() -> frozenset[str]:
    cpuinfo = Path("/proc/cpuinfo")
    if not cpuinfo.is_file():
        return frozenset()
    try:
        lines = cpuinfo.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return frozenset()
    features: set[str] = set()
    for line in lines:
        label, separator, values = line.partition(":")
        if separator and label.strip().casefold() in {"flags", "features"}:
            features.update(value.casefold() for value in values.split())
    return frozenset(features)


def load_accepted_knowledge_profile() -> AcceptedKnowledgeProfile:
    try:
        raw = json.loads(
            files("ai_intel_agent").joinpath("data", PROFILE_RESOURCE).read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AcceptedKnowledgeConfigurationError(
            "Accepted-knowledge Retrieval Profile is unreadable"
        ) from error
    if not isinstance(raw, dict):
        raise AcceptedKnowledgeConfigurationError(
            "Accepted-knowledge Retrieval Profile must be an object"
        )
    profile = _parse_profile(raw)
    _validate_approved_profile(profile)
    return profile


def _profile_sha256(profile: AcceptedKnowledgeProfile) -> str:
    return sha256(
        json.dumps(
            asdict(profile),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _parse_profile(raw: dict[str, Any]) -> AcceptedKnowledgeProfile:
    embedding = _required_dict(raw, "embedding")
    reranker = _required_dict(raw, "reranker")
    chunking = _required_dict(raw, "chunking")
    retrieval = _required_dict(raw, "retrieval")
    fusion = _required_dict(raw, "fusion")
    document_types = _required_dict(chunking, "document_types")
    weights = _required_dict(fusion, "weights")
    fallback_channels = raw.get("fallback_channels")
    if not isinstance(fallback_channels, list) or not all(
        isinstance(value, str) for value in fallback_channels
    ):
        raise AcceptedKnowledgeConfigurationError("fallback_channels must be a text list")
    return AcceptedKnowledgeProfile(
        schema_version=_required_text(raw, "schema_version"),
        profile_id=_required_text(raw, "profile_id"),
        embedding=EmbeddingProfile(
            provider=_required_text(embedding, "provider"),
            model_id=_required_text(embedding, "model_id"),
            source_repository=_required_text(embedding, "source_repository"),
            revision=_required_text(embedding, "revision"),
            artifact_path=_required_text(embedding, "artifact_path"),
            artifact_sha256=_required_text(embedding, "artifact_sha256"),
            pooling=_required_text(embedding, "pooling"),
            normalization=_required_text(embedding, "normalization"),
            token_limit=_required_int(embedding, "token_limit"),
            dimensions=_required_int(embedding, "dimensions"),
            distance_metric=_required_text(embedding, "distance_metric"),
            runtime_version=_required_text(embedding, "runtime_version"),
            rebuild_lifecycle=_required_text(embedding, "rebuild_lifecycle"),
            metadata_files=_required_text_tuple(embedding, "metadata_files"),
        ),
        reranker=RerankerProfile(
            provider=_required_text(reranker, "provider"),
            model_id=_required_text(reranker, "model_id"),
            source_repository=_required_text(reranker, "source_repository"),
            revision=_required_text(reranker, "revision"),
            artifact_path=_required_text(reranker, "artifact_path"),
            artifact_sha256=_required_text(reranker, "artifact_sha256"),
            quantization=_required_text(reranker, "quantization"),
            required_cpu_feature=_required_text(reranker, "required_cpu_feature"),
            runtime_version=_required_text(reranker, "runtime_version"),
            metadata_files=_required_text_tuple(reranker, "metadata_files"),
        ),
        chunking=ChunkingProfile(
            strategy=_required_text(chunking, "strategy"),
            tokenizer=_required_text(chunking, "tokenizer"),
            document_types={
                name: ChunkWindow(
                    max_tokens=_required_int(_as_dict(value, name), "max_tokens"),
                    overlap_tokens=_required_int(_as_dict(value, name), "overlap_tokens"),
                )
                for name, value in document_types.items()
            },
        ),
        retrieval=RetrievalLimits(
            lexical_candidates=_required_int(retrieval, "lexical_candidates"),
            semantic_candidates=_required_int(retrieval, "semantic_candidates"),
            entity_candidates=_required_int(retrieval, "entity_candidates"),
            rerank_depth=_required_int(retrieval, "rerank_depth"),
            final_top_k=_required_int(retrieval, "final_top_k"),
        ),
        fusion=FusionProfile(
            method=_required_text(fusion, "method"),
            rrf_k=_required_int(fusion, "rrf_k"),
            weights={name: _required_number(weights, name) for name in weights},
        ),
        entity_policy=_required_text(raw, "entity_policy"),
        fallback_channels=tuple(fallback_channels),
    )


def _validate_approved_profile(profile: AcceptedKnowledgeProfile) -> None:
    embedding = profile.embedding
    reranker = profile.reranker
    if (
        profile.schema_version != PROFILE_SCHEMA_VERSION
        or profile.profile_id != APPROVED_PROFILE_ID
        or embedding.provider != "fastembed"
        or embedding.model_id != APPROVED_EMBEDDING_MODEL_ID
        or embedding.source_repository != APPROVED_EMBEDDING_SOURCE_REPOSITORY
        or embedding.revision != APPROVED_EMBEDDING_REVISION
        or embedding.artifact_path != "model_optimized.onnx"
        or embedding.artifact_sha256 != APPROVED_EMBEDDING_SHA256
        or embedding.pooling != "mean"
        or embedding.normalization != "l2"
        or embedding.token_limit != 128
        or embedding.dimensions != 384
        or embedding.distance_metric != "cosine"
        or embedding.runtime_version != APPROVED_RUNTIME_VERSION
        or embedding.rebuild_lifecycle != "new-profile-then-atomic-activate"
        or embedding.metadata_files != APPROVED_MODEL_METADATA_FILES
        or reranker.provider != "fastembed"
        or reranker.model_id != APPROVED_RERANKER_MODEL_ID
        or reranker.source_repository != APPROVED_RERANKER_SOURCE_REPOSITORY
        or reranker.revision != APPROVED_RERANKER_REVISION
        or reranker.artifact_path != "onnx/model_quint8_avx2.onnx"
        or reranker.artifact_sha256 != APPROVED_RERANKER_SHA256
        or reranker.quantization != "avx2-uint8"
        or reranker.required_cpu_feature != "avx2"
        or reranker.runtime_version != APPROVED_RUNTIME_VERSION
        or reranker.metadata_files != APPROVED_MODEL_METADATA_FILES
        or profile.retrieval.rerank_depth != 8
        or profile.retrieval.final_top_k != 5
        or profile.entity_policy != APPROVED_ENTITY_POLICY
        or profile.fallback_channels != ("lexical", "exact_entity")
    ):
        raise AcceptedKnowledgeConfigurationError(
            "Accepted-knowledge Retrieval Profile does not match the approved M4 identity"
        )
    if embedding.artifact_path.startswith(("/", "\\")) or reranker.artifact_path.startswith(
        ("/", "\\")
    ):
        raise AcceptedKnowledgeConfigurationError("Model artifact paths must be relative")
    if profile.chunking.strategy != "type-aware-token-windows":
        raise AcceptedKnowledgeConfigurationError("Chunking strategy is not approved")
    if profile.chunking.tokenizer != "unicode-script-windows-with-minilm-wordpiece-validation-v1":
        raise AcceptedKnowledgeConfigurationError("Chunk tokenizer contract is not approved")
    for window in profile.chunking.document_types.values():
        if not 0 <= window.overlap_tokens < window.max_tokens < embedding.token_limit:
            raise AcceptedKnowledgeConfigurationError("Chunk token window exceeds model limit")
    if profile.fusion.method != "weighted-reciprocal-rank-fusion" or set(
        profile.fusion.weights
    ) != {"lexical", "semantic", "exact_entity"}:
        raise AcceptedKnowledgeConfigurationError("Fusion profile is not approved")
    if _profile_sha256(profile) != APPROVED_PROFILE_SHA256:
        raise AcceptedKnowledgeConfigurationError(
            "Accepted-knowledge Retrieval Profile hash is not approved"
        )


def _as_dict(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AcceptedKnowledgeConfigurationError(f"{label} must be an object")
    return value


def _required_dict(mapping: dict[str, Any], key: str) -> dict[str, Any]:
    return _as_dict(mapping.get(key), key)


def _required_text(mapping: dict[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AcceptedKnowledgeConfigurationError(f"{key} must be non-empty text")
    return value


def _required_text_tuple(mapping: dict[str, Any], key: str) -> tuple[str, ...]:
    value = mapping.get(key)
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise AcceptedKnowledgeConfigurationError(f"{key} must be a non-empty text list")
    return tuple(value)


def _required_int(mapping: dict[str, Any], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise AcceptedKnowledgeConfigurationError(f"{key} must be an integer")
    return value


def _required_number(mapping: dict[str, Any], key: str) -> float:
    value = mapping.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise AcceptedKnowledgeConfigurationError(f"{key} must be numeric")
    return float(value)
