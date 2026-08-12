from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from importlib.resources import files
from pathlib import Path
from typing import Any

from ai_intel_agent.domain import Topic


class AuditConclusion(StrEnum):
    APPROVED = "approved"
    METADATA_ONLY = "metadata-only"
    BLOCKED = "blocked"
    NEEDS_VERIFICATION = "needs-verification"


@dataclass(frozen=True)
class SourceDefinitionActivationAudit:
    name: str
    conclusion: AuditConclusion
    conclusion_reason: str
    entry_point: str
    discovery_method: str
    language: str
    topic_scope: tuple[Topic, ...]
    extraction_adapter: str
    cursor: str
    health_policy: str
    robots_url: str
    robots_findings: str
    terms_url: str
    terms_findings: str
    storage_policy: str
    public_excerpt_policy: str
    public_excerpt_max_characters: int
    pause_conditions: tuple[str, ...]


@dataclass(frozen=True)
class FirstWaveSourceDefinitionAudit:
    version: str
    checked_on: date
    source_definitions: tuple[SourceDefinitionActivationAudit, ...]


def run_source_definition_activation_audit(
    output_path: Path,
) -> FirstWaveSourceDefinitionAudit:
    audit = _load_first_wave_audit()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(_render_markdown(audit), encoding="utf-8")
    return audit


def _load_first_wave_audit() -> FirstWaveSourceDefinitionAudit:
    resource = files("ai_intel_agent").joinpath("data/first_wave_source_audit.json")
    payload = json.loads(resource.read_text(encoding="utf-8"))
    source_definitions = tuple(
        _parse_source_definition(item) for item in payload["source_definitions"]
    )
    if len(source_definitions) != 16:
        raise ValueError(
            f"first-wave audit must contain 16 Source Definitions, found {len(source_definitions)}"
        )
    if len({definition.name for definition in source_definitions}) != len(source_definitions):
        raise ValueError("first-wave audit contains duplicate Source Definition names")
    return FirstWaveSourceDefinitionAudit(
        version=_required_text(payload, "version"),
        checked_on=date.fromisoformat(_required_text(payload, "checked_on")),
        source_definitions=source_definitions,
    )


def _parse_source_definition(item: dict[str, Any]) -> SourceDefinitionActivationAudit:
    topics = tuple(Topic(topic) for topic in _required_text_list(item, "topic_scope"))
    pause_conditions = _required_text_list(item, "pause_conditions")
    public_excerpt_max_characters = item.get("public_excerpt_max_characters")
    if not isinstance(public_excerpt_max_characters, int) or not (
        0 <= public_excerpt_max_characters <= 1000
    ):
        raise ValueError(
            "Source Definition audit field 'public_excerpt_max_characters' must be an integer "
            "between 0 and 1000"
        )
    return SourceDefinitionActivationAudit(
        name=_required_text(item, "name"),
        conclusion=AuditConclusion(_required_text(item, "conclusion")),
        conclusion_reason=_required_text(item, "conclusion_reason"),
        entry_point=_required_text(item, "entry_point"),
        discovery_method=_required_text(item, "discovery_method"),
        language=_required_text(item, "language"),
        topic_scope=topics,
        extraction_adapter=_required_text(item, "extraction_adapter"),
        cursor=_required_text(item, "cursor"),
        health_policy=_required_text(item, "health_policy"),
        robots_url=_required_text(item, "robots_url"),
        robots_findings=_required_text(item, "robots_findings"),
        terms_url=_required_text(item, "terms_url"),
        terms_findings=_required_text(item, "terms_findings"),
        storage_policy=_required_text(item, "storage_policy"),
        public_excerpt_policy=_required_text(item, "public_excerpt_policy"),
        public_excerpt_max_characters=public_excerpt_max_characters,
        pause_conditions=pause_conditions,
    )


def _required_text(item: dict[str, Any], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Source Definition audit field {key!r} must be non-empty text")
    return value.strip()


def _required_text_list(item: dict[str, Any], key: str) -> tuple[str, ...]:
    value = item.get(key)
    if not isinstance(value, list) or not value:
        raise ValueError(f"Source Definition audit field {key!r} must be a non-empty list")
    if not all(isinstance(entry, str) and entry.strip() for entry in value):
        raise ValueError(f"Source Definition audit field {key!r} must contain non-empty text")
    return tuple(entry.strip() for entry in value)


def _render_markdown(audit: FirstWaveSourceDefinitionAudit) -> str:
    counts = Counter(definition.conclusion for definition in audit.source_definitions)
    summary = ", ".join(
        f"{conclusion.value}={counts[conclusion]}" for conclusion in AuditConclusion
    )
    lines = [
        "# First-wave Source Definition Activation Audit",
        "",
        f"- Audit version: `{audit.version}`",
        f"- Findings checked on: `{audit.checked_on.isoformat()}`",
        f"- Source Definitions: {len(audit.source_definitions)}",
        f"- Conclusions: {summary}",
        (
            "- Scope note: This is a policy and metadata activation audit. It does not fetch or "
            "store source content, test Hong Kong reachability, or prove parser behavior."
        ),
    ]
    for definition in audit.source_definitions:
        excerpt_limit = (
            "disabled"
            if definition.public_excerpt_max_characters == 0
            else f"{definition.public_excerpt_max_characters} Unicode characters"
        )
        lines.extend(
            [
                "",
                f"## {definition.name}",
                "",
                (f"- Conclusion: `{definition.conclusion.value}` — {definition.conclusion_reason}"),
                f"- Entry point: {definition.entry_point}",
                f"- Discovery method: {definition.discovery_method}",
                f"- Language: {definition.language}",
                f"- Topic scope: {', '.join(definition.topic_scope)}",
                f"- Extraction adapter: {definition.extraction_adapter}",
                f"- Cursor: {definition.cursor}",
                f"- Health policy: {definition.health_policy}",
                (f"- Robots findings: {definition.robots_findings} ({definition.robots_url})"),
                (f"- Terms findings: {definition.terms_findings} ({definition.terms_url})"),
                f"- Storage policy: {definition.storage_policy}",
                (
                    f"- Public excerpt policy: {definition.public_excerpt_policy} "
                    f"(maximum source excerpt: {excerpt_limit})"
                ),
                f"- Pause conditions: {'; '.join(definition.pause_conditions)}",
            ]
        )
    return "\n".join(lines) + "\n"
