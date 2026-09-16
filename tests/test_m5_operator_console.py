from __future__ import annotations

import os
import re
import socket
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Thread
from time import monotonic, sleep
from urllib.parse import parse_qs, urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient
from pg0 import Pg0
from sqlalchemy.orm import Session
from uvicorn import Config, Server

from ai_intel_agent import web_templates
from ai_intel_agent.editorial import (
    DigestPlan,
    DigestPlanAnomaly,
    DigestPlanInclusion,
    DigestPlanStory,
    _content_hash,
)
from ai_intel_agent.operator_console import (
    GitHubIdentity,
    GitHubOAuthError,
    OperatorSecurityConfiguration,
)
from ai_intel_agent.persistence import (
    CandidateRecord,
    DigestPlanRecord,
    DocumentVersionRecord,
    EditorialRepository,
    create_database_engine,
    upgrade_database,
)
from ai_intel_agent.pipeline import publish_sample_digest
from ai_intel_agent.web import create_app


class MutableClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 9, 16, 12, tzinfo=UTC)

    def now(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


class FakeGitHubOAuth:
    def __init__(self, identity: GitHubIdentity) -> None:
        self.identity = identity
        self.error: Exception | None = None
        self.exchanges: list[tuple[str, str]] = []

    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        return (
            "https://github.test/login/oauth/authorize"
            f"?client_id=fixture-client&state={state}&redirect_uri={redirect_uri}"
        )

    def identity_for_code(self, *, code: str, redirect_uri: str) -> GitHubIdentity:
        self.exchanges.append((code, redirect_uri))
        if self.error is not None:
            raise self.error
        return self.identity


@pytest.fixture(scope="module")
def operator_database_url() -> Iterator[str]:
    name = f"ai_intel_operator_{os.urandom(8).hex()}"
    data_dir = Path(tempfile.gettempdir()) / name
    server = Pg0(name=name, data_dir=data_dir)
    server.start()
    try:
        upgrade_database(server.uri)
        yield server.uri
    finally:
        server.drop()


@pytest.fixture
def operator_configuration() -> OperatorSecurityConfiguration:
    return OperatorSecurityConfiguration(
        public_host="public.test",
        operator_host="operator.test",
        operator_origin="https://operator.test",
        allowed_github_user_ids=frozenset({42}),
        session_idle_ttl=timedelta(minutes=30),
        session_absolute_ttl=timedelta(hours=8),
        login_ttl=timedelta(minutes=10),
    )


def _state_from(response) -> str:
    return parse_qs(urlsplit(response.headers["location"]).query)["state"][0]


def _login(client: TestClient, *, state_override: str | None = None):
    started = client.get("/operator/login", follow_redirects=False)
    assert started.status_code == 303
    state = _state_from(started)
    return client.get(
        "/operator/oauth/callback",
        params={"code": "fixture-code", "state": state_override or state},
        follow_redirects=False,
    )


def _operator_app(
    operator_database_url: str,
    operator_configuration: OperatorSecurityConfiguration,
    clock: MutableClock,
    oauth: FakeGitHubOAuth,
):
    return create_app(
        operator_database_url,
        operator_configuration=operator_configuration,
        github_oauth_client=oauth,
        operator_clock=clock,
    )


def _persist_operator_fixture_plan(database_url: str) -> tuple[UUID, UUID]:
    publish_sample_digest(database_url)
    engine = create_database_engine(database_url)
    try:
        story = EditorialRepository(engine).story("sample-story-v1")
        assert story is not None
        prepared_at = datetime(2026, 9, 16, 8, tzinfo=UTC)
        malicious_candidate_id = uuid5(NAMESPACE_URL, "operator-fixture-malicious-candidate")
        malicious_document_id = uuid5(NAMESPACE_URL, "operator-fixture-malicious-document")
        plan = DigestPlan(
            id=UUID(int=0),
            publication_date=date(2026, 9, 16),
            window_start=datetime(2026, 9, 15, 16, tzinfo=UTC),
            window_end=datetime(2026, 9, 16, 16, tzinfo=UTC),
            version=1,
            prepared_at=prepared_at,
            digest_summary="Operator fixture digest summary",
            stories=(
                DigestPlanStory(
                    id=story.id,
                    stable_key=story.stable_key,
                    headline=story.headline,
                    review_state=story.review_state,
                    claims=story.claims,
                    publisher=story.publisher,
                    canonical_url=story.canonical_url,
                    original_published_at=story.original_published_at,
                    primary_document_version_id=malicious_document_id,
                    primary_document_content_hash="b" * 64,
                    source_definition_id=story.source_definition_id,
                    source_definition_name=story.source_definition_name,
                    inclusion=DigestPlanInclusion.INCLUDED,
                    order=0,
                    summary=story.summary or "Fixture summary",
                    why_it_matters=story.why_it_matters or "Fixture impact",
                    primary_topic=(story.primary_topic.value if story.primary_topic else "Research"),
                    secondary_topics=tuple(topic.value for topic in story.secondary_topics),
                    exclusion_reason=None,
                ),
            ),
            source_health=(),
            scheduler_health=None,
            source_coverage=(story.publisher,),
            topic_coverage=(story.primary_topic.value if story.primary_topic else "Research",),
            anomalies=(
                DigestPlanAnomaly(
                    code="publisher-diversity",
                    message="Fixture has one Publisher",
                    blocking=False,
                ),
            ),
            provider_identifier="fixture-provider",
            protocol_version="fixture-v1",
            current_state_hash="a" * 64,
            content_hash="",
        )
        content_hash = _content_hash(plan.content_payload())
        plan_id = uuid5(
            NAMESPACE_URL,
            f"ai-intel-agent:digest-plan:{plan.publication_date.isoformat()}:v1:{content_hash}",
        )
        plan = replace(plan, id=plan_id, content_hash=content_hash)
        with Session(engine) as session, session.begin():
            session.add(
                DigestPlanRecord(
                    id=plan.id,
                    publication_date=plan.publication_date,
                    window_start=plan.window_start,
                    window_end=plan.window_end,
                    version=plan.version,
                    prepared_at=plan.prepared_at,
                    content=plan.content_payload(),
                    content_hash=plan.content_hash,
                    current_state_hash=plan.current_state_hash,
                    provider_identifier=plan.provider_identifier,
                    protocol_version=plan.protocol_version,
                )
            )
            session.add(
                CandidateRecord(
                    id=malicious_candidate_id,
                    title="Malicious fixture",
                    canonical_url="https://fixture.invalid/malicious",
                    publisher="Fixture",
                    discovered_at=prepared_at,
                )
            )
            session.add(
                DocumentVersionRecord(
                    id=malicious_document_id,
                    candidate_id=malicious_candidate_id,
                    source_url="javascript:alert(1)",
                    title="Malicious fixture",
                    body="<img src=x onerror=alert(1)><script>alert('raw')</script>",
                    content_hash="b" * 64,
                    observed_at=prepared_at,
                    published_at=prepared_at,
                    published_at_raw=prepared_at.isoformat(),
                    updated_at=None,
                    updated_at_raw=None,
                )
            )
        return plan.id, malicious_document_id
    finally:
        engine.dispose()


def test_operator_routes_and_assets_fail_closed_by_host_and_session(
    operator_database_url: str,
    operator_configuration: OperatorSecurityConfiguration,
) -> None:
    clock = MutableClock()
    oauth = FakeGitHubOAuth(GitHubIdentity(user_id=42, login="allowed"))
    app = _operator_app(operator_database_url, operator_configuration, clock, oauth)

    with TestClient(app, base_url="https://operator.test") as operator:
        assert operator.get("/api/operator/dashboard").status_code == 401
        assert operator.get("/archive").status_code == 404
    with TestClient(app, base_url="https://public.test") as public:
        assert public.get("/").status_code == 200
        assert public.get("/operator").status_code == 404
        assert public.get("/api/operator/dashboard").status_code == 404
        assert public.get("/operator-assets/fixture.js").status_code == 404
    with TestClient(app, base_url="https://wrong.test") as wrong:
        assert wrong.get("/").status_code == 404
        assert wrong.get("/api/operator/dashboard").status_code == 404


def test_oauth_uses_numeric_allowlist_and_keeps_tokens_server_side(
    operator_database_url: str,
    operator_configuration: OperatorSecurityConfiguration,
) -> None:
    clock = MutableClock()
    oauth = FakeGitHubOAuth(GitHubIdentity(user_id=42, login="renamed-account"))
    app = _operator_app(operator_database_url, operator_configuration, clock, oauth)

    with TestClient(app, base_url="https://operator.test") as client:
        callback = _login(client)
        assert callback.status_code == 303
        assert callback.headers["location"] == "/"
        assert oauth.exchanges == [
            ("fixture-code", "https://operator.test/operator/oauth/callback")
        ]
        set_cookie = callback.headers["set-cookie"]
        assert "__Host-ai-ledger-operator=" in set_cookie
        assert "HttpOnly" in set_cookie
        assert "Secure" in set_cookie
        assert "SameSite=lax" in set_cookie
        assert "github" not in set_cookie.lower()

        dashboard = client.get("/api/operator/dashboard")
        assert dashboard.status_code == 200
        assert dashboard.json()["operator"] == {
            "github_user_id": 42,
            "github_login": "renamed-account",
        }
        assert "token" not in dashboard.text.lower()


def test_oauth_state_provider_and_allowlist_fail_closed(
    operator_database_url: str,
    operator_configuration: OperatorSecurityConfiguration,
) -> None:
    clock = MutableClock()
    oauth = FakeGitHubOAuth(GitHubIdentity(user_id=42, login="allowed"))
    app = _operator_app(operator_database_url, operator_configuration, clock, oauth)

    with TestClient(app, base_url="https://operator.test") as client:
        assert _login(client, state_override="mismatched-state").status_code == 400
        assert client.get("/api/operator/dashboard").status_code == 401
        provider_error = client.get(
            "/operator/oauth/callback",
            params={"error": "access_denied", "error_description": "denied"},
        )
        assert provider_error.status_code == 400

    oauth.error = GitHubOAuthError("provider unavailable")
    with TestClient(app, base_url="https://operator.test") as client:
        assert _login(client).status_code == 502
        assert client.get("/api/operator/dashboard").status_code == 401

    oauth.error = None
    oauth.identity = GitHubIdentity(user_id=99, login="not-allowed")
    with TestClient(app, base_url="https://operator.test") as client:
        assert _login(client).status_code == 403
        assert client.get("/api/operator/dashboard").status_code == 401


def test_login_rotates_session_and_server_revocation_invalidates_it(
    operator_database_url: str,
    operator_configuration: OperatorSecurityConfiguration,
) -> None:
    clock = MutableClock()
    oauth = FakeGitHubOAuth(GitHubIdentity(user_id=42, login="allowed"))
    app = _operator_app(operator_database_url, operator_configuration, clock, oauth)

    with TestClient(app, base_url="https://operator.test") as client:
        assert _login(client).status_code == 303
        first_token = client.cookies.get("__Host-ai-ledger-operator")
        assert first_token
        assert _login(client).status_code == 303
        second_token = client.cookies.get("__Host-ai-ledger-operator")
        assert second_token and second_token != first_token

        with TestClient(app, base_url="https://operator.test") as replay:
            replay.cookies.set("__Host-ai-ledger-operator", first_token)
            assert replay.get("/api/operator/dashboard").status_code == 401

        app.state.operator_sessions.revoke(second_token, revoked_at=clock.now())
        assert client.get("/api/operator/dashboard").status_code == 401


def test_existing_session_fails_closed_after_allowlist_removes_numeric_id(
    operator_database_url: str,
    operator_configuration: OperatorSecurityConfiguration,
) -> None:
    clock = MutableClock()
    oauth = FakeGitHubOAuth(GitHubIdentity(user_id=42, login="allowed"))
    app = _operator_app(operator_database_url, operator_configuration, clock, oauth)
    with TestClient(app, base_url="https://operator.test") as client:
        assert _login(client).status_code == 303
        token = client.cookies.get("__Host-ai-ledger-operator")
        assert token

    restricted_configuration = replace(
        operator_configuration,
        allowed_github_user_ids=frozenset({99}),
    )
    restarted_app = _operator_app(
        operator_database_url,
        restricted_configuration,
        clock,
        FakeGitHubOAuth(GitHubIdentity(user_id=99, login="replacement")),
    )
    with TestClient(restarted_app, base_url="https://operator.test") as client:
        client.cookies.set("__Host-ai-ledger-operator", token)
        assert client.get("/api/operator/dashboard").status_code == 401


def test_session_idle_absolute_expiry_and_logout_csrf_origin(
    operator_database_url: str,
    operator_configuration: OperatorSecurityConfiguration,
) -> None:
    clock = MutableClock()
    oauth = FakeGitHubOAuth(GitHubIdentity(user_id=42, login="allowed"))
    app = _operator_app(operator_database_url, operator_configuration, clock, oauth)

    with TestClient(app, base_url="https://operator.test") as client:
        assert _login(client).status_code == 303
        session = client.get("/api/operator/session").json()
        csrf = session["csrf_token"]
        assert client.post("/operator/logout").status_code == 403
        assert (
            client.post(
                "/operator/logout",
                headers={"Origin": "https://evil.test", "X-CSRF-Token": csrf},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/operator/logout",
                headers={
                    "Origin": "https://operator.test",
                    "X-CSRF-Token": "wrong",
                },
            ).status_code
            == 403
        )
        logout = client.post(
            "/operator/logout",
            headers={"Origin": "https://operator.test", "X-CSRF-Token": csrf},
        )
        assert logout.status_code == 204
        assert client.get("/api/operator/dashboard").status_code == 401

    with TestClient(app, base_url="https://operator.test") as idle_client:
        assert _login(idle_client).status_code == 303
        clock.advance(timedelta(minutes=31))
        assert idle_client.get("/api/operator/dashboard").status_code == 401

    with TestClient(app, base_url="https://operator.test") as absolute_client:
        assert _login(absolute_client).status_code == 303
        for _ in range(31):
            clock.advance(timedelta(minutes=15))
            assert absolute_client.get("/api/operator/dashboard").status_code == 200
        clock.advance(timedelta(minutes=15, seconds=1))
        assert absolute_client.get("/api/operator/dashboard").status_code == 401


def test_operator_responses_set_private_security_headers(
    operator_database_url: str,
    operator_configuration: OperatorSecurityConfiguration,
) -> None:
    clock = MutableClock()
    oauth = FakeGitHubOAuth(GitHubIdentity(user_id=42, login="allowed"))
    app = _operator_app(operator_database_url, operator_configuration, clock, oauth)
    with TestClient(app, base_url="https://operator.test") as client:
        assert _login(client).status_code == 303
        response = client.get("/")
        assert response.status_code == 200
        asset_paths = tuple(re.findall(r'(?:src|href)="(/operator-assets/[^"]+)"', response.text))
        assert asset_paths
        assert all(client.get(path).status_code == 200 for path in asset_paths)
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-frame-options"] == "DENY"
        assert "default-src 'none'" in response.headers["content-security-policy"]
        assert "script-src 'self'" in response.headers["content-security-policy"]
    with TestClient(app, base_url="https://public.test") as public:
        assert all(public.get(path).status_code == 404 for path in asset_paths)


def test_operator_page_fails_explicitly_when_frontend_manifest_is_unavailable(
    operator_database_url: str,
    operator_configuration: OperatorSecurityConfiguration,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = MutableClock()
    oauth = FakeGitHubOAuth(GitHubIdentity(user_id=42, login="allowed"))
    app = _operator_app(operator_database_url, operator_configuration, clock, oauth)
    web_templates._operator_frontend_assets.cache_clear()
    monkeypatch.setattr(web_templates, "_OPERATOR_STATIC_ROOT", tmp_path)
    try:
        with TestClient(
            app,
            base_url="https://operator.test",
            raise_server_exceptions=False,
        ) as client:
            assert _login(client).status_code == 303
            response = client.get("/")
    finally:
        web_templates._operator_frontend_assets.cache_clear()

    assert response.status_code == 500
    assert "正在加载只读运行状态" not in response.text


def test_operator_read_projection_exposes_plan_evidence_and_escaped_raw_text(
    operator_database_url: str,
    operator_configuration: OperatorSecurityConfiguration,
) -> None:
    plan_id, document_version_id = _persist_operator_fixture_plan(operator_database_url)
    clock = MutableClock()
    oauth = FakeGitHubOAuth(GitHubIdentity(user_id=42, login="allowed"))
    app = _operator_app(operator_database_url, operator_configuration, clock, oauth)

    with TestClient(app, base_url="https://operator.test") as client:
        assert _login(client).status_code == 303
        dashboard = client.get("/api/operator/dashboard")
        assert dashboard.status_code == 200
        assert dashboard.json()["pending_story_count"] >= 0
        assert dashboard.json()["plans"][0]["id"] == str(plan_id)
        assert "scheduler" in dashboard.json()
        assert "sources" in dashboard.json()

        history = client.get("/api/operator/plans")
        assert history.status_code == 200
        assert history.json()["items"][0]["version"] == 1
        detail = client.get(f"/api/operator/plans/{plan_id}")
        assert detail.status_code == 200
        story = detail.json()["stories"][0]
        assert story["stable_key"] == "sample-story-v1"
        assert story["claims"][0]["evidence"][0]["document_version_id"]
        assert detail.json()["warnings"] == [
            {"code": "publisher-diversity", "message": "Fixture has one Publisher"}
        ]
        assert detail.json()["blockers"] == []

        raw = client.get(f"/api/operator/document-versions/{document_version_id}")
        assert raw.status_code == 200
        assert raw.json()["body"] == (
            "<img src=x onerror=alert(1)><script>alert('raw')</script>"
        )
        assert raw.json()["source_url"] is None
        assert raw.headers["content-type"].startswith("application/json")

    with TestClient(app, base_url="https://public.test") as public:
        assert public.get(f"/api/operator/plans/{plan_id}").status_code == 404
        assert (
            public.get(f"/api/operator/document-versions/{document_version_id}").status_code
            == 404
        )


@pytest.mark.skipif(
    os.environ.get("RUN_OPERATOR_BROWSER_ACCEPTANCE") != "1",
    reason="run explicitly for the deterministic local browser acceptance",
)
def test_operator_browser_acceptance_uses_fake_oauth_and_escapes_raw_html(
    tmp_path: Path,
) -> None:
    from playwright.sync_api import sync_playwright

    name = f"ai_intel_operator_browser_{os.urandom(8).hex()}"
    server_database = Pg0(name=name, data_dir=tmp_path / "pg0")
    server_database.start()
    with socket.socket() as reserved_port:
        reserved_port.bind(("127.0.0.1", 0))
        port = reserved_port.getsockname()[1]
    public_host = f"public.localhost:{port}"
    operator_host = f"operator.localhost:{port}"
    operator_origin = f"https://{operator_host}"

    class LocalFakeGitHubOAuth(FakeGitHubOAuth):
        def authorization_url(self, *, state: str, redirect_uri: str) -> str:
            return f"{operator_origin}/operator/fake-github/authorize?state={state}"

    cert_path = tmp_path / "operator-cert.pem"
    key_path = tmp_path / "operator-key.pem"
    openssl = Path(r"C:\Program Files\Git\usr\bin\openssl.exe")
    if not openssl.is_file():
        pytest.skip("Git OpenSSL is unavailable")
    subprocess.run(
        [
            str(openssl),
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key_path),
            "-out",
            str(cert_path),
            "-days",
            "1",
            "-subj",
            "/CN=operator.localhost",
            "-addext",
            "subjectAltName=DNS:operator.localhost,DNS:public.localhost",
        ],
        check=True,
        capture_output=True,
    )
    server: Server | None = None
    thread: Thread | None = None
    try:
        upgrade_database(server_database.uri)
        plan_id, _ = _persist_operator_fixture_plan(server_database.uri)
        clock = MutableClock()
        oauth = LocalFakeGitHubOAuth(GitHubIdentity(user_id=42, login="browser-operator"))
        security = OperatorSecurityConfiguration(
            public_host=public_host,
            operator_host=operator_host,
            operator_origin=operator_origin,
            allowed_github_user_ids=frozenset({42}),
        )
        web_app = _operator_app(server_database.uri, security, clock, oauth)

        @web_app.get("/operator/fake-github/authorize", include_in_schema=False)
        def fake_github_authorize(state: str) -> RedirectResponse:
            return RedirectResponse(
                f"/operator/oauth/callback?code=browser-code&state={state}",
                status_code=303,
            )

        server = Server(
            Config(
                web_app,
                host="127.0.0.1",
                port=port,
                log_level="warning",
                ssl_certfile=str(cert_path),
                ssl_keyfile=str(key_path),
            )
        )
        thread = Thread(target=server.run, daemon=True)
        thread.start()
        deadline = monotonic() + 10
        while not server.started and monotonic() < deadline:
            sleep(0.05)
        assert server.started

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(ignore_https_errors=True)
            page = context.new_page()
            page.goto(f"{operator_origin}/")
            page.wait_for_selector("text=browser-operator")
            assert "今日 Dashboard" in page.locator("body").inner_text()
            page.locator(f'[data-plan-id="{plan_id}"]').click()
            page.wait_for_selector("text=示例发布者的 AI Agent 会记录任务轨迹。")
            page.locator("[data-document-id]").first.click()
            page.wait_for_selector("[data-raw-document]")
            raw = page.locator("[data-raw-document]")
            assert "<script>alert('raw')</script>" in raw.inner_text()
            assert raw.locator("script").count() == 0
            assert raw.locator("img").count() == 0
            page.locator('[data-action="logout"]').click()
            page.wait_for_selector("text=已注销")
            public_response = page.goto(f"https://{public_host}/api/operator/dashboard")
            assert public_response is not None and public_response.status == 404
            context.close()
            browser.close()
    finally:
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=10)
        server_database.drop()
