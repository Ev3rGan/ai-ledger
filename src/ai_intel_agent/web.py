from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import date
from email.utils import format_datetime
from html import escape
from urllib.parse import quote, urlsplit
from xml.etree import ElementTree

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from ai_intel_agent.domain import EvidenceRelation, EvidenceRole, EvidenceState
from ai_intel_agent.persistence import create_database_engine
from ai_intel_agent.publication import (
    PublicClaim,
    PublicDigest,
    PublicEvidence,
    PublicPublicationRepository,
    PublicStory,
)
from ai_intel_agent.research import (
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
) -> FastAPI:
    engine = create_database_engine(database_url)
    repository = PublicPublicationRepository(engine)
    research_repository = ResearchRepository(engine)

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

    @app.get("/", response_class=HTMLResponse, name="home")
    def home() -> HTMLResponse:
        digest = repository.latest_digest()
        if digest is None:
            content = "<h1>AI Intelligence</h1><p>暂无已发布 Digest。</p>"
        else:
            content = (
                "<h1>AI Intelligence</h1>"
                f'<p><a href="/digests/{digest.publication_date.isoformat()}">'
                f"阅读 {digest.publication_date.isoformat()} Digest</a></p>"
                + _render_linked_stories(digest.stories, _relative_story_url)
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
            f"<h1>Digest · {digest.publication_date.isoformat()}</h1>"
            + _render_linked_stories(digest.stories, _relative_story_url)
        )
        return HTMLResponse(_render_page(f"Digest {publication_date.isoformat()}", content))

    @app.get(
        "/stories/{stable_key}", response_class=HTMLResponse, name="story_page"
    )
    def story_page(stable_key: str) -> HTMLResponse:
        story = repository.published_story(stable_key)
        if story is None:
            raise HTTPException(status_code=404, detail="Story not found")
        return HTMLResponse(_render_page(story.headline, _render_story(story)))

    @app.get("/browse", response_class=HTMLResponse, name="browse")
    def browse() -> HTMLResponse:
        stories = repository.browse_published_stories()
        content = "<h1>Browse</h1>" + _render_linked_stories(
            stories, _relative_story_url
        )
        return HTMLResponse(_render_page("Browse", content))

    @app.get("/research", response_class=HTMLResponse, name="research")
    def research() -> HTMLResponse:
        return HTMLResponse(_render_page("Research", _render_research_page()))

    @app.post("/research/answer", name="research_answer")
    def research_answer(payload: ResearchQuestion) -> StreamingResponse:
        events = stream_research_events(
            payload.question,
            repository=research_repository,
            provider=research_provider,
        )
        return StreamingResponse(
            (_encode_sse(event, data) for event, data in events),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get("/rss.xml", name="rss")
    def rss(request: Request) -> Response:
        body = _render_rss(
            repository.published_digests(),
            home_url=str(request.url_for("home")),
            digest_url=lambda value: str(
                request.url_for("digest_page", publication_date=value.isoformat())
            ),
            story_url=lambda value: str(
                request.url_for("story_page", stable_key=value)
            ),
        )
        return Response(content=body, media_type="application/rss+xml")

    return app


def _render_page(title: str, content: str) -> str:
    return (
        "<!doctype html>"
        '<html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)}</title>"
        "<style>body{font-family:system-ui,sans-serif;line-height:1.6;max-width:52rem;"
        "margin:2rem auto;padding:0 1rem}nav{display:flex;gap:1rem}article{border-top:1px "
        "solid #bbb;margin-top:1.5rem;padding-top:1rem}blockquote{margin-left:0;padding-left:1rem;"
        "border-left:3px solid #777}a{color:#075985}</style></head><body>"
        '<nav><a href="/">首页</a><a href="/browse">Browse</a>'
        '<a href="/research">Research</a>'
        '<a href="/rss.xml">RSS</a></nav><main>'
        f"{content}</main></body></html>"
    )


def _render_story(
    story: PublicStory, *, headline_url: str | None = None
) -> str:
    headline = escape(story.headline)
    if headline_url is not None:
        headline = (
            f'<a href="{escape(headline_url, quote=True)}">{headline}</a>'
        )
    claims = "".join(_render_claim(claim) for claim in story.claims)
    return (
        "<article>"
        f"<h2>{headline}</h2>"
        f"{claims}</article>"
    )


def _relative_story_url(stable_key: str) -> str:
    return f"/stories/{quote(stable_key, safe='')}"


def _render_linked_stories(
    stories: tuple[PublicStory, ...], story_url: Callable[[str], str]
) -> str:
    return "".join(
        _render_story(story, headline_url=story_url(story.stable_key))
        for story in stories
    )


def _render_claim(claim: PublicClaim) -> str:
    evidence_state = claim.evidence_state
    evidence = "".join(_render_evidence(item) for item in claim.evidence)
    return (
        f'<section id="claim-{claim.id}">'
        f"<p><strong>Claim：</strong>{escape(claim.text)}</p>"
        f'<p data-evidence-state="{evidence_state.value}">'
        f"<strong>证据状态：</strong>{EVIDENCE_STATE_LABELS[evidence_state]}</p>"
        f"{evidence}</section>"
    )


def _render_evidence(evidence: PublicEvidence) -> str:
    source_url = _safe_source_url(evidence.canonical_url)
    source_link = ""
    if source_url is not None:
        source_link = (
            "<p><strong>原文链接：</strong>"
            f'<a href="{escape(source_url, quote=True)}" rel="noopener noreferrer">'
            f"{escape(source_url)}</a></p>"
        )
    role = EVIDENCE_ROLE_LABELS[evidence.role]
    relation = EVIDENCE_RELATION_LABELS[evidence.relation]
    return (
        f'<blockquote id="evidence-{evidence.id}">{escape(evidence.exact_text)}</blockquote>'
        f"<p><strong>Evidence Role：</strong>{escape(role)}</p>"
        f"<p><strong>Evidence Relation：</strong>{escape(relation)}</p>"
        f"<p><strong>发布者：</strong>{escape(evidence.publisher)}</p>"
        f"{source_link}"
    )


def _safe_source_url(value: str) -> str | None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return value


def _render_research_page() -> str:
    return r"""
<h1>Research</h1>
<form id="research-form">
  <label for="research-question">问题</label>
  <textarea id="research-question" maxlength="500" rows="4" required></textarea>
  <button type="submit">提问</button>
</form>
<p id="research-status" role="status"></p>
<p id="research-answer"></p>
<p id="research-refusal"></p>
<ul id="research-citations"></ul>
<script>
(() => {
  const form = document.getElementById("research-form");
  const question = document.getElementById("research-question");
  const status = document.getElementById("research-status");
  const answer = document.getElementById("research-answer");
  const refusal = document.getElementById("research-refusal");
  const citations = document.getElementById("research-citations");

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
        ElementTree.SubElement(item, "title").text = (
            f"AI Intelligence Digest · {digest.publication_date.isoformat()}"
        )
        ElementTree.SubElement(item, "link").text = digest_url(digest.publication_date)
        ElementTree.SubElement(item, "guid", isPermaLink="false").text = digest.stable_key
        ElementTree.SubElement(item, "pubDate").text = format_datetime(digest.published_at)
        ElementTree.SubElement(item, "description").text = _render_linked_stories(
            digest.stories, story_url
        )

    return ElementTree.tostring(rss, encoding="utf-8", xml_declaration=True)
