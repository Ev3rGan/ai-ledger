from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from types import MappingProxyType
from typing import Any
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, UUID, uuid5

from ai_intel_agent.domain import ApprovedFeedSourceDefinition, Topic

SOURCE_UNIVERSE_VERSION = "mvp-v2-1-m2-source-universe-2026-08-19.v3"
SOURCE_UNIVERSE_SET_VERSION = "mvp-v2-1-m2-active-source-universe-2026-08-19.v3"
CORE_PROFILE_KEYS = (
    "gemini-api-release-notes",
    "the-decoder.com",
    "techcrunch.com",
    "hugging-face-blog",
    "qbitai.com",
    "openai-news",
    "github-trending",
    "hugging-face-daily-papers",
)
SUPPLEMENTAL_PROFILE_KEYS = (
    "arxiv-ai",
    "curated-github-releases",
    "qwen-hub",
    "google-ai",
    "google-deepmind",
    "google-research",
    "mistral-news",
    "microsoft-research",
    "hacker-news",
    "simon-willison-ai",
)
EXPECTED_PROFILE_KEYS = (*CORE_PROFILE_KEYS, *SUPPLEMENTAL_PROFILE_KEYS, "machine-heart")
SUPPLEMENTAL_ROLE_COUNTS = {
    "Structured Primary Record": 3,
    "Official Metadata": 5,
    "Community Signal": 1,
    "Analyst Signal": 1,
}
PROFILE_KEYS = {
    "key",
    "host",
    "publisher",
    "entry_point",
    "adapter",
    "enabled",
    "acceptance_group",
    "contribution_role",
    "evidence_eligibility",
    "pause_state",
    "allowed_hosts",
    "allowed_path_prefixes",
    "language",
    "topic_scope",
    "access_scope",
    "discovery_method",
    "cadence",
    "expected_contribution",
    "overlap_rationale",
    "body_eligibility",
    "cursor_policy",
    "health_policy",
    "pause_policy",
    "settings",
}


class SourcePortfolioConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class SourceProfile:
    id: UUID
    profile_version: str
    key: str
    host: str
    publisher: str
    entry_point: str
    adapter: str
    enabled: bool
    acceptance_group: str
    contribution_role: str
    evidence_eligibility: str
    pause_state: str
    allowed_hosts: tuple[str, ...]
    allowed_path_prefixes: tuple[str, ...]
    language: str
    topic_scope: tuple[Topic, ...]
    access_scope: str
    discovery_method: str
    cadence: str
    expected_contribution: str
    overlap_rationale: str
    body_eligibility: str
    cursor_policy: str
    health_policy: str
    pause_policy: str
    settings: Mapping[str, Any]

    @property
    def feed_url(self) -> str:
        return self.entry_point


@dataclass(frozen=True)
class SourcePortfolioDefinition(ApprovedFeedSourceDefinition):
    activation_conclusion: str
    acceptance_group: str
    contribution_role: str
    evidence_eligibility: str
    body_eligibility: str
    pause_state: str
    expected_contribution: str
    overlap_rationale: str


def load_source_universe() -> tuple[SourceProfile, ...]:
    resource = files("ai_intel_agent").joinpath("data/source_profiles.v1.json")
    try:
        payload: Any = json.loads(resource.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SourcePortfolioConfigurationError("Source universe is unreadable") from error
    if not isinstance(payload, dict) or set(payload) != {
        "version",
        "active_set_version",
        "profiles",
    }:
        raise SourcePortfolioConfigurationError("Source universe keys do not match v2")
    if (
        payload["version"] != SOURCE_UNIVERSE_VERSION
        or payload["active_set_version"] != SOURCE_UNIVERSE_SET_VERSION
        or not isinstance(payload["profiles"], list)
    ):
        raise SourcePortfolioConfigurationError("Source universe version is invalid")
    profiles = tuple(_load_profile(raw) for raw in payload["profiles"])
    if tuple(profile.key for profile in profiles) != EXPECTED_PROFILE_KEYS:
        raise SourcePortfolioConfigurationError(
            "Source universe does not match the approved ordered profiles"
        )
    if len({profile.id for profile in profiles}) != len(profiles):
        raise SourcePortfolioConfigurationError("Source Profile identities are not unique")
    _validate_groups(profiles)
    return profiles


def load_legacy_article_profiles() -> tuple[SourceProfile, ...]:
    legacy_keys = {
        "the-decoder.com",
        "techcrunch.com",
        "hugging-face-blog",
        "qbitai.com",
    }
    return tuple(profile for profile in load_source_universe() if profile.key in legacy_keys)


def _load_profile(raw: Any) -> SourceProfile:
    if not isinstance(raw, dict) or set(raw) != PROFILE_KEYS:
        raise SourcePortfolioConfigurationError("Source Profile keys do not match v2")
    for key in ("allowed_hosts", "allowed_path_prefixes", "topic_scope"):
        if not isinstance(raw[key], list) or not all(isinstance(item, str) for item in raw[key]):
            raise SourcePortfolioConfigurationError("Source Profile list value is invalid")
    if not isinstance(raw["settings"], dict) or not isinstance(raw["enabled"], bool):
        raise SourcePortfolioConfigurationError("Source Profile policy value is invalid")
    try:
        key = str(raw["key"])
        profile = SourceProfile(
            id=uuid5(NAMESPACE_URL, f"ai-intel-agent:source-profile:{SOURCE_UNIVERSE_VERSION}:{key}"),
            profile_version=SOURCE_UNIVERSE_VERSION,
            key=key,
            host=str(raw["host"]).casefold(),
            publisher=str(raw["publisher"]),
            entry_point=str(raw["entry_point"]),
            adapter=str(raw["adapter"]),
            enabled=raw["enabled"],
            acceptance_group=str(raw["acceptance_group"]),
            contribution_role=str(raw["contribution_role"]),
            evidence_eligibility=str(raw["evidence_eligibility"]),
            pause_state=str(raw["pause_state"]),
            allowed_hosts=tuple(str(item).casefold() for item in raw["allowed_hosts"]),
            allowed_path_prefixes=tuple(raw["allowed_path_prefixes"]),
            language=str(raw["language"]),
            topic_scope=tuple(Topic(item) for item in raw["topic_scope"]),
            access_scope=str(raw["access_scope"]),
            discovery_method=str(raw["discovery_method"]),
            cadence=str(raw["cadence"]),
            expected_contribution=str(raw["expected_contribution"]),
            overlap_rationale=str(raw["overlap_rationale"]),
            body_eligibility=str(raw["body_eligibility"]),
            cursor_policy=str(raw["cursor_policy"]),
            health_policy=str(raw["health_policy"]),
            pause_policy=str(raw["pause_policy"]),
            settings=_freeze_mapping(raw["settings"]),
        )
    except (TypeError, ValueError) as error:
        raise SourcePortfolioConfigurationError("Source Profile value is invalid") from error
    _validate_profile(profile)
    return profile


def _validate_profile(profile: SourceProfile) -> None:
    required = (
        profile.key,
        profile.host,
        profile.publisher,
        profile.entry_point,
        profile.adapter,
        profile.acceptance_group,
        profile.contribution_role,
        profile.language,
        profile.access_scope,
        profile.discovery_method,
        profile.cadence,
        profile.expected_contribution,
        profile.overlap_rationale,
        profile.body_eligibility,
        profile.cursor_policy,
        profile.health_policy,
        profile.pause_policy,
    )
    if (
        not all(value.strip() for value in required)
        or not profile.topic_scope
        or profile.evidence_eligibility
        not in {"body-valid", "policy-valid-structured", "never"}
        or profile.pause_state not in {"active", "authorization-required"}
    ):
        raise SourcePortfolioConfigurationError("Source Profile policy is incomplete")
    if not profile.enabled:
        if (
            profile.key != "machine-heart"
            or profile.entry_point != "authorization-required"
            or profile.allowed_hosts
            or profile.allowed_path_prefixes
            or profile.pause_state != "authorization-required"
            or profile.evidence_eligibility != "never"
        ):
            raise SourcePortfolioConfigurationError("Disabled profile boundary is invalid")
        return
    parsed = urlparse(profile.entry_point)
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or (parsed.hostname or "").casefold() not in profile.allowed_hosts
        or not profile.allowed_path_prefixes
        or profile.pause_state != "active"
    ):
        raise SourcePortfolioConfigurationError("Active profile is outside its HTTPS scope")


def _validate_groups(profiles: tuple[SourceProfile, ...]) -> None:
    core = tuple(profile for profile in profiles if profile.acceptance_group == "core")
    supplemental = tuple(
        profile for profile in profiles if profile.acceptance_group == "supplemental"
    )
    conditional = tuple(
        profile for profile in profiles if profile.acceptance_group == "conditional"
    )
    if (
        tuple(profile.key for profile in core) != CORE_PROFILE_KEYS
        or tuple(profile.key for profile in supplemental) != SUPPLEMENTAL_PROFILE_KEYS
        or tuple(profile.key for profile in conditional) != ("machine-heart",)
        or not all(profile.enabled for profile in (*core, *supplemental))
        or any(profile.enabled for profile in conditional)
    ):
        raise SourcePortfolioConfigurationError("Source acceptance groups are invalid")
    actual_roles = {
        role: sum(profile.contribution_role == role for profile in supplemental)
        for role in SUPPLEMENTAL_ROLE_COUNTS
    }
    if actual_roles != SUPPLEMENTAL_ROLE_COUNTS:
        raise SourcePortfolioConfigurationError("Supplemental roles are invalid")


def _freeze_mapping(value: dict[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(
        {key: _freeze_policy_value(item) for key, item in value.items()}
    )


def _freeze_policy_value(value: Any) -> Any:
    if isinstance(value, dict):
        return _freeze_mapping(value)
    if isinstance(value, list):
        return tuple(_freeze_policy_value(item) for item in value)
    return value
