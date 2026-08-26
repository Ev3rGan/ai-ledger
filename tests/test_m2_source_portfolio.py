from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from ipaddress import IPv4Address
from pathlib import Path
from types import MappingProxyType
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx
import pytest
from alembic.config import Config
from pg0 import Pg0
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from ai_intel_agent.cli import _source_status_payload
from ai_intel_agent.domain import Candidate, DocumentVersion
from ai_intel_agent.gemini_collection import DraftPreparationError
from ai_intel_agent.multisource_collection import collect_source_profiles
from ai_intel_agent.persistence import (
    MultiSourceCollectionRepository,
    create_database_engine,
    database_url_for_alembic_config,
    upgrade_database,
)
from ai_intel_agent.source_portfolio import load_source_universe
from ai_intel_agent.source_portfolio_acquisition import (
    HttpSourcePortfolioAdapter,
    SourceAcquisition,
    SourceItemStatus,
    SourcePortfolioAccessBlockedError,
    SourcePortfolioInvalidFormatError,
    SourcePortfolioItemResult,
    SourceSpecificRecord,
)
from alembic import command

PUBLIC_ADDRESS = IPv4Address("93.184.216.34")


class StaticResolver:
    def resolve(self, hostname: str) -> tuple[IPv4Address, ...]:
        return (PUBLIC_ADDRESS,)


class FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 8, 19, 4, 0, tzinfo=UTC)


class EmptyFeedAdapter:
    def discover(self, profile):
        return ()


class NeverArticleAdapter:
    def fetch(self, profile, entry):
        raise AssertionError("empty Feed cannot fetch an article")


class NeverDraftProvider:
    def prepare(self, document):
        raise AssertionError("ineligible or empty results cannot reach the Provider")


class SkipDraftProvider:
    def prepare(self, document):
        raise DraftPreparationError("deterministic fixture skips draft creation")


class EmptyPortfolioAdapter:
    def __init__(self) -> None:
        self.profile_keys: list[str] = []

    def acquire(
        self,
        profile,
        *,
        observed_at,
        backfill_limit,
        cursor_value,
        known_paper_identities,
        known_signal_targets,
    ) -> SourceAcquisition:
        self.profile_keys.append(profile.key)
        return SourceAcquisition(items=(), cursor_value=None)


@pytest.fixture
def m2_portfolio_database_url():
    server = Pg0(name=f"ai_intel_m2_portfolio_{uuid4().hex}")
    server.start()
    try:
        upgrade_database(server.uri)
        yield server.uri
    finally:
        server.drop()


@pytest.mark.postgres
def test_0007_terminal_candidate_results_upgrade_to_0008_with_protections() -> None:
    server = Pg0(name=f"ai_intel_m2_0007_upgrade_{uuid4().hex}")
    server.start()
    try:
        project_root = Path(__file__).resolve().parents[1]
        config = Config(str(project_root / "alembic.ini"))
        config.set_main_option(
            "sqlalchemy.url",
            database_url_for_alembic_config(server.uri),
        )
        command.upgrade(config, "0007")

        source_definition_id = uuid4()
        collection_run_id = uuid4()
        body_candidate_id = uuid4()
        invalid_candidate_id = uuid4()
        late_candidate_id = uuid4()
        document_version_id = uuid4()
        started_at = datetime(2026, 8, 19, 0, 0, tzinfo=UTC)
        completed_at = datetime(2026, 8, 19, 0, 1, tzinfo=UTC)
        engine = create_database_engine(server.uri)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO source_definitions (
                            id, name, publisher, entry_point, audit_version,
                            activation_conclusion, collection_schedule,
                            discovery_method, language, topic_scope,
                            access_constraints, extraction_adapter, health_policy,
                            cursor, storage_policy, public_excerpt_policy,
                            public_excerpt_max_characters, pause_conditions,
                            canonical_url_prefixes
                        ) VALUES (
                            :id, 'Legacy source', 'Legacy publisher',
                            'https://example.com/feed', 'legacy-audit.v1',
                            'approved', 'daily', 'legacy feed', 'en', '[]'::json,
                            '[]'::json, 'legacy adapter', 'legacy health',
                            'legacy cursor', 'legacy storage', 'legacy excerpt',
                            280, '[]'::json, '["https://example.com/"]'::json
                        )
                        """
                    ),
                    {"id": source_definition_id},
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO collection_runs (
                            id, retry_of_run_id, status, started_at, completed_at,
                            operation_key
                        ) VALUES (
                            :id, NULL, 'running', :started_at, NULL,
                            'm2-0007-upgrade-regression'
                        )
                        """
                    ),
                    {"id": collection_run_id, "started_at": started_at},
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO candidates (
                            id, title, canonical_url, publisher, discovered_at
                        ) VALUES
                            (:body_id, 'Body result', 'https://example.com/body',
                             'Legacy publisher', :started_at),
                            (:invalid_id, 'Invalid result', 'https://example.com/invalid',
                             'Legacy publisher', :started_at)
                        """
                    ),
                    {
                        "body_id": body_candidate_id,
                        "invalid_id": invalid_candidate_id,
                        "started_at": started_at,
                    },
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO document_versions (
                            id, candidate_id, source_url, title, body, content_hash,
                            observed_at, published_at, published_at_raw,
                            updated_at, updated_at_raw
                        ) VALUES (
                            :id, :candidate_id, 'https://example.com/body',
                            'Body result', 'Legacy body', :content_hash,
                            :started_at, NULL, NULL, NULL, NULL
                        )
                        """
                    ),
                    {
                        "id": document_version_id,
                        "candidate_id": body_candidate_id,
                        "content_hash": sha256(b"Legacy body").hexdigest(),
                        "started_at": started_at,
                    },
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO source_candidate_results (
                            collection_run_id, source_definition_id, candidate_id,
                            document_version_id, article_status, error_code,
                            error_message
                        ) VALUES
                            (:run_id, :source_id, :body_id, :document_id,
                             'body-valid', NULL, NULL),
                            (:run_id, :source_id, :invalid_id, NULL,
                             'invalid-format', 'invalid-format', 'Legacy invalid result')
                        """
                    ),
                    {
                        "run_id": collection_run_id,
                        "source_id": source_definition_id,
                        "body_id": body_candidate_id,
                        "document_id": document_version_id,
                        "invalid_id": invalid_candidate_id,
                    },
                )
                connection.execute(
                    text(
                        """
                        UPDATE collection_runs
                        SET status = 'complete', completed_at = :completed_at
                        WHERE id = :id
                        """
                    ),
                    {"id": collection_run_id, "completed_at": completed_at},
                )
        finally:
            engine.dispose()

        command.upgrade(config, "0008")

        engine = create_database_engine(server.uri)
        try:
            with engine.connect() as connection:
                migrated_rows = {
                    candidate_id: (evidence_eligible, eligibility_kind)
                    for candidate_id, evidence_eligible, eligibility_kind in (
                        connection.execute(
                            text(
                                """
                                SELECT candidate_id,
                                       evidence_eligible,
                                       eligibility_kind
                                FROM source_candidate_results
                                """
                            )
                        ).all()
                    )
                }
                migration_head = connection.scalar(text("SELECT version_num FROM alembic_version"))

            assert migrated_rows == {
                body_candidate_id: (True, "body-valid"),
                invalid_candidate_id: (False, "ineligible"),
            }
            assert migration_head == "0008"

            for statement in (
                text(
                    """
                    UPDATE source_candidate_results
                    SET eligibility_kind = eligibility_kind
                    WHERE candidate_id = :candidate_id
                    """
                ),
                text(
                    """
                    DELETE FROM source_candidate_results
                    WHERE candidate_id = :candidate_id
                    """
                ),
            ):
                with (
                    pytest.raises(
                        DBAPIError,
                        match="Source candidate collection result is immutable",
                    ),
                    engine.begin() as connection,
                ):
                    connection.execute(
                        statement,
                        {"candidate_id": body_candidate_id},
                    )

            with (
                pytest.raises(
                    DBAPIError,
                    match="Source candidate collection result is immutable",
                ),
                engine.begin() as connection,
            ):
                connection.execute(
                    text(
                        """
                        INSERT INTO candidates (
                            id, title, canonical_url, publisher, discovered_at
                        ) VALUES (
                            :id, 'Late result', 'https://example.com/late',
                            'Legacy publisher', :started_at
                        )
                        """
                    ),
                    {"id": late_candidate_id, "started_at": started_at},
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO source_candidate_results (
                            collection_run_id, source_definition_id, candidate_id,
                            document_version_id, article_status, error_code,
                            error_message, evidence_eligible, eligibility_kind
                        ) VALUES (
                            :run_id, :source_id, :candidate_id, NULL,
                            'invalid-format', 'invalid-format', 'Late result',
                            false, 'ineligible'
                        )
                        """
                    ),
                    {
                        "run_id": collection_run_id,
                        "source_id": source_definition_id,
                        "candidate_id": late_candidate_id,
                    },
                )
        finally:
            engine.dispose()
    finally:
        server.drop()


def test_versioned_source_universe_has_exact_approved_groups_and_domain_policies() -> None:
    profiles = load_source_universe()

    assert len(profiles) == 19
    assert len({profile.id for profile in profiles}) == 19
    assert [profile.key for profile in profiles if profile.acceptance_group == "core"] == [
        "gemini-api-release-notes",
        "the-decoder.com",
        "techcrunch.com",
        "hugging-face-blog",
        "qbitai.com",
        "openai-news",
        "github-trending",
        "hugging-face-daily-papers",
    ]
    supplemental = [
        profile for profile in profiles if profile.acceptance_group == "supplemental"
    ]
    assert len(supplemental) == 10
    assert {
        role: sum(profile.contribution_role == role for profile in supplemental)
        for role in (
            "Structured Primary Record",
            "Official Metadata",
            "Community Signal",
            "Analyst Signal",
        )
    } == {
        "Structured Primary Record": 3,
        "Official Metadata": 5,
        "Community Signal": 1,
        "Analyst Signal": 1,
    }
    disabled = [profile for profile in profiles if not profile.enabled]
    assert [(profile.key, profile.pause_state) for profile in disabled] == [
        ("machine-heart", "authorization-required")
    ]
    assert all(profile.cursor_policy for profile in profiles)
    assert all(profile.health_policy for profile in profiles)
    assert all(profile.pause_policy for profile in profiles)
    assert all(profile.access_scope for profile in profiles)
    assert all(profile.expected_contribution for profile in profiles)
    assert all(profile.overlap_rationale for profile in profiles)
    releases = next(
        profile for profile in profiles if profile.key == "curated-github-releases"
    )
    assert isinstance(releases.settings["repositories"], tuple)
    assert all(
        isinstance(policy, Mapping)
        and set(policy) == {
            "name",
            "licence_spdx",
            "release_body_eligible",
            "exclude_tag_patterns",
        }
        and isinstance(policy["exclude_tag_patterns"], tuple)
        and all(
            isinstance(pattern, str) and pattern.strip()
            for pattern in policy["exclude_tag_patterns"]
        )
        for policy in releases.settings["repositories"]
    )
    with pytest.raises(TypeError):
        releases.settings["repositories"][0]["release_body_eligible"] = False


def test_arxiv_is_throttled_abstract_only_and_version_deduplicated() -> None:
    profile = next(profile for profile in load_source_universe() if profile.key == "arxiv-ai")
    payload = b"""<feed xmlns="http://www.w3.org/2005/Atom">
      <title>arXiv</title>
      <entry><title>Version one</title><id>https://arxiv.org/abs/2401.00001v1</id>
      <link href="https://arxiv.org/abs/2401.00001v1" rel="alternate" />
      <updated>2026-08-19T02:00:00Z</updated><summary>Permitted abstract.</summary></entry>
      <entry><title>Version two</title><id>https://arxiv.org/abs/2401.00001v2</id>
      <link href="https://arxiv.org/abs/2401.00001v2" rel="alternate" />
      <updated>2026-08-19T03:00:00Z</updated><summary>Later abstract.</summary></entry>
    </feed>"""
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "application/atom+xml"}, content=payload)

    waits: list[float] = []
    with httpx.Client(transport=httpx.MockTransport(respond), trust_env=False) as client:
        adapter = HttpSourcePortfolioAdapter(
            client,
            resolver=StaticResolver(),
            monotonic=lambda: 100.0,
            wait=waits.append,
        )
        result = adapter.acquire(
            profile,
            observed_at=datetime(2026, 8, 19, 4, 0, tzinfo=UTC),
            backfill_limit=20,
            known_paper_identities=frozenset({("2401.00001", "v2")}),
            known_signal_targets=frozenset(),
        )
        adapter.acquire(
            profile,
            observed_at=datetime(2026, 8, 19, 4, 1, tzinfo=UTC),
            backfill_limit=20,
            known_paper_identities=frozenset(
                {("2401.00001", "v1"), ("2401.00001", "v2")}
            ),
            known_signal_targets=frozenset(),
        )

    assert len(requests) == 2
    assert requests[0].url.params["max_results"] == "20"
    assert "cat:cs.AI" in requests[0].url.params["search_query"]
    assert all("pdf" not in str(request.url).casefold() for request in requests)
    assert waits == [3.0]
    assert len(result.items) == 1
    item = result.items[0]
    assert item.status is SourceItemStatus.POLICY_VALID_STRUCTURED
    assert (item.source_record.external_id, item.source_record.external_version) == (
        "2401.00001",
        "v1",
    )
    assert item.source_record.policy_metadata == {
        "abstract_only": True,
        "pdf_fetched": False,
        "query_version": "ai-categories-keywords-2026-08-19.v1",
    }
    assert item.document_version is not None
    assert item.document_version.body == "Version one\n\nPermitted abstract."


def test_curated_releases_use_fixed_repositories_and_exclude_assets_and_previews() -> None:
    profile = next(
        profile
        for profile in load_source_universe()
        if profile.key == "curated-github-releases"
    )
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        repository = "/".join(request.url.path.split("/")[2:4])
        releases = []
        if repository == "vllm-project/vllm":
            releases = [
                {
                    "id": 701,
                    "html_url": "https://github.com/vllm-project/vllm/releases/tag/v1.2.3",
                    "tag_name": "v1.2.3",
                    "name": "vLLM 1.2.3",
                    "body": "Licensed release notes.",
                    "draft": False,
                    "prerelease": False,
                    "published_at": "2026-08-19T01:00:00Z",
                    "assets": [{"url": "https://api.github.com/assets/forbidden"}],
                },
                {"id": 702, "draft": True, "prerelease": False},
                {"id": 703, "draft": False, "prerelease": True},
                {
                    "id": 704,
                    "html_url": (
                        "https://github.com/vllm-project/vllm/releases/tag/"
                        "nightly-20260819"
                    ),
                    "tag_name": "nightly-20260819",
                    "name": "Nightly build",
                    "draft": False,
                    "prerelease": False,
                    "published_at": "2026-08-19T00:30:00Z",
                },
            ]
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps(releases).encode(),
        )

    with httpx.Client(transport=httpx.MockTransport(respond), trust_env=False) as client:
        result = HttpSourcePortfolioAdapter(client, resolver=StaticResolver()).acquire(
            profile,
            observed_at=datetime(2026, 8, 19, 4, 0, tzinfo=UTC),
            backfill_limit=5,
            known_paper_identities=frozenset(),
            known_signal_targets=frozenset(),
        )

    assert [request.url.path for request in requests] == [
        "/repos/vllm-project/vllm/releases",
        "/repos/sgl-project/sglang/releases",
        "/repos/huggingface/transformers/releases",
        "/repos/pytorch/pytorch/releases",
    ]
    assert len(result.items) == 1
    item = result.items[0]
    assert (item.source_record.external_id, item.source_record.external_version) == (
        "701",
        "v1.2.3",
    )
    assert item.source_record.policy_metadata["licence_spdx"] == "Apache-2.0"
    assert item.source_record.policy_metadata["assets_fetched"] is False
    assert "assets" not in item.source_record.structured_metadata
    assert item.source_record.structured_metadata["repository"] == "vllm-project/vllm"
    assert item.source_record.provenance["repository"] == "vllm-project/vllm"
    assert item.candidate.canonical_url.startswith(
        "https://github.com/vllm-project/vllm/releases/"
    )
    assert item.source_record.canonical_url.startswith(
        "https://github.com/vllm-project/vllm/releases/"
    )
    assert item.document_version is not None
    assert "Licensed release notes." in item.document_version.body


def test_curated_releases_apply_per_repository_tag_exclusion() -> None:
    release_profile = next(
        profile
        for profile in load_source_universe()
        if profile.key == "curated-github-releases"
    )
    profile = replace(
        release_profile,
        settings=MappingProxyType(
            {
                "maximum_items_per_repository": 5,
                "repositories": (
                    {
                        "name": "vllm-project/vllm",
                        "licence_spdx": "Apache-2.0",
                        "release_body_eligible": True,
                        "exclude_tag_patterns": ("ci-release",),
                    },
                    {
                        "name": "sgl-project/sglang",
                        "licence_spdx": "Apache-2.0",
                        "release_body_eligible": True,
                        "exclude_tag_patterns": ("nightly",),
                    },
                    {
                        "name": "huggingface/transformers",
                        "licence_spdx": "Apache-2.0",
                        "release_body_eligible": True,
                        "exclude_tag_patterns": (),
                    },
                    {
                        "name": "pytorch/pytorch",
                        "licence_spdx": "BSD-3-Clause",
                        "release_body_eligible": True,
                        "exclude_tag_patterns": (),
                    },
                ),
            }
        ),
    )

    def respond(request: httpx.Request) -> httpx.Response:
        repository = "/".join(request.url.path.split("/")[2:4])
        releases = {
            "vllm-project/vllm": [
                {
                    "id": 801,
                    "html_url": (
                        "https://github.com/vllm-project/vllm/releases/tag/v1.3.0"
                    ),
                    "tag_name": "v1.3.0",
                    "name": "vLLM 1.3.0",
                    "draft": False,
                    "prerelease": False,
                    "published_at": "2026-08-19T01:00:00Z",
                },
                {
                    "id": 802,
                    "html_url": (
                        "https://github.com/vllm-project/vllm/releases/tag/"
                        "v1.3.0-ci-release"
                    ),
                    "tag_name": "v1.3.0-ci-release",
                    "name": "CI build",
                    "draft": False,
                    "prerelease": False,
                    "published_at": "2026-08-19T00:30:00Z",
                },
            ],
            "sgl-project/sglang": [
                {
                    "id": 901,
                    "html_url": (
                        "https://github.com/sgl-project/sglang/releases/tag/v1.0.0"
                    ),
                    "tag_name": "v1.0.0",
                    "name": "SGLang 1.0.0",
                    "draft": False,
                    "prerelease": False,
                    "published_at": "2026-08-19T01:00:00Z",
                },
                {
                    "id": 902,
                    "html_url": (
                        "https://github.com/sgl-project/sglang/releases/tag/"
                        "v1.0.0-nightly"
                    ),
                    "tag_name": "v1.0.0-nightly",
                    "name": "Nightly build",
                    "draft": False,
                    "prerelease": False,
                    "published_at": "2026-08-19T00:30:00Z",
                },
            ],
        }.get(repository, [])
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps(releases).encode(),
        )

    with httpx.Client(transport=httpx.MockTransport(respond), trust_env=False) as client:
        result = HttpSourcePortfolioAdapter(client, resolver=StaticResolver()).acquire(
            profile,
            observed_at=datetime(2026, 8, 19, 4, 0, tzinfo=UTC),
            backfill_limit=5,
            known_paper_identities=frozenset(),
            known_signal_targets=frozenset(),
        )

    included = {
        (item.source_record.external_id, item.source_record.external_version)
        for item in result.items
    }
    assert included == {("801", "v1.3.0"), ("901", "v1.0.0")}
    assert all(
        item.source_record.structured_metadata["repository"]
        == item.source_record.provenance["repository"]
        for item in result.items
    )


def test_curated_releases_reject_invalid_tag_patterns() -> None:
    release_profile = next(
        profile
        for profile in load_source_universe()
        if profile.key == "curated-github-releases"
    )
    repositories = release_profile.settings["repositories"]
    policy = repositories[0]
    profile = replace(
        release_profile,
        settings=MappingProxyType(
            {
                "maximum_items_per_repository": 5,
                "repositories": (
                    {
                        "name": policy["name"],
                        "licence_spdx": policy["licence_spdx"],
                        "release_body_eligible": policy["release_body_eligible"],
                        "exclude_tag_patterns": ("(unclosed",),
                    },
                    *repositories[1:],
                ),
            }
        ),
    )

    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200)),
            trust_env=False,
        ) as client,
        pytest.raises(
            SourcePortfolioInvalidFormatError,
            match="tag exclusion pattern is invalid",
        ),
    ):
        HttpSourcePortfolioAdapter(client, resolver=StaticResolver()).acquire(
                profile,
                observed_at=datetime(2026, 8, 19, 4, 0, tzinfo=UTC),
                backfill_limit=5,
                known_paper_identities=frozenset(),
                known_signal_targets=frozenset(),
            )


def test_qwen_requires_verified_owner_policy_metadata_and_no_model_files() -> None:
    profile = next(profile for profile in load_source_universe() if profile.key == "qwen-hub")
    payload = [
        {
            "id": "Qwen/Qwen3-8B",
            "author": "Qwen",
            "sha": "a1b2c3d4",
            "lastModified": "2026-08-19T02:00:00Z",
            "gated": False,
            "private": False,
            "pipeline_tag": "text-generation",
            "tags": ["license:apache-2.0"],
            "cardData": {"license": "apache-2.0"},
            "siblings": [{"rfilename": "model.safetensors"}],
        },
        {"id": "Qwen/Qwen3-8B-AWQ", "author": "Qwen", "sha": "quantized"},
        {"id": "Qwen/Qwen3-8B-FP8", "author": "Qwen", "sha": "fp8"},
        {"id": "Qwen/Qwen3-8B-Mirror", "author": "Qwen", "sha": "mirror"},
        {"id": "mirror/Qwen3-8B", "author": "mirror", "sha": "mirror"},
    ]
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps(payload).encode(),
        )

    with httpx.Client(transport=httpx.MockTransport(respond), trust_env=False) as client:
        adapter = HttpSourcePortfolioAdapter(client, resolver=StaticResolver())
        result = adapter.acquire(
            profile,
            observed_at=datetime(2026, 8, 19, 4, 0, tzinfo=UTC),
            backfill_limit=20,
            known_paper_identities=frozenset(),
            known_signal_targets=frozenset(),
        )
        replay = adapter.acquire(
            profile,
            observed_at=datetime(2026, 8, 19, 5, 0, tzinfo=UTC),
            backfill_limit=20,
            cursor_value=result.cursor_value,
            known_paper_identities=frozenset(),
            known_signal_targets=frozenset(),
        )

    assert len(requests) == 2
    assert requests[0].url.params["author"] == "Qwen"
    assert all("resolve" not in request.url.path for request in requests)
    assert len(result.items) == 1
    item = result.items[0]
    assert (item.source_record.external_id, item.source_record.external_version) == (
        "Qwen/Qwen3-8B",
        "a1b2c3d4",
    )
    assert item.source_record.policy_metadata == {
        "gated": False,
        "licence": "apache-2.0",
        "model_files_fetched": False,
        "private": False,
        "verified_owner": "Qwen",
    }
    assert "siblings" not in item.source_record.structured_metadata
    assert replay.items == ()
    assert replay.cursor_value == result.cursor_value


def test_official_feed_metadata_never_creates_evidence_and_plain_text_requires_safe_xml() -> None:
    profiles = {profile.key: profile for profile in load_source_universe()}
    google_payload = b"""<rss><channel><title>Google</title>
      <item><title>AI post</title><link>https://blog.google/innovation-and-ai/technology/ai/example/</link>
      <pubDate>Wed, 19 Aug 2026 02:00:00 GMT</pubDate><description>AI excerpt</description></item>
      <item><title>Phone post</title><link>https://blog.google/products/android/example/</link>
      <pubDate>Wed, 19 Aug 2026 02:00:00 GMT</pubDate></item>
    </channel></rss>"""
    mistral_payload = b"""<rss><channel><title>Mistral</title>
      <item><title>Mistral post</title><link>https://mistral.ai/news/example</link>
      <pubDate>Wed, 19 Aug 2026 02:00:00 GMT</pubDate><description>Feed excerpt</description></item>
    </channel></rss>"""
    openai_payload = b"""<rss><channel><title>OpenAI</title>
      <item><title>OpenAI post</title><link>https://openai.com/index/example/</link>
      <pubDate>Wed, 19 Aug 2026 02:00:00 GMT</pubDate><description>Feed description</description></item>
    </channel></rss>"""

    def response(payload: bytes, content_type: str):
        return lambda request: httpx.Response(
            200,
            headers={"content-type": content_type},
            content=payload,
        )

    with httpx.Client(
        transport=httpx.MockTransport(response(google_payload, "application/rss+xml")),
        trust_env=False,
    ) as client:
        google = HttpSourcePortfolioAdapter(client, resolver=StaticResolver()).acquire(
            profiles["google-ai"],
            observed_at=datetime(2026, 8, 19, 4, 0, tzinfo=UTC),
            backfill_limit=5,
            known_paper_identities=frozenset(),
            known_signal_targets=frozenset(),
        )
    with httpx.Client(
        transport=httpx.MockTransport(response(mistral_payload, "text/plain")),
        trust_env=False,
    ) as client:
        mistral = HttpSourcePortfolioAdapter(client, resolver=StaticResolver()).acquire(
            profiles["mistral-news"],
            observed_at=datetime(2026, 8, 19, 4, 0, tzinfo=UTC),
            backfill_limit=5,
            known_paper_identities=frozenset(),
            known_signal_targets=frozenset(),
        )
    with httpx.Client(
        transport=httpx.MockTransport(response(openai_payload, "application/rss+xml")),
        trust_env=False,
    ) as client:
        openai = HttpSourcePortfolioAdapter(client, resolver=StaticResolver()).acquire(
            profiles["openai-news"],
            observed_at=datetime(2026, 8, 19, 4, 0, tzinfo=UTC),
            backfill_limit=5,
            known_paper_identities=frozenset(),
            known_signal_targets=frozenset(),
        )

    assert [item.candidate.title for item in google.items] == ["AI post"]
    assert [item.candidate.title for item in mistral.items] == ["Mistral post"]
    assert [item.candidate.title for item in openai.items] == ["OpenAI post"]
    assert all(
        item.status is SourceItemStatus.METADATA_ONLY
        and not item.evidence_eligible
        and item.document_version is None
        for item in (*google.items, *mistral.items, *openai.items)
    )

    unsafe = b'<!DOCTYPE rss [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><rss></rss>'
    with httpx.Client(
        transport=httpx.MockTransport(response(unsafe, "text/plain")),
        trust_env=False,
    ) as client, pytest.raises(SourcePortfolioInvalidFormatError):
        HttpSourcePortfolioAdapter(client, resolver=StaticResolver()).acquire(
            profiles["mistral-news"],
            observed_at=datetime(2026, 8, 19, 4, 0, tzinfo=UTC),
            backfill_limit=5,
            known_paper_identities=frozenset(),
            known_signal_targets=frozenset(),
        )


def test_microsoft_metadata_requires_explicit_fail_closed_access_policy() -> None:
    profile = next(
        profile
        for profile in load_source_universe()
        if profile.key == "microsoft-research"
    )
    feed = b"<rss><channel><title>Microsoft Research</title></channel></rss>"

    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "application/rss+xml"},
                content=feed,
            )
        ),
        trust_env=False,
    ) as client, pytest.raises(SourcePortfolioInvalidFormatError):
        HttpSourcePortfolioAdapter(client, resolver=StaticResolver()).acquire(
            replace(profile, settings={"fail_closed_access_probe": False}),
            observed_at=datetime(2026, 8, 19, 4, 0, tzinfo=UTC),
            backfill_limit=5,
            known_paper_identities=frozenset(),
            known_signal_targets=frozenset(),
        )

    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                403,
                headers={"content-type": "application/rss+xml"},
            )
        ),
        trust_env=False,
    ) as client, pytest.raises(SourcePortfolioAccessBlockedError):
        HttpSourcePortfolioAdapter(client, resolver=StaticResolver()).acquire(
            profile,
            observed_at=datetime(2026, 8, 19, 4, 0, tzinfo=UTC),
            backfill_limit=5,
            known_paper_identities=frozenset(),
            known_signal_targets=frozenset(),
        )


def test_daily_papers_preserve_versioned_identity_and_never_fetch_pdf() -> None:
    profile = next(
        profile
        for profile in load_source_universe()
        if profile.key == "hugging-face-daily-papers"
    )
    payload = [
        {
            "paper": {
                "id": "2401.00001",
                "title": "Daily paper",
                "summary": "Official interface abstract.",
                "publishedAt": "2026-08-19T01:00:00Z",
                "authors": [{"name": "A. Researcher"}],
            },
            "publishedAt": "2026-08-19T02:00:00Z",
        }
    ]
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps(payload).encode(),
        )

    with httpx.Client(transport=httpx.MockTransport(respond), trust_env=False) as client:
        result = HttpSourcePortfolioAdapter(client, resolver=StaticResolver()).acquire(
            profile,
            observed_at=datetime(2026, 8, 19, 4, 0, tzinfo=UTC),
            backfill_limit=5,
            known_paper_identities=frozenset(),
            known_signal_targets=frozenset(),
        )

    assert [request.url.path for request in requests] == ["/api/daily_papers"]
    assert all("pdf" not in str(request.url).casefold() for request in requests)
    item = result.items[0]
    assert (item.source_record.external_id, item.source_record.external_version) == (
        "2401.00001",
        "v1",
    )
    assert item.source_record.policy_metadata == {
        "abstract_only": True,
        "pdf_fetched": False,
        "source_interface": "official-hub-papers",
    }
    assert item.document_version is not None
    assert item.document_version.body == "Daily paper\n\nOfficial interface abstract."


def test_gemini_uses_existing_bounded_dated_section_contract() -> None:
    profile = next(
        profile
        for profile in load_source_universe()
        if profile.key == "gemini-api-release-notes"
    )
    payload = Path("tests/fixtures/gemini_api_release_notes.html").read_bytes()
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/html"}, content=payload)

    with httpx.Client(transport=httpx.MockTransport(respond), trust_env=False) as client:
        result = HttpSourcePortfolioAdapter(client, resolver=StaticResolver()).acquire(
            profile,
            observed_at=datetime(2026, 8, 19, 4, 0, tzinfo=UTC),
            backfill_limit=2,
            known_paper_identities=frozenset(),
            known_signal_targets=frozenset(),
        )

    assert [request.url.path for request in requests] == ["/gemini-api/docs/changelog"]
    assert len(result.items) == 2
    assert all(item.status is SourceItemStatus.BODY_VALID for item in result.items)
    assert all(item.document_version is not None for item in result.items)
    assert all("#" in item.candidate.canonical_url for item in result.items)


def test_community_signals_never_create_documents_or_collect_hn_comments() -> None:
    profiles = {profile.key: profile for profile in load_source_universe()}
    trending_html = b"""<html><body>
      <h2><a href="/Example/AI-Tool.GIT">Example / AI-Tool.GIT</a></h2>
    </body></html>"""

    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/html"},
                content=trending_html,
            )
        ),
        trust_env=False,
    ) as client:
        trending = HttpSourcePortfolioAdapter(client, resolver=StaticResolver()).acquire(
            profiles["github-trending"],
            observed_at=datetime(2026, 8, 19, 4, 0, tzinfo=UTC),
            backfill_limit=5,
            known_paper_identities=frozenset(),
            known_signal_targets=frozenset(),
        )

    requests: list[httpx.Request] = []

    def hn_response(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("topstories.json"):
            payload: object = [101, 102]
        elif request.url.path.endswith("beststories.json"):
            payload = [102]
        elif request.url.path.endswith("101.json"):
            payload = {
                "id": 101,
                "type": "story",
                "title": "Duplicate GitHub target",
                "url": "https://github.com/example/ai-tool/issues/1",
                "by": "alice",
                "score": 10,
                "time": 1787104800,
                "kids": [999],
                "text": "forbidden item body",
            }
        elif request.url.path.endswith("102.json"):
            payload = {
                "id": 102,
                "type": "story",
                "title": "External AI story",
                "url": "https://owner.example/ai-story",
                "by": "bob",
                "score": 20,
                "time": 1787104801,
                "kids": [998],
                "text": "forbidden item body",
            }
        else:
            raise AssertionError(request.url)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps(payload).encode(),
        )

    with httpx.Client(transport=httpx.MockTransport(hn_response), trust_env=False) as client:
        hn = HttpSourcePortfolioAdapter(client, resolver=StaticResolver()).acquire(
            profiles["hacker-news"],
            observed_at=datetime(2026, 8, 19, 4, 0, tzinfo=UTC),
            backfill_limit=5,
            known_paper_identities=frozenset(),
            known_signal_targets=frozenset(
                item.candidate.canonical_url for item in trending.items
            ),
        )

    assert [item.candidate.canonical_url for item in trending.items] == [
        "https://github.com/example/ai-tool"
    ]
    assert [item.source_record.external_id for item in hn.items] == ["102"]
    assert all("comment" not in request.url.path for request in requests)
    assert all(not request.url.path.endswith("999.json") for request in requests)
    assert "kids" not in hn.items[0].source_record.structured_metadata
    assert "text" not in hn.items[0].source_record.structured_metadata
    assert hn.items[0].source_record.policy_metadata["owner_resolution"] == (
        "required-before-factual-use"
    )
    assert all(
        item.status is SourceItemStatus.SIGNAL_ONLY
        and item.document_version is None
        and not item.evidence_eligible
        for item in (*trending.items, *hn.items)
    )


def test_hacker_news_bounds_item_request_attempts() -> None:
    profile = next(
        profile for profile in load_source_universe() if profile.key == "hacker-news"
    )
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload: object
        if request.url.path.endswith("topstories.json"):
            payload = list(range(1, 51))
        elif request.url.path.endswith("beststories.json"):
            payload = list(range(1001, 1051))
        else:
            item_id = int(request.url.path.rsplit("/", 1)[-1].removesuffix(".json"))
            payload = {"id": item_id, "type": "comment"}
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps(payload).encode(),
        )

    with httpx.Client(
        transport=httpx.MockTransport(respond),
        trust_env=False,
    ) as client:
        result = HttpSourcePortfolioAdapter(client, resolver=StaticResolver()).acquire(
            profile,
            observed_at=datetime(2026, 8, 19, 4, 0, tzinfo=UTC),
            backfill_limit=20,
            known_paper_identities=frozenset(),
            known_signal_targets=frozenset(),
        )

    item_requests = [request for request in requests if "/item/" in request.url.path]
    assert len(item_requests) == 20
    assert any("/item/1.json" in request.url.path for request in item_requests)
    assert any("/item/1001.json" in request.url.path for request in item_requests)
    assert result.items == ()


@pytest.mark.postgres
def test_collection_persists_all_profile_policy_without_manufacturing_documents(
    m2_portfolio_database_url,
) -> None:
    profiles = load_source_universe()
    portfolio_adapter = EmptyPortfolioAdapter()

    first = collect_source_profiles(
        m2_portfolio_database_url,
        profiles=profiles,
        feed_adapter=EmptyFeedAdapter(),
        article_adapter=NeverArticleAdapter(),
        portfolio_adapter=portfolio_adapter,
        provider=NeverDraftProvider(),
        clock=FixedClock(),
        operation_key="m2-source-universe:empty-fixture",
        backfill_limit=5,
    )
    replay = collect_source_profiles(
        m2_portfolio_database_url,
        profiles=profiles,
        feed_adapter=EmptyFeedAdapter(),
        article_adapter=NeverArticleAdapter(),
        portfolio_adapter=portfolio_adapter,
        provider=NeverDraftProvider(),
        clock=FixedClock(),
        operation_key="m2-source-universe:empty-fixture",
        backfill_limit=5,
    )

    assert len(first.source_results) == 19
    assert first.core_results_persisted == 8
    assert first.core_eligible_contributors == 0
    assert first.core_acceptance_met is False
    assert first.document_versions_created == 0
    assert first.drafts_created == 0
    assert replay.replayed is True
    assert "machine-heart" not in portfolio_adapter.profile_keys

    engine = create_database_engine(m2_portfolio_database_url)
    try:
        snapshots = MultiSourceCollectionRepository(engine).source_statuses(
            {profile.id for profile in profiles}
        )
    finally:
        engine.dispose()
    assert len(snapshots) == 19
    machine = next(snapshot for snapshot in snapshots if snapshot.name == "machine-heart")
    assert machine.pause_state == "authorization-required"
    assert machine.evidence_eligibility == "never"
    google_ai = next(snapshot for snapshot in snapshots if snapshot.name == "google-ai")
    assert google_ai.contribution_role == "Official Metadata"
    assert google_ai.evidence_eligibility == "never"
    status_payload = _source_status_payload(profiles, snapshots)
    google_payload = next(item for item in status_payload if item["key"] == "google-ai")
    assert google_payload["contribution_role"] == "Official Metadata"
    assert google_payload["evidence_eligibility"] == "never"
    machine_payload = next(
        item for item in status_payload if item["key"] == "machine-heart"
    )
    assert machine_payload["pause_state"] == "authorization-required"


@pytest.mark.postgres
def test_policy_valid_record_is_replay_safe_across_collection_runs(
    m2_portfolio_database_url,
) -> None:
    profiles = load_source_universe()
    daily_papers = next(
        profile for profile in profiles if profile.key == "hugging-face-daily-papers"
    )
    canonical_url = "https://huggingface.co/papers/2401.00001"
    body = "Stable paper\n\nA bounded abstract is eligible under the profile policy."
    content_hash = sha256(body.encode()).hexdigest()
    candidate = Candidate(
        id=uuid5(NAMESPACE_URL, f"candidate:{canonical_url}"),
        title="Stable paper",
        canonical_url=canonical_url,
        publisher=daily_papers.publisher,
        discovered_at=FixedClock().now(),
    )
    document = DocumentVersion(
        id=uuid5(NAMESPACE_URL, f"document:{candidate.id}:{content_hash}"),
        candidate_id=candidate.id,
        source_url=canonical_url,
        title=candidate.title,
        body=body,
        content_hash=content_hash,
        observed_at=FixedClock().now(),
        published_at=None,
        published_at_raw=None,
        updated_at=None,
        updated_at_raw=None,
    )
    source_record = SourceSpecificRecord(
        id=uuid5(NAMESPACE_URL, "paper:2401.00001:v1:stable-record"),
        source_definition_id=daily_papers.id,
        candidate_id=candidate.id,
        document_version_id=document.id,
        record_kind="paper",
        external_id="2401.00001",
        external_version="v1",
        canonical_url=canonical_url,
        record_hash=sha256(b"stable-record").hexdigest(),
        provenance={"entry_point": daily_papers.entry_point},
        policy_metadata={"abstract_only": True, "pdf_fetched": False},
        structured_metadata={"identifier": "2401.00001", "version": "v1"},
        evidence_eligible=True,
        observed_at=FixedClock().now(),
    )
    item = SourcePortfolioItemResult(
        source_definition_id=daily_papers.id,
        candidate=candidate,
        status=SourceItemStatus.POLICY_VALID_STRUCTURED,
        evidence_eligible=True,
        eligibility_kind="policy-valid-structured",
        source_record=source_record,
        document_version=document,
    )

    class StablePaperAdapter:
        def acquire(self, profile, **_kwargs):
            return SourceAcquisition(
                items=(item,) if profile.id == daily_papers.id else (),
                cursor_value="paper:2401.00001:v1" if profile.id == daily_papers.id else None,
            )

    first = collect_source_profiles(
        m2_portfolio_database_url,
        profiles=profiles,
        feed_adapter=EmptyFeedAdapter(),
        article_adapter=NeverArticleAdapter(),
        portfolio_adapter=StablePaperAdapter(),
        provider=SkipDraftProvider(),
        clock=FixedClock(),
        operation_key="m2-source-universe:paper-first",
    )
    second = collect_source_profiles(
        m2_portfolio_database_url,
        profiles=profiles,
        feed_adapter=EmptyFeedAdapter(),
        article_adapter=NeverArticleAdapter(),
        portfolio_adapter=StablePaperAdapter(),
        provider=SkipDraftProvider(),
        clock=FixedClock(),
        operation_key="m2-source-universe:paper-second",
    )

    assert first.document_versions_created == 1
    assert second.document_versions_created == 0
    assert first.core_eligible_contributors == 1
    assert second.core_eligible_contributors == 1
    engine = create_database_engine(m2_portfolio_database_url)
    try:
        repository = MultiSourceCollectionRepository(engine)
        assert repository.known_paper_identities() == frozenset(
            {("2401.00001", "v1")}
        )
    finally:
        engine.dispose()


@pytest.mark.postgres
def test_supplemental_adapter_failure_is_isolated(m2_portfolio_database_url) -> None:
    profiles = load_source_universe()

    class OneFailureAdapter:
        def __init__(self) -> None:
            self.profile_keys: list[str] = []

        def acquire(self, profile, **_kwargs):
            self.profile_keys.append(profile.key)
            if profile.key == "arxiv-ai":
                raise SourcePortfolioInvalidFormatError("deterministic schema drift")
            return SourceAcquisition(items=(), cursor_value=None)

    adapter = OneFailureAdapter()
    summary = collect_source_profiles(
        m2_portfolio_database_url,
        profiles=profiles,
        feed_adapter=EmptyFeedAdapter(),
        article_adapter=NeverArticleAdapter(),
        portfolio_adapter=adapter,
        provider=NeverDraftProvider(),
        clock=FixedClock(),
        operation_key="m2-source-universe:isolated-failure",
    )

    assert summary.status.value == "partial"
    assert summary.source_results["arxiv-ai"] == "invalid-format"
    assert summary.source_results["curated-github-releases"] == "empty"
    assert "curated-github-releases" in adapter.profile_keys
    assert "machine-heart" not in adapter.profile_keys
