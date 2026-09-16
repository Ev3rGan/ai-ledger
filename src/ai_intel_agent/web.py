from __future__ import annotations

import json
import logging
import secrets
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import date, datetime
from email.utils import format_datetime
from hashlib import sha256
from importlib.resources import files
from typing import Annotated
from urllib.parse import quote, urlencode
from uuid import UUID
from xml.etree import ElementTree

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
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
from ai_intel_agent.editorial import (
    DigestPlanIdentity,
    EditorialConflictError,
    EditorialNotFoundError,
    EditorialPlanProvider,
    EditorialStateError,
    EditorialWorkflow,
)
from ai_intel_agent.operator_console import (
    GitHubOAuthClient,
    GitHubOAuthError,
    OperatorIdempotencyConflictError,
    OperatorMutationInProgressError,
    OperatorMutationReceiptStore,
    OperatorReadProjection,
    OperatorSecurityConfiguration,
    OperatorSession,
    OperatorSessionStore,
    operator_follow_up_payload,
    operator_outcome_payload,
    utc_now,
)
from ai_intel_agent.persistence import EditorialRepository, create_database_engine
from ai_intel_agent.publication import (
    PublicContent,
    PublicDigest,
    PublicStory,
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
from ai_intel_agent.web_templates import (
    render_operator_page,
    render_public_page,
    render_story_cards,
)

LOGGER = logging.getLogger(__name__)
BROWSE_PAGE_SIZE = 12

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


class ExactPlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    version: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    def identity(self) -> DigestPlanIdentity:
        return DigestPlanIdentity(
            id=self.id,
            version=self.version,
            content_hash=self.content_hash,
        )


class PreparePlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    publication_date: date
    expected_plan: ExactPlanInput | None


class ExactPlanMutationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    expected_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    def identity(self, plan_id: UUID) -> DigestPlanIdentity:
        return DigestPlanIdentity(
            id=plan_id,
            version=self.expected_version,
            content_hash=self.expected_content_hash,
        )


class RemovePlanStoryInput(ExactPlanMutationInput):
    reason: str = Field(min_length=1, max_length=1000)


class _EditorialProviderUnavailableError(RuntimeError):
    pass


def create_app(
    database_url: str,
    *,
    research_provider: ResearchProvider | None = None,
    anonymous_research_daily_limit: int | None = None,
    anonymous_identity_salt: bytes | None = None,
    accepted_knowledge_retrieval: AcceptedKnowledgeOperation | None = None,
    retrieval_embedding: EmbeddingBackend | None = None,
    retrieval_reranker: RerankerBackend | None = None,
    operator_configuration: OperatorSecurityConfiguration | None = None,
    github_oauth_client: GitHubOAuthClient | None = None,
    editorial_provider: EditorialPlanProvider | None = None,
    operator_clock: object | None = None,
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
    now = getattr(operator_clock, "now", utc_now)
    operator_sessions: OperatorSessionStore | None = None
    if operator_configuration is not None:
        if github_oauth_client is None:
            raise ValueError("Operator Console requires a GitHub OAuth client")
        operator_sessions = OperatorSessionStore(engine, operator_configuration)
        operator_projection = OperatorReadProjection(
            engine,
            public_origin=f"https://{operator_configuration.public_host}",
        )
        operator_editorial = EditorialWorkflow(EditorialRepository(engine))
        operator_mutations = OperatorMutationReceiptStore(engine)
        app.state.operator_sessions = operator_sessions

        def require_operator_session(request: Request) -> OperatorSession:
            session = _operator_session(request, operator_sessions, now())
            if session is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            return session

        def execute_operator_mutation(
            request: Request,
            session: OperatorSession,
            *,
            operation: str,
            input_payload: dict[str, object],
            action: Callable[[], tuple[int, dict[str, object]]],
        ) -> Response:
            try:
                idempotency_key = _validate_operator_mutation_request(
                    request,
                    session,
                    operator_origin=operator_configuration.operator_origin,
                )
            except ValueError as error:
                return JSONResponse({"detail": str(error)}, status_code=403)
            request_hash = _operator_mutation_request_hash(operation, input_payload)
            try:
                replay = operator_mutations.begin(
                    github_user_id=session.github_user_id,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    created_at=now(),
                )
            except (OperatorIdempotencyConflictError, OperatorMutationInProgressError) as error:
                return JSONResponse({"detail": str(error)}, status_code=409)
            if replay is not None:
                return JSONResponse(replay.body, status_code=replay.status_code)

            def abandon_receipt() -> None:
                operator_mutations.abandon(
                    github_user_id=session.github_user_id,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                )

            try:
                status_code, body = action()
            except EditorialNotFoundError as error:
                abandon_receipt()
                return JSONResponse({"detail": str(error)}, status_code=404)
            except EditorialConflictError as error:
                abandon_receipt()
                return JSONResponse({"detail": str(error)}, status_code=409)
            except EditorialStateError as error:
                abandon_receipt()
                return JSONResponse({"detail": str(error)}, status_code=422)
            except _EditorialProviderUnavailableError as error:
                abandon_receipt()
                return JSONResponse({"detail": str(error)}, status_code=503)
            except Exception:
                abandon_receipt()
                raise
            operator_mutations.complete(
                github_user_id=session.github_user_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                status_code=status_code,
                body=body,
                completed_at=now(),
            )
            return JSONResponse(body, status_code=status_code)

        @app.middleware("http")
        async def isolate_operator_host(request: Request, call_next):
            host_values = request.headers.getlist("host")
            host = host_values[0].lower() if len(host_values) == 1 else ""
            path = request.url.path
            if path.startswith("/health/"):
                return await call_next(request)
            if host == operator_configuration.operator_host:
                if path == "/":
                    request.scope["path"] = "/operator"
                    request.scope["raw_path"] = b"/operator"
                elif not _is_operator_path(path):
                    return Response(status_code=404)
                response = await call_next(request)
                _set_operator_security_headers(response)
                return response
            if host == operator_configuration.public_host:
                if _is_operator_path(path):
                    return Response(status_code=404)
                return await call_next(request)
            return Response(status_code=404)

        @app.get("/operator", response_class=HTMLResponse, include_in_schema=False)
        def operator_page(request: Request) -> Response:
            session = _operator_session(request, operator_sessions, now())
            if session is None:
                return RedirectResponse("/operator/login", status_code=303)
            return HTMLResponse(render_operator_page())

        @app.get("/operator/login", include_in_schema=False)
        def operator_login() -> Response:
            state, browser_nonce = operator_sessions.begin_login(created_at=now())
            response = RedirectResponse(
                github_oauth_client.authorization_url(
                    state=state,
                    redirect_uri=operator_configuration.callback_uri,
                ),
                status_code=303,
            )
            response.set_cookie(
                "__Host-ai-ledger-operator-login",
                browser_nonce,
                max_age=int(operator_configuration.login_ttl.total_seconds()),
                path="/",
                secure=True,
                httponly=True,
                samesite="lax",
            )
            return response

        @app.get("/operator/oauth/callback", include_in_schema=False)
        def operator_oauth_callback(
            request: Request,
            code: str | None = None,
            state: str | None = None,
            error: str | None = None,
            error_description: str | None = None,
        ) -> Response:
            del error_description
            if error is not None or not code or not state:
                return JSONResponse({"detail": "OAuth authorization failed"}, status_code=400)
            browser_nonce = request.cookies.get("__Host-ai-ledger-operator-login", "")
            if not browser_nonce or not operator_sessions.consume_login(
                state=state,
                browser_nonce=browser_nonce,
                consumed_at=now(),
            ):
                return JSONResponse({"detail": "OAuth state mismatch"}, status_code=400)
            try:
                identity = github_oauth_client.identity_for_code(
                    code=code,
                    redirect_uri=operator_configuration.callback_uri,
                )
            except GitHubOAuthError:
                return JSONResponse({"detail": "OAuth provider unavailable"}, status_code=502)
            if identity.user_id not in operator_configuration.allowed_github_user_ids:
                return JSONResponse({"detail": "Operator identity is not authorized"}, status_code=403)
            created = operator_sessions.create(
                identity,
                created_at=now(),
                rotated_token=request.cookies.get("__Host-ai-ledger-operator"),
            )
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(
                "__Host-ai-ledger-operator",
                created.token,
                max_age=int(operator_configuration.session_absolute_ttl.total_seconds()),
                path="/",
                secure=True,
                httponly=True,
                samesite="lax",
            )
            response.delete_cookie(
                "__Host-ai-ledger-operator-login",
                path="/",
                secure=True,
                httponly=True,
                samesite="lax",
            )
            return response

        @app.get("/api/operator/session", include_in_schema=False)
        def operator_session(request: Request) -> Response:
            session = require_operator_session(request)
            return JSONResponse(_operator_session_payload(session))

        @app.get("/api/operator/dashboard", include_in_schema=False)
        def operator_dashboard(request: Request) -> Response:
            session = require_operator_session(request)
            return JSONResponse(operator_projection.dashboard(session))

        @app.get("/api/operator/plans", include_in_schema=False)
        def operator_plans(request: Request) -> Response:
            require_operator_session(request)
            return JSONResponse(operator_projection.plan_history())

        @app.get("/api/operator/plans/{plan_id}", include_in_schema=False)
        def operator_plan(plan_id: str, request: Request) -> Response:
            require_operator_session(request)
            try:
                parsed_plan_id = UUID(plan_id)
            except ValueError:
                return JSONResponse({"detail": "Digest Plan not found"}, status_code=404)
            payload = operator_projection.plan(parsed_plan_id)
            if payload is None:
                return JSONResponse({"detail": "Digest Plan not found"}, status_code=404)
            return JSONResponse(payload)

        @app.get(
            "/api/operator/document-versions/{document_version_id}",
            include_in_schema=False,
        )
        def operator_document_version(
            document_version_id: str,
            request: Request,
        ) -> Response:
            require_operator_session(request)
            try:
                parsed_document_id = UUID(document_version_id)
            except ValueError:
                return JSONResponse({"detail": "Document Version not found"}, status_code=404)
            payload = operator_projection.document_version(parsed_document_id)
            if payload is None:
                return JSONResponse({"detail": "Document Version not found"}, status_code=404)
            return JSONResponse(payload)

        @app.post("/api/operator/plans/prepare", include_in_schema=False)
        def operator_prepare_plan(
            request: Request,
            input_data: PreparePlanInput,
        ) -> Response:
            session = require_operator_session(request)

            def prepare() -> tuple[int, dict[str, object]]:
                if editorial_provider is None:
                    raise _EditorialProviderUnavailableError(
                        "Editorial Provider is not configured"
                    )
                plan = operator_editorial.prepare_exact(
                    input_data.publication_date,
                    expected_latest=(
                        input_data.expected_plan.identity()
                        if input_data.expected_plan is not None
                        else None
                    ),
                    provider=editorial_provider,
                    prepared_at=now(),
                )
                payload = operator_projection.plan(plan.id)
                if payload is None:
                    raise RuntimeError("Prepared Digest Plan is not readable")
                return 201, payload

            return execute_operator_mutation(
                request,
                session,
                operation="digest-plan.prepare",
                input_payload=input_data.model_dump(mode="json"),
                action=prepare,
            )

        @app.post(
            "/api/operator/plans/{plan_id}/stories/{story_stable_key}/remove",
            include_in_schema=False,
        )
        def operator_remove_plan_story(
            plan_id: UUID,
            story_stable_key: str,
            request: Request,
            input_data: RemovePlanStoryInput,
        ) -> Response:
            session = require_operator_session(request)

            def remove() -> tuple[int, dict[str, object]]:
                plan = operator_editorial.remove_story_exact(
                    input_data.identity(plan_id),
                    story_stable_key=story_stable_key,
                    reason=input_data.reason,
                    actor_identifier=f"github:{session.github_user_id}",
                    removed_at=now(),
                )
                payload = operator_projection.plan(plan.id)
                if payload is None:
                    raise RuntimeError("Derived Digest Plan is not readable")
                return 201, payload

            return execute_operator_mutation(
                request,
                session,
                operation=f"digest-plan.remove-story:{plan_id}:{story_stable_key}",
                input_payload=input_data.model_dump(mode="json"),
                action=remove,
            )

        @app.post("/api/operator/plans/{plan_id}/approve", include_in_schema=False)
        def operator_approve_plan(
            plan_id: UUID,
            request: Request,
            input_data: ExactPlanMutationInput,
        ) -> Response:
            session = require_operator_session(request)

            def approve() -> tuple[int, dict[str, object]]:
                outcome = operator_editorial.approve_exact(
                    input_data.identity(plan_id),
                    actor_identifier=f"github:{session.github_user_id}",
                    approved_at=now(),
                )
                return 200, operator_outcome_payload(
                    outcome,
                    public_origin=f"https://{operator_configuration.public_host}",
                )

            return execute_operator_mutation(
                request,
                session,
                operation=f"digest-plan.approve:{plan_id}",
                input_payload=input_data.model_dump(mode="json"),
                action=approve,
            )

        @app.post(
            "/api/operator/plans/{plan_id}/follow-up/retry",
            include_in_schema=False,
        )
        def operator_retry_plan_follow_up(
            plan_id: UUID,
            request: Request,
            input_data: ExactPlanMutationInput,
        ) -> Response:
            session = require_operator_session(request)

            def retry() -> tuple[int, dict[str, object]]:
                follow_up = operator_editorial.retry_follow_up_exact(
                    input_data.identity(plan_id),
                    actor_identifier=f"github:{session.github_user_id}",
                    requested_at=now(),
                )
                return 200, operator_follow_up_payload(follow_up)

            return execute_operator_mutation(
                request,
                session,
                operation=f"digest-plan.follow-up-retry:{plan_id}",
                input_payload=input_data.model_dump(mode="json"),
                action=retry,
            )

        @app.post("/operator/logout", include_in_schema=False)
        def operator_logout(request: Request) -> Response:
            session_token = request.cookies.get("__Host-ai-ledger-operator", "")
            session = require_operator_session(request)
            origin_values = request.headers.getlist("origin")
            csrf_values = request.headers.getlist("x-csrf-token")
            if len(origin_values) != 1 or origin_values[0] != operator_configuration.operator_origin:
                return JSONResponse({"detail": "CSRF validation failed"}, status_code=403)
            if len(csrf_values) != 1 or not secrets.compare_digest(
                csrf_values[0],
                session.csrf_token,
            ):
                return JSONResponse({"detail": "CSRF validation failed"}, status_code=403)
            operator_sessions.revoke(session_token, revoked_at=now())
            response = Response(status_code=204)
            response.delete_cookie(
                "__Host-ai-ledger-operator",
                path="/",
                secure=True,
                httponly=True,
                samesite="lax",
            )
            return response
    static_root = files("ai_intel_agent").joinpath("static")
    if static_root.is_dir():
        app.mount("/assets", StaticFiles(directory=str(static_root)), name="assets")
    if operator_configuration is not None:
        operator_static_root = files("ai_intel_agent").joinpath("operator_static")
        if operator_static_root.is_dir():
            app.mount(
                "/operator-assets",
                StaticFiles(directory=str(operator_static_root)),
                name="operator-assets",
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
        page: Annotated[int, Query(ge=1)] = 1,
    ) -> HTMLResponse:
        payload, stories = _browse_page(
            public_content=public_content,
            retrieval=retrieval,
            query=q,
            publisher=publisher,
            topic=topic,
            publication_date=publication_date,
            page=page,
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
                sources=tuple(payload["facets"]["sources"]),
                topics=tuple(
                    Topic(value) for value in payload["facets"]["topics"]
                ),
                stories=stories,
                story_url=_relative_story_url,
                browse_bootstrap=payload,
                browse_page_urls=_browse_page_urls(payload),
            )
        )

    @app.get("/api/public/browse", name="browse_public_api")
    def browse_public_api(
        q: str | None = None,
        publisher: Annotated[str | None, Query(alias="source")] = None,
        topic: Annotated[Topic | None, Query(alias="topic")] = None,
        publication_date: Annotated[date | None, Query(alias="date")] = None,
        page: Annotated[int, Query(ge=1)] = 1,
    ) -> JSONResponse:
        payload, _ = _browse_page(
            public_content=public_content,
            retrieval=retrieval,
            query=q,
            publisher=publisher,
            topic=topic,
            publication_date=publication_date,
            page=page,
        )
        return JSONResponse(payload)

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


def _is_operator_path(path: str) -> bool:
    return (
        path in {"/operator", "/api/operator", "/operator-assets"}
        or path.startswith(("/operator/", "/api/operator/", "/operator-assets/"))
    )


def _operator_session(
    request: Request,
    store: OperatorSessionStore,
    observed_at: datetime,
) -> OperatorSession | None:
    token = request.cookies.get("__Host-ai-ledger-operator")
    if not token:
        return None
    return store.resolve(token, observed_at=observed_at)


def _operator_session_payload(session: OperatorSession) -> dict[str, object]:
    return {
        "operator": {
            "github_user_id": session.github_user_id,
            "github_login": session.github_login,
        },
        "csrf_token": session.csrf_token,
        "absolute_expires_at": session.absolute_expires_at.isoformat(),
    }


def _validate_operator_mutation_request(
    request: Request,
    session: OperatorSession,
    *,
    operator_origin: str,
) -> str:
    origin_values = request.headers.getlist("origin")
    csrf_values = request.headers.getlist("x-csrf-token")
    idempotency_values = request.headers.getlist("idempotency-key")
    if len(origin_values) != 1 or origin_values[0] != operator_origin:
        raise ValueError("Operator mutation Origin validation failed")
    if len(csrf_values) != 1 or not secrets.compare_digest(
        csrf_values[0],
        session.csrf_token,
    ):
        raise ValueError("Operator mutation CSRF validation failed")
    if len(idempotency_values) != 1:
        raise ValueError("Operator mutation requires one Idempotency-Key")
    idempotency_key = idempotency_values[0]
    if (
        idempotency_key != idempotency_key.strip()
        or not 1 <= len(idempotency_key) <= 255
        or any(ord(character) < 33 or ord(character) > 126 for character in idempotency_key)
    ):
        raise ValueError("Operator mutation Idempotency-Key is invalid")
    return idempotency_key


def _operator_mutation_request_hash(
    operation: str,
    input_payload: dict[str, object],
) -> str:
    encoded = json.dumps(
        {"operation": operation, "input": input_payload},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _set_operator_security_headers(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
        "frame-ancestors 'none'; form-action 'self' https://github.com"
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"


def _relative_story_url(stable_key: str) -> str:
    return f"/stories/{quote(stable_key, safe='')}"


def _browse_page(
    *,
    public_content: PublicContent,
    retrieval: AcceptedKnowledgeOperation,
    query: str | None,
    publisher: str | None,
    topic: Topic | None,
    publication_date: date | None,
    page: int,
) -> tuple[dict[str, object], tuple[PublicStory, ...]]:
    """Keep Browse retrieval semantics while projecting only public-safe Stories."""
    result = retrieval.retrieve(
        RetrievalQuery(
            text=query or "",
            filters=RetrievalFilters(
                publisher=publisher,
                topic=topic,
                publication_date=publication_date,
            ),
        )
    )
    all_stories = public_content.browse_published_stories()
    stories_by_id = {story.id: story for story in all_stories}
    matching_stories = tuple(
        stories_by_id[story_id]
        for story_id in result.matching_story_ids
        if story_id in stories_by_id
    )
    total_items = len(matching_stories)
    total_pages = max(1, (total_items + BROWSE_PAGE_SIZE - 1) // BROWSE_PAGE_SIZE)
    current_page = min(page, total_pages)
    offset = (current_page - 1) * BROWSE_PAGE_SIZE
    page_stories = matching_stories[offset : offset + BROWSE_PAGE_SIZE]
    payload: dict[str, object] = {
        "filters": {
            "q": query,
            "source": publisher,
            "topic": topic.value if topic is not None else None,
            "date": publication_date.isoformat() if publication_date is not None else None,
        },
        "facets": {
            "sources": sorted({story.publisher for story in all_stories}),
            "topics": sorted(
                {
                    story.primary_topic.value
                    for story in all_stories
                    if story.primary_topic is not None
                }
            ),
        },
        "items": [_browse_story_payload(story) for story in page_stories],
        "pagination": {
            "page": current_page,
            "page_size": BROWSE_PAGE_SIZE,
            "total_items": total_items,
            "total_pages": total_pages,
        },
    }
    return payload, page_stories


def _browse_story_payload(story: PublicStory) -> dict[str, object]:
    return {
        "url": _relative_story_url(story.stable_key),
        "headline": story.headline,
        "summary": story.lead,
        "publisher": story.publisher,
        "topic": story.primary_topic.value if story.primary_topic is not None else None,
        "secondary_topics": [topic.value for topic in story.secondary_topics],
        "published_at": (
            story.original_published_at.isoformat()
            if story.original_published_at is not None
            else None
        ),
    }


def _browse_page_urls(payload: dict[str, object]) -> dict[str, str | None]:
    filters = payload["filters"]
    pagination = payload["pagination"]
    if not isinstance(filters, dict) or not isinstance(pagination, dict):
        raise TypeError("Browse payload has invalid filters or pagination")
    current_page = int(pagination["page"])
    total_pages = int(pagination["total_pages"])
    return {
        "previous": _browse_url(filters, current_page - 1) if current_page > 1 else None,
        "next": _browse_url(filters, current_page + 1) if current_page < total_pages else None,
    }


def _browse_url(filters: dict[str, object], page: int) -> str:
    query = {
        name: str(value)
        for name, value in filters.items()
        if value is not None and str(value)
    }
    if page > 1:
        query["page"] = str(page)
    encoded = urlencode(query)
    return f"/browse?{encoded}" if encoded else "/browse"


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
