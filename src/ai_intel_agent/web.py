from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import date
from email.utils import format_datetime
from typing import Annotated
from urllib.parse import quote
from xml.etree import ElementTree

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from ai_intel_agent.accepted_knowledge import (
    AcceptedKnowledgeOperation,
    AcceptedKnowledgeRetrieval,
    EmbeddingBackend,
    RerankerBackend,
    RetrievalFilters,
    RetrievalQuery,
)
from ai_intel_agent.domain import (
    EvidenceRelation,
    EvidenceRole,
    EvidenceState,
    Topic,
)
from ai_intel_agent.persistence import create_database_engine
from ai_intel_agent.publication import (
    PublicContent,
    PublicDigest,
)
from ai_intel_agent.research import (
    RESEARCH_QUESTION_MAX_CHARACTERS,
    PersistentAnonymousResearchAllowance,
    ResearchError,
    ResearchProvider,
    ResearchRepository,
    ResearchTaskType,
    interpret_query_intent,
    stream_research_events,
)
from ai_intel_agent.web_templates import render_public_page, render_story_cards

LOGGER = logging.getLogger(__name__)

EVIDENCE_STATE_LABELS: dict[EvidenceState, str] = {
    EvidenceState.SINGLE_SOURCE: "单一来源",
    EvidenceState.MULTI_SOURCE: "多来源",
    EvidenceState.CONFLICT: "证据冲突",
    EvidenceState.INSUFFICIENT_EVIDENCE: "证据不足",
}
EVIDENCE_ROLE_LABELS: dict[EvidenceRole, str] = {
    EvidenceRole.PRIMARY: "第一方证据",
    EvidenceRole.INDEPENDENT: "独立证据",
    EvidenceRole.SECONDARY: "二手证据",
    EvidenceRole.COMMUNITY: "社区证据",
}
EVIDENCE_RELATION_LABELS: dict[EvidenceRelation, str] = {
    EvidenceRelation.SUPPORTS: "支持",
    EvidenceRelation.CONTRADICTS: "反驳",
}


class ResearchQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(
        min_length=1,
        max_length=RESEARCH_QUESTION_MAX_CHARACTERS,
    )


def create_app(
    database_url: str,
    *,
    research_provider: ResearchProvider | None = None,
    anonymous_research_daily_limit: int | None = None,
    anonymous_identity_salt: bytes | None = None,
    accepted_knowledge_retrieval: AcceptedKnowledgeOperation | None = None,
    retrieval_embedding: EmbeddingBackend | None = None,
    retrieval_reranker: RerankerBackend | None = None,
) -> FastAPI:
    if (anonymous_research_daily_limit is None) != (anonymous_identity_salt is None):
        raise ValueError("Anonymous Research limit and identity salt must be configured together")
    engine = create_database_engine(database_url)
    public_content = PublicContent(engine)
    retrieval = accepted_knowledge_retrieval or AcceptedKnowledgeRetrieval(
        engine,
        embedding=retrieval_embedding,
        reranker=retrieval_reranker,
    )
    research_repository = ResearchRepository(engine, retrieval=retrieval)
    research_allowance = (
        PersistentAnonymousResearchAllowance(
            engine,
            daily_limit=anonymous_research_daily_limit,
            identity_salt=anonymous_identity_salt,
        )
        if anonymous_research_daily_limit is not None and anonymous_identity_salt is not None
        else None
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        engine.dispose()

    app = FastAPI(
        title="AI Intelligence",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.get("/health/live", include_in_schema=False)
    def health_live() -> dict[str, str]:
        return {"status": "live"}

    @app.get("/health/ready", include_in_schema=False)
    def health_ready() -> JSONResponse:
        try:
            with engine.connect() as connection:
                connection.exec_driver_sql("SELECT 1")
        except Exception:  # noqa: BLE001 - readiness must fail closed for any database error.
            return JSONResponse({"status": "not-ready"}, status_code=503)
        return JSONResponse({"status": "ready"})

    @app.get("/", response_class=HTMLResponse, name="home")
    def home() -> HTMLResponse:
        digests = public_content.published_digests()
        digest = digests[0] if digests else None
        return HTMLResponse(
            render_public_page(
                "home.html",
                page_name="home",
                title="AI Intelligence",
                digest=digest,
                publishers=(
                    tuple(sorted({story.publisher for story in digest.stories}))
                    if digest is not None
                    else ()
                ),
                recent_digests=digests[1:6],
                story_url=_relative_story_url,
            )
        )

    @app.get(
        "/digests/{publication_date}",
        response_class=HTMLResponse,
        name="digest_page",
    )
    def digest_page(publication_date: date) -> HTMLResponse:
        digest = public_content.digest_for_date(publication_date)
        if digest is None:
            raise HTTPException(status_code=404, detail="Digest not found")
        return HTMLResponse(
            render_public_page(
                "digest.html",
                page_name="digest",
                title=f"Digest {publication_date.isoformat()}",
                digest=digest,
                story_url=_relative_story_url,
            )
        )

    @app.get("/archive", response_class=HTMLResponse, name="archive")
    def archive() -> HTMLResponse:
        return HTMLResponse(
            render_public_page(
                "archive.html",
                page_name="archive",
                title="Digest archive",
                digests=public_content.published_digests(),
                story_url=_relative_story_url,
            )
        )

    @app.get("/stories/{stable_key}", response_class=HTMLResponse, name="story_page")
    def story_page(stable_key: str) -> HTMLResponse:
        story = public_content.published_story(stable_key)
        if story is None:
            raise HTTPException(status_code=404, detail="Story not found")
        return HTMLResponse(
            render_public_page(
                "story.html",
                page_name="story",
                title=story.headline,
                story=story,
                evidence_labels={
                    "states": EVIDENCE_STATE_LABELS,
                    "roles": EVIDENCE_ROLE_LABELS,
                    "relations": EVIDENCE_RELATION_LABELS,
                },
            )
        )

    @app.get("/browse", response_class=HTMLResponse, name="browse")
    def browse(
        q: str | None = None,
        publisher: Annotated[str | None, Query(alias="source")] = None,
        topic: Annotated[Topic | None, Query(alias="topic")] = None,
        publication_date: Annotated[date | None, Query(alias="date")] = None,
    ) -> HTMLResponse:
        result = retrieval.retrieve(
            RetrievalQuery(
                text=q or "",
                filters=RetrievalFilters(
                    publisher=publisher,
                    topic=topic,
                    publication_date=publication_date,
                ),
            )
        )
        all_stories = public_content.browse_published_stories()
        stories_by_id = {story.id: story for story in all_stories}
        stories = tuple(
            stories_by_id[story_id]
            for story_id in result.matching_story_ids
            if story_id in stories_by_id
        )
        return HTMLResponse(
            render_public_page(
                "browse.html",
                page_name="browse",
                title="Browse",
                query=q,
                selected_publisher=publisher,
                selected_topic=topic,
                publication_date=publication_date,
                sources=tuple(sorted({story.publisher for story in all_stories})),
                topics=tuple(
                    sorted(
                        {
                            story.primary_topic
                            for story in all_stories
                            if story.primary_topic is not None
                        },
                        key=lambda item: item.value,
                    )
                ),
                stories=stories,
                story_url=_relative_story_url,
            )
        )

    @app.get("/research", response_class=HTMLResponse, name="research")
    def research() -> HTMLResponse:
        try:
            digest = public_content.latest_digest()
        except Exception as error:  # noqa: BLE001 - examples are best-effort UI data.
            LOGGER.warning(
                "Research examples unavailable: %s",
                type(error).__name__,
            )
            digest = None
        examples = _research_example_questions(digest)
        return HTMLResponse(
            render_public_page(
                "research.html",
                page_name="research",
                title="Research",
                examples=examples,
            )
        )

    @app.post("/research/answer", name="research_answer")
    def research_answer(payload: ResearchQuestion, request: Request) -> StreamingResponse:
        anonymous_client_id = request.headers.get("X-AI-Anonymous-Client")
        if anonymous_client_id is None and request.client is not None:
            anonymous_client_id = request.client.host
        events = stream_research_events(
            payload.question,
            repository=research_repository,
            provider=research_provider,
            allowance=research_allowance,
            anonymous_client_id=anonymous_client_id,
        )
        return StreamingResponse(
            (_encode_sse(event, data) for event, data in events),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get("/rss", response_class=HTMLResponse, name="rss_page")
    def rss_page() -> HTMLResponse:
        return HTMLResponse(
            render_public_page(
                "rss.html",
                page_name="rss",
                title="RSS 订阅",
            )
        )

    @app.get("/rss.xml", name="rss")
    def rss(request: Request) -> Response:
        body = _render_rss(
            public_content.published_digests(),
            home_url=str(request.url_for("home")),
            digest_url=lambda value: str(
                request.url_for("digest_page", publication_date=value.isoformat())
            ),
            story_url=lambda value: str(request.url_for("story_page", stable_key=value)),
        )
        return Response(
            content=body,
            headers={"Content-Type": "application/rss+xml; charset=utf-8"},
        )

    return app


def _relative_story_url(stable_key: str) -> str:
    return f"/stories/{quote(stable_key, safe='')}"


def _research_example_questions(digest: PublicDigest | None) -> tuple[str, ...]:
    if digest is None:
        return ()
    questions: list[str] = []
    for story in digest.stories:
        if not any(
            evidence.relation is EvidenceRelation.SUPPORTS
            and evidence.role is not EvidenceRole.COMMUNITY
            for claim in story.claims
            for evidence in claim.evidence
        ):
            continue
        question = f"关于「{story.headline}」，已发布知识支持什么事实？"
        if len(question) > RESEARCH_QUESTION_MAX_CHARACTERS:
            continue
        try:
            intent = interpret_query_intent(question)
        except ResearchError:
            continue
        if intent.task_type is not ResearchTaskType.SIMPLE_LOOKUP:
            continue
        if question not in questions:
            questions.append(question)
        if len(questions) == 4:
            break
    return tuple(questions)


def _encode_sse(event: str, data: dict[str, object]) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n"


def _render_rss(
    digests: tuple[PublicDigest, ...],
    *,
    home_url: str,
    digest_url: Callable[[date], str],
    story_url: Callable[[str], str],
) -> bytes:
    rss = ElementTree.Element("rss", version="2.0")
    channel = ElementTree.SubElement(rss, "channel")
    ElementTree.SubElement(channel, "title").text = "AI Intelligence Digests"
    ElementTree.SubElement(channel, "link").text = home_url
    ElementTree.SubElement(channel, "description").text = "经审核的每日 AI Intelligence Digest"

    for digest in digests:
        item = ElementTree.SubElement(channel, "item")
        ElementTree.SubElement(
            item, "title"
        ).text = f"AI Intelligence Digest · {digest.publication_date.isoformat()}"
        ElementTree.SubElement(item, "link").text = digest_url(digest.publication_date)
        ElementTree.SubElement(item, "guid", isPermaLink="false").text = digest.stable_key
        ElementTree.SubElement(item, "pubDate").text = format_datetime(digest.published_at)
        ElementTree.SubElement(item, "description").text = render_story_cards(
            digest.stories,
            story_url=story_url,
        )

    return ElementTree.tostring(rss, encoding="utf-8", xml_declaration=True)
