from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Self
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
import pytest
from fastapi.testclient import TestClient
from pg0 import Pg0
from sqlalchemy import text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from typer.testing import CliRunner

import ai_intel_agent.cli as cli_module
from ai_intel_agent.accepted_knowledge import (
    AcceptedKnowledgeHit,
    AcceptedKnowledgeResult,
    RetrievalQuery,
    RetrievalTrace,
)
from ai_intel_agent.cli import app
from ai_intel_agent.domain import DigestState, StoryReviewState
from ai_intel_agent.editorial import DigestPublicationContract
from ai_intel_agent.persistence import (
    CandidateRecord,
    ClaimRecord,
    DigestRecord,
    DigestStoryRecord,
    DocumentVersionRecord,
    EvidenceSpanRecord,
    StoryPresentationRecord,
    StoryRecord,
    create_database_engine,
    upgrade_database,
)
from ai_intel_agent.research import (
    DeepSeekResearchProvider,
    ResearchEvidence,
    ResearchEvidenceSet,
    ResearchRepository,
    load_research_protocol,
    stream_research_events,
)
from ai_intel_agent.web import create_app

runner = CliRunner()

STORY_KEY = "gemini-release-notes:2026-08-12"
HEADLINE = "Gemini 3.6 Flash 正式发布"
CLAIM_TEXT = "Google 已正式发布 Gemini 3.6 Flash。"
EVIDENCE_TEXT = "Gemini 3.6 Flash is generally available."


def _id(name: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"mvp-m3-test:{name}")


@pytest.fixture
def research_database_url() -> Iterator[str]:
    name = f"ai_intel_research_{_id(os.urandom(8).hex).hex}"
    data_root = os.getenv("PG0_TEST_DATA_ROOT")
    data_dir = str(Path(data_root) / name) if data_root else None
    server = Pg0(name=name, data_dir=data_dir)
    server.start()
    try:
        upgrade_database(server.uri)
        yield server.uri
    finally:
        server.drop()


def _persist_research_story(
    database_url: str,
    *,
    identity: str,
    review_state: StoryReviewState,
    digest_state: DigestState,
    headline: str | None = None,
) -> tuple[UUID, UUID, UUID]:
    candidate_id = _id(f"{identity}:candidate")
    document_id = _id(f"{identity}:document")
    story_id = _id(f"{identity}:story")
    claim_id = _id(f"{identity}:claim")
    evidence_id = _id(f"{identity}:evidence")
    digest_id = _id(f"{identity}:digest")
    story_headline = headline or f"{HEADLINE} · {identity}"
    body = f"August 12, 2026\n{EVIDENCE_TEXT}"
    now = datetime(2026, 8, 15, tzinfo=UTC)
    engine = create_database_engine(database_url)
    try:
        with Session(engine) as session, session.begin():
            session.execute(
                insert(CandidateRecord).values(
                    id=candidate_id,
                    title=f"Gemini API Release Notes — {identity}",
                    canonical_url=(
                        "https://ai.google.dev/gemini-api/docs/changelog"
                        f"#august-12-2026-{identity}"
                    ),
                    publisher="Google",
                    discovered_at=now,
                )
            )
            session.execute(
                insert(DocumentVersionRecord).values(
                    id=document_id,
                    candidate_id=candidate_id,
                    source_url="https://ai.google.dev/gemini-api/docs/changelog",
                    title=f"Gemini API Release Notes — {identity}",
                    body=body,
                    content_hash=sha256(body.encode("utf-8")).hexdigest(),
                    observed_at=now,
                    published_at=datetime(2026, 8, 12, tzinfo=UTC),
                    published_at_raw="August 12, 2026",
                    updated_at=None,
                    updated_at_raw=None,
                )
            )
            session.execute(
                insert(StoryRecord).values(
                    id=story_id,
                    primary_document_version_id=document_id,
                    stable_key=f"{STORY_KEY}:{identity}",
                    headline=story_headline,
                    occurred_at=datetime(2026, 8, 12, tzinfo=UTC),
                    review_state=review_state.value,
                )
            )
            session.execute(
                insert(StoryPresentationRecord).values(
                    story_id=story_id,
                    summary=f"{story_headline} 的已审核读者摘要。",
                    why_it_matters=(
                        f"这条 {identity} 信息帮助开发者理解 Gemini 发布与采用影响。"
                    ),
                    primary_topic="Models",
                )
            )
            session.execute(
                insert(ClaimRecord).values(
                    id=claim_id,
                    story_id=story_id,
                    position=0,
                    text=f"{CLAIM_TEXT}（{identity}）",
                )
            )
            start_offset = body.index(EVIDENCE_TEXT)
            session.execute(
                insert(EvidenceSpanRecord).values(
                    id=evidence_id,
                    claim_id=claim_id,
                    document_version_id=document_id,
                    exact_text=EVIDENCE_TEXT,
                    start_offset=start_offset,
                    end_offset=start_offset + len(EVIDENCE_TEXT),
                    text_hash=sha256(EVIDENCE_TEXT.encode("utf-8")).hexdigest(),
                    role="primary",
                    relation="supports",
                )
            )
            session.execute(
                insert(DigestRecord).values(
                    id=digest_id,
                    stable_key=f"research-digest:{identity}",
                    publication_date=now.date(),
                    state=DigestState.DRAFT.value,
                    published_at=None,
                    introduction=None,
                    publication_contract=(
                        DigestPublicationContract.LEGACY_FIXTURE.value
                    ),
                )
            )
            if review_state is StoryReviewState.ACCEPTED:
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
                    .values(state=DigestState.PUBLISHED.value, published_at=now)
                )
    finally:
        engine.dispose()
    return story_id, claim_id, evidence_id


class FakeResearchProvider:
    def __init__(self, output: dict[str, object]) -> None:
        self.output = output
        self.calls: list[object] = []

    def stream(self, evidence_set: object) -> Iterator[str]:
        self.calls.append(evidence_set)
        payload = json.dumps(self.output, ensure_ascii=False)
        midpoint = len(payload) // 2
        yield payload[:midpoint]
        yield payload[midpoint:]


class FailingResearchProvider:
    def __init__(self) -> None:
        self.calls = 0

    def stream(self, evidence_set: object) -> Iterator[str]:
        self.calls += 1
        raise RuntimeError("fixture Provider failure")


class StaticAcceptedKnowledge:
    def __init__(self, hit: AcceptedKnowledgeHit) -> None:
        self.hit = hit

    def retrieve(self, query: RetrievalQuery) -> AcceptedKnowledgeResult:
        return AcceptedKnowledgeResult(
            query=query,
            hits=(self.hit,),
            matching_story_ids=(self.hit.story_id,),
            trace=RetrievalTrace(
                lexical=(),
                semantic=(),
                entity=(),
                fusion=(),
                final=(),
                faults=(),
            ),
        )


class RecordingResearchAllowance:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def reserve(self, anonymous_client_id: str) -> bool:
        self.calls.append(anonymous_client_id)
        return True


def _stream_simple_lookup(
    question: str,
    *,
    claim_text: str,
    exact_text: str,
    answer: str,
) -> tuple[
    list[tuple[str, dict[str, object]]],
    FakeResearchProvider,
    RecordingResearchAllowance,
]:
    story_id = _id("simple-lookup:story")
    claim_id = _id("simple-lookup:claim")
    evidence_id = _id("simple-lookup:evidence")
    hit = AcceptedKnowledgeHit(
        story_id=story_id,
        story_stable_key="gemini-3-7-flash-release",
        story_headline="Gemini 3.7 Flash 正式发布",
        claim_id=claim_id,
        claim_text=claim_text,
        evidence_span_id=evidence_id,
        exact_text=exact_text,
        chunk_id=None,
    )
    provider = FakeResearchProvider(
        {
            "answer": answer,
            "citations": [
                {
                    "story_id": str(story_id),
                    "claim_id": str(claim_id),
                    "evidence_span_id": str(evidence_id),
                }
            ],
        }
    )
    allowance = RecordingResearchAllowance()
    events = list(
        stream_research_events(
            question,
            repository=ResearchRepository(retrieval=StaticAcceptedKnowledge(hit)),
            provider=provider,
            allowance=allowance,
            anonymous_client_id="fixture-client",
        )
    )
    return events, provider, allowance


@pytest.mark.parametrize(
    ("question", "exact_text"),
    (
        ("Gemini 3.7 Flash 的价格是多少？", "Gemini 3.7 Flash is available."),
        ("Gemini 3.7 Flash 售价多少？", "Gemini 3.7 Flash is available."),
        ("What does Gemini 3.7 Flash cost?", "Gemini 3.7 Flash is available."),
        ("How much is Gemini 3.7 Flash?", "Gemini 3.7 Flash is available."),
        ("Gemini 3.7 Flash 的价格是多少？", "Pricing has not been announced."),
        ("What does Gemini 3.7 Flash cost?", "The outage cost users time."),
        (
            "Gemini 3.7 Flash 的价格是多少？",
            "Google invested $1 billion while announcing Gemini 3.7 Flash.",
        ),
        (
            "Gemini 3.7 Flash 的价格是多少？",
            "Gemini pricing was not announced in 2026. Google invested $1 billion.",
        ),
        (
            "How much is Gemini 3.7 Flash?",
            "This article explains how Claude pricing costs $1.",
        ),
        (
            "Does the Gemini API cost anything?",
            "Claude does cost $1.",
        ),
        ("Gemini 3.7 Flash 是否免费？", "Gemini 3.7 Flash is not free."),
        ("Gemini 3.7 Flash 是否免费？", "Gemini 3.7 Flash was never free."),
        ("Gemini 3.7 Flash 是否免费？", "There is no free tier for Gemini 3.7 Flash."),
        (
            "Gemini 3.7 Flash 是否免费？",
            "The Gemini 3.7 Flash free tier is unavailable.",
        ),
        (
            "Gemini 3.7 Flash 是否免费？",
            "The Gemini 3.7 Flash free plan was discontinued.",
        ),
        ("Gemini 3.7 Flash 是否免费？", "Gemini 3.7 Flash 并不免费。"),
        ("Gemini 3.7 Flash 是否免费？", "Gemini 3.7 Flash 免费版本已取消。"),
    ),
)
def test_simple_lookup_refuses_without_requested_price_value(
    question: str,
    exact_text: str,
) -> None:
    events, provider, allowance = _stream_simple_lookup(
        question,
        claim_text="Google 正式发布了 Gemini 3.7 Flash。",
        exact_text=exact_text,
        answer="Gemini 3.7 Flash 的价格是 1 美元。",
    )

    assert provider.calls == []
    assert allowance.calls == []
    assert [event for event, _ in events] == ["status", "refusal", "done"]
    assert events[1][1]["reason"] == "insufficient-evidence"
    assert events[-1][1]["status"] == "refused"


@pytest.mark.parametrize(
    ("question", "exact_text", "answer"),
    (
        (
            "Gemini 3.7 Flash 的价格是多少？",
            "Gemini 3.7 Flash costs $1 per request.",
            "Gemini 3.7 Flash 每次请求收费 1 美元。",
        ),
        (
            "Gemini 3.7 Flash 是否免费？",
            "Gemini 3.7 Flash is free.",
            "Gemini 3.7 Flash 免费提供。",
        ),
        (
            "Gemini 3.7 Flash 售价多少？",
            "Gemini 3.7 Flash 的售价为 1 美元。",
            "Gemini 3.7 Flash 的售价为 1 美元。",
        ),
        (
            "Gemini 3.7 Flash 的价钱是多少？",
            "Gemini 3.7 Flash 的价钱为 1 美元。",
            "Gemini 3.7 Flash 的价钱为 1 美元。",
        ),
        (
            "Gemini 3.7 Flash 是否免费？",
            "Gemini 3.7 Flash 免费。",
            "Gemini 3.7 Flash 免费。",
        ),
    ),
)
def test_simple_lookup_accepts_supported_price_value(
    question: str,
    exact_text: str,
    answer: str,
) -> None:
    events, provider, allowance = _stream_simple_lookup(
        question,
        claim_text=exact_text,
        exact_text=exact_text,
        answer=answer,
    )

    assert len(provider.calls) == 1
    assert allowance.calls == ["fixture-client"]
    assert events[-1][0] == "done"
    assert events[-1][1]["status"] == "answered"


@pytest.mark.parametrize(
    ("question", "evidence", "answer"),
    (
        (
            "Gemini 3.7 Flash 有什么更新？",
            "Google 正式发布了 Gemini 3.7 Flash。",
            "Google 正式发布了 Gemini 3.7 Flash。",
        ),
        (
            "How does Gemini free developers from repetitive work?",
            "Gemini frees developers from repetitive work.",
            "Gemini 可帮助开发者摆脱重复工作。",
        ),
        (
            "How is Gemini able to free developers from repetitive work?",
            "Gemini frees developers from repetitive work.",
            "Gemini 可帮助开发者摆脱重复工作。",
        ),
        (
            "Is Gemini free from vendor lock-in?",
            "Gemini avoids vendor lock-in.",
            "Gemini 可避免供应商锁定。",
        ),
    ),
)
def test_simple_lookup_keeps_non_price_questions_supported(
    question: str,
    evidence: str,
    answer: str,
) -> None:
    events, provider, allowance = _stream_simple_lookup(
        question,
        claim_text=evidence,
        exact_text=evidence,
        answer=answer,
    )

    assert len(provider.calls) == 1
    assert allowance.calls == ["fixture-client"]
    assert events[-1][0] == "done"
    assert events[-1][1]["status"] == "answered"


def _sse_events(response_text: str) -> list[tuple[str, dict[str, object]]]:
    events: list[tuple[str, dict[str, object]]] = []
    for block in response_text.strip().split("\n\n"):
        lines = block.splitlines()
        event = next(line.removeprefix("event: ") for line in lines if line.startswith("event: "))
        data = next(line.removeprefix("data: ") for line in lines if line.startswith("data: "))
        events.append((event, json.loads(data)))
    return events


def test_research_protocol_reuses_the_human_approved_m1_route() -> None:
    protocol = load_research_protocol()

    assert protocol.version == "research-protocol-2026-09-02.v4"
    assert protocol.prompt_version == "research-prompt-2026-09-02.v4"
    assert protocol.output_schema_version == "research-output-2026-08-22.v2"
    assert protocol.sse_contract_version == "research-sse-2026-09-02.v2"
    assert protocol.route_identifier == "deepseek:v4-pro"
    assert protocol.routing_evaluation_version == "model-routing-evaluation-2026-08-12.v1"
    assert protocol.maximum_iterations == 2
    assert protocol.maximum_retrieval_calls == 4
    assert protocol.maximum_elapsed_seconds == 45.0
    assert len(protocol.routing_evaluation_cases_sha256) == 64


def test_deepseek_research_provider_uses_streaming_without_tools_or_reasoning() -> None:
    story_id = _id("provider:story")
    claim_id = _id("provider:claim")
    evidence_id = _id("provider:evidence")
    evidence_set = ResearchEvidenceSet(
        question="Gemini 3.6 Flash 有什么更新？",
        evidence=(
            ResearchEvidence(
                story_id=story_id,
                story_stable_key=STORY_KEY,
                story_headline=HEADLINE,
                claim_id=claim_id,
                claim_text=CLAIM_TEXT,
                evidence_span_id=evidence_id,
                exact_text=EVIDENCE_TEXT,
            ),
        ),
    )
    output = json.dumps(
        {
            "answer": "Google 已正式发布 Gemini 3.6 Flash。",
            "citations": [
                {
                    "story_id": str(story_id),
                    "claim_id": str(claim_id),
                    "evidence_span_id": str(evidence_id),
                }
            ],
        },
        ensure_ascii=False,
    )

    def streamed_response(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.deepseek.com/chat/completions"
        payload = json.loads(request.content)
        assert payload["model"] == "deepseek-v4-pro"
        assert payload["stream"] is True
        assert payload["thinking"] == {"type": "disabled"}
        assert payload["response_format"] == {"type": "json_object"}
        assert "tools" not in payload
        first = json.dumps(
            {
                "model": "deepseek-v4-pro",
                "choices": [
                    {"delta": {"content": output[:20]}, "finish_reason": None}
                ],
            },
            ensure_ascii=False,
        )
        second = json.dumps(
            {
                "model": "deepseek-v4-pro",
                "choices": [
                    {"delta": {"content": output[20:]}, "finish_reason": "stop"}
                ],
            },
            ensure_ascii=False,
        )
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/event-stream"},
            content=f"data: {first}\n\ndata: {second}\n\ndata: [DONE]\n\n".encode(),
        )

    with httpx.Client(transport=httpx.MockTransport(streamed_response)) as client:
        provider = DeepSeekResearchProvider(
            client,
            api_key="fixture-deepseek-key",
            sleeper=lambda _: None,
        )
        streamed_output = "".join(provider.stream(evidence_set))

    assert json.loads(streamed_output) == json.loads(output)


@pytest.mark.postgres
def test_research_page_explains_curated_capabilities_and_offers_fill_only_examples(
    research_database_url: str,
) -> None:
    _persist_research_story(
        research_database_url,
        identity="example",
        review_state=StoryReviewState.ACCEPTED,
        digest_state=DigestState.PUBLISHED,
    )
    with TestClient(create_app(research_database_url)) as client:
        response = client.get("/research")

    assert response.status_code == 200
    assert '<form id="research-form">' in response.text
    assert 'id="research-question"' in response.text
    assert 'id="research-status"' in response.text
    assert 'id="research-answer"' in response.text
    assert 'id="research-refusal"' in response.text
    assert 'id="research-citations"' in response.text
    assert 'fetch("/research/answer"' in response.text
    assert "支持什么" in response.text
    assert "如何提问" in response.text
    assert "仅检索已接受且已发布的知识" in response.text
    assert "证据不足时会明确拒答" in response.text
    assert "不会联网搜索" in response.text
    assert "Hybrid" in response.text
    assert "简单查找" in response.text
    assert "比较" in response.text
    assert "时间线" in response.text
    assert "有界多跳" in response.text
    assert "检索降级" in response.text
    assert response.text.count('class="research-example"') == 1
    assert "关于「Gemini 3.6 Flash 正式发布 · example」，已发布知识支持什么事实？" in response.text
    assert "Anthropic 的年化营收运行率是多少？" not in response.text
    assert 'type="button"' in response.text
    assert "question.value = button.dataset.question" in response.text
    assert "question.focus()" in response.text
    assert "requestSubmit" not in response.text
    assert "form.submit" not in response.text
    assert 'block.split("\\n")' in response.text
    assert 'buffer.indexOf("\\n\\n")' in response.text
    assert "retrieval-degraded" in response.text
    assert "evidence-assembled" in response.text
    assert "verifying-citations" in response.text
    assert "statement_support" in response.text
    assert '"source-publication": "来源发布时间"' in response.text
    assert '"digest-publication": "Digest 发布时间"' in response.text
    assert "会话历史" not in response.text
    assert "管理员" not in response.text


def test_research_page_stays_available_when_dynamic_examples_cannot_be_loaded(
) -> None:
    unavailable_database_url = (
        "postgresql://postgres:postgres@127.0.0.1:1/postgres?connect_timeout=1"
    )
    with TestClient(create_app(unavailable_database_url)) as client:
        response = client.get("/research")

    assert response.status_code == 200
    assert '<form id="research-form">' in response.text
    assert 'class="research-example"' not in response.text
    assert "当前没有可由已发布知识支持的示例问题。" in response.text


@pytest.mark.postgres
@pytest.mark.parametrize(
    "headline",
    (
        "比较 OpenAI 和 Anthropic 的模型发布进展",
        "模型" * 250,
        "2026–2025 模型发布",
    ),
)
def test_research_page_hides_examples_that_are_not_valid_simple_lookup_questions(
    research_database_url: str,
    headline: str,
) -> None:
    _persist_research_story(
        research_database_url,
        identity="invalid-example",
        review_state=StoryReviewState.ACCEPTED,
        digest_state=DigestState.PUBLISHED,
        headline=headline,
    )

    with TestClient(create_app(research_database_url)) as client:
        response = client.get("/research")

    assert response.status_code == 200
    assert 'class="research-example"' not in response.text
    assert "当前没有可由已发布知识支持的示例问题。" in response.text


@pytest.mark.postgres
def test_serve_wires_research_provider_from_in_process_key_without_requesting_network(
    research_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FixtureClient:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            captured["client_closed"] = True

    class ConfiguredProvider:
        def __init__(self, client: object, *, api_key: str) -> None:
            captured["provider_configured"] = (
                isinstance(client, FixtureClient) and api_key == "fixture-deepseek-key"
            )

        def stream(self, evidence_set: object) -> Iterator[str]:
            raise AssertionError("serve wiring must not make a Provider request")

    def run_server(web_app: object, *, host: str, port: int) -> None:
        captured.update(app=web_app, host=host, port=port)

    monkeypatch.setattr(cli_module.httpx, "Client", lambda **_: FixtureClient())
    monkeypatch.setattr(
        cli_module,
        "DeepSeekResearchProvider",
        ConfiguredProvider,
        raising=False,
    )
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=run_server))

    result = runner.invoke(
        app,
        ["serve", "--host", "127.0.0.2", "--port", "8124"],
        env={
            "AI_INTEL_DATABASE_URL": research_database_url,
            "DEEPSEEK_API_KEY": "fixture-deepseek-key",
        },
    )

    assert result.exit_code == 0, result.output
    assert captured["provider_configured"] is True
    assert captured["client_closed"] is True
    assert captured["host"] == "127.0.0.2"
    assert captured["port"] == 8124


@pytest.mark.postgres
def test_research_streams_answer_from_accepted_published_evidence_with_public_citations(
    research_database_url: str,
) -> None:
    story_id, claim_id, evidence_id = _persist_research_story(
        research_database_url,
        identity="published",
        review_state=StoryReviewState.ACCEPTED,
        digest_state=DigestState.PUBLISHED,
    )
    provider = FakeResearchProvider(
        {
            "answer": "Google 已正式发布 Gemini 3.6 Flash。",
            "citations": [
                {
                    "story_id": str(story_id),
                    "claim_id": str(claim_id),
                    "evidence_span_id": str(evidence_id),
                }
            ],
        }
    )

    with TestClient(
        create_app(research_database_url, research_provider=provider)
    ) as client:
        response = client.post(
            "/research/answer",
            json={"question": "Gemini 3.6 Flash 有什么更新？"},
        )
        story = client.get(f"/stories/{STORY_KEY}:published")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _sse_events(response.text)
    assert {payload["version"] for _, payload in events} == {
        "research-sse-2026-09-02.v2"
    }
    answer = "".join(
        str(payload["text"]) for event, payload in events if event == "answer.delta"
    )
    assert answer == "Google 已正式发布 Gemini 3.6 Flash。"
    citation = next(payload for event, payload in events if event == "citation")
    expected_story_url = "/stories/gemini-release-notes%3A2026-08-12%3Apublished"
    assert citation["story_url"] == expected_story_url
    assert citation["claim_url"] == f"{expected_story_url}#claim-{claim_id}"
    assert citation["evidence_url"] == (
        f"{expected_story_url}#evidence-{evidence_id}"
    )
    assert f'id="claim-{claim_id}"' in story.text
    assert f'id="evidence-{evidence_id}"' in story.text
    assert len(provider.calls) == 1


@pytest.mark.postgres
def test_public_simple_lookup_uses_a_compact_provider_contract_and_returns_citations(
    research_database_url: str,
) -> None:
    story_id, claim_id, evidence_id = _persist_research_story(
        research_database_url,
        identity="deepseek-simple",
        review_state=StoryReviewState.ACCEPTED,
        digest_state=DigestState.PUBLISHED,
    )
    output = json.dumps(
        {
            "answer": "Google 已正式发布 Gemini 3.6 Flash。",
            "citations": [
                {
                    "story_id": str(story_id),
                    "claim_id": str(claim_id),
                    "evidence_span_id": str(evidence_id),
                }
            ],
        },
        ensure_ascii=False,
    )

    def streamed_response(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        system_prompt = payload["messages"][0]["content"]
        assert "exactly answer and citations" in system_prompt
        assert "A concise paraphrase of supported Evidence is allowed" in system_prompt
        assert "Abstain only when no supplied item directly supports" in system_prompt
        chunk = json.dumps(
            {
                "model": "deepseek-v4-pro",
                "choices": [
                    {"delta": {"content": output}, "finish_reason": "stop"}
                ],
            },
            ensure_ascii=False,
        )
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/event-stream"},
            content=f"data: {chunk}\n\ndata: [DONE]\n\n".encode(),
        )

    with httpx.Client(transport=httpx.MockTransport(streamed_response)) as http_client:
        provider = DeepSeekResearchProvider(
            http_client,
            api_key="fixture-deepseek-key",
            sleeper=lambda _: None,
        )
        with TestClient(
            create_app(research_database_url, research_provider=provider)
        ) as client:
            response = client.post(
                "/research/answer",
                json={"question": "Gemini 3.6 Flash 有什么更新？"},
            )

    events = _sse_events(response.text)
    assert "".join(
        str(payload["text"])
        for event, payload in events
        if event == "answer.delta"
    ) == "Google 已正式发布 Gemini 3.6 Flash。"
    assert next(payload for event, payload in events if event == "citation")[
        "evidence_span_id"
    ] == str(evidence_id)
    assert events[-1][1]["status"] == "answered"


@pytest.mark.postgres
def test_research_accepts_only_formatting_differences_between_answer_and_support(
    research_database_url: str,
) -> None:
    story_id, claim_id, evidence_id = _persist_research_story(
        research_database_url,
        identity="support-formatting",
        review_state=StoryReviewState.ACCEPTED,
        digest_state=DigestState.PUBLISHED,
    )
    citation = {
        "story_id": str(story_id),
        "claim_id": str(claim_id),
        "evidence_span_id": str(evidence_id),
    }
    provider = FakeResearchProvider(
        {
            "answer": "Google 已正式发布 Gemini 3.6 Flash。\n\n该版本已正式可用。",
            "support": [
                {
                    "statement": "Google 已正式发布 Gemini 3.6 Flash。",
                    "citations": [citation],
                    "requirement_ids": ["requirement-1"],
                    "dimension": None,
                    "time_semantic": None,
                },
                {
                    "statement": "该版本已正式可用。",
                    "citations": [citation],
                    "requirement_ids": ["requirement-1"],
                    "dimension": None,
                    "time_semantic": None,
                },
            ],
        }
    )

    with TestClient(
        create_app(research_database_url, research_provider=provider)
    ) as client:
        response = client.post(
            "/research/answer",
            json={"question": "Gemini 3.6 Flash 有什么更新？"},
        )

    events = _sse_events(response.text)
    answer = "".join(
        str(payload["text"])
        for event, payload in events
        if event == "answer.delta"
    )
    assert answer == "Google 已正式发布 Gemini 3.6 Flash。\n\n该版本已正式可用。"
    assert events[-1][1]["status"] == "answered"


@pytest.mark.postgres
def test_research_refuses_without_calling_provider_when_matches_are_not_public_knowledge(
    research_database_url: str,
) -> None:
    _persist_research_story(
        research_database_url,
        identity="unreviewed",
        review_state=StoryReviewState.UNREVIEWED,
        digest_state=DigestState.DRAFT,
    )
    _persist_research_story(
        research_database_url,
        identity="rejected",
        review_state=StoryReviewState.REJECTED,
        digest_state=DigestState.DRAFT,
    )
    _persist_research_story(
        research_database_url,
        identity="unpublished",
        review_state=StoryReviewState.ACCEPTED,
        digest_state=DigestState.DRAFT,
    )
    provider = FakeResearchProvider({"answer": "不应调用。", "citations": []})

    with TestClient(
        create_app(research_database_url, research_provider=provider)
    ) as client:
        response = client.post(
            "/research/answer",
            json={"question": "Google 已正式发布 Gemini 3.6 Flash"},
        )

    events = _sse_events(response.text)
    refusal = next(payload for event, payload in events if event == "refusal")
    assert refusal == {
        "version": "research-sse-2026-09-02.v2",
        "reason": "insufficient-evidence",
        "message": "证据不足：已发布知识中没有足够证据回答这个问题。",
    }
    assert events[-1] == (
        "done",
        {"version": "research-sse-2026-09-02.v2", "status": "refused"},
    )
    assert provider.calls == []


@pytest.mark.postgres
def test_research_refuses_unsupported_question_before_provider(
    research_database_url: str,
) -> None:
    _persist_research_story(
        research_database_url,
        identity="published",
        review_state=StoryReviewState.ACCEPTED,
        digest_state=DigestState.PUBLISHED,
    )
    provider = FakeResearchProvider({"answer": "不应调用。", "citations": []})

    with TestClient(
        create_app(research_database_url, research_provider=provider)
    ) as client:
        response = client.post(
            "/research/answer",
            json={"question": "火星基地今天的天气如何？"},
        )

    events = _sse_events(response.text)
    assert [event for event, _ in events] == ["status", "refusal", "done"]
    assert events[1][1]["reason"] == "insufficient-evidence"
    assert provider.calls == []


@pytest.mark.postgres
def test_research_refuses_topically_overlapping_question_without_supporting_evidence(
    research_database_url: str,
) -> None:
    story_id, claim_id, evidence_id = _persist_research_story(
        research_database_url,
        identity="published",
        review_state=StoryReviewState.ACCEPTED,
        digest_state=DigestState.PUBLISHED,
    )
    provider = FakeResearchProvider(
        {
            "answer": "价格是 1 美元。",
            "citations": [
                {
                    "story_id": str(story_id),
                    "claim_id": str(claim_id),
                    "evidence_span_id": str(evidence_id),
                }
            ],
        }
    )

    with TestClient(
        create_app(research_database_url, research_provider=provider)
    ) as client:
        response = client.post(
            "/research/answer",
            json={"question": "Gemini 3.6 Flash 的价格是多少？"},
        )

    events = _sse_events(response.text)
    assert [event for event, _ in events] == ["status", "refusal", "done"]
    assert events[1][1]["reason"] == "insufficient-evidence"
    assert provider.calls == []


@pytest.mark.postgres
def test_research_streams_explicit_refusal_when_provider_abstains_from_retrieved_evidence(
    research_database_url: str,
) -> None:
    _persist_research_story(
        research_database_url,
        identity="published",
        review_state=StoryReviewState.ACCEPTED,
        digest_state=DigestState.PUBLISHED,
    )
    provider = FakeResearchProvider({"answer": None, "citations": []})

    with TestClient(
        create_app(research_database_url, research_provider=provider)
    ) as client:
        response = client.post(
            "/research/answer",
            json={"question": "Gemini 3.6 Flash 怎么样？"},
        )

    events = _sse_events(response.text)
    assert [event for event, _ in events] == [
        "status",
        "status",
        "refusal",
        "done",
    ]
    assert events[2][1] == {
        "version": "research-sse-2026-09-02.v2",
        "reason": "provider-abstained",
        "message": "Provider 未能根据已检索证据生成受支持的答案。",
    }
    assert events[-1][1]["status"] == "refused"
    assert len(provider.calls) == 1


@pytest.mark.postgres
@pytest.mark.parametrize("review_state", [StoryReviewState.UNREVIEWED, StoryReviewState.REJECTED])
def test_research_review_state_gate_excludes_even_published_digest_members(
    research_database_url: str,
    review_state: StoryReviewState,
) -> None:
    story_id, _, _ = _persist_research_story(
        research_database_url,
        identity=review_state.value,
        review_state=StoryReviewState.ACCEPTED,
        digest_state=DigestState.PUBLISHED,
    )
    engine = create_database_engine(research_database_url)
    try:
        with Session(engine) as session, session.begin():
            session.execute(text("ALTER TABLE stories DISABLE TRIGGER protect_published_story"))
            session.execute(
                update(StoryRecord)
                .where(StoryRecord.id == story_id)
                .values(review_state=review_state.value)
            )
            session.execute(text("ALTER TABLE stories ENABLE TRIGGER protect_published_story"))
    finally:
        engine.dispose()
    provider = FakeResearchProvider({"answer": "不应调用。", "citations": []})

    with TestClient(
        create_app(research_database_url, research_provider=provider)
    ) as client:
        response = client.post(
            "/research/answer",
            json={"question": "Google 已正式发布 Gemini 3.6 Flash"},
        )

    events = _sse_events(response.text)
    assert events[1][1]["reason"] == "insufficient-evidence"
    assert provider.calls == []


@pytest.mark.postgres
def test_research_fails_closed_for_invalid_or_out_of_set_provider_output(
    research_database_url: str,
) -> None:
    story_id, claim_id, evidence_id = _persist_research_story(
        research_database_url,
        identity="published",
        review_state=StoryReviewState.ACCEPTED,
        digest_state=DigestState.PUBLISHED,
    )
    valid_citation = {
        "story_id": str(story_id),
        "claim_id": str(claim_id),
        "evidence_span_id": str(evidence_id),
    }
    invalid_outputs = (
        {
            "answer": "详情见 https://example.com/fabricated 。",
            "citations": [valid_citation],
        },
        {"answer": "详情见 example.com。", "citations": [valid_citation]},
        {"answer": "请联系 mailto:fake@example.com。", "citations": [valid_citation]},
        {"answer": "详情见 /stories/fabricated。", "citations": [valid_citation]},
        {"answer": "查看[伪造链接](example.com)。", "citations": [valid_citation]},
        {"answer": "<think>隐藏推理</think>这是答案。", "citations": [valid_citation]},
        {
            "answer": "这是多出字段的答案。",
            "citations": [valid_citation],
            "analysis": "not allowed",
        },
        {
            "answer": "这是伪造引用的答案。",
            "citations": [
                {**valid_citation, "evidence_span_id": str(_id("outside:evidence"))}
            ],
        },
        {
            "answer": "这是伪造 Evidence 的答案。",
            "citations": [{**valid_citation, "evidence_text": "fabricated"}],
        },
        {"answer": "这是没有引用的答案。", "citations": []},
    )

    for output in invalid_outputs:
        provider = FakeResearchProvider(output)
        with TestClient(
            create_app(research_database_url, research_provider=provider)
        ) as client:
            response = client.post(
                "/research/answer",
                json={"question": "Google 已正式发布 Gemini 3.6 Flash"},
            )

        events = _sse_events(response.text)
        assert not {"answer.delta", "citation"} & {event for event, _ in events}
        assert next(payload for event, payload in events if event == "error")[
            "code"
        ] == "provider-output-rejected"
        assert events[-1][1]["status"] == "failed"


@pytest.mark.postgres
def test_research_fails_closed_when_provider_raises(
    research_database_url: str,
) -> None:
    _persist_research_story(
        research_database_url,
        identity="published",
        review_state=StoryReviewState.ACCEPTED,
        digest_state=DigestState.PUBLISHED,
    )
    provider = FailingResearchProvider()

    with TestClient(
        create_app(research_database_url, research_provider=provider)
    ) as client:
        response = client.post(
            "/research/answer",
            json={"question": "Google 已正式发布 Gemini 3.6 Flash"},
        )

    events = _sse_events(response.text)
    assert [event for event, _ in events] == ["status", "status", "error", "done"]
    assert events[2][1]["code"] == "provider-failed"
    assert provider.calls == 1
