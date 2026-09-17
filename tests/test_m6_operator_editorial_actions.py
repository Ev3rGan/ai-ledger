from __future__ import annotations

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Barrier
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest
from editorial_test_support import (
    FakeEditorialProvider,
    persist_pending_stories,
)
from fastapi.testclient import TestClient
from pg0 import Pg0

from ai_intel_agent.editorial import (
    DigestPlanIdentity,
    EditorialConflictError,
    EditorialContext,
    EditorialPlanProposal,
    EditorialWorkflow,
)
from ai_intel_agent.operator_console import GitHubIdentity, OperatorSecurityConfiguration
from ai_intel_agent.persistence import (
    EditorialRepository,
    create_database_engine,
    upgrade_database,
)
from ai_intel_agent.publication import PublicPublicationRepository
from ai_intel_agent.web import create_app


@pytest.fixture
def m6_editorial_database_url() -> Iterator[str]:
    database = Pg0(name=f"ai_intel_m6_editorial_{os.urandom(8).hex()}")
    database.start()
    try:
        upgrade_database(database.uri)
        yield database.uri
    finally:
        database.drop()


class _M6FakeGitHubOAuth:
    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        return f"https://github.test/login?state={state}&redirect_uri={redirect_uri}"

    def identity_for_code(self, *, code: str, redirect_uri: str) -> GitHubIdentity:
        assert code == "fixture-code"
        assert redirect_uri.startswith("https://")
        assert redirect_uri.endswith("/operator/oauth/callback")
        return GitHubIdentity(user_id=42, login="operator")


class _M6RecordingProvider(FakeEditorialProvider):
    def __init__(self) -> None:
        self.contexts: list[EditorialContext] = []

    def prepare(self, context: EditorialContext) -> EditorialPlanProposal:
        self.contexts.append(context)
        return super().prepare(context)


class _M6ConcurrentProvider(FakeEditorialProvider):
    def __init__(self, barrier: Barrier) -> None:
        self._barrier = barrier

    def prepare(self, context: EditorialContext) -> EditorialPlanProposal:
        self._barrier.wait(timeout=10)
        return super().prepare(context)


class _M6BlockingReprepareProvider(_M6RecordingProvider):
    def __init__(self, entered: Barrier, release: Barrier) -> None:
        super().__init__()
        self._entered = entered
        self._release = release

    def prepare(self, context: EditorialContext) -> EditorialPlanProposal:
        if self.contexts:
            self._entered.wait(timeout=10)
            self._release.wait(timeout=10)
        return super().prepare(context)


def _m6_login(client: TestClient) -> str:
    started = client.get("/operator/login", follow_redirects=False)
    assert started.status_code == 303
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    completed = client.get(
        "/operator/oauth/callback",
        params={"code": "fixture-code", "state": state},
        follow_redirects=False,
    )
    assert completed.status_code == 303
    session = client.get("/api/operator/session")
    assert session.status_code == 200
    return str(session.json()["csrf_token"])


def test_operator_http_removes_a_and_d_then_approves_exact_b_and_c(
    m6_editorial_database_url: str,
) -> None:
    editorial_database_url = m6_editorial_database_url
    persist_pending_stories(
        editorial_database_url,
        story_count=4,
        batch="m6-http",
    )
    provider = _M6RecordingProvider()
    configuration = OperatorSecurityConfiguration(
        public_host="public.test",
        operator_host="operator.test",
        operator_origin="https://operator.test",
        allowed_github_user_ids=frozenset({42}),
    )
    app_under_test = create_app(
        editorial_database_url,
        operator_configuration=configuration,
        github_oauth_client=_M6FakeGitHubOAuth(),
        editorial_provider=provider,
    )

    with TestClient(app_under_test, base_url="https://operator.test") as client:
        csrf_token = _m6_login(client)
        mutation_headers = {
            "Origin": "https://operator.test",
            "X-CSRF-Token": csrf_token,
        }
        prepared_response = client.post(
            "/api/operator/plans/prepare",
            headers={**mutation_headers, "Idempotency-Key": "prepare-2026-08-21"},
            json={"publication_date": "2026-08-21", "expected_plan": None},
        )
        assert prepared_response.status_code == 201, prepared_response.text
        first = prepared_response.json()
        story_keys = [story["stable_key"] for story in first["stories"]]
        assert len(story_keys) == 4

        current = first
        first_removal_response = None
        for label, stable_key in (
            ("a", story_keys[0]),
            ("d", story_keys[3]),
        ):
            removed_response = client.post(
                f"/api/operator/plans/{current['id']}/stories/{stable_key}/remove",
                headers={**mutation_headers, "Idempotency-Key": f"remove-{label}"},
                json={
                    "expected_version": current["version"],
                    "expected_content_hash": current["content_hash"],
                    "reason": f"Remove Story {label.upper()} from this edition.",
                },
            )
            assert removed_response.status_code == 201, removed_response.text
            current = removed_response.json()
            if first_removal_response is None:
                first_removal_response = removed_response
                replayed_removal = client.post(
                    f"/api/operator/plans/{first['id']}/stories/{stable_key}/remove",
                    headers={**mutation_headers, "Idempotency-Key": "remove-a"},
                    json={
                        "expected_version": first["version"],
                        "expected_content_hash": first["content_hash"],
                        "reason": "Remove Story A from this edition.",
                    },
                )
                assert replayed_removal.status_code == 201
                assert replayed_removal.json() == first_removal_response.json()

        assert provider.contexts and len(provider.contexts) == 1
        assert [story["stable_key"] for story in current["stories"] if story["inclusion"] == "included"] == [
            story_keys[1],
            story_keys[2],
        ]

        stale_response = client.post(
            f"/api/operator/plans/{first['id']}/stories/{story_keys[1]}/remove",
            headers={**mutation_headers, "Idempotency-Key": "stale-tab"},
            json={
                "expected_version": first["version"],
                "expected_content_hash": first["content_hash"],
                "reason": "This stale tab must never switch to the latest Plan.",
            },
        )
        assert stale_response.status_code == 409
        assert "latest" in stale_response.json()["detail"].lower()

        approved_response = client.post(
            f"/api/operator/plans/{current['id']}/approve",
            headers={**mutation_headers, "Idempotency-Key": "approve-b-c"},
            json={
                "expected_version": current["version"],
                "expected_content_hash": current["content_hash"],
            },
        )
        assert approved_response.status_code == 200, approved_response.text
        approved = approved_response.json()
        assert approved["kind"] == "published"
        assert approved["public_url"] == "https://public.test/digests/2026-08-21"
        assert approved["follow_up"]["state"] == "queued"

        replay = client.post(
            f"/api/operator/plans/{current['id']}/approve",
            headers={**mutation_headers, "Idempotency-Key": "approve-b-c"},
            json={
                "expected_version": current["version"],
                "expected_content_hash": current["content_hash"],
            },
        )
        assert replay.status_code == 200
        assert replay.json() == approved

        engine = create_database_engine(editorial_database_url)
        try:
            repository = EditorialRepository(engine)
            claimed = repository.claim_retrieval_index_follow_ups(
                claimed_at=datetime(2026, 8, 21, 13, tzinfo=UTC)
            )
            assert len(claimed) == 1
            claim_id = claimed[0].claim_id
            assert claim_id is not None
            repository.fail_retrieval_index_follow_ups(
                claim_id,
                fault_code="fixture-failure",
                last_error="Deterministic fixture index failure.",
                completed_at=datetime(2026, 8, 21, 13, 1, tzinfo=UTC),
            )
        finally:
            engine.dispose()

        failed_detail = client.get(f"/api/operator/plans/{current['id']}")
        assert failed_detail.status_code == 200
        assert failed_detail.json()["index_follow_up"]["state"] == "failed"
        retry = client.post(
            f"/api/operator/plans/{current['id']}/follow-up/retry",
            headers={**mutation_headers, "Idempotency-Key": "retry-index"},
            json={
                "expected_version": current["version"],
                "expected_content_hash": current["content_hash"],
            },
        )
        assert retry.status_code == 200, retry.text
        assert retry.json()["state"] == "queued"
        replayed_retry = client.post(
            f"/api/operator/plans/{current['id']}/follow-up/retry",
            headers={**mutation_headers, "Idempotency-Key": "retry-index"},
            json={
                "expected_version": current["version"],
                "expected_content_hash": current["content_hash"],
            },
        )
        assert replayed_retry.status_code == 200
        assert replayed_retry.json() == retry.json()

    engine = create_database_engine(editorial_database_url)
    try:
        digest = PublicPublicationRepository(engine).digest_for_date(date(2026, 8, 21))
        assert digest is not None
        assert [story.stable_key for story in digest.stories] == [
            story_keys[1],
            story_keys[2],
        ]
    finally:
        engine.dispose()


def test_operator_http_approves_all_removed_stories_as_no_publication_and_fails_closed(
    m6_editorial_database_url: str,
) -> None:
    editorial_database_url = m6_editorial_database_url
    persist_pending_stories(
        editorial_database_url,
        story_count=4,
        batch="m6-zero",
        source_day=date(2026, 8, 21),
    )
    provider = _M6RecordingProvider()
    configuration = OperatorSecurityConfiguration(
        public_host="public.test",
        operator_host="operator.test",
        operator_origin="https://operator.test",
        allowed_github_user_ids=frozenset({42}),
    )
    app_under_test = create_app(
        editorial_database_url,
        operator_configuration=configuration,
        github_oauth_client=_M6FakeGitHubOAuth(),
        editorial_provider=provider,
    )

    with TestClient(app_under_test, base_url="https://operator.test") as client:
        csrf_token = _m6_login(client)
        mutation_headers = {
            "Origin": "https://operator.test",
            "X-CSRF-Token": csrf_token,
        }
        prepared = client.post(
            "/api/operator/plans/prepare",
            headers={**mutation_headers, "Idempotency-Key": "prepare-zero"},
            json={"publication_date": "2026-08-22", "expected_plan": None},
        )
        assert prepared.status_code == 201, prepared.text
        current = prepared.json()
        included_keys = [
            story["stable_key"]
            for story in current["stories"]
            if story["inclusion"] == "included"
        ]
        assert len(included_keys) == 4

        for position, stable_key in enumerate(included_keys):
            removed = client.post(
                f"/api/operator/plans/{current['id']}/stories/{stable_key}/remove",
                headers={
                    **mutation_headers,
                    "Idempotency-Key": f"remove-zero-{position}",
                },
                json={
                    "expected_version": current["version"],
                    "expected_content_hash": current["content_hash"],
                    "reason": f"Remove zero-publication fixture Story {position}.",
                },
            )
            assert removed.status_code == 201, removed.text
            current = removed.json()

        assert current["included_story_count"] == 0
        assert len(provider.contexts) == 1
        approved = client.post(
            f"/api/operator/plans/{current['id']}/approve",
            headers={**mutation_headers, "Idempotency-Key": "approve-zero"},
            json={
                "expected_version": current["version"],
                "expected_content_hash": current["content_hash"],
            },
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["kind"] == "no-publication"
        assert approved.json()["digest_id"] is None
        assert approved.json()["public_url"] is None
        assert approved.json()["follow_up"] is None

        unauthenticated = TestClient(app_under_test, base_url="https://operator.test")
        without_session = unauthenticated.post(
            f"/api/operator/plans/{current['id']}/approve",
            headers={
                "Origin": "https://operator.test",
                "X-CSRF-Token": csrf_token,
                "Idempotency-Key": "without-session",
            },
            json={
                "expected_version": current["version"],
                "expected_content_hash": current["content_hash"],
            },
        )
        assert without_session.status_code == 401

        bad_origin = client.post(
            f"/api/operator/plans/{current['id']}/approve",
            headers={
                **mutation_headers,
                "Origin": "https://attacker.test",
                "Idempotency-Key": "bad-origin",
            },
            json={
                "expected_version": current["version"],
                "expected_content_hash": current["content_hash"],
            },
        )
        assert bad_origin.status_code == 403

        bad_csrf = client.post(
            f"/api/operator/plans/{current['id']}/approve",
            headers={
                **mutation_headers,
                "X-CSRF-Token": "invalid",
                "Idempotency-Key": "bad-csrf",
            },
            json={
                "expected_version": current["version"],
                "expected_content_hash": current["content_hash"],
            },
        )
        assert bad_csrf.status_code == 403

        missing_idempotency = client.post(
            f"/api/operator/plans/{current['id']}/approve",
            headers=mutation_headers,
            json={
                "expected_version": current["version"],
                "expected_content_hash": current["content_hash"],
            },
        )
        assert missing_idempotency.status_code == 403

        first_key_use = client.post(
            f"/api/operator/plans/{current['id']}/approve",
            headers={**mutation_headers, "Idempotency-Key": "key-reuse"},
            json={
                "expected_version": current["version"],
                "expected_content_hash": current["content_hash"],
            },
        )
        assert first_key_use.status_code == 200
        conflicting_key_use = client.post(
            f"/api/operator/plans/{current['id']}/approve",
            headers={**mutation_headers, "Idempotency-Key": "key-reuse"},
            json={
                "expected_version": current["version"] + 1,
                "expected_content_hash": current["content_hash"],
            },
        )
        assert conflicting_key_use.status_code == 409

        prohibited_edit = client.post(
            f"/api/operator/plans/{current['id']}/approve",
            headers={**mutation_headers, "Idempotency-Key": "prohibited-edit"},
            json={
                "expected_version": current["version"],
                "expected_content_hash": current["content_hash"],
                "headline": "Operator must not edit this",
            },
        )
        assert prohibited_edit.status_code == 422
        assert client.post(
            "/api/operator/source-definitions/arbitrary",
            headers={**mutation_headers, "Idempotency-Key": "source-edit"},
            json={},
        ).status_code == 404

    with TestClient(app_under_test, base_url="https://public.test") as public_client:
        assert public_client.post(
            f"/api/operator/plans/{current['id']}/approve",
            json={
                "expected_version": current["version"],
                "expected_content_hash": current["content_hash"],
            },
        ).status_code == 404

    engine = create_database_engine(editorial_database_url)
    try:
        repository = EditorialRepository(engine)
        assert PublicPublicationRepository(engine).digest_for_date(date(2026, 8, 22)) is None
        outcome = repository.editorial_outcome(UUID(current["id"]))
        assert outcome is not None and outcome.digest is None
        assert repository.retrieval_index_follow_up(UUID(current["id"])) is None
    finally:
        engine.dispose()


def test_exact_prepare_and_reprepare_reject_a_stale_view_before_calling_provider(
    m6_editorial_database_url: str,
) -> None:
    editorial_database_url = m6_editorial_database_url
    persist_pending_stories(
        editorial_database_url,
        story_count=2,
        batch="m6-reprepare",
        source_day=date(2026, 8, 22),
    )
    engine = create_database_engine(editorial_database_url)
    provider = _M6RecordingProvider()
    observed_at = datetime(2026, 8, 22, 16, tzinfo=UTC)
    try:
        workflow = EditorialWorkflow(EditorialRepository(engine))
        first = workflow.prepare_exact(
            date(2026, 8, 23),
            expected_latest=None,
            provider=provider,
            prepared_at=observed_at,
        )
        assert len(provider.contexts) == 1

        unchanged = workflow.prepare_exact(
            date(2026, 8, 23),
            expected_latest=DigestPlanIdentity.from_plan(first),
            provider=provider,
            prepared_at=observed_at + timedelta(minutes=1),
        )
        assert unchanged == first
        assert len(provider.contexts) == 2

        with pytest.raises(EditorialConflictError, match="reload"):
            workflow.prepare_exact(
                date(2026, 8, 23),
                expected_latest=None,
                provider=provider,
                prepared_at=observed_at + timedelta(minutes=2),
            )
        assert len(provider.contexts) == 2
    finally:
        engine.dispose()


def test_concurrent_first_prepare_returns_one_plan_and_one_explicit_conflict(
    m6_editorial_database_url: str,
) -> None:
    editorial_database_url = m6_editorial_database_url
    persist_pending_stories(
        editorial_database_url,
        story_count=4,
        batch="m6-concurrent-prepare",
    )
    provider = _M6ConcurrentProvider(Barrier(2))
    configuration = OperatorSecurityConfiguration(
        public_host="public.test",
        operator_host="operator.test",
        operator_origin="https://operator.test",
        allowed_github_user_ids=frozenset({42}),
    )
    app_under_test = create_app(
        editorial_database_url,
        operator_configuration=configuration,
        github_oauth_client=_M6FakeGitHubOAuth(),
        editorial_provider=provider,
    )

    def prepare(idempotency_key: str) -> tuple[int, dict[str, object]]:
        with TestClient(app_under_test, base_url="https://operator.test") as client:
            csrf_token = _m6_login(client)
            response = client.post(
                "/api/operator/plans/prepare",
                headers={
                    "Origin": "https://operator.test",
                    "X-CSRF-Token": csrf_token,
                    "Idempotency-Key": idempotency_key,
                },
                json={"publication_date": "2026-08-21", "expected_plan": None},
            )
            return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(prepare, ("concurrent-a", "concurrent-b")))

    assert sorted(status for status, _body in results) == [201, 409]
    successful = next(body for status, body in results if status == 201)
    conflict = next(body for status, body in results if status == 409)
    assert successful["version"] == 1
    assert "changed" in str(conflict["detail"]).lower()

    engine = create_database_engine(editorial_database_url)
    try:
        latest = EditorialRepository(engine).latest_digest_plan(date(2026, 8, 21))
        assert latest is not None and latest.version == 1
    finally:
        engine.dispose()


def test_unchanged_reprepare_conflicts_when_story_removal_creates_a_new_latest_plan(
    m6_editorial_database_url: str,
) -> None:
    editorial_database_url = m6_editorial_database_url
    persist_pending_stories(
        editorial_database_url,
        story_count=4,
        batch="m6-reprepare-removal-race",
    )
    entered = Barrier(2)
    release = Barrier(2)
    provider = _M6BlockingReprepareProvider(entered, release)
    configuration = OperatorSecurityConfiguration(
        public_host="public.test",
        operator_host="operator.test",
        operator_origin="https://operator.test",
        allowed_github_user_ids=frozenset({42}),
    )
    app_under_test = create_app(
        editorial_database_url,
        operator_configuration=configuration,
        github_oauth_client=_M6FakeGitHubOAuth(),
        editorial_provider=provider,
    )

    with TestClient(app_under_test, base_url="https://operator.test") as removal_client:
        removal_csrf = _m6_login(removal_client)
        mutation_headers = {
            "Origin": "https://operator.test",
            "X-CSRF-Token": removal_csrf,
        }
        prepared_response = removal_client.post(
            "/api/operator/plans/prepare",
            headers={**mutation_headers, "Idempotency-Key": "prepare-before-race"},
            json={"publication_date": "2026-08-21", "expected_plan": None},
        )
        assert prepared_response.status_code == 201
        first = prepared_response.json()

        def reprepare() -> tuple[int, dict[str, object]]:
            with TestClient(app_under_test, base_url="https://operator.test") as client:
                csrf_token = _m6_login(client)
                response = client.post(
                    "/api/operator/plans/prepare",
                    headers={
                        "Origin": "https://operator.test",
                        "X-CSRF-Token": csrf_token,
                        "Idempotency-Key": "unchanged-reprepare-race",
                    },
                    json={
                        "publication_date": "2026-08-21",
                        "expected_plan": {
                            "id": first["id"],
                            "version": first["version"],
                            "content_hash": first["content_hash"],
                        },
                    },
                )
                return response.status_code, response.json()

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(reprepare)
            entered.wait(timeout=10)
            first_story_key = first["stories"][0]["stable_key"]
            removed = removal_client.post(
                f"/api/operator/plans/{first['id']}/stories/{first_story_key}/remove",
                headers={**mutation_headers, "Idempotency-Key": "remove-during-reprepare"},
                json={
                    "expected_version": first["version"],
                    "expected_content_hash": first["content_hash"],
                    "reason": "Create a new latest Plan while re-prepare is in flight.",
                },
            )
            assert removed.status_code == 201
            assert removed.json()["version"] == 2
            release.wait(timeout=10)
            status_code, body = future.result(timeout=10)

    assert status_code == 409
    assert "changed" in str(body["detail"]).lower()


@pytest.mark.skipif(
    os.environ.get("RUN_OPERATOR_BROWSER_ACCEPTANCE") != "1",
    reason="run explicitly for the deterministic local M6 browser acceptance",
)
def test_operator_browser_completes_exact_publication_and_no_publication(
    tmp_path: Path,
) -> None:
    import socket
    import subprocess
    from threading import Thread
    from time import monotonic, sleep

    from fastapi.responses import RedirectResponse
    from playwright.sync_api import Request as PlaywrightRequest
    from playwright.sync_api import sync_playwright
    from uvicorn import Config, Server

    name = f"ai_intel_m6_browser_{os.urandom(8).hex()}"
    database = Pg0(name=name, data_dir=tmp_path / "pg0")
    database.start()
    with socket.socket() as reserved_port:
        reserved_port.bind(("127.0.0.1", 0))
        port = reserved_port.getsockname()[1]
    public_host = f"public.localhost:{port}"
    operator_host = f"operator.localhost:{port}"
    operator_origin = f"https://{operator_host}"

    class LocalOAuth(_M6FakeGitHubOAuth):
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
        upgrade_database(database.uri)
        persist_pending_stories(
            database.uri,
            story_count=4,
            batch="m6-browser-publish",
        )
        persist_pending_stories(
            database.uri,
            story_count=4,
            batch="m6-browser-zero",
            source_day=date(2026, 8, 21),
        )
        provider = _M6RecordingProvider()
        security = OperatorSecurityConfiguration(
            public_host=public_host,
            operator_host=operator_host,
            operator_origin=operator_origin,
            allowed_github_user_ids=frozenset({42}),
        )
        web_app = create_app(
            database.uri,
            operator_configuration=security,
            github_oauth_client=LocalOAuth(),
            editorial_provider=provider,
        )

        @web_app.get("/operator/fake-github/authorize", include_in_schema=False)
        def fake_github_authorize(state: str) -> RedirectResponse:
            return RedirectResponse(
                f"/operator/oauth/callback?code=fixture-code&state={state}",
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
            page.wait_for_selector("text=operator")
            page.locator("#plan-publication-date").fill("2026-08-21")
            page.locator('[data-action="prepare-plan"]').click()
            page.wait_for_selector("text=Plan 检查 · 2026-08-21 v1")
            original_headlines = page.locator(".operator-story h3").all_inner_texts()
            assert len(original_headlines) == 4

            stale_page = context.new_page()
            stale_page.goto(f"{operator_origin}/")
            stale_page.locator(".plan-row").first.click()
            stale_page.wait_for_selector("text=Plan 检查 · 2026-08-21 v1")

            first_story = page.locator(".operator-story").filter(
                has=page.locator("[data-removal-reason]")
            ).first
            removal_requests: list[dict[str, object]] = []

            def record_removal_request(request: PlaywrightRequest) -> None:
                if request.method == "POST" and "/stories/" in request.url:
                    removal_requests.append(
                        {
                            "url": request.url,
                            "headers": request.headers,
                            "body": request.post_data,
                        }
                    )

            page.on("request", record_removal_request)
            first_story.locator("textarea").fill("Remove browser Story A.")
            first_story.locator("button[data-action^=remove-]").evaluate(
                "button => { button.click(); button.click(); }"
            )
            page.wait_for_selector("text=Plan 检查 · 2026-08-21 v2")
            assert len(removal_requests) == 1
            first_removal_request = removal_requests[0]
            first_removal_headers = first_removal_request["headers"]
            assert isinstance(first_removal_headers, dict)
            replayed_removal = page.evaluate(
                """
                async ({url, idempotencyKey, csrfToken, body}) => {
                  const response = await fetch(url, {
                    method: "POST",
                    headers: {
                      "Content-Type": "application/json",
                      "X-CSRF-Token": csrfToken,
                      "Idempotency-Key": idempotencyKey,
                    },
                    body,
                  });
                  return {status: response.status, body: await response.json()};
                }
                """,
                {
                    "url": first_removal_request["url"],
                    "idempotencyKey": first_removal_headers["idempotency-key"],
                    "csrfToken": first_removal_headers["x-csrf-token"],
                    "body": first_removal_request["body"],
                },
            )
            assert replayed_removal["status"] == 201
            assert replayed_removal["body"]["version"] == 2

            last_story = page.locator(".operator-story").filter(
                has=page.locator("[data-removal-reason]")
            ).last
            last_story.locator("textarea").fill("Remove browser Story D.")
            last_story.locator("button[data-action^=remove-]").click()
            page.wait_for_selector("text=Plan 检查 · 2026-08-21 v3")

            stale_story = stale_page.locator(".operator-story").filter(
                has=stale_page.locator("[data-removal-reason]")
            ).nth(1)
            stale_story.locator("textarea").fill("Stale browser tab must conflict.")
            stale_story.locator("button[data-action^=remove-]").click()
            stale_page.wait_for_selector("text=latest", state="attached")
            stale_page.get_by_role("button", name="重新加载").click()
            latest_plan_row = stale_page.locator(".plan-row").filter(
                has_text="2026-08-21 · v3"
            )
            latest_plan_row.wait_for()
            latest_plan_row.click()
            stale_page.wait_for_selector("text=Plan 检查 · 2026-08-21 v3")

            stale_page.locator('[data-action="approve-plan"]').click()
            public_link = stale_page.locator('[data-approval-result] a')
            public_link.wait_for()
            public_url = public_link.get_attribute("href")
            assert public_url == f"https://{public_host}/digests/2026-08-21"
            public_page = context.new_page()
            public_page.goto(public_url)
            assert public_page.locator(".story-card h2").all_inner_texts() == [
                original_headlines[1],
                original_headlines[2],
            ]
            public_page.close()

            engine = create_database_engine(database.uri)
            try:
                repository = EditorialRepository(engine)
                claimed = repository.claim_retrieval_index_follow_ups(
                    claimed_at=datetime.now(UTC)
                )
                assert len(claimed) == 1 and claimed[0].claim_id is not None
                repository.fail_retrieval_index_follow_ups(
                    claimed[0].claim_id,
                    fault_code="browser-fixture",
                    last_error="Browser fixture failure.",
                    completed_at=datetime.now(UTC),
                )
            finally:
                engine.dispose()
            page.locator(".plan-row").first.click()
            page.wait_for_selector("text=Browser fixture failure.")
            page.locator('[data-action="retry-follow-up"]').click()
            page.wait_for_selector("text=Retrieval Index Follow-Up：queued")

            page.locator("#plan-publication-date").fill("2026-08-22")
            page.locator('[data-action="prepare-plan"]').click()
            page.wait_for_selector("text=Plan 检查 · 2026-08-22 v1")
            for version in range(2, 6):
                removable = page.locator(".operator-story").filter(
                    has=page.locator("[data-removal-reason]")
                ).first
                removable.locator("textarea").fill(
                    f"Remove browser no-publication Story {version - 1}."
                )
                removable.locator("button[data-action^=remove-]").click()
                page.wait_for_selector(f"text=Plan 检查 · 2026-08-22 v{version}")
            page.locator('[data-action="approve-plan"]').click()
            page.wait_for_selector("text=本日已确认不发布")
            assert page.locator('input[name="headline"]').count() == 0
            assert page.locator('[data-action="reorder-story"]').count() == 0
            assert len(provider.contexts) == 2
            stale_page.close()
            context.close()
            browser.close()
    finally:
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=10)
        database.drop()
