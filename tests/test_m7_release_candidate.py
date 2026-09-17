from __future__ import annotations

import os
import re
import shutil
import socket
import tomllib
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import unquote
from uuid import UUID
from zoneinfo import ZoneInfo

from editorial_test_support import FakeEditorialProvider, persist_pending_stories
from fastapi.testclient import TestClient
from pg0 import Pg0

from ai_intel_agent.operator_console import GitHubIdentity, OperatorSecurityConfiguration
from ai_intel_agent.persistence import (
    EditorialRepository,
    SchedulerStatusRepository,
    create_database_engine,
    upgrade_database,
)
from ai_intel_agent.runtime import GeminiScheduler, PostgresSchedulerLease
from ai_intel_agent.web import create_app

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")


def test_production_image_builds_locked_frontend_before_python_runtime() -> None:
    dockerfile = _read("deploy/m1/production.Dockerfile")
    dockerignore = _read(".dockerignore")
    project = tomllib.loads(_read("pyproject.toml"))["project"]
    cli = _read("src/ai_intel_agent/cli.py")

    assert re.search(
        r"^FROM node:[^\s]+@sha256:[0-9a-f]{64} AS frontend-build$",
        dockerfile,
        flags=re.MULTILINE,
    )
    assert "COPY frontend/package.json frontend/package-lock.json ./frontend/" in dockerfile
    assert "RUN npm --prefix frontend ci" in dockerfile
    assert "RUN npm --prefix frontend run build" in dockerfile

    runtime = dockerfile[dockerfile.index("FROM python:") :]
    assert "COPY --from=frontend-build /build/src/ai_intel_agent/static" in runtime
    assert "COPY --from=frontend-build /build/src/ai_intel_agent/operator_static" in runtime
    assert "RUN npm " not in runtime
    assert "RUN node " not in runtime
    assert not any(
        str(dependency).split("[", 1)[0].split(">", 1)[0] == "playwright"
        for dependency in project["dependencies"]
    )
    assert any(
        str(dependency).startswith("playwright")
        for dependency in project["optional-dependencies"]["dev"]
    )
    benchmark_wrapper = cli.index("def run_document_extraction_benchmark(")
    benchmark_import = cli.index("from ai_intel_agent.extraction_benchmark import")
    assert benchmark_import > benchmark_wrapper
    assert "ai_intel_agent.extraction_benchmark" not in cli[:benchmark_wrapper]

    ignored = set(dockerignore.splitlines())
    assert "frontend/node_modules" in ignored
    assert "src/ai_intel_agent/static" in ignored
    assert "src/ai_intel_agent/operator_static" in ignored


def test_caddy_isolates_hosts_and_keeps_cache_policy_on_its_trust_side() -> None:
    caddy = _read("deploy/m1/Caddyfile")
    public_marker = "{$AI_INTEL_DOMAIN} {"
    operator_marker = "{$AI_INTEL_OPERATOR_DOMAIN} {"
    assert caddy.count(public_marker) == 1
    assert caddy.count(operator_marker) == 1

    public = caddy[caddy.index(public_marker) : caddy.index(operator_marker)]
    operator = caddy[caddy.index(operator_marker) :]

    assert "@operator_surface" in public
    assert "respond @operator_surface 404" in public
    assert "@public_assets" in public
    assert 'Cache-Control "public, max-age=31536000, immutable"' in public
    assert "match status 2xx" in public
    assert "X-AI-Anonymous-Client {client_ip}" in public

    assert "@outside_operator" in operator
    assert "respond @outside_operator 404" in operator
    assert 'Cache-Control "no-store"' in operator
    assert "header_up -X-AI-Anonymous-Client" in operator


def test_ci_rebuilds_assets_and_proves_the_runtime_has_no_node() -> None:
    workflow = _read(".github/workflows/ci.yml")

    assert "actions/setup-node@" in workflow
    assert "npm --prefix frontend ci" in workflow
    assert "npm --prefix frontend test" in workflow
    assert "npm --prefix frontend run build" in workflow
    assert "git diff --exit-code -- src/ai_intel_agent/static" in workflow
    assert "docker build" in workflow
    assert "command -v node" in workflow
    assert "command -v npm" in workflow
    assert "playwright/driver/node" in workflow
    assert "m7_caddy_backend.py" in workflow
    assert "m7_caddy_acceptance.py" in workflow


def test_current_product_docs_describe_the_same_release_candidate() -> None:
    english = _read("README.md")
    chinese = _read("README.zh-CN.md")
    local_runbook = _read("docs/mvp-local-runbook.md")
    production_runbook = _read("docs/mvp-production-runbook.md")
    inventory = _read("docs/legacy-flow-inventory.md")
    archive = _read("docs/archive/README.md")
    operator_adr = _read("docs/adr/0012-isolate-operator-console-by-host.md")

    assert "0–12 Stories" in english
    assert "0–12 条 Story" in chinese
    assert "Operator Console" in english
    assert "Operator Console" in chinese
    assert "no-publication" in english
    assert "no-publication" in chinese

    for runbook in (local_runbook, production_runbook):
        assert "0-12 Stories" in runbook
        assert "prepare or re-prepare" in runbook
        assert "remove any number" in runbook
        assert "exact latest Plan" in runbook
        assert "no-publication" in runbook

    assert "M7 frozen Release Candidate" in production_runbook
    assert "M4 frozen Release Candidate" not in production_runbook
    assert "M4 live acceptance record" not in production_runbook
    assert "only the authenticated read projection" not in production_runbook
    assert "No legacy flow is deleted by Issue #125" in " ".join(inventory.split())
    assert "No deletion" in inventory
    assert "grilling-checkpoint-2026-08-11.md" in archive
    assert "Caddy" in operator_adr and "immutable" in operator_adr


def test_local_markdown_links_resolve() -> None:
    missing: list[str] = []
    markdown_paths = tuple(REPOSITORY_ROOT.glob("*.md")) + tuple(
        (REPOSITORY_ROOT / "docs").rglob("*.md")
    )
    for document_path in markdown_paths:
        document = document_path.read_text(encoding="utf-8")
        for match in re.finditer(r"(?<!!)\[[^\]]+\]\(([^)]+)\)", document):
            raw_target = match.group(1).strip()
            target = raw_target.split("#", 1)[0].split("?", 1)[0]
            if not target or re.match(r"^[a-z][a-z0-9+.-]*:", target, re.IGNORECASE):
                continue
            resolved = (document_path.parent / unquote(target)).resolve()
            if not resolved.exists():
                missing.append(
                    f"{document_path.relative_to(REPOSITORY_ROOT)} -> {raw_target}"
                )

    assert missing == []


class _FakeGitHubOAuth:
    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        return f"https://github.test/login?state={state}&redirect_uri={redirect_uri}"

    def identity_for_code(self, *, code: str, redirect_uri: str) -> GitHubIdentity:
        assert code == "fixture-code"
        assert redirect_uri == "https://operator.test/operator/oauth/callback"
        return GitHubIdentity(user_id=42, login="release-operator")


def _login(client: TestClient) -> str:
    started = client.get("/operator/login", follow_redirects=False)
    assert started.status_code == 303
    state = re.search(r"[?&]state=([^&]+)", started.headers["location"])
    assert state is not None
    completed = client.get(
        "/operator/oauth/callback",
        params={"code": "fixture-code", "state": state.group(1)},
        follow_redirects=False,
    )
    assert completed.status_code == 303
    session = client.get("/api/operator/session")
    assert session.status_code == 200
    return str(session.json()["csrf_token"])


def _mutate(
    client: TestClient,
    path: str,
    *,
    csrf_token: str,
    idempotency_key: str,
    body: dict[str, object],
):
    return client.post(
        path,
        headers={
            "Origin": "https://operator.test",
            "X-CSRF-Token": csrf_token,
            "Idempotency-Key": idempotency_key,
        },
        json=body,
    )


def test_frozen_release_candidate_survives_restart_and_zero_story_completion(
    tmp_path: Path,
) -> None:
    database = Pg0(name=f"ai_intel_m7_rc_{os.urandom(8).hex()}")
    assert database.data_dir is None
    restored_database: Pg0 | None = None
    restored_instance_dir: Path | None = None
    started_database = database.start()
    assert started_database.data_dir is not None
    assert started_database.port is not None
    primary_data_dir = Path(started_database.data_dir)
    managed_instances_dir = primary_data_dir.parent.parent
    assert primary_data_dir == managed_instances_dir / database.name / "data"
    configuration = OperatorSecurityConfiguration(
        public_host="public.test",
        operator_host="operator.test",
        operator_origin="https://operator.test",
        allowed_github_user_ids=frozenset({42}),
    )
    provider = FakeEditorialProvider()
    first_plan: dict[str, object]
    session_token: str
    csrf_token: str
    try:
        upgrade_database(database.uri)
        scheduler_engine = create_database_engine(database.uri)
        collection_calls: list[str] = []

        def collect_authorized_fixture() -> None:
            persist_pending_stories(database.uri, story_count=4, batch="m7-published")
            collection_calls.append("complete")

        scheduler_now = datetime(2026, 8, 21, 5, 59, tzinfo=ZoneInfo("Asia/Shanghai"))
        scheduler_waits = iter((False, True))
        scheduler_status = SchedulerStatusRepository(scheduler_engine)
        scheduler = GeminiScheduler(
            collect=collect_authorized_fixture,
            now=lambda: scheduler_now,
            wait=lambda _: next(scheduler_waits),
            status=scheduler_status,
        )
        try:
            with PostgresSchedulerLease(scheduler_engine):
                scheduler.run()
            scheduler_snapshot = scheduler_status.snapshot()
        finally:
            scheduler_engine.dispose()
        assert collection_calls == ["complete"]
        assert scheduler_snapshot is not None
        assert scheduler_snapshot.state == "stopped"
        assert scheduler_snapshot.last_result == "succeeded"

        persist_pending_stories(
            database.uri,
            story_count=4,
            batch="m7-zero",
            source_day=date(2026, 8, 21),
        )
        first_app = create_app(
            database.uri,
            operator_configuration=configuration,
            github_oauth_client=_FakeGitHubOAuth(),
            editorial_provider=provider,
        )
        with TestClient(first_app, base_url="https://operator.test") as operator:
            csrf_token = _login(operator)
            session_token = str(operator.cookies.get("__Host-ai-ledger-operator"))
            prepared = _mutate(
                operator,
                "/api/operator/plans/prepare",
                csrf_token=csrf_token,
                idempotency_key="m7-prepare-published",
                body={"publication_date": "2026-08-21", "expected_plan": None},
            )
            assert prepared.status_code == 201, prepared.text
            original = prepared.json()
            included = [
                story for story in original["stories"] if story["inclusion"] == "included"
            ]
            assert len(included) == 4

            current = original
            for label, story in (("first", included[0]), ("last", included[-1])):
                removed = _mutate(
                    operator,
                    f"/api/operator/plans/{current['id']}/stories/{story['stable_key']}/remove",
                    csrf_token=csrf_token,
                    idempotency_key=f"m7-remove-{label}",
                    body={
                        "expected_version": current["version"],
                        "expected_content_hash": current["content_hash"],
                        "reason": f"Remove the {label} RC fixture Story.",
                    },
                )
                assert removed.status_code == 201, removed.text
                current = removed.json()

            remaining = [
                story for story in current["stories"] if story["inclusion"] == "included"
            ]
            assert [story["stable_key"] for story in remaining] == [
                included[1]["stable_key"],
                included[2]["stable_key"],
            ]
            approved = _mutate(
                operator,
                f"/api/operator/plans/{current['id']}/approve",
                csrf_token=csrf_token,
                idempotency_key="m7-approve-published",
                body={
                    "expected_version": current["version"],
                    "expected_content_hash": current["content_hash"],
                },
            )
            assert approved.status_code == 200, approved.text
            assert approved.json()["kind"] == "published"
            first_plan = current

        with TestClient(first_app, base_url="https://public.test") as public:
            home = public.get("/")
            digest = public.get("/digests/2026-08-21")
            browse = public.get("/browse")
            rss = public.get("/rss.xml")
            research = public.get("/research")
            assert all(
                response.status_code == 200
                for response in (home, digest, browse, rss, research)
            )
            remaining_headlines = [str(story["headline"]) for story in remaining]
            assert digest.text.index(remaining_headlines[0]) < digest.text.index(
                remaining_headlines[1]
            )
            for story in remaining:
                headline = str(story["headline"])
                stable_key = str(story["stable_key"])
                evidence = str(story["claims"][0]["evidence"][0]["exact_text"])
                assert headline in home.text
                assert headline in browse.text
                assert headline in rss.text
                assert headline in research.text
                story_page = public.get(f"/stories/{stable_key}")
                assert story_page.status_code == 200
                assert evidence in story_page.text
            for story in (included[0], included[-1]):
                headline = str(story["headline"])
                stable_key = str(story["stable_key"])
                assert headline not in home.text
                assert headline not in digest.text
                assert headline not in browse.text
                assert headline not in rss.text
                assert headline not in research.text
                assert public.get(f"/stories/{stable_key}").status_code == 404

        engine = create_database_engine(database.uri)
        try:
            repository = EditorialRepository(engine)
            claimed = repository.claim_retrieval_index_follow_ups(
                claimed_at=datetime(2026, 8, 21, 13, tzinfo=UTC)
            )
            assert len(claimed) == 1 and claimed[0].claim_id is not None
            repository.fail_retrieval_index_follow_ups(
                claimed[0].claim_id,
                fault_code="m7-controlled-failure",
                last_error="M7 controlled index failure.",
                completed_at=datetime(2026, 8, 21, 13, 1, tzinfo=UTC),
            )
        finally:
            engine.dispose()

        restarted_app = create_app(
            database.uri,
            operator_configuration=configuration,
            github_oauth_client=_FakeGitHubOAuth(),
            editorial_provider=provider,
        )
        with TestClient(restarted_app, base_url="https://operator.test") as operator:
            operator.cookies.set("__Host-ai-ledger-operator", session_token)
            assert operator.get("/api/operator/session").status_code == 200
            failed = operator.get(f"/api/operator/plans/{first_plan['id']}")
            assert failed.status_code == 200
            assert failed.json()["index_follow_up"]["state"] == "failed"
            retried = _mutate(
                operator,
                f"/api/operator/plans/{first_plan['id']}/follow-up/retry",
                csrf_token=csrf_token,
                idempotency_key="m7-retry-index",
                body={
                    "expected_version": first_plan["version"],
                    "expected_content_hash": first_plan["content_hash"],
                },
            )
            assert retried.status_code == 200, retried.text
            assert retried.json()["state"] == "queued"

            prepared_zero = _mutate(
                operator,
                "/api/operator/plans/prepare",
                csrf_token=csrf_token,
                idempotency_key="m7-prepare-zero",
                body={"publication_date": "2026-08-22", "expected_plan": None},
            )
            assert prepared_zero.status_code == 201, prepared_zero.text
            zero = prepared_zero.json()
            for position, story in enumerate(
                story for story in zero["stories"] if story["inclusion"] == "included"
            ):
                removed = _mutate(
                    operator,
                    f"/api/operator/plans/{zero['id']}/stories/{story['stable_key']}/remove",
                    csrf_token=csrf_token,
                    idempotency_key=f"m7-remove-zero-{position}",
                    body={
                        "expected_version": zero["version"],
                        "expected_content_hash": zero["content_hash"],
                        "reason": f"Remove zero-day RC Story {position}.",
                    },
                )
                assert removed.status_code == 201, removed.text
                zero = removed.json()
            assert zero["included_story_count"] == 0
            no_publication = _mutate(
                operator,
                f"/api/operator/plans/{zero['id']}/approve",
                csrf_token=csrf_token,
                idempotency_key="m7-approve-zero",
                body={
                    "expected_version": zero["version"],
                    "expected_content_hash": zero["content_hash"],
                },
            )
            assert no_publication.status_code == 200, no_publication.text
            no_publication_result = no_publication.json()
            assert no_publication_result["kind"] == "no-publication"
            assert no_publication_result["digest_id"] is None
            assert no_publication_result["public_url"] is None
            assert no_publication_result["follow_up"] is None

        with TestClient(restarted_app, base_url="https://public.test") as public:
            assert public.get("/digests/2026-08-21").status_code == 200
            assert public.get("/digests/2026-08-22").status_code == 404
            assert "2026-08-22" not in public.get("/rss.xml").text
            assert public.get(f"/api/operator/plans/{first_plan['id']}").status_code == 404

        engine = create_database_engine(database.uri)
        try:
            repository = EditorialRepository(engine)
            assert repository.editorial_outcome(UUID(str(zero["id"]))) is not None
            assert repository.retrieval_index_follow_up(UUID(str(zero["id"]))) is None
        finally:
            engine.dispose()

        database.stop()
        shutil.copytree(primary_data_dir, tmp_path / "backup")
        restored_name = f"ai_intel_m7_restored_{os.urandom(8).hex()}"
        restored_instance_dir = managed_instances_dir / restored_name
        restored_data_dir = restored_instance_dir / "data"
        assert restored_data_dir == managed_instances_dir / restored_name / "data"
        assert not restored_instance_dir.exists()
        shutil.copytree(tmp_path / "backup", restored_data_dir)
        restored_database = Pg0(
            name=restored_name,
            data_dir=str(restored_data_dir),
        )
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as primary_port_guard:
            primary_port_guard.bind(("127.0.0.1", started_database.port))
            primary_port_guard.listen()
            restored_info = restored_database.start()
        assert restored_info.port is not None
        assert restored_info.port != started_database.port
        restored_app = create_app(
            restored_database.uri,
            operator_configuration=configuration,
            github_oauth_client=_FakeGitHubOAuth(),
            editorial_provider=provider,
        )
        with TestClient(restored_app, base_url="https://operator.test") as operator:
            operator.cookies.set("__Host-ai-ledger-operator", session_token)
            assert operator.get("/api/operator/session").status_code == 200
            restored_plan = operator.get(f"/api/operator/plans/{first_plan['id']}")
            assert restored_plan.status_code == 200
            assert restored_plan.json()["content_hash"] == first_plan["content_hash"]
            assert restored_plan.json()["index_follow_up"]["state"] == "queued"

        with TestClient(restored_app, base_url="https://public.test") as public:
            assert public.get("/digests/2026-08-21").status_code == 200
            assert public.get("/digests/2026-08-22").status_code == 404
            assert "2026-08-22" not in public.get("/rss.xml").text
    finally:
        database.drop()
        if restored_database is not None:
            restored_database.drop()
        if restored_instance_dir is not None and restored_instance_dir.exists():
            assert restored_instance_dir.parent == managed_instances_dir
            shutil.rmtree(restored_instance_dir)
        if restored_instance_dir is not None:
            assert not restored_instance_dir.exists()
