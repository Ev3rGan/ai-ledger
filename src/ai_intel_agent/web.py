from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from email.utils import format_datetime
from html import escape
from urllib.parse import quote, urlsplit
from uuid import UUID
from xml.etree import ElementTree

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import Select, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ai_intel_agent.domain import EvidenceRole
from ai_intel_agent.persistence import (
    CandidateRecord,
    ClaimRecord,
    DigestRecord,
    DigestStoryRecord,
    DocumentVersionRecord,
    EvidenceSpanRecord,
    StoryRecord,
    create_database_engine,
)

EVIDENCE_STATE_LABELS = {
    "single-source": "单一来源",
    "multi-source": "多来源",
}
EVIDENCE_ROLE_LABELS: dict[EvidenceRole, str] = {
    EvidenceRole.PRIMARY: "第一方证据",
    EvidenceRole.INDEPENDENT: "独立证据",
    EvidenceRole.SECONDARY: "二手证据",
    EvidenceRole.COMMUNITY: "社区证据",
}


@dataclass(frozen=True)
class PublicEvidence:
    exact_text: str
    role: EvidenceRole
    source_url: str
    publisher: str


@dataclass(frozen=True)
class PublicClaim:
    text: str
    evidence: tuple[PublicEvidence, ...]


@dataclass(frozen=True)
class PublicStory:
    stable_key: str
    headline: str
    claims: tuple[PublicClaim, ...]

    @property
    def evidence_state(self) -> str:
        source_urls = {
            evidence.source_url
            for claim in self.claims
            for evidence in claim.evidence
        }
        return "single-source" if len(source_urls) == 1 else "multi-source"


@dataclass(frozen=True)
class PublicDigest:
    stable_key: str
    publication_date: date
    published_at: datetime
    stories: tuple[PublicStory, ...]


@dataclass
class _ClaimBuilder:
    text: str
    evidence: list[PublicEvidence] = field(default_factory=list)
    evidence_ids: set[UUID] = field(default_factory=set)


@dataclass
class _StoryBuilder:
    stable_key: str
    headline: str
    claims: list[_ClaimBuilder] = field(default_factory=list)
    claims_by_id: dict[UUID, _ClaimBuilder] = field(default_factory=dict)


class PublicPublicationRepository:
    """Read only the public projection of published Digests and their Stories."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def latest_digest(self) -> PublicDigest | None:
        with Session(self._engine) as session:
            record = session.scalars(self._published_digests_statement()).first()
            return self._to_public_digest(session, record) if record is not None else None

    def digest_for_date(self, publication_date: date) -> PublicDigest | None:
        with Session(self._engine) as session:
            record = session.scalars(
                self._published_digests_statement().where(
                    DigestRecord.publication_date == publication_date
                )
            ).first()
            return self._to_public_digest(session, record) if record is not None else None

    def published_digests(self) -> tuple[PublicDigest, ...]:
        with Session(self._engine) as session:
            records = session.scalars(self._published_digests_statement()).all()
            return tuple(self._to_public_digest(session, record) for record in records)

    def published_story(self, stable_key: str) -> PublicStory | None:
        with Session(self._engine) as session:
            stories = self._load_public_stories(session, stable_key=stable_key)
            return stories[0] if stories else None

    def browse_published_stories(self) -> tuple[PublicStory, ...]:
        with Session(self._engine) as session:
            return self._load_public_stories(session)

    @staticmethod
    def _published_digests_statement() -> Select[tuple[DigestRecord]]:
        return (
            select(DigestRecord)
            .where(DigestRecord.state == "published")
            .order_by(DigestRecord.publication_date.desc())
        )

    def _to_public_digest(
        self, session: Session, record: DigestRecord
    ) -> PublicDigest:
        if record.published_at is None:
            raise ValueError("A published Digest must have a publication time")
        return PublicDigest(
            stable_key=record.stable_key,
            publication_date=record.publication_date,
            published_at=record.published_at,
            stories=self._load_public_stories(session, digest_id=record.id),
        )

    @staticmethod
    def _load_public_stories(
        session: Session,
        *,
        digest_id: UUID | None = None,
        stable_key: str | None = None,
    ) -> tuple[PublicStory, ...]:
        statement = (
            select(
                StoryRecord.id.label("story_id"),
                StoryRecord.stable_key,
                StoryRecord.headline,
                ClaimRecord.id.label("claim_id"),
                ClaimRecord.text.label("claim_text"),
                EvidenceSpanRecord.id.label("evidence_id"),
                EvidenceSpanRecord.exact_text,
                EvidenceSpanRecord.role,
                DocumentVersionRecord.source_url,
                CandidateRecord.publisher,
            )
            .join(DigestStoryRecord, DigestStoryRecord.story_id == StoryRecord.id)
            .join(DigestRecord, DigestRecord.id == DigestStoryRecord.digest_id)
            .join(ClaimRecord, ClaimRecord.story_id == StoryRecord.id)
            .join(EvidenceSpanRecord, EvidenceSpanRecord.claim_id == ClaimRecord.id)
            .join(
                DocumentVersionRecord,
                DocumentVersionRecord.id == EvidenceSpanRecord.document_version_id,
            )
            .join(CandidateRecord, CandidateRecord.id == DocumentVersionRecord.candidate_id)
            .where(DigestRecord.state == "published")
            .order_by(
                DigestRecord.publication_date.desc(),
                DigestStoryRecord.position,
                ClaimRecord.position,
                EvidenceSpanRecord.start_offset,
            )
        )
        if digest_id is not None:
            statement = statement.where(DigestRecord.id == digest_id)
        if stable_key is not None:
            statement = statement.where(StoryRecord.stable_key == stable_key)

        builders: dict[UUID, _StoryBuilder] = {}
        for row in session.execute(statement):
            story = builders.setdefault(
                row.story_id,
                _StoryBuilder(stable_key=row.stable_key, headline=row.headline),
            )
            claim = story.claims_by_id.get(row.claim_id)
            if claim is None:
                claim = _ClaimBuilder(text=row.claim_text)
                story.claims_by_id[row.claim_id] = claim
                story.claims.append(claim)
            if row.evidence_id not in claim.evidence_ids:
                claim.evidence_ids.add(row.evidence_id)
                claim.evidence.append(
                    PublicEvidence(
                        exact_text=row.exact_text,
                        role=EvidenceRole(row.role),
                        source_url=row.source_url,
                        publisher=row.publisher,
                    )
                )

        return tuple(
            PublicStory(
                stable_key=story.stable_key,
                headline=story.headline,
                claims=tuple(
                    PublicClaim(text=claim.text, evidence=tuple(claim.evidence))
                    for claim in story.claims
                ),
            )
            for story in builders.values()
        )


def create_app(database_url: str) -> FastAPI:
    engine = create_database_engine(database_url)
    repository = PublicPublicationRepository(engine)

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
    evidence_state = story.evidence_state
    return (
        "<article>"
        f"<h2>{headline}</h2>"
        f'<p data-evidence-state="{evidence_state}"><strong>证据状态：</strong>'
        f"{EVIDENCE_STATE_LABELS[evidence_state]}</p>"
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
    evidence = "".join(_render_evidence(item) for item in claim.evidence)
    return f"<section><p><strong>Claim：</strong>{escape(claim.text)}</p>{evidence}</section>"


def _render_evidence(evidence: PublicEvidence) -> str:
    source_url = _safe_source_url(evidence.source_url)
    source_link = ""
    if source_url is not None:
        source_link = (
            "<p><strong>原文链接：</strong>"
            f'<a href="{escape(source_url, quote=True)}" rel="noopener noreferrer">'
            f"{escape(source_url)}</a></p>"
        )
    role = EVIDENCE_ROLE_LABELS[evidence.role]
    return (
        f"<blockquote>{escape(evidence.exact_text)}</blockquote>"
        f"<p><strong>Evidence Role：</strong>{escape(role)}</p>"
        f"<p><strong>发布者：</strong>{escape(evidence.publisher)}</p>"
        f"{source_link}"
    )


def _safe_source_url(value: str) -> str | None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return value


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
