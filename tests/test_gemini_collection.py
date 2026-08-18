from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from ipaddress import IPv4Address
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pg0 import Pg0
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from ai_intel_agent import gemini_collection as gemini_collection_module
from ai_intel_agent.cli import app
from ai_intel_agent.domain import (
    DocumentVersion,
    EvidenceRelation,
    EvidenceRole,
    StoryReviewState,
)
from ai_intel_agent.gemini_collection import (
    DatedReleaseSection,
    DeepSeekGeminiDraftProvider,
    DraftPreparationError,
    GeminiSourceError,
    HttpGeminiReleaseNotesFetcher,
    load_gemini_draft_protocol,
    select_release_sections_for_backfill,
)
from ai_intel_agent.model_routing_evaluation import load_evaluation_corpus
from ai_intel_agent.persistence import (
    CandidateRecord,
    ClaimRecord,
    CollectionDiscoveryRecord,
    DocumentVersionRecord,
    EvidenceSpanRecord,
    SourceDefinitionRecord,
    StoryRecord,
    TraceRecord,
    create_database_engine,
    upgrade_database,
)

FIXTURE = Path(__file__).parent / "fixtures" / "gemini_api_release_notes.html"
RELEASE_NOTES_URL = "https://ai.google.dev/gemini-api/docs/changelog"
runner = CliRunner()
PUBLIC_ADDRESS = IPv4Address("93.184.216.34")


@dataclass(frozen=True)
class FixedClock:
    current: datetime = datetime(2026, 8, 14, tzinfo=UTC)

    def now(self) -> datetime:
        return self.current


@dataclass(frozen=True)
class StaticResolver:
    addresses: tuple[IPv4Address, ...] = (PUBLIC_ADDRESS,)

    def resolve(self, hostname: str) -> tuple[IPv4Address, ...]:
        return self.addresses


def test_deepseek_draft_constrains_live_output_to_three_short_evidence_claims() -> None:
    body = (
        "The system now supports deterministic source collection for public feeds. "
        "Every stored document keeps an immutable canonical source URL for provenance. "
        "Draft claims cite one unique exact evidence substring from the source body."
    )
    document = DocumentVersion(
        id=uuid4(),
        candidate_id=uuid4(),
        source_url="https://example.com/live-output-contract",
        title="Live output contract",
        body=body,
        content_hash=sha256(body.encode("utf-8")).hexdigest(),
        observed_at=datetime(2026, 8, 18, tzinfo=UTC),
        published_at=datetime(2026, 8, 18, tzinfo=UTC),
        published_at_raw="2026-08-18",
    )
    output = {
        "headline": "多来源采集草稿采用精确证据约束",
        "claims": [
            {
                "text": "系统支持确定性的公开 Feed 采集。",
                "evidence": (
                    "The system now supports deterministic source collection for public feeds."
                ),
            },
            {
                "text": "每个文档保存不可变的规范来源链接。",
                "evidence": (
                    "Every stored document keeps an immutable canonical source URL for provenance."
                ),
            },
            {
                "text": "草稿 Claim 使用来源正文中的唯一精确证据片段。",
                "evidence": (
                    "Draft claims cite one unique exact evidence substring from the source body."
                ),
            },
        ],
    }

    def live_output_contract(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        system_prompt = payload["messages"][0]["content"]
        constrained = all(
            requirement in system_prompt
            for requirement in (
                "Return exactly 3 claims.",
                "between 20 and 120 characters",
                "Keep the complete JSON concise.",
            )
        )
        if not constrained:
            return httpx.Response(
                200,
                request=request,
                json={
                    "model": "deepseek-v4-pro",
                    "choices": [
                        {
                            "message": {"content": '{"headline":"truncated'},
                            "finish_reason": "length",
                        }
                    ],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 1024},
                },
            )
        return httpx.Response(
            200,
            request=request,
            json={
                "model": "deepseek-v4-pro",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(output, ensure_ascii=False)
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 180},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(live_output_contract)) as client:
        prepared = DeepSeekGeminiDraftProvider(
            client,
            api_key="fixture-deepseek-key",
            sleeper=lambda _: None,
        ).prepare(document)

    assert prepared.headline == output["headline"]
    assert len(prepared.claims) == 3
    assert load_gemini_draft_protocol().maximum_claims == 3


@pytest.fixture
def gemini_database_url():
    name = f"ai_intel_gemini_{uuid4().hex}"
    data_root = os.getenv("PG0_TEST_DATA_ROOT")
    data_dir = str(Path(data_root) / name) if data_root else None
    server = Pg0(name=name, data_dir=data_dir)
    server.start()
    try:
        upgrade_database(server.uri)
        yield server.uri
    finally:
        server.drop()


@pytest.mark.postgres
def test_collect_gemini_cli_keeps_one_story_when_source_revision_changes(
    gemini_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_calls: list[dict[str, object]] = []
    fixture_html = FIXTURE.read_text(encoding="utf-8")
    revised_fixture_html = fixture_html.replace(
        "<li>The deprecated preview model shuts down on September 1, 2026.</li>",
        "<li>Deployment guidance was clarified after publication.</li>\n"
        "        <li>The deprecated preview model shuts down on September 1, 2026.</li>",
    )
    assert revised_fixture_html != fixture_html
    source_fetches = 0
    provider_output = {
        "headline": "Gemini 3.6 Flash 正式发布",
        "claims": [
            {
                "text": "Google 已正式发布 Gemini 3.6 Flash。",
                "evidence": "Gemini 3.6 Flash is generally available.",
            },
            {
                "text": "该模型面向代码与智能体规划任务。",
                "evidence": "It is a stable, production-ready model for code and agentic planning.",
            },
        ],
    }

    def handle_request(request: httpx.Request) -> httpx.Response:
        nonlocal source_fetches
        if request.method == "GET" and request.headers["host"] == "ai.google.dev":
            assert request.url.host == str(PUBLIC_ADDRESS)
            assert request.extensions["sni_hostname"] == "ai.google.dev"
            source_html = fixture_html if source_fetches == 0 else revised_fixture_html
            source_fetches += 1
            return httpx.Response(
                200,
                request=request,
                headers={"content-type": "text/html; charset=utf-8"},
                text=source_html,
            )
        if request.method == "POST" and request.url.host == "api.deepseek.com":
            payload = json.loads(request.content)
            provider_calls.append(payload)
            assert request.headers["authorization"] == "Bearer fixture-deepseek-key"
            assert payload["model"] == "deepseek-v4-pro"
            assert payload["response_format"] == {"type": "json_object"}
            return httpx.Response(
                200,
                request=request,
                json={
                    "model": "deepseek-v4-pro",
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(provider_output, ensure_ascii=False)
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 80},
                },
            )
        raise AssertionError(f"Unexpected HTTP request: {request.method} {request.url}")

    original_client = httpx.Client
    transport = httpx.MockTransport(handle_request)

    def create_client(*args: object, **kwargs: object) -> httpx.Client:
        return original_client(transport=transport, timeout=kwargs.get("timeout", 30))

    monkeypatch.setattr("ai_intel_agent.gemini_collection.httpx.Client", create_client)
    monkeypatch.setattr(
        "ai_intel_agent.feed_acquisition.SystemHostResolver",
        lambda: StaticResolver(),
    )
    monkeypatch.setattr("ai_intel_agent.cli.SystemClock", lambda: FixedClock())
    environment = {
        "AI_INTEL_DATABASE_URL": gemini_database_url,
        "DEEPSEEK_API_KEY": "fixture-deepseek-key",
    }

    first = runner.invoke(app, ["collect-gemini"], env=environment)
    assert first.exit_code == 0, first.output
    assert "sections_collected=1" in first.output
    assert "document_versions_created=1" in first.output
    assert "drafts_created=1" in first.output

    second = runner.invoke(app, ["collect-gemini"], env=environment)
    assert second.exit_code == 0, second.output
    assert "sections_collected=1" in second.output
    assert "document_versions_created=1" in second.output
    assert "drafts_created=0" in second.output

    third = runner.invoke(app, ["collect-gemini"], env=environment)
    assert third.exit_code == 0, third.output
    assert "sections_collected=1" in third.output
    assert "document_versions_created=0" in third.output
    assert "drafts_created=0" in third.output
    assert len(provider_calls) == 1

    engine = create_database_engine(gemini_database_url)
    try:
        with Session(engine) as session:
            counts = {
                record_type.__tablename__: session.scalar(
                    select(func.count()).select_from(record_type)
                )
                for record_type in (
                    SourceDefinitionRecord,
                    CandidateRecord,
                    DocumentVersionRecord,
                    StoryRecord,
                    ClaimRecord,
                    EvidenceSpanRecord,
                    TraceRecord,
                )
            }
            candidate = session.scalar(select(CandidateRecord))
            documents = session.scalars(select(DocumentVersionRecord)).all()
            story = session.scalar(select(StoryRecord))
            claims = session.scalars(select(ClaimRecord).order_by(ClaimRecord.position)).all()
            evidence = session.scalars(
                select(EvidenceSpanRecord).order_by(EvidenceSpanRecord.start_offset)
            ).all()
            traces = session.scalars(select(TraceRecord)).all()
            discovery_count = session.scalar(
                select(func.count()).select_from(CollectionDiscoveryRecord)
            )
    finally:
        engine.dispose()

    assert counts == {
        "source_definitions": 1,
        "candidates": 1,
        "document_versions": 2,
        "stories": 1,
        "claims": 2,
        "evidence_spans": 2,
        "structured_traces": 2,
    }
    assert discovery_count == 3
    assert candidate is not None
    assert candidate.canonical_url == f"{RELEASE_NOTES_URL}#august-12-2026"
    assert len(documents) == 2
    assert all(
        document.content_hash == sha256(document.body.encode("utf-8")).hexdigest()
        for document in documents
    )
    assert len({document.content_hash for document in documents}) == 2
    assert story is not None
    document = next(
        item for item in documents if item.id == story.primary_document_version_id
    )
    assert story.headline == provider_output["headline"]
    assert story.review_state == StoryReviewState.UNREVIEWED.value
    assert [claim.text for claim in claims] == [
        item["text"] for item in provider_output["claims"]
    ]
    assert all(item.role == EvidenceRole.PRIMARY.value for item in evidence)
    assert all(item.relation == EvidenceRelation.SUPPORTS.value for item in evidence)
    assert all(
        item.exact_text == document.body[item.start_offset : item.end_offset]
        and item.text_hash == sha256(item.exact_text.encode("utf-8")).hexdigest()
        for item in evidence
    )
    assert {
        trace.attributes["route_identifier"] for trace in traces
    } == {"deepseek:v4-pro"}
    assert {
        trace.attributes["prompt_version"] for trace in traces
    } == {"gemini-draft-prompt-2026-08-18.v2"}
    assert {
        trace.attributes["routing_evaluation_version"] for trace in traces
    } == {"model-routing-evaluation-2026-08-12.v1"}
    assert {
        trace.attributes["routing_evaluation_cases_sha256"] for trace in traces
    } == {load_evaluation_corpus().cases_sha256}
    assert {
        trace.attributes["protocol_content_sha256"] for trace in traces
    } == {load_gemini_draft_protocol().content_sha256}


def test_gemini_draft_protocol_fails_closed_when_routing_evaluation_drifts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus = replace(
        load_evaluation_corpus(),
        version="model-routing-evaluation-drifted.v1",
    )
    monkeypatch.setattr(
        gemini_collection_module,
        "load_evaluation_corpus",
        lambda: corpus,
    )

    with pytest.raises(
        DraftPreparationError,
        match="routing evaluation version drifted",
    ):
        load_gemini_draft_protocol()


@pytest.mark.parametrize(
    "corpus_changes",
    [
        {"review_state": "awaiting-human-approval"},
        {"cases_sha256": "0" * 64},
    ],
)
def test_gemini_draft_protocol_requires_approval_of_exact_evaluation_cases(
    monkeypatch: pytest.MonkeyPatch,
    corpus_changes: dict[str, str],
) -> None:
    corpus = replace(load_evaluation_corpus(), **corpus_changes)
    monkeypatch.setattr(
        gemini_collection_module,
        "load_evaluation_corpus",
        lambda: corpus,
    )

    with pytest.raises(
        DraftPreparationError,
        match="human-approved.*exact cases SHA-256",
    ):
        load_gemini_draft_protocol()


@pytest.mark.postgres
def test_collect_gemini_cli_fails_closed_without_deepseek_key(
    gemini_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_client(*args: object, **kwargs: object) -> httpx.Client:
        raise AssertionError("HTTP client must not be created without Provider credentials")

    monkeypatch.setattr("ai_intel_agent.gemini_collection.httpx.Client", unexpected_client)
    result = runner.invoke(
        app,
        ["collect-gemini"],
        env={
            "AI_INTEL_DATABASE_URL": gemini_database_url,
            "DEEPSEEK_API_KEY": "",
        },
    )

    assert result.exit_code == 2
    assert "Set DEEPSEEK_API_KEY for collect-gemini" in result.output
    engine = create_database_engine(gemini_database_url)
    try:
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(StoryRecord)) == 0
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "returned_model",
    ["unapproved-returned-model", None, 42, ""],
)
def test_deepseek_draft_rejects_an_unexpected_or_missing_returned_model(
    returned_model: object,
) -> None:
    body = "August 12, 2026\nGemini 3.6 Flash is generally available."
    document = DocumentVersion(
        id=uuid4(),
        candidate_id=uuid4(),
        source_url=f"{RELEASE_NOTES_URL}#august-12-2026",
        title="Gemini API Release Notes — August 12, 2026",
        body=body,
        content_hash=sha256(body.encode("utf-8")).hexdigest(),
        observed_at=datetime(2026, 8, 14, tzinfo=UTC),
        published_at=datetime(2026, 8, 12, tzinfo=UTC),
        published_at_raw="August 12, 2026",
    )
    output = {
        "headline": "Gemini 3.6 Flash 正式发布",
        "claims": [
            {
                "text": "Google 已正式发布 Gemini 3.6 Flash。",
                "evidence": "Gemini 3.6 Flash is generally available.",
            }
        ],
    }

    def unexpected_model(request: httpx.Request) -> httpx.Response:
        response_body: dict[str, object] = {
            "choices": [
                {
                    "message": {"content": json.dumps(output, ensure_ascii=False)},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 50, "completion_tokens": 40},
        }
        if returned_model is not None:
            response_body["model"] = returned_model
        return httpx.Response(
            200,
            request=request,
            json=response_body,
        )

    with httpx.Client(transport=httpx.MockTransport(unexpected_model)) as client:
        provider = DeepSeekGeminiDraftProvider(
            client,
            api_key="fixture-deepseek-key",
            sleeper=lambda _: None,
        )
        with pytest.raises(DraftPreparationError, match="returned model"):
            provider.prepare(document)


@pytest.mark.parametrize(
    ("body", "evidence"),
    [
        (
            "August 12, 2026\nGemini 3.6 Flash is generally available.",
            "Gemini 3.6 Flash has a fabricated capability.",
        ),
        (
            "August 12, 2026\nStable release. Stable release.",
            "Stable release.",
        ),
    ],
)
def test_deepseek_draft_rejects_evidence_that_is_not_one_unique_exact_substring(
    body: str,
    evidence: str,
) -> None:
    document = DocumentVersion(
        id=uuid4(),
        candidate_id=uuid4(),
        source_url=f"{RELEASE_NOTES_URL}#august-12-2026",
        title="Gemini API Release Notes — August 12, 2026",
        body=body,
        content_hash=sha256(body.encode("utf-8")).hexdigest(),
        observed_at=datetime(2026, 8, 14, tzinfo=UTC),
        published_at=datetime(2026, 8, 12, tzinfo=UTC),
        published_at_raw="August 12, 2026",
    )
    output = {
        "headline": "Gemini 更新草稿",
        "claims": [{"text": "这是一条无法锚定的草稿 Claim。", "evidence": evidence}],
    }

    def invalid_evidence_response(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={
                "model": "deepseek-v4-pro",
                "choices": [
                    {
                        "message": {"content": json.dumps(output, ensure_ascii=False)},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 50, "completion_tokens": 40},
            },
        )

    with (
        httpx.Client(
            transport=httpx.MockTransport(invalid_evidence_response)
        ) as client,
        pytest.raises(DraftPreparationError, match="unique exact source substring"),
    ):
        DeepSeekGeminiDraftProvider(
            client,
            api_key="fixture-deepseek-key",
            sleeper=lambda _: None,
        ).prepare(document)


def test_ten_day_backfill_includes_the_cutoff_and_excludes_the_day_before() -> None:
    sections = tuple(
        DatedReleaseSection(
            heading=heading,
            anchor=heading.casefold().replace(" ", "-"),
            published_date=published_at.date(),
            body=f"{heading}\nRelease note",
        )
        for heading, published_at in (
            ("August 4, 2026", datetime(2026, 8, 4, tzinfo=UTC)),
            ("August 3, 2026", datetime(2026, 8, 3, tzinfo=UTC)),
        )
    )

    selected = select_release_sections_for_backfill(
        sections,
        observed_at=datetime(2026, 8, 14, tzinfo=UTC),
        backfill_days=10,
    )

    assert [section.heading for section in selected] == ["August 4, 2026"]


def test_gemini_fetcher_rejects_cross_host_redirect_before_second_request() -> None:
    requests: list[httpx.Request] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            302,
            request=request,
            headers={"location": "https://redirect.example/private"},
        )

    with (
        httpx.Client(transport=httpx.MockTransport(redirect)) as client,
        pytest.raises(GeminiSourceError, match="fixed official"),
    ):
        HttpGeminiReleaseNotesFetcher(
            client,
            resolver=StaticResolver(),
        ).fetch()

    assert len(requests) == 1


def test_gemini_fetcher_rejects_private_resolution_before_request() -> None:
    requests: list[httpx.Request] = []

    def unexpected_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, request=request)

    with (
        httpx.Client(transport=httpx.MockTransport(unexpected_request)) as client,
        pytest.raises(GeminiSourceError, match="public network"),
    ):
        HttpGeminiReleaseNotesFetcher(
            client,
            resolver=StaticResolver((IPv4Address("127.0.0.1"),)),
        ).fetch()

    assert requests == []


def test_gemini_fetcher_stops_streaming_at_the_size_limit() -> None:
    class ChunkStream(httpx.SyncByteStream):
        def __init__(self) -> None:
            self.yielded = 0

        def __iter__(self):
            for chunk in (b"1234", b"5678", b"must-not-be-read"):
                self.yielded += 1
                yield chunk

    stream = ChunkStream()

    def oversized_response(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/html"},
            stream=stream,
        )

    with (
        httpx.Client(transport=httpx.MockTransport(oversized_response)) as client,
        pytest.raises(GeminiSourceError, match="size limit"),
    ):
        HttpGeminiReleaseNotesFetcher(
            client,
            resolver=StaticResolver(),
            maximum_bytes=5,
        ).fetch()

    assert stream.yielded == 2
