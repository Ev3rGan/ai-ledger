from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from time import monotonic as system_monotonic
from time import sleep
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx

from ai_intel_agent.domain import Candidate, DocumentVersion
from ai_intel_agent.feed_acquisition import (
    BoundedPublicHttpsError,
    BoundedPublicHttpsFetcher,
    BoundedPublicHttpsSecurityError,
    HostResolver,
    parse_feed,
)
from ai_intel_agent.gemini_collection import GeminiSourceError, parse_gemini_release_notes
from ai_intel_agent.source_portfolio import SourceProfile

XML_MIME_TYPES = frozenset(
    {
        "application/atom+xml",
        "application/rdf+xml",
        "application/rss+xml",
        "application/xml",
        "text/xml",
    }
)
ARXIV_IDENTIFIER = re.compile(
    r"^/abs/(?P<identifier>(?:\d{4}\.\d{4,5}|[a-z-]+/\d{7}))(?P<version>v\d+)$",
    re.IGNORECASE,
)
APPROVED_RELEASE_REPOSITORIES = (
    "vllm-project/vllm",
    "sgl-project/sglang",
    "huggingface/transformers",
    "pytorch/pytorch",
)


class SourceItemStatus(StrEnum):
    BODY_VALID = "body-valid"
    POLICY_VALID_STRUCTURED = "policy-valid-structured"
    METADATA_ONLY = "metadata-only"
    SIGNAL_ONLY = "signal-only"
    INVALID_FORMAT = "invalid-format"
    ACCESS_BLOCKED = "access-blocked"
    TEMPORARY_FAILURE = "temporary-failure"


class SourcePortfolioAcquisitionError(ValueError):
    code = "temporary-failure"


class SourcePortfolioInvalidFormatError(SourcePortfolioAcquisitionError):
    code = "invalid-format"


class SourcePortfolioAccessBlockedError(SourcePortfolioAcquisitionError):
    code = "access-blocked"


class SourcePortfolioTemporaryFailureError(SourcePortfolioAcquisitionError):
    code = "temporary-failure"


@dataclass(frozen=True)
class SourceSpecificRecord:
    id: UUID
    source_definition_id: UUID
    candidate_id: UUID
    record_kind: str
    external_id: str
    external_version: str
    canonical_url: str
    record_hash: str
    provenance: Mapping[str, Any]
    policy_metadata: Mapping[str, Any]
    structured_metadata: Mapping[str, Any]
    evidence_eligible: bool
    observed_at: datetime
    document_version_id: UUID | None = None

    def __post_init__(self) -> None:
        if (
            self.source_definition_id.int == 0
            or self.candidate_id.int == 0
            or not self.record_kind.strip()
            or not self.external_id.strip()
            or not self.external_version.strip()
            or not self.canonical_url.startswith("https://")
            or not re.fullmatch(r"[0-9a-f]{64}", self.record_hash)
            or self.observed_at.tzinfo is None
            or self.evidence_eligible != (self.document_version_id is not None)
        ):
            raise ValueError("Source-specific record policy is invalid")


@dataclass(frozen=True)
class SourcePortfolioItemResult:
    source_definition_id: UUID
    candidate: Candidate
    status: SourceItemStatus
    evidence_eligible: bool
    eligibility_kind: str
    source_record: SourceSpecificRecord
    document_version: DocumentVersion | None = None
    error_code: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        eligible_statuses = {
            SourceItemStatus.BODY_VALID,
            SourceItemStatus.POLICY_VALID_STRUCTURED,
        }
        non_evidence_statuses = {
            SourceItemStatus.METADATA_ONLY,
            SourceItemStatus.SIGNAL_ONLY,
        }
        failure_statuses = {
            SourceItemStatus.INVALID_FORMAT,
            SourceItemStatus.ACCESS_BLOCKED,
            SourceItemStatus.TEMPORARY_FAILURE,
        }
        eligible = self.status in eligible_statuses
        errors_present = self.error_code is not None and self.error_message is not None
        if (
            self.source_definition_id != self.source_record.source_definition_id
            or self.candidate.id != self.source_record.candidate_id
            or self.candidate.canonical_url != self.source_record.canonical_url
            or (self.document_version is None)
            != (self.source_record.document_version_id is None)
            or (
                self.document_version is not None
                and self.document_version.id
                != self.source_record.document_version_id
            )
            or self.evidence_eligible != self.source_record.evidence_eligible
            or eligible != self.evidence_eligible
            or (eligible and self.document_version is None)
            or (eligible and self.eligibility_kind != self.status.value)
            or (eligible and (self.error_code is not None or self.error_message is not None))
            or (
                self.status in non_evidence_statuses
                and (
                    self.document_version is not None
                    or self.eligibility_kind != "ineligible"
                    or self.error_code is not None
                    or self.error_message is not None
                )
            )
            or (
                self.status in failure_statuses
                and (
                    self.document_version is not None
                    or self.eligibility_kind != "ineligible"
                    or not errors_present
                )
            )
        ):
            raise ValueError("Source item Evidence policy is invalid")


@dataclass(frozen=True)
class SourceAcquisition:
    items: tuple[SourcePortfolioItemResult, ...]
    cursor_value: str | None


class HttpSourcePortfolioAdapter:
    """Acquire one approved non-article Source Profile through a fixed adapter."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        resolver: HostResolver | None = None,
        timeout_seconds: float = 15.0,
        max_response_bytes: int = 2_000_000,
        max_redirects: int = 3,
        monotonic: Callable[[], float] = system_monotonic,
        wait: Callable[[float], object] = sleep,
    ) -> None:
        self._fetcher = BoundedPublicHttpsFetcher(
            client,
            resolver=resolver,
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
            max_redirects=max_redirects,
        )
        self._monotonic = monotonic
        self._wait = wait
        self._last_arxiv_request_at: float | None = None

    def acquire(
        self,
        profile: SourceProfile,
        *,
        observed_at: datetime,
        backfill_limit: int,
        cursor_value: str | None = None,
        known_paper_identities: frozenset[tuple[str, str]],
        known_signal_targets: frozenset[str],
    ) -> SourceAcquisition:
        if observed_at.tzinfo is None:
            raise ValueError("Source acquisition time must be timezone-aware")
        if not 1 <= backfill_limit <= 100:
            raise ValueError("Source acquisition limit must be between 1 and 100")
        if profile.adapter == "arxiv":
            acquisition = self._acquire_arxiv(
                profile,
                observed_at=observed_at,
                backfill_limit=backfill_limit,
                known_paper_identities=known_paper_identities,
            )
        elif profile.adapter == "github-releases":
            acquisition = self._acquire_github_releases(
                profile,
                observed_at=observed_at,
                backfill_limit=backfill_limit,
            )
        elif profile.adapter == "qwen-hub":
            acquisition = self._acquire_qwen(
                profile,
                observed_at=observed_at,
                backfill_limit=backfill_limit,
            )
        elif profile.adapter in {
            "official-metadata-feed",
            "mistral-metadata-feed",
            "microsoft-research-feed",
            "analyst-feed",
        }:
            acquisition = self._acquire_metadata_feed(
                profile,
                observed_at=observed_at,
                backfill_limit=backfill_limit,
            )
        elif profile.adapter == "hugging-face-daily-papers":
            acquisition = self._acquire_daily_papers(
                profile,
                observed_at=observed_at,
                backfill_limit=backfill_limit,
            )
        elif profile.adapter == "gemini-release-notes":
            acquisition = self._acquire_gemini(
                profile,
                observed_at=observed_at,
                backfill_limit=backfill_limit,
            )
        elif profile.adapter == "github-trending":
            acquisition = self._acquire_github_trending(
                profile,
                observed_at=observed_at,
                backfill_limit=backfill_limit,
                known_signal_targets=known_signal_targets,
            )
        elif profile.adapter == "hacker-news":
            acquisition = self._acquire_hacker_news(
                profile,
                observed_at=observed_at,
                backfill_limit=backfill_limit,
                known_signal_targets=known_signal_targets,
            )
        else:
            raise SourcePortfolioInvalidFormatError(
                f"Source Profile {profile.key} has no concrete adapter"
            )
        return _apply_incremental_cursor(acquisition, cursor_value=cursor_value)

    def _acquire_arxiv(
        self,
        profile: SourceProfile,
        *,
        observed_at: datetime,
        backfill_limit: int,
        known_paper_identities: frozenset[tuple[str, str]],
    ) -> SourceAcquisition:
        categories = _string_list_setting(profile, "categories")
        keywords = _string_list_setting(profile, "keywords")
        query_version = _string_setting(profile, "query_version")
        maximum = min(backfill_limit, _positive_integer_setting(profile, "maximum_items"))
        interval = float(_positive_integer_setting(profile, "minimum_request_interval_seconds"))
        category_query = " OR ".join(f"cat:{category}" for category in categories)
        keyword_query = " OR ".join(f'all:"{keyword}"' for keyword in keywords)
        query = f"({category_query}) AND ({keyword_query})"
        location = f"{profile.entry_point}?{urlencode({'search_query': query, 'start': 0, 'max_results': maximum, 'sortBy': 'lastUpdatedDate', 'sortOrder': 'descending'})}"
        self._throttle_arxiv(interval)
        payload = self._fetch(
            location,
            allowed_mime_types=XML_MIME_TYPES,
            user_agent="ai-intel-agent/0.1 arxiv-metadata-collector",
        )
        try:
            entries = parse_feed(payload)
        except ValueError as error:
            raise SourcePortfolioInvalidFormatError("arXiv Atom response is invalid") from error
        items: list[SourcePortfolioItemResult] = []
        for entry in entries[:maximum]:
            canonical_url = _canonical_https_url(entry.canonical_url)
            _validate_profile_url(profile, canonical_url)
            match = ARXIV_IDENTIFIER.fullmatch(urlparse(canonical_url).path)
            if match is None:
                raise SourcePortfolioInvalidFormatError(
                    "arXiv entry has no explicit identifier version"
                )
            identity = (match.group("identifier"), match.group("version").casefold())
            if identity in known_paper_identities:
                continue
            title = " ".join(entry.title.split())
            abstract = " ".join(entry.summary.split())
            if not title or not abstract:
                raise SourcePortfolioInvalidFormatError(
                    "arXiv entry is missing title or abstract"
                )
            items.append(
                _structured_item(
                    profile,
                    record_kind="paper",
                    external_id=identity[0],
                    external_version=identity[1],
                    canonical_url=canonical_url,
                    title=title,
                    body=f"{title}\n\n{abstract}",
                    structured_metadata={
                        "identifier": identity[0],
                        "version": identity[1],
                        "title": title,
                        "abstract": abstract,
                        "updated_at": _isoformat(entry.updated_at),
                    },
                    provenance={"entry_point": profile.entry_point},
                    policy_metadata={
                        "abstract_only": True,
                        "pdf_fetched": False,
                        "query_version": query_version,
                    },
                    observed_at=observed_at,
                    published_at=entry.published_at,
                    published_at_raw=entry.published_at_raw,
                    updated_at=entry.updated_at,
                    updated_at_raw=entry.updated_at_raw,
                )
            )
        return SourceAcquisition(items=tuple(items), cursor_value=_record_cursor(items))

    def _acquire_github_trending(
        self,
        profile: SourceProfile,
        *,
        observed_at: datetime,
        backfill_limit: int,
        known_signal_targets: frozenset[str],
    ) -> SourceAcquisition:
        payload = self._fetch(
            profile.entry_point,
            allowed_mime_types=frozenset({"text/html"}),
            user_agent="ai-intel-agent/0.1 github-trending-signal",
        )
        html = payload.decode("utf-8", errors="replace")
        repositories: list[str] = []
        for owner, repository in re.findall(
            r'href=["\']/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)["\']', html
        ):
            target = _github_repository_target(
                f"https://github.com/{owner}/{repository}"
            )
            if target is None:
                continue
            if target not in repositories and target not in known_signal_targets:
                repositories.append(target)
        maximum = min(backfill_limit, _positive_integer_setting(profile, "maximum_items"))
        items = [
            _non_evidence_item(
                profile,
                status=SourceItemStatus.SIGNAL_ONLY,
                record_kind="community-signal",
                external_id=urlparse(target).path.strip("/"),
                external_version=observed_at.astimezone(UTC).date().isoformat(),
                canonical_url=target,
                title=urlparse(target).path.strip("/"),
                structured_metadata={"repository": urlparse(target).path.strip("/")},
                provenance={"entry_point": profile.entry_point},
                policy_metadata={
                    "evidence_eligible": False,
                    "popularity_is_not_evidence": True,
                    "repository_body_fetched": False,
                },
                observed_at=observed_at,
            )
            for target in repositories[:maximum]
        ]
        return SourceAcquisition(items=tuple(items), cursor_value=_record_cursor(items))

    def _acquire_hacker_news(
        self,
        profile: SourceProfile,
        *,
        observed_at: datetime,
        backfill_limit: int,
        known_signal_targets: frozenset[str],
    ) -> SourceAcquisition:
        ranking_sets = _string_list_setting(profile, "ranking_sets")
        if ranking_sets != ("topstories", "beststories"):
            raise SourcePortfolioInvalidFormatError("HN ranking sets are invalid")
        maximum = min(backfill_limit, _positive_integer_setting(profile, "maximum_items"))
        ranked_ids: list[list[int]] = []
        for ranking_set in ranking_sets:
            payload = self._fetch_json(
                f"{profile.entry_point}/{ranking_set}.json",
                user_agent="ai-intel-agent/0.1 hacker-news-signal",
            )
            if not isinstance(payload, list) or not all(
                isinstance(item, int) and not isinstance(item, bool) for item in payload
            ):
                raise SourcePortfolioInvalidFormatError("HN ranking response is invalid")
            ranked_ids.append(payload)
        item_ids: list[int] = []
        for position in range(max(len(items) for items in ranked_ids)):
            for ranking in ranked_ids:
                if position < len(ranking) and ranking[position] not in item_ids:
                    item_ids.append(ranking[position])
                    if len(item_ids) >= maximum:
                        break
            if len(item_ids) >= maximum:
                break
        items: list[SourcePortfolioItemResult] = []
        for item_id in item_ids:
            raw = self._fetch_json(
                f"{profile.entry_point}/item/{item_id}.json",
                user_agent="ai-intel-agent/0.1 hacker-news-signal",
            )
            if not isinstance(raw, dict):
                raise SourcePortfolioInvalidFormatError("HN item response is invalid")
            if raw.get("type") != "story" or raw.get("deleted") is True or raw.get("dead") is True:
                continue
            title = raw.get("title")
            owner_url = raw.get("url")
            if not isinstance(title, str) or not title.strip() or not isinstance(owner_url, str):
                continue
            owner = urlparse(owner_url)
            if (
                owner.scheme not in {"http", "https"}
                or owner.username
                or owner.password
                or not owner.hostname
            ):
                continue
            github_target = _github_repository_target(owner_url)
            if github_target is not None and github_target in known_signal_targets:
                continue
            canonical_url = f"https://news.ycombinator.com/item?id={item_id}"
            _validate_profile_url(profile, canonical_url)
            items.append(
                _non_evidence_item(
                    profile,
                    status=SourceItemStatus.SIGNAL_ONLY,
                    record_kind="community-signal",
                    external_id=str(item_id),
                    external_version=str(raw.get("time", "undated")),
                    canonical_url=canonical_url,
                    title=title.strip(),
                    structured_metadata={
                        "item_id": item_id,
                        "title": title.strip(),
                        "owner_url": owner_url,
                        "by": raw.get("by"),
                        "score": raw.get("score"),
                        "time": raw.get("time"),
                    },
                    provenance={"entry_point": profile.entry_point},
                    policy_metadata={
                        "comments_fetched": False,
                        "evidence_eligible": False,
                        "owner_resolution": "required-before-factual-use",
                    },
                    observed_at=observed_at,
                )
            )
            if len(items) >= maximum:
                break
        return SourceAcquisition(items=tuple(items), cursor_value=_record_cursor(items))

    def _acquire_gemini(
        self,
        profile: SourceProfile,
        *,
        observed_at: datetime,
        backfill_limit: int,
    ) -> SourceAcquisition:
        payload = self._fetch(
            profile.entry_point,
            allowed_mime_types=frozenset({"text/html"}),
            user_agent="ai-intel-agent/0.1 Gemini-release-notes-collector",
        )
        try:
            sections = parse_gemini_release_notes(payload)
        except GeminiSourceError as error:
            raise SourcePortfolioInvalidFormatError(
                "Gemini dated-section response is invalid"
            ) from error
        items = [
            _structured_item(
                profile,
                record_kind="release-notes-section",
                external_id=section.published_date.isoformat(),
                external_version=section.anchor,
                canonical_url=f"{profile.entry_point}#{section.anchor}",
                title=f"Gemini API Release Notes — {section.heading}",
                body=section.body,
                structured_metadata={
                    "heading": section.heading,
                    "anchor": section.anchor,
                    "published_date": section.published_date.isoformat(),
                },
                provenance={"entry_point": profile.entry_point},
                policy_metadata={"existing_audited_body_policy": True},
                observed_at=observed_at,
                status=SourceItemStatus.BODY_VALID,
                eligibility_kind="body-valid",
            )
            for section in sections[-backfill_limit:]
        ]
        return SourceAcquisition(items=tuple(items), cursor_value=_record_cursor(items))

    def _acquire_daily_papers(
        self,
        profile: SourceProfile,
        *,
        observed_at: datetime,
        backfill_limit: int,
    ) -> SourceAcquisition:
        payload = self._fetch_json(
            profile.entry_point,
            user_agent="ai-intel-agent/0.1 daily-papers-collector",
        )
        if not isinstance(payload, list):
            raise SourcePortfolioInvalidFormatError("Daily Papers response is not a list")
        maximum = min(backfill_limit, _positive_integer_setting(profile, "maximum_items"))
        items: list[SourcePortfolioItemResult] = []
        for raw in payload[:maximum]:
            paper = raw.get("paper") if isinstance(raw, dict) else None
            if not isinstance(paper, dict):
                raise SourcePortfolioInvalidFormatError("Daily Papers record is invalid")
            raw_identifier = paper.get("id")
            title = paper.get("title")
            abstract = paper.get("summary")
            if not all(isinstance(value, str) and value.strip() for value in (raw_identifier, title, abstract)):
                raise SourcePortfolioInvalidFormatError(
                    "Daily Papers record lacks identifier, title, or abstract"
                )
            match = re.fullmatch(
                r"(?P<identifier>\d{4}\.\d{4,5})(?P<version>v\d+)?",
                raw_identifier,
                re.IGNORECASE,
            )
            if match is None:
                raise SourcePortfolioInvalidFormatError("Daily Papers identifier is invalid")
            identifier = match.group("identifier")
            version = (match.group("version") or "v1").casefold()
            canonical_url = f"https://huggingface.co/papers/{identifier}"
            _validate_profile_url(profile, canonical_url)
            published = _parse_optional_datetime(
                paper.get("publishedAt") or raw.get("publishedAt")
            )
            author_names = [
                author["name"]
                for author in paper.get("authors", [])
                if isinstance(author, dict) and isinstance(author.get("name"), str)
            ]
            normalized_title = " ".join(title.split())
            normalized_abstract = " ".join(abstract.split())
            items.append(
                _structured_item(
                    profile,
                    record_kind="paper",
                    external_id=identifier,
                    external_version=version,
                    canonical_url=canonical_url,
                    title=normalized_title,
                    body=f"{normalized_title}\n\n{normalized_abstract}",
                    structured_metadata={
                        "identifier": identifier,
                        "version": version,
                        "title": normalized_title,
                        "abstract": normalized_abstract,
                        "authors": author_names,
                        "published_at": _isoformat(published),
                    },
                    provenance={"entry_point": profile.entry_point},
                    policy_metadata={
                        "abstract_only": True,
                        "pdf_fetched": False,
                        "source_interface": "official-hub-papers",
                    },
                    observed_at=observed_at,
                    published_at=published,
                    published_at_raw=(
                        paper.get("publishedAt")
                        if isinstance(paper.get("publishedAt"), str)
                        else None
                    ),
                )
            )
        return SourceAcquisition(items=tuple(items), cursor_value=_record_cursor(items))

    def _acquire_metadata_feed(
        self,
        profile: SourceProfile,
        *,
        observed_at: datetime,
        backfill_limit: int,
    ) -> SourceAcquisition:
        allowed_mime_types = XML_MIME_TYPES
        if (
            profile.adapter == "microsoft-research-feed"
            and profile.settings.get("fail_closed_access_probe") is not True
        ):
            raise SourcePortfolioInvalidFormatError(
                "Microsoft Research fail-closed access policy is not explicit"
            )
        if profile.adapter == "mistral-metadata-feed":
            if profile.settings.get("allow_text_plain_xml") is not True:
                raise SourcePortfolioInvalidFormatError(
                    "Mistral text/plain XML policy is not explicit"
                )
            allowed_mime_types = frozenset({*XML_MIME_TYPES, "text/plain"})
        payload = self._fetch(
            profile.entry_point,
            allowed_mime_types=allowed_mime_types,
            user_agent="ai-intel-agent/0.1 official-metadata-collector",
        )
        try:
            entries = parse_feed(payload)
        except ValueError as error:
            raise SourcePortfolioInvalidFormatError("Metadata Feed XML is invalid") from error
        canonical_prefix = profile.settings.get("canonical_path_prefix")
        items: list[SourcePortfolioItemResult] = []
        for entry in entries:
            canonical_url = _canonical_https_url(entry.canonical_url)
            if (
                isinstance(canonical_prefix, str)
                and not urlparse(canonical_url).path.startswith(canonical_prefix)
            ):
                continue
            _validate_profile_url(profile, canonical_url)
            timestamp = entry.updated_at or entry.published_at
            status = (
                SourceItemStatus.SIGNAL_ONLY
                if profile.adapter == "analyst-feed"
                else SourceItemStatus.METADATA_ONLY
            )
            record_kind = (
                "analyst-signal"
                if profile.adapter == "analyst-feed"
                else "official-metadata"
            )
            items.append(
                _non_evidence_item(
                    profile,
                    status=status,
                    record_kind=record_kind,
                    external_id=canonical_url,
                    external_version=_isoformat(timestamp) or "undated",
                    canonical_url=canonical_url,
                    title=entry.title,
                    structured_metadata={
                        "title": entry.title,
                        "feed_excerpt": entry.summary,
                        "published_at": _isoformat(entry.published_at),
                        "updated_at": _isoformat(entry.updated_at),
                    },
                    provenance={"entry_point": profile.entry_point},
                    policy_metadata={
                        "body_fetched": False,
                        "evidence_eligible": False,
                        "linked_first_party_evidence_required": (
                            profile.adapter == "analyst-feed"
                        ),
                    },
                    observed_at=observed_at,
                )
            )
            if len(items) >= backfill_limit:
                break
        return SourceAcquisition(items=tuple(items), cursor_value=_record_cursor(items))

    def _acquire_qwen(
        self,
        profile: SourceProfile,
        *,
        observed_at: datetime,
        backfill_limit: int,
    ) -> SourceAcquisition:
        owner = _string_setting(profile, "verified_owner")
        rejected = {
            *(_string_list_setting(profile, "rejected_variant_tokens")),
            "adapter",
            "derivative",
            "finetune",
            "lora",
            "merge",
        }
        maximum = min(backfill_limit, _positive_integer_setting(profile, "maximum_items"))
        payload = self._fetch_json(
            profile.entry_point,
            user_agent="ai-intel-agent/0.1 qwen-metadata-collector",
        )
        if not isinstance(payload, list):
            raise SourcePortfolioInvalidFormatError("Qwen model response is not a list")
        items: list[SourcePortfolioItemResult] = []
        for raw in payload:
            if not isinstance(raw, dict):
                raise SourcePortfolioInvalidFormatError("Qwen model record is invalid")
            model_id = raw.get("id") or raw.get("modelId")
            author = raw.get("author")
            revision = raw.get("sha")
            if (
                not isinstance(model_id, str)
                or not isinstance(author, str)
                or author != owner
                or not model_id.startswith(f"{owner}/")
            ):
                continue
            normalized_id = model_id.casefold()
            if any(
                re.search(rf"(?:^|[-_/]){re.escape(token.casefold())}(?:$|[-_/])", normalized_id)
                for token in rejected
            ):
                continue
            if not isinstance(revision, str) or not revision.strip():
                raise SourcePortfolioInvalidFormatError("Qwen model revision is invalid")
            card_data = raw.get("cardData")
            licence = card_data.get("license") if isinstance(card_data, dict) else None
            if not isinstance(licence, str) or not licence.strip():
                tags = raw.get("tags")
                if isinstance(tags, list):
                    licence_tag = next(
                        (
                            tag
                            for tag in tags
                            if isinstance(tag, str) and tag.startswith("license:")
                        ),
                        None,
                    )
                    licence = licence_tag.partition(":")[2] if licence_tag else None
            gated = raw.get("gated")
            private = raw.get("private")
            if (
                not isinstance(licence, str)
                or not licence.strip()
                or not isinstance(gated, bool)
                or not isinstance(private, bool)
            ):
                raise SourcePortfolioInvalidFormatError(
                    "Qwen model lacks licence or gated-state metadata"
                )
            canonical_url = f"https://huggingface.co/{model_id}"
            _validate_profile_url(profile, canonical_url)
            last_modified = _parse_optional_datetime(raw.get("lastModified"))
            metadata = {
                "model_id": model_id,
                "owner": author,
                "revision": revision,
                "last_modified": _isoformat(last_modified),
                "pipeline_tag": raw.get("pipeline_tag"),
            }
            body = "\n".join(
                (
                    model_id,
                    "",
                    f"Owner: {author}",
                    f"Revision: {revision}",
                    f"Licence: {licence}",
                    f"Gated: {str(gated).lower()}",
                )
            )
            items.append(
                _structured_item(
                    profile,
                    record_kind="model-release",
                    external_id=model_id,
                    external_version=revision,
                    canonical_url=canonical_url,
                    title=model_id,
                    body=body,
                    structured_metadata=metadata,
                    provenance={"entry_point": profile.entry_point},
                    policy_metadata={
                        "gated": gated,
                        "licence": licence,
                        "model_files_fetched": False,
                        "private": private,
                        "verified_owner": owner,
                    },
                    observed_at=observed_at,
                    updated_at=last_modified,
                    updated_at_raw=(
                        raw.get("lastModified")
                        if isinstance(raw.get("lastModified"), str)
                        else None
                    ),
                )
            )
            if len(items) >= maximum:
                break
        return SourceAcquisition(items=tuple(items), cursor_value=_record_cursor(items))

    def _acquire_github_releases(
        self,
        profile: SourceProfile,
        *,
        observed_at: datetime,
        backfill_limit: int,
    ) -> SourceAcquisition:
        policies = _release_policies(profile)
        if tuple(policy.name for policy in policies) != APPROVED_RELEASE_REPOSITORIES:
            raise SourcePortfolioInvalidFormatError("Release allowlist is invalid")
        maximum = min(
            backfill_limit,
            _positive_integer_setting(profile, "maximum_items_per_repository"),
        )
        items: list[SourcePortfolioItemResult] = []
        for policy in policies:
            repository = policy.name
            location = profile.entry_point.format(repository=repository)
            payload = self._fetch_json(
                location,
                user_agent="ai-intel-agent/0.1 curated-release-collector",
            )
            if not isinstance(payload, list):
                raise SourcePortfolioInvalidFormatError("Release response is not a list")
            accepted = 0
            for raw in payload:
                if not isinstance(raw, dict):
                    raise SourcePortfolioInvalidFormatError("Release record is invalid")
                if raw.get("draft") is True or raw.get("prerelease") is True:
                    continue
                if not isinstance(raw.get("draft"), bool) or not isinstance(
                    raw.get("prerelease"), bool
                ):
                    raise SourcePortfolioInvalidFormatError("Release state is invalid")
                release_id = raw.get("id")
                tag = raw.get("tag_name")
                html_url = raw.get("html_url")
                if (
                    not isinstance(release_id, int)
                    or isinstance(release_id, bool)
                    or not isinstance(tag, str)
                    or not tag.strip()
                    or not isinstance(html_url, str)
                ):
                    raise SourcePortfolioInvalidFormatError("Release identity is invalid")
                if any(
                    pattern.search(tag) is not None
                    for pattern in policy.exclude_tag_patterns
                ):
                    continue
                canonical_url = _canonical_https_url(html_url)
                _validate_profile_url(profile, canonical_url)
                if not urlparse(canonical_url).path.startswith(
                    f"/{repository}/releases/"
                ):
                    raise SourcePortfolioInvalidFormatError(
                        "Release URL is outside its repository"
                    )
                name = raw.get("name")
                title = (
                    name.strip()
                    if isinstance(name, str) and name.strip()
                    else f"{repository} {tag.strip()}"
                )
                raw_body = raw.get("body")
                notes = "\n".join(
                    line.rstrip()
                    for line in (raw_body if isinstance(raw_body, str) else "").splitlines()
                    if line.strip()
                )
                body_eligible = policy.release_body_eligible
                published = _parse_optional_datetime(raw.get("published_at"))
                document_lines = [title, "", f"Repository: {repository}", f"Tag: {tag}"]
                if body_eligible and notes:
                    document_lines.extend(("", "Release notes:", notes))
                items.append(
                    _structured_item(
                        profile,
                        record_kind="software-release",
                        external_id=str(release_id),
                        external_version=tag.strip(),
                        canonical_url=canonical_url,
                        title=title,
                        body="\n".join(document_lines),
                        structured_metadata={
                            "numeric_release_id": release_id,
                            "repository": repository,
                            "tag_name": tag.strip(),
                            "name": title,
                            "published_at": _isoformat(published),
                        },
                        provenance={"entry_point": location, "repository": repository},
                        policy_metadata={
                            "assets_fetched": False,
                            "licence_spdx": policy.licence_spdx,
                            "release_body_eligible": body_eligible,
                            "release_notes_sha256": (
                                sha256(notes.encode()).hexdigest()
                                if body_eligible and notes
                                else None
                            ),
                        },
                        observed_at=observed_at,
                        published_at=published,
                        published_at_raw=(
                            raw.get("published_at")
                            if isinstance(raw.get("published_at"), str)
                            else None
                        ),
                    )
                )
                accepted += 1
                if accepted >= maximum:
                    break
        return SourceAcquisition(items=tuple(items), cursor_value=_record_cursor(items))

    def _throttle_arxiv(self, minimum_interval: float) -> None:
        now = self._monotonic()
        if self._last_arxiv_request_at is not None:
            remaining = minimum_interval - (now - self._last_arxiv_request_at)
            if remaining > 0:
                self._wait(remaining)
                now += remaining
        self._last_arxiv_request_at = now

    def _fetch(
        self,
        location: str,
        *,
        allowed_mime_types: frozenset[str],
        user_agent: str,
    ) -> bytes:
        try:
            payload, _ = self._fetcher.fetch(
                location,
                allowed_mime_types=allowed_mime_types,
                user_agent=user_agent,
                location_validator=lambda actual: _validate_exact_location(location, actual),
            )
            return payload
        except BoundedPublicHttpsSecurityError as error:
            raise SourcePortfolioInvalidFormatError(
                "Source request left its exact endpoint"
            ) from error
        except BoundedPublicHttpsError as error:
            message = str(error)
            if any(f"HTTP {status}" in message for status in (401, 403, 407, 451)):
                raise SourcePortfolioAccessBlockedError("Source access was blocked") from error
            if "unsupported MIME" in message:
                raise SourcePortfolioInvalidFormatError(
                    "Source returned an unsupported format"
                ) from error
            raise SourcePortfolioTemporaryFailureError(
                "Source request failed temporarily"
            ) from error

    def _fetch_json(self, location: str, *, user_agent: str) -> Any:
        payload = self._fetch(
            location,
            allowed_mime_types=frozenset(
                {"application/json", "application/vnd.github+json"}
            ),
            user_agent=user_agent,
        )
        try:
            return json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SourcePortfolioInvalidFormatError("Source returned invalid JSON") from error


def _structured_item(
    profile: SourceProfile,
    *,
    record_kind: str,
    external_id: str,
    external_version: str,
    canonical_url: str,
    title: str,
    body: str,
    structured_metadata: Mapping[str, Any],
    provenance: Mapping[str, Any],
    policy_metadata: Mapping[str, Any],
    observed_at: datetime,
    published_at: datetime | None = None,
    published_at_raw: str | None = None,
    updated_at: datetime | None = None,
    updated_at_raw: str | None = None,
    status: SourceItemStatus = SourceItemStatus.POLICY_VALID_STRUCTURED,
    eligibility_kind: str = "policy-valid-structured",
) -> SourcePortfolioItemResult:
    candidate = _candidate(profile, title, canonical_url, observed_at=observed_at)
    content_hash = sha256(body.encode()).hexdigest()
    document = DocumentVersion(
        id=uuid5(
            NAMESPACE_URL,
            f"ai-intel-agent:source-document:{candidate.id}:{content_hash}",
        ),
        candidate_id=candidate.id,
        source_url=canonical_url,
        title=title,
        body=body,
        content_hash=content_hash,
        observed_at=observed_at,
        published_at=published_at,
        published_at_raw=published_at_raw,
        updated_at=updated_at,
        updated_at_raw=updated_at_raw,
    )
    record_hash = _record_hash(structured_metadata, provenance, policy_metadata)
    record = SourceSpecificRecord(
        id=uuid5(
            NAMESPACE_URL,
            f"ai-intel-agent:source-record:{profile.id}:{record_kind}:{external_id}:{external_version}:{record_hash}",
        ),
        source_definition_id=profile.id,
        candidate_id=candidate.id,
        record_kind=record_kind,
        external_id=external_id,
        external_version=external_version,
        canonical_url=canonical_url,
        record_hash=record_hash,
        provenance=dict(provenance),
        policy_metadata=dict(policy_metadata),
        structured_metadata=dict(structured_metadata),
        evidence_eligible=True,
        observed_at=observed_at,
        document_version_id=document.id,
    )
    return SourcePortfolioItemResult(
        source_definition_id=profile.id,
        candidate=candidate,
        status=status,
        evidence_eligible=True,
        eligibility_kind=eligibility_kind,
        source_record=record,
        document_version=document,
    )


def _non_evidence_item(
    profile: SourceProfile,
    *,
    status: SourceItemStatus,
    record_kind: str,
    external_id: str,
    external_version: str,
    canonical_url: str,
    title: str,
    structured_metadata: Mapping[str, Any],
    provenance: Mapping[str, Any],
    policy_metadata: Mapping[str, Any],
    observed_at: datetime,
) -> SourcePortfolioItemResult:
    candidate = _candidate(profile, title, canonical_url, observed_at=observed_at)
    record_hash = _record_hash(structured_metadata, provenance, policy_metadata)
    record = SourceSpecificRecord(
        id=uuid5(
            NAMESPACE_URL,
            f"ai-intel-agent:source-record:{profile.id}:{record_kind}:{external_id}:{external_version}:{record_hash}",
        ),
        source_definition_id=profile.id,
        candidate_id=candidate.id,
        record_kind=record_kind,
        external_id=external_id,
        external_version=external_version,
        canonical_url=canonical_url,
        record_hash=record_hash,
        provenance=dict(provenance),
        policy_metadata=dict(policy_metadata),
        structured_metadata=dict(structured_metadata),
        evidence_eligible=False,
        observed_at=observed_at,
    )
    return SourcePortfolioItemResult(
        source_definition_id=profile.id,
        candidate=candidate,
        status=status,
        evidence_eligible=False,
        eligibility_kind="ineligible",
        source_record=record,
    )


def _candidate(
    profile: SourceProfile,
    title: str,
    canonical_url: str,
    *,
    observed_at: datetime,
) -> Candidate:
    return Candidate(
        id=uuid5(NAMESPACE_URL, f"ai-intel-agent:source-candidate:{canonical_url}"),
        title=title,
        canonical_url=canonical_url,
        publisher=profile.publisher,
        discovered_at=observed_at,
    )


RecordIdentity = tuple[str, str, str]
MAX_CURSOR_IDENTITIES = 100


def _record_cursor(items: list[SourcePortfolioItemResult]) -> str | None:
    if not items:
        return None
    return _serialize_record_cursor([_record_identity(item) for item in items])


def _apply_incremental_cursor(
    acquisition: SourceAcquisition,
    *,
    cursor_value: str | None,
) -> SourceAcquisition:
    previous = _parse_record_cursor(cursor_value)
    previous_set = set(previous)
    new_items = tuple(
        item for item in acquisition.items if _record_identity(item) not in previous_set
    )
    merged: list[RecordIdentity] = []
    for identity in (
        *(_record_identity(item) for item in acquisition.items),
        *previous,
    ):
        if identity not in merged:
            merged.append(identity)
        if len(merged) >= MAX_CURSOR_IDENTITIES:
            break
    return SourceAcquisition(
        items=new_items,
        cursor_value=_serialize_record_cursor(merged) if merged else cursor_value,
    )


def _record_identity(item: SourcePortfolioItemResult) -> RecordIdentity:
    record = item.source_record
    return record.external_id, record.external_version, record.record_hash


def _serialize_record_cursor(identities: list[RecordIdentity]) -> str:
    return json.dumps(
        {
            "version": "source-record-window.v1",
            "seen": [
                {
                    "external_id": external_id,
                    "external_version": external_version,
                    "record_hash": record_hash,
                }
                for external_id, external_version, record_hash in identities
            ],
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _parse_record_cursor(cursor_value: str | None) -> list[RecordIdentity]:
    if cursor_value is None:
        return []
    try:
        payload = json.loads(cursor_value)
    except (TypeError, json.JSONDecodeError) as error:
        raise SourcePortfolioInvalidFormatError(
            "Persisted source-record cursor is invalid"
        ) from error
    if isinstance(payload, dict) and set(payload) == {
        "external_id",
        "external_version",
        "record_hash",
    }:
        raw_identities = [payload]
    elif (
        isinstance(payload, dict)
        and payload.get("version") == "source-record-window.v1"
        and set(payload) == {"version", "seen"}
        and isinstance(payload["seen"], list)
    ):
        raw_identities = payload["seen"]
    else:
        raise SourcePortfolioInvalidFormatError(
            "Persisted source-record cursor is invalid"
        )
    identities: list[RecordIdentity] = []
    for raw in raw_identities:
        if (
            not isinstance(raw, dict)
            or set(raw) != {"external_id", "external_version", "record_hash"}
            or not isinstance(raw["external_id"], str)
            or not isinstance(raw["external_version"], str)
            or not isinstance(raw["record_hash"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", raw["record_hash"])
        ):
            raise SourcePortfolioInvalidFormatError(
                "Persisted source-record cursor is invalid"
            )
        identities.append(
            (raw["external_id"], raw["external_version"], raw["record_hash"])
        )
    return identities


def _record_hash(*values: Mapping[str, Any]) -> str:
    payload = json.dumps(values, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return sha256(payload.encode()).hexdigest()


def _validate_exact_location(expected: str, actual: str) -> None:
    expected_url = urlparse(expected)
    actual_url = urlparse(actual)
    if (
        actual_url.scheme != "https"
        or actual_url.username
        or actual_url.password
        or actual_url.port not in (None, 443)
        or actual_url.hostname != expected_url.hostname
        or actual_url.path != expected_url.path
        or sorted(parse_qsl(actual_url.query, keep_blank_values=True))
        != sorted(parse_qsl(expected_url.query, keep_blank_values=True))
    ):
        raise BoundedPublicHttpsSecurityError("request left the exact endpoint")


def _validate_profile_url(profile: SourceProfile, location: str) -> None:
    parsed = urlparse(location)
    path = parsed.path or "/"
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or (parsed.hostname or "").casefold() not in profile.allowed_hosts
        or not any(
            path == prefix.rstrip("/") or path.startswith(prefix)
            for prefix in profile.allowed_path_prefixes
        )
    ):
        raise SourcePortfolioInvalidFormatError("Source item is outside its profile")


def _canonical_https_url(location: str) -> str:
    parsed = urlparse(location)
    return urlunparse(
        (
            "https",
            (parsed.hostname or "").casefold(),
            parsed.path.rstrip("/") or "/",
            "",
            urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True))),
            "",
        )
    )


def _github_repository_target(location: str) -> str | None:
    parsed = urlparse(location)
    if (parsed.hostname or "").casefold() != "github.com":
        return None
    parts = tuple(part for part in parsed.path.split("/") if part)
    if len(parts) < 2:
        return None
    owner = parts[0].casefold()
    repository = parts[1].casefold().removesuffix(".git")
    if not owner or not repository:
        return None
    return f"https://github.com/{owner}/{repository}"


def _string_setting(profile: SourceProfile, key: str) -> str:
    value = profile.settings.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SourcePortfolioInvalidFormatError(f"{profile.key} setting {key} is invalid")
    return value


def _string_list_setting(profile: SourceProfile, key: str) -> tuple[str, ...]:
    value = profile.settings.get(key)
    if (
        not isinstance(value, (list, tuple))
        or not value
        or not all(isinstance(item, str) and item.strip() for item in value)
    ):
        raise SourcePortfolioInvalidFormatError(f"{profile.key} setting {key} is invalid")
    return tuple(value)


def _positive_integer_setting(profile: SourceProfile, key: str) -> int:
    value = profile.settings.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise SourcePortfolioInvalidFormatError(f"{profile.key} setting {key} is invalid")
    return value


@dataclass(frozen=True)
class ReleasePolicy:
    """One configurable repository policy for curated GitHub Releases."""

    name: str
    licence_spdx: str
    release_body_eligible: bool
    exclude_tag_patterns: tuple[re.Pattern[str], ...]


def _release_policies(profile: SourceProfile) -> tuple[ReleasePolicy, ...]:
    raw = profile.settings.get("repositories")
    if not isinstance(raw, (list, tuple)) or not raw:
        raise SourcePortfolioInvalidFormatError("Release policies are invalid")
    policies: list[ReleasePolicy] = []
    for item in raw:
        if (
            not isinstance(item, Mapping)
            or set(item)
            != {"name", "licence_spdx", "release_body_eligible", "exclude_tag_patterns"}
            or not isinstance(item["name"], str)
            or not isinstance(item["licence_spdx"], str)
            or not isinstance(item["release_body_eligible"], bool)
        ):
            raise SourcePortfolioInvalidFormatError("Release policy is invalid")
        policies.append(
            ReleasePolicy(
                name=item["name"],
                licence_spdx=item["licence_spdx"],
                release_body_eligible=item["release_body_eligible"],
                exclude_tag_patterns=_compiled_tag_patterns(
                    item["exclude_tag_patterns"]
                ),
            )
        )
    return tuple(policies)


def _compiled_tag_patterns(value: Any) -> tuple[re.Pattern[str], ...]:
    """Compile the configured automated-build tag exclusion patterns."""
    if (
        not isinstance(value, (list, tuple))
        or not all(isinstance(pattern, str) and pattern.strip() for pattern in value)
    ):
        raise SourcePortfolioInvalidFormatError(
            "Release tag exclusion patterns are invalid"
        )
    compiled: list[re.Pattern[str]] = []
    for pattern in value:
        try:
            compiled.append(re.compile(pattern.strip(), re.IGNORECASE))
        except re.error as error:
            raise SourcePortfolioInvalidFormatError(
                "Release tag exclusion pattern is invalid"
            ) from error
    return tuple(compiled)


def _parse_optional_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise SourcePortfolioInvalidFormatError("Source timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise SourcePortfolioInvalidFormatError("Source timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise SourcePortfolioInvalidFormatError("Source timestamp has no time zone")
    return parsed


def _isoformat(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None
