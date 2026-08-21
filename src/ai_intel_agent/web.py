from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import date
from email.utils import format_datetime
from html import escape
from typing import Annotated
from urllib.parse import quote, urlsplit
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
    PublicClaim,
    PublicDigest,
    PublicEvidence,
    PublicPublicationRepository,
    PublicStory,
)
from ai_intel_agent.research import (
    PersistentAnonymousResearchAllowance,
    ResearchProvider,
    ResearchRepository,
    stream_research_events,
)

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

    question: str = Field(min_length=1, max_length=500)


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
    repository = PublicPublicationRepository(engine)
    retrieval = accepted_knowledge_retrieval or AcceptedKnowledgeRetrieval(
        engine,
        embedding=retrieval_embedding,
        reranker=retrieval_reranker,
    )
    research_repository = ResearchRepository(retrieval=retrieval)
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
        digests = repository.published_digests()
        if not digests:
            content = (
                '<section class="hero"><p class="eyebrow">AI Intelligence</p>'
                "<h1>今日 AI Digest</h1><p>暂无已发布 Digest。</p></section>"
                + _render_entry_points()
            )
        else:
            digest = digests[0]
            publishers = sorted({story.publisher for story in digest.stories})
            coverage = "".join(
                f'<li class="badge">{escape(publisher)}</li>' for publisher in publishers
            )
            recent = (
                "".join(
                    f'<li><a href="/digests/{item.publication_date.isoformat()}">'
                    f"{item.publication_date.isoformat()}</a></li>"
                    for item in digests[1:6]
                )
                or "<li>暂无更早的 Digest。</li>"
            )
            content = (
                '<section class="hero"><p class="eyebrow">AI Intelligence</p>'
                "<h1>今日 AI Digest</h1>"
                f'<p class="digest-date">{digest.publication_date.isoformat()}</p>'
                f'<p class="lede">{escape(digest.introduction)}</p>'
                f'<a class="primary-action" href="/digests/{digest.publication_date.isoformat()}">'
                "阅读完整 Digest</a></section>"
                '<section aria-labelledby="coverage-heading"><h2 id="coverage-heading">来源覆盖</h2>'
                f'<ul class="badge-list">{coverage}</ul></section>'
                '<section aria-labelledby="latest-heading"><h2 id="latest-heading">今日重点</h2>'
                f'<div class="story-grid">{_render_story_cards(digest.stories, _relative_story_url)}</div>'
                "</section>"
                '<section aria-labelledby="recent-heading"><h2 id="recent-heading">近期 Digest</h2>'
                f'<ul class="digest-list">{recent}</ul></section>' + _render_entry_points()
            )
        return HTMLResponse(_render_page("AI Intelligence", content))

    @app.get(
        "/digests/{publication_date}",
        response_class=HTMLResponse,
        name="digest_page",
    )
    def digest_page(publication_date: date) -> HTMLResponse:
        digest = repository.digest_for_date(publication_date)
        if digest is None:
            raise HTTPException(status_code=404, detail="Digest not found")
        content = (
            '<header class="page-header"><p class="eyebrow">Daily Digest</p>'
            f"<h1>{digest.publication_date.isoformat()} AI Digest</h1>"
            f'<p class="lede">{escape(digest.introduction)}</p>'
            f'<p class="muted">共 {len(digest.stories)} 条已审核进展</p></header>'
            '<div class="story-grid">'
            + _render_story_cards(digest.stories, _relative_story_url)
            + "</div>"
        )
        return HTMLResponse(_render_page(f"Digest {publication_date.isoformat()}", content))

    @app.get("/archive", response_class=HTMLResponse, name="archive")
    def archive() -> HTMLResponse:
        digests = repository.published_digests()
        entries = (
            "".join(
                '<section class="archive-entry">'
                f'<h2><a href="/digests/{digest.publication_date.isoformat()}">'
                f"{digest.publication_date.isoformat()} AI Digest</a></h2>"
                f'<p class="lede">{escape(digest.introduction)}</p>'
                f'<div class="story-grid">{_render_story_cards(digest.stories, _relative_story_url)}</div>'
                "</section>"
                for digest in digests
            )
            or '<p class="empty-state">暂无已发布 Digest。</p>'
        )
        content = (
            '<header class="page-header"><p class="eyebrow">Published history</p>'
            '<h1>Digest archive</h1><p class="lede">浏览仍公开可见的历史 Digest。</p>'
            f"</header>{entries}"
        )
        return HTMLResponse(_render_page("Digest archive", content))

    @app.get("/stories/{stable_key}", response_class=HTMLResponse, name="story_page")
    def story_page(stable_key: str) -> HTMLResponse:
        story = repository.published_story(stable_key)
        if story is None:
            raise HTTPException(status_code=404, detail="Story not found")
        return HTMLResponse(_render_page(story.headline, _render_story_detail(story)))

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
        all_stories = repository.browse_published_stories()
        stories_by_id = {story.id: story for story in all_stories}
        stories = tuple(
            stories_by_id[story_id]
            for story_id in result.matching_story_ids
            if story_id in stories_by_id
        )
        form = _render_browse_form(
            q=q,
            publisher=publisher,
            topic=topic,
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
        )
        results = (
            f'<div class="story-grid">{_render_story_cards(stories, _relative_story_url)}</div>'
            if stories
            else '<p class="empty-state">没有符合这些条件的已发布 Story。</p>'
        )
        content = (
            '<header class="page-header"><p class="eyebrow">Published knowledge</p>'
            '<h1>Browse</h1><p class="lede">按关键词、发布者、主题和原始发布日期查找已发布内容。</p>'
            f'</header>{form}<p class="muted">找到 {len(stories)} 条 Story</p>{results}'
        )
        return HTMLResponse(_render_page("Browse", content))

    @app.get("/research", response_class=HTMLResponse, name="research")
    def research() -> HTMLResponse:
        return HTMLResponse(_render_page("Research", _render_research_page()))

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
        return HTMLResponse(_render_page("RSS 订阅", _render_rss_page()))

    @app.get("/rss.xml", name="rss")
    def rss(request: Request) -> Response:
        body = _render_rss(
            repository.published_digests(),
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


def _render_page(title: str, content: str) -> str:
    return (
        "<!doctype html>"
        '<html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)}</title>"
        "<style>"
        ":root{color-scheme:light;--ink:#172033;--muted:#5f6b7a;--line:#d9e0e8;"
        "--paper:#fff;--wash:#f4f7fa;--accent:#0b6bcb;--accent-dark:#084e96;"
        "--radius:1rem}*{box-sizing:border-box}html{background:var(--wash)}"
        "body{font-family:Inter,ui-sans-serif,system-ui,-apple-system,sans-serif;"
        "line-height:1.65;color:var(--ink);max-width:72rem;margin:0 auto;padding:0 1.5rem 4rem}"
        "a{color:var(--accent);text-underline-offset:.18em}a:hover{color:var(--accent-dark)}"
        ".site-nav{display:flex;align-items:center;justify-content:space-between;gap:1rem;"
        "padding:1.1rem 0;border-bottom:1px solid var(--line);flex-wrap:wrap}"
        ".brand{font-weight:800;color:var(--ink);text-decoration:none}.nav-links{display:flex;"
        "gap:1rem;flex-wrap:wrap}.nav-links a{font-weight:650;text-decoration:none}"
        "main{padding-top:2.25rem}.hero,.page-header{max-width:52rem;margin-bottom:2.5rem}"
        ".eyebrow{text-transform:uppercase;letter-spacing:.12em;font-size:.78rem;font-weight:800;"
        "color:var(--accent);margin:0 0 .5rem}h1{font-size:clamp(2.2rem,7vw,4.6rem);line-height:1.02;"
        "letter-spacing:-.045em;margin:.2rem 0 1rem}h2{font-size:clamp(1.4rem,3vw,2rem);"
        "line-height:1.2;margin:2.4rem 0 1rem}h3{line-height:1.3}.lede{font-size:clamp(1.08rem,"
        "2vw,1.3rem);color:#334155;max-width:48rem}.muted,.digest-date{color:var(--muted)}"
        ".primary-action{display:inline-block;background:var(--accent);color:#fff;text-decoration:none;"
        "font-weight:750;padding:.75rem 1rem;border-radius:.65rem;margin-top:.6rem}"
        ".badge-list{display:flex;gap:.55rem;flex-wrap:wrap;list-style:none;padding:0}.badge{background:#e6f1fc;"
        "color:#084e96;border-radius:999px;padding:.25rem .7rem;font-size:.9rem;font-weight:700}"
        ".story-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:1rem}"
        ".story-card{background:var(--paper);border:1px solid var(--line);border-radius:var(--radius);"
        "padding:1.25rem;box-shadow:0 10px 30px rgba(23,32,51,.05);min-width:0}"
        ".story-card h2{font-size:1.3rem;margin:.35rem 0 .7rem;letter-spacing:-.015em}"
        ".story-card p{margin:.55rem 0}.story-meta{display:flex;gap:.5rem;flex-wrap:wrap;"
        "color:var(--muted);font-size:.88rem}.story-meta span+span:before{content:'·';margin-right:.5rem}"
        ".topic{color:var(--accent-dark);font-weight:750}.story-detail{max-width:52rem}"
        ".story-summary{font-size:1.28rem;color:#334155}.importance{background:#eaf3fc;"
        "border-radius:var(--radius);padding:1rem 1.25rem;margin:1.5rem 0}.importance h2{margin:.1rem 0 .4rem}"
        ".source-link{display:inline-block;font-weight:750;margin-top:.5rem}.key-fact{background:var(--paper);"
        "border:1px solid var(--line);border-radius:var(--radius);padding:1rem 1.25rem;margin:1rem 0}"
        ".key-fact h3{margin:.1rem 0 .6rem}.source-details{border-top:1px solid var(--line);"
        "margin-top:1rem;padding-top:.75rem}.source-details summary{cursor:pointer;font-weight:750;"
        "color:var(--accent-dark)}.evidence-item{padding:.75rem 0}.evidence-item blockquote{margin:.5rem 0;"
        "padding:.75rem 1rem;border-left:3px solid var(--accent);background:var(--wash);"
        "overflow-wrap:anywhere}.source-meta{color:var(--muted);font-size:.9rem}"
        ".digest-list{padding-left:1.2rem}.entry-grid{display:grid;grid-template-columns:1fr 1fr;gap:1rem}"
        ".entry-card{background:#172033;color:#fff;border-radius:var(--radius);padding:1.25rem}"
        ".entry-card a{color:#9ed0ff;font-weight:750}.browse-form{display:grid;"
        "grid-template-columns:minmax(12rem,2fr) repeat(3,minmax(9rem,1fr)) auto;gap:.75rem;"
        "align-items:end;background:var(--paper);border:1px solid var(--line);border-radius:var(--radius);"
        "padding:1rem;margin-bottom:1rem}.browse-form label{display:grid;gap:.35rem;font-weight:700;"
        "font-size:.88rem}.browse-form input,.browse-form select,.browse-form button{width:100%;"
        "min-height:2.75rem;border:1px solid #9aa7b5;border-radius:.55rem;padding:.55rem .65rem;"
        "font:inherit}.browse-form button{background:var(--accent);color:#fff;border-color:var(--accent);"
        "font-weight:750;cursor:pointer}.empty-state{padding:2rem;background:var(--paper);"
        "border:1px dashed #9aa7b5;border-radius:var(--radius)}textarea,button{font:inherit}"
        ".capability-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:1rem}"
        ".capability-card,.research-panel,.subscription-card{background:var(--paper);border:1px solid var(--line);"
        "border-radius:var(--radius);padding:1.25rem;box-shadow:0 10px 30px rgba(23,32,51,.05)}"
        ".capability-card h3,.research-panel h2,.subscription-card h2{margin:.1rem 0 .55rem}"
        ".boundary-note{margin:1.25rem 0;padding:1rem 1.25rem;border-left:4px solid var(--accent);"
        "background:#eaf3fc;border-radius:.25rem var(--radius) var(--radius) .25rem}"
        ".boundary-note h2{font-size:1.1rem;margin:0 0 .3rem}.boundary-note p{margin:0}"
        ".research-examples{display:flex;gap:.65rem;flex-wrap:wrap;margin-bottom:1.5rem}"
        ".research-example{border:1px solid #8db7df;border-radius:999px;background:#f3f8fd;color:var(--accent-dark);"
        "padding:.55rem .8rem;font-weight:700;cursor:pointer;text-align:left}"
        ".research-example:hover,.research-example:focus-visible{background:#dcecff;border-color:var(--accent)}"
        ".research-panel{max-width:52rem;margin-top:1rem}#research-form{display:grid;gap:.75rem}"
        "#research-form label{font-weight:750}#research-question{width:100%;padding:.75rem;border:1px solid #9aa7b5;"
        "border-radius:.55rem;resize:vertical}#research-form button[type=submit]{justify-self:start;background:var(--accent);"
        "color:#fff;border:0;border-radius:.65rem;padding:.65rem 1rem;font-weight:750;cursor:pointer}"
        ".research-results{margin-top:1rem}.research-results p:empty,.research-results ul:empty{display:none}"
        ".rss-address{display:block;margin:.8rem 0;padding:.75rem;background:var(--wash);border:1px solid var(--line);"
        "border-radius:.55rem;overflow-wrap:anywhere}.rss-actions{display:flex;gap:1rem;flex-wrap:wrap;margin-top:1.5rem}"
        "@media (max-width: 56rem){.browse-form{grid-template-columns:1fr 1fr}.browse-form button{grid-column:1/-1}}"
        "@media (max-width: 42rem){body{padding:0 1rem 3rem}.site-nav{align-items:flex-start}"
        ".story-grid,.entry-grid,.browse-form,.capability-grid{grid-template-columns:1fr}h1{font-size:2.45rem}"
        ".story-card,.key-fact{padding:1rem}.nav-links{gap:.75rem}.browse-form button{grid-column:auto}}"
        "</style></head><body>"
        '<nav class="site-nav"><a class="brand" href="/">AI Intelligence</a>'
        '<div class="nav-links"><a href="/">首页</a><a href="/archive">Archive</a>'
        '<a href="/browse">Browse</a>'
        '<a href="/research">Research</a><a href="/rss">RSS</a></div></nav><main>'
        f"{content}</main></body></html>"
    )


def _render_entry_points() -> str:
    return (
        '<section aria-labelledby="continue-heading"><h2 id="continue-heading">继续探索</h2>'
        '<div class="entry-grid"><article class="entry-card"><h3>Browse</h3>'
        '<p>按发布者、主题和日期查找已发布 Story。</p><a href="/browse">浏览知识库</a></article>'
        '<article class="entry-card"><h3>Research</h3><p>基于已接受、已发布的依据提出问题。</p>'
        '<a href="/research">开始 Research</a></article></div></section>'
    )


def _relative_story_url(stable_key: str) -> str:
    return f"/stories/{quote(stable_key, safe='')}"


def _render_story_cards(stories: tuple[PublicStory, ...], story_url: Callable[[str], str]) -> str:
    return "".join(
        _render_story_card(story, headline_url=story_url(story.stable_key)) for story in stories
    )


def _render_story_card(story: PublicStory, *, headline_url: str) -> str:
    published = (
        story.original_published_at.date().isoformat()
        if story.original_published_at is not None
        else "时间未知"
    )
    topic = (
        f'<p class="topic">{escape(story.primary_topic.value)}'
        + (
            " · " + " · ".join(escape(item.value) for item in story.secondary_topics)
            if story.secondary_topics
            else ""
        )
        + "</p>"
        if story.primary_topic is not None
        else ""
    )
    summary = f"<p>{escape(story.summary)}</p>" if story.summary is not None else ""
    return (
        '<article class="story-card">'
        f"{topic}"
        f'<h2><a href="{escape(headline_url, quote=True)}">{escape(story.headline)}</a></h2>'
        f'{summary}<p class="story-meta">'
        f"<span>{escape(story.publisher)}</span><span>{published}</span></p></article>"
    )


def _render_story_detail(story: PublicStory) -> str:
    published = (
        story.original_published_at.isoformat()
        if story.original_published_at is not None
        else "时间未知"
    )
    source_url = _safe_source_url(story.canonical_url)
    source_link = (
        f'<a class="source-link" href="{escape(source_url, quote=True)}" '
        f'rel="noopener noreferrer">阅读 {escape(story.publisher)} 原文</a>'
        if source_url is not None
        else ""
    )
    key_facts = (
        "".join(
            _render_claim(claim, position=position)
            for position, claim in enumerate(story.claims, start=1)
        )
        or '<p class="empty-state">暂无可公开的关键事实。</p>'
    )
    topic = (
        f'<p class="eyebrow">{escape(story.primary_topic.value)}'
        + (
            " · " + " · ".join(escape(item.value) for item in story.secondary_topics)
            if story.secondary_topics
            else ""
        )
        + "</p>"
        if story.primary_topic is not None
        else ""
    )
    summary = (
        f'<p class="story-summary">{escape(story.summary)}</p>' if story.summary is not None else ""
    )
    importance = (
        '<section class="importance"><h2>为什么重要</h2>'
        f"<p>{escape(story.why_it_matters)}</p></section>"
        if story.why_it_matters is not None
        else ""
    )
    return (
        '<article class="story-detail"><header class="page-header">'
        f'{topic}<h1>{escape(story.headline)}</h1>{summary}<p class="story-meta">'
        f"<span>{escape(story.publisher)}</span><span>原始发布时间 {published}</span></p>{source_link}"
        f"</header>{importance}"
        '<section aria-labelledby="facts-heading"><h2 id="facts-heading">关键事实</h2>'
        f"{key_facts}</section></article>"
    )


def _render_claim(claim: PublicClaim, *, position: int) -> str:
    evidence_state = claim.evidence_state
    evidence = "".join(_render_evidence(item) for item in claim.evidence)
    details = (
        '<details class="source-details"><summary>来源与依据</summary>'
        f'<p class="source-meta">{EVIDENCE_STATE_LABELS[evidence_state]}</p>{evidence}</details>'
        if evidence
        else '<p class="source-meta">证据不足：尚无可公开的来源依据。</p>'
    )
    return (
        f'<section class="key-fact" id="claim-{claim.id}" '
        f'data-evidence-state="{evidence_state.value}"><h3>事实 {position}</h3>'
        f"<p>{escape(claim.text)}</p>{details}</section>"
    )


def _render_evidence(evidence: PublicEvidence) -> str:
    source_url = _safe_source_url(evidence.canonical_url)
    source_link = ""
    if source_url is not None:
        source_link = (
            "<p>"
            f'<a href="{escape(source_url, quote=True)}" rel="noopener noreferrer">'
            f"查看 {escape(evidence.publisher)} 原文</a></p>"
        )
    role = EVIDENCE_ROLE_LABELS[evidence.role]
    relation = EVIDENCE_RELATION_LABELS[evidence.relation]
    return (
        '<div class="evidence-item">'
        f'<blockquote id="evidence-{evidence.id}">{escape(evidence.exact_text)}</blockquote>'
        f'<p class="source-meta">{escape(evidence.publisher)} · {escape(role)} · '
        f"{escape(relation)}</p>{source_link}</div>"
    )


def _render_browse_form(
    *,
    q: str | None,
    publisher: str | None,
    topic: Topic | None,
    publication_date: date | None,
    sources: tuple[str, ...],
    topics: tuple[Topic, ...],
) -> str:
    def options(values: tuple[str, ...], selected: str | None) -> str:
        return "".join(
            f'<option value="{escape(value, quote=True)}"'
            f"{' selected' if value == selected else ''}>{escape(value)}</option>"
            for value in values
        )

    return (
        '<form class="browse-form" method="get" action="/browse">'
        '<label>关键词<input name="q" type="search" value="'
        f'{escape(q or "", quote=True)}" placeholder="模型、公司或事实"></label>'
        '<label>发布者<select name="source"><option value="">全部来源</option>'
        f"{options(sources, publisher)}</select></label>"
        '<label>主题<select name="topic"><option value="">全部主题</option>'
        f"{options(tuple(item.value for item in topics), topic.value if topic else None)}</select></label>"
        '<label>原始发布日期<input name="date" type="date" value="'
        f'{publication_date.isoformat() if publication_date else ""}"></label>'
        '<button type="submit">筛选</button></form>'
    )


def _safe_source_url(value: str) -> str | None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return value


def _render_research_page() -> str:
    return r"""
<header class="page-header">
  <p class="eyebrow">Published knowledge, cited</p>
  <h1>Research</h1>
  <p class="lede">从本期已发布 Story 中查找答案，并沿引用返回具体事实与原始来源。</p>
</header>
<section aria-labelledby="research-capabilities">
  <h2 id="research-capabilities">支持什么</h2>
  <div class="capability-grid">
    <article class="capability-card">
      <h3>已发布知识</h3>
      <p>仅检索已接受且已发布的知识，草稿和已拒绝内容不会进入答案。</p>
    </article>
    <article class="capability-card">
      <h3>可追溯依据</h3>
      <p>答案引用已发布 Story 中的关键事实，并链接到对应依据和原始来源。</p>
    </article>
    <article class="capability-card">
      <h3>明确拒答</h3>
      <p>证据不足时会明确拒答，不用缺失的材料补写结论。</p>
    </article>
  </div>
</section>
<aside class="boundary-note">
  <h2>边界提示</h2>
  <p>Research 不会联网搜索；它只回答当前公开知识库能够支持的问题。</p>
</aside>
<section aria-labelledby="research-examples-heading">
  <h2 id="research-examples-heading">试试这些问题</h2>
  <div class="research-examples">
    <button class="research-example" type="button" data-question="Anthropic 的年化营收运行率是多少？">Anthropic 的年化营收运行率是多少？</button>
    <button class="research-example" type="button" data-question="约束感知 GPU 分配器将利用率提升到多少？">约束感知 GPU 分配器将利用率提升到多少？</button>
    <button class="research-example" type="button" data-question="OpenAI 在俄亥俄州规划了多大规模的数据中心？">OpenAI 在俄亥俄州规划了多大规模的数据中心？</button>
  </div>
</section>
<section class="research-panel" aria-labelledby="research-question-heading">
  <h2 id="research-question-heading">如何提问</h2>
  <p>选择一个示例或输入一个具体问题；示例只会填入文本框，提交仍由你决定。</p>
  <form id="research-form">
    <label for="research-question">你的问题</label>
    <textarea id="research-question" maxlength="500" rows="4" required></textarea>
    <button type="submit">查找答案</button>
  </form>
  <div class="research-results" aria-live="polite">
    <p id="research-status" role="status"></p>
    <p id="research-answer"></p>
    <p id="research-refusal"></p>
    <ul id="research-citations"></ul>
  </div>
</section>
<script>
(() => {
  const form = document.getElementById("research-form");
  const question = document.getElementById("research-question");
  const examples = document.querySelectorAll(".research-example");
  const status = document.getElementById("research-status");
  const answer = document.getElementById("research-answer");
  const refusal = document.getElementById("research-refusal");
  const citations = document.getElementById("research-citations");

  for (const button of examples) {
    button.addEventListener("click", () => {
      question.value = button.dataset.question;
      question.focus();
    });
  }

  function handleEvent(block) {
    let event = "";
    let data = "";
    for (const line of block.split("\n")) {
      if (line.startsWith("event: ")) event = line.slice(7);
      if (line.startsWith("data: ")) data += line.slice(6);
    }
    if (!event || !data) return;
    const payload = JSON.parse(data);
    if (event === "status") {
      status.textContent = payload.state === "retrieving" ? "正在检索…" : "正在生成…";
    } else if (event === "answer.delta") {
      answer.textContent += payload.text;
    } else if (event === "citation") {
      const item = document.createElement("li");
      const link = document.createElement("a");
      link.href = payload.evidence_url;
      link.textContent = `${payload.story_title} — ${payload.claim_text} — ${payload.evidence_text}`;
      item.appendChild(link);
      citations.appendChild(item);
    } else if (event === "refusal" || event === "error") {
      refusal.textContent = payload.message;
    } else if (event === "done") {
      status.textContent = payload.status === "answered" ? "回答完成" : "已结束";
    }
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    answer.textContent = "";
    refusal.textContent = "";
    citations.replaceChildren();
    status.textContent = "正在连接…";
    try {
      const response = await fetch("/research/answer", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({question: question.value}),
      });
      if (!response.ok || !response.body) throw new Error("Research request failed");
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const {value, done} = await reader.read();
        buffer += decoder.decode(value || new Uint8Array(), {stream: !done});
        let boundary;
        while ((boundary = buffer.indexOf("\n\n")) >= 0) {
          handleEvent(buffer.slice(0, boundary));
          buffer = buffer.slice(boundary + 2);
        }
        if (done) break;
      }
    } catch (_) {
      status.textContent = "请求失败";
      refusal.textContent = "Research 服务当前不可用。";
    }
  });
})();
</script>
"""


def _render_rss_page() -> str:
    return (
        '<header class="page-header"><p class="eyebrow">Follow every edition</p>'
        '<h1>RSS 订阅</h1><p class="lede">RSS 是供阅读器订阅的机器可读更新流。'
        "每次发布 Digest 后，你的阅读器都能收到新一期及其 Story。</p></header>"
        '<section class="subscription-card" aria-labelledby="rss-address-heading">'
        '<h2 id="rss-address-heading">订阅地址</h2>'
        "<p>将下面的地址复制到你的 RSS 阅读器。直接打开时看到 XML 文本是正常的。</p>"
        '<code class="rss-address">/rss.xml</code>'
        '<a class="primary-action" href="/rss.xml">打开机器可读 RSS</a></section>'
        '<nav class="rss-actions" aria-label="RSS 后续路径">'
        '<a href="/">返回 Digest</a><a href="/browse">前往 Browse</a></nav>'
    )


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
        ElementTree.SubElement(item, "description").text = _render_story_cards(
            digest.stories, story_url
        )

    return ElementTree.tostring(rss, encoding="utf-8", xml_declaration=True)
