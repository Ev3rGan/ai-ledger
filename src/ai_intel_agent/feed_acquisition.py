from __future__ import annotations

import socket
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from importlib.resources import files
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import Protocol
from urllib.parse import urljoin, urlparse
from uuid import NAMESPACE_URL, uuid5
from xml.etree import ElementTree

import httpx

from ai_intel_agent.domain import ApprovedFeedSourceDefinition
from ai_intel_agent.source_audit import (
    AuditConclusion,
    load_first_wave_source_definition_audit,
)

IPAddress = IPv4Address | IPv6Address
ALLOWED_FEED_MIME_TYPES = frozenset(
    {
        "application/atom+xml",
        "application/rdf+xml",
        "application/rss+xml",
        "application/xml",
        "text/xml",
    }
)
REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})
BLOCKED_CONTENT_TAGS = frozenset({"iframe", "math", "object", "script", "style", "svg"})
FEED_PUBLISHERS = {
    "Google AI": "Google",
    "Hugging Face Blog": "Hugging Face",
    "GitHub AI and ML": "GitHub",
    "GitHub Changelog": "GitHub",
}
SAMPLE_FEED_FILES = {
    "Google AI": "google-ai.rss",
    "Hugging Face Blog": "hugging-face.atom",
    "GitHub AI and ML": "malformed.xml",
}


@dataclass(frozen=True)
class FeedEntry:
    title: str
    canonical_url: str
    summary: str
    published_at: datetime | None
    published_at_raw: str | None
    updated_at: datetime | None
    updated_at_raw: str | None


class FeedFetcher(Protocol):
    def fetch(self, source_definition: ApprovedFeedSourceDefinition) -> bytes: ...


class HostResolver(Protocol):
    def resolve(self, hostname: str) -> tuple[IPAddress, ...]: ...


class FeedAcquisitionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class FeedFormatError(FeedAcquisitionError):
    def __init__(self, message: str) -> None:
        super().__init__("invalid_feed", message)


class FeedFetchError(FeedAcquisitionError):
    def __init__(self, message: str) -> None:
        super().__init__("fetch_failed", message)


class FeedSecurityError(FeedAcquisitionError):
    def __init__(self, message: str) -> None:
        super().__init__("unsafe_feed_location", message)


class FeedSourceDefinitionConfigurationError(ValueError):
    pass


class SystemHostResolver:
    def resolve(self, hostname: str) -> tuple[IPAddress, ...]:
        try:
            direct_address = ip_address(hostname)
        except ValueError:
            records = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
            return tuple({ip_address(record[4][0]) for record in records})
        return (direct_address,)


class HttpFeedFetcher:
    def __init__(
        self,
        client: httpx.Client,
        *,
        resolver: HostResolver | None = None,
        timeout_seconds: float = 10.0,
        max_response_bytes: int = 2_000_000,
        max_redirects: int = 3,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Feed timeout must be positive")
        if max_response_bytes <= 0:
            raise ValueError("Feed response-size limit must be positive")
        if max_redirects < 0:
            raise ValueError("Feed redirect limit cannot be negative")
        self._client = client
        self._resolver = resolver or SystemHostResolver()
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._max_redirects = max_redirects

    def fetch(self, source_definition: ApprovedFeedSourceDefinition) -> bytes:
        location = source_definition.entry_point
        for redirect_count in range(self._max_redirects + 1):
            self._validate_public_https_location(location)
            try:
                with self._client.stream(
                    "GET",
                    location,
                    follow_redirects=False,
                    timeout=self._timeout_seconds,
                    headers={
                        "accept": ", ".join(sorted(ALLOWED_FEED_MIME_TYPES)),
                        "user-agent": "ai-intel-agent/0.1 feed-collector",
                    },
                ) as response:
                    if response.status_code in REDIRECT_STATUS_CODES:
                        if redirect_count == self._max_redirects:
                            raise FeedFetchError("Feed redirect limit exceeded")
                        redirect_location = response.headers.get("location")
                        if not redirect_location:
                            raise FeedFetchError("Feed redirect has no Location header")
                        location = urljoin(str(response.url), redirect_location)
                        continue
                    if not response.is_success:
                        raise FeedFetchError(
                            f"Feed request returned HTTP {response.status_code}"
                        )
                    self._validate_mime_type(response)
                    return self._read_bounded_body(response)
            except FeedAcquisitionError:
                raise
            except httpx.HTTPError as error:
                raise FeedFetchError("Feed request failed") from error
        raise FeedFetchError("Feed redirect limit exceeded")

    def _validate_public_https_location(self, location: str) -> None:
        parsed = urlparse(location)
        if parsed.scheme != "https":
            raise FeedSecurityError("Feed location must use HTTPS")
        if not parsed.hostname or parsed.username or parsed.password:
            raise FeedSecurityError("Feed location must be an absolute HTTPS URL")
        try:
            port = parsed.port
        except ValueError as error:
            raise FeedSecurityError("Feed location has an invalid port") from error
        if port not in (None, 443):
            raise FeedSecurityError("Feed location must use the standard HTTPS port")
        try:
            addresses = self._resolver.resolve(parsed.hostname)
        except OSError as error:
            raise FeedFetchError("Feed hostname could not be resolved") from error
        if not addresses or any(not address.is_global for address in addresses):
            raise FeedSecurityError("Feed location must resolve only to the public network")

    @staticmethod
    def _validate_mime_type(response: httpx.Response) -> None:
        content_type = response.headers.get("content-type", "")
        mime_type = content_type.partition(";")[0].strip().casefold()
        if mime_type not in ALLOWED_FEED_MIME_TYPES:
            raise FeedFetchError(f"Feed response has unsupported MIME type {mime_type!r}")

    def _read_bounded_body(self, response: httpx.Response) -> bytes:
        declared_length = response.headers.get("content-length")
        if declared_length:
            try:
                parsed_length = int(declared_length)
            except ValueError as error:
                raise FeedFetchError("Feed response has an invalid Content-Length") from error
            if parsed_length > self._max_response_bytes:
                raise FeedFetchError("Feed response exceeds the size limit")

        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > self._max_response_bytes:
                raise FeedFetchError("Feed response exceeds the size limit")
            chunks.append(chunk)
        return b"".join(chunks)


class SampleFeedFetcher:
    def fetch(self, source_definition: ApprovedFeedSourceDefinition) -> bytes:
        filename = SAMPLE_FEED_FILES.get(source_definition.name)
        if filename is None:
            raise FeedFetchError(
                f"No deterministic sample Feed exists for {source_definition.name}"
            )
        resource = files("ai_intel_agent").joinpath(f"data/sample_feeds/{filename}")
        return resource.read_bytes()


class _SummaryTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._blocked_tags: list[str] = []
        self._parts: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        normalized_tag = tag.casefold()
        if normalized_tag in BLOCKED_CONTENT_TAGS:
            self._blocked_tags.append(normalized_tag)

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        return

    def handle_endtag(self, tag: str) -> None:
        if self._blocked_tags and tag.casefold() == self._blocked_tags[-1]:
            self._blocked_tags.pop()

    def handle_data(self, data: str) -> None:
        if not self._blocked_tags:
            self._parts.append(data)

    def text(self) -> str:
        return " ".join(" ".join(self._parts).split())


def load_approved_feed_source_definitions() -> tuple[ApprovedFeedSourceDefinition, ...]:
    audit = load_first_wave_source_definition_audit()
    definitions: list[ApprovedFeedSourceDefinition] = []
    for source_definition in audit.source_definitions:
        adapter = source_definition.extraction_adapter.casefold()
        if source_definition.conclusion is not AuditConclusion.APPROVED:
            continue
        if "rss" not in adapter or "atom" not in adapter:
            continue
        publisher = FEED_PUBLISHERS.get(source_definition.name)
        if publisher is None:
            raise FeedSourceDefinitionConfigurationError(
                f"Approved Feed Source Definition {source_definition.name!r} "
                "has no Publisher identity"
            )
        definitions.append(
            ApprovedFeedSourceDefinition(
                id=uuid5(
                    NAMESPACE_URL,
                    f"ai-intel-agent:source-definition:{audit.version}:"
                    f"{source_definition.name}:{source_definition.entry_point}",
                ),
                name=source_definition.name,
                publisher=publisher,
                entry_point=source_definition.entry_point,
                audit_version=audit.version,
                storage_policy=source_definition.storage_policy,
            )
        )
    return tuple(definitions)


def load_sample_feed_source_definitions() -> tuple[ApprovedFeedSourceDefinition, ...]:
    approved_by_name = {
        source_definition.name: source_definition
        for source_definition in load_approved_feed_source_definitions()
    }
    return tuple(approved_by_name[name] for name in SAMPLE_FEED_FILES)


def parse_feed(payload: bytes) -> tuple[FeedEntry, ...]:
    lowered_prefix = payload[:4096].lower()
    if b"<!doctype" in lowered_prefix or b"<!entity" in lowered_prefix:
        raise FeedFormatError("Feed XML cannot contain document type or entity declarations")
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as error:
        raise FeedFormatError(f"Feed XML is malformed: {error}") from error

    root_name = _local_name(root.tag)
    if root_name == "rss":
        return _parse_rss(root)
    if root_name == "feed":
        return _parse_atom(root)
    raise FeedFormatError(f"Unsupported Feed root element: {root_name}")


def _parse_rss(root: ElementTree.Element) -> tuple[FeedEntry, ...]:
    channel = _first_child(root, "channel")
    if channel is None:
        raise FeedFormatError("RSS Feed has no channel")
    entries: list[FeedEntry] = []
    for item in _children(channel, "item"):
        published_at, published_at_raw = _parse_optional_timestamp(
            _child_text(item, "pubDate")
        )
        entries.append(
            _feed_entry(
                title=_child_plain_text(item, "title"),
                canonical_url=_child_text(item, "link"),
                summary=_child_plain_text(item, "description")
                or _child_plain_text(item, "encoded"),
                published_at=published_at,
                published_at_raw=published_at_raw,
                updated_at=None,
                updated_at_raw=None,
            )
        )
    return tuple(entries)


def _parse_atom(root: ElementTree.Element) -> tuple[FeedEntry, ...]:
    entries: list[FeedEntry] = []
    for item in _children(root, "entry"):
        link = next(
            (
                element.attrib.get("href", "").strip()
                for element in _children(item, "link")
                if element.attrib.get("rel", "alternate") in ("", "alternate")
                and element.attrib.get("href", "").strip()
            ),
            "",
        )
        published_at, published_at_raw = _parse_optional_timestamp(
            _child_text(item, "published")
        )
        updated_at, updated_at_raw = _parse_optional_timestamp(
            _child_text(item, "updated")
        )
        entries.append(
            _feed_entry(
                title=_child_plain_text(item, "title"),
                canonical_url=link,
                summary=_child_plain_text(item, "summary")
                or _child_plain_text(item, "content"),
                published_at=published_at,
                published_at_raw=published_at_raw,
                updated_at=updated_at,
                updated_at_raw=updated_at_raw,
            )
        )
    return tuple(entries)


def _feed_entry(
    *,
    title: str,
    canonical_url: str,
    summary: str,
    published_at: datetime | None,
    published_at_raw: str | None,
    updated_at: datetime | None,
    updated_at_raw: str | None,
) -> FeedEntry:
    sanitized_title = _plain_text(title)
    if not sanitized_title:
        raise FeedFormatError("Feed entry has no title")
    parsed_url = urlparse(canonical_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise FeedFormatError("Feed entry has no canonical HTTP URL")
    return FeedEntry(
        title=sanitized_title,
        canonical_url=canonical_url,
        summary=_plain_text(summary),
        published_at=published_at,
        published_at_raw=published_at_raw,
        updated_at=updated_at,
        updated_at_raw=updated_at_raw,
    )


def _parse_optional_timestamp(value: str) -> tuple[datetime | None, str | None]:
    if not value:
        return None, None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise FeedFormatError(f"Feed entry timestamp is invalid: {value!r}") from error
    if parsed.tzinfo is None:
        raise FeedFormatError(f"Feed entry timestamp has no time zone: {value!r}")
    return parsed, value


def _plain_text(value: str) -> str:
    extractor = _SummaryTextExtractor()
    extractor.feed(value)
    extractor.close()
    return extractor.text()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(element: ElementTree.Element, name: str) -> tuple[ElementTree.Element, ...]:
    return tuple(child for child in element if _local_name(child.tag) == name)


def _first_child(
    element: ElementTree.Element,
    name: str,
) -> ElementTree.Element | None:
    return next(iter(_children(element, name)), None)


def _child_text(element: ElementTree.Element, name: str) -> str:
    child = _first_child(element, name)
    if child is None:
        return ""
    return " ".join(part.strip() for part in child.itertext() if part.strip())


def _child_plain_text(element: ElementTree.Element, name: str) -> str:
    child = _first_child(element, name)
    if child is None:
        return ""
    return _plain_text(_element_text_without_blocked_content(child))


def _element_text_without_blocked_content(element: ElementTree.Element) -> str:
    parts = [element.text or ""]
    for child in element:
        if _local_name(child.tag).casefold() not in BLOCKED_CONTENT_TAGS:
            parts.append(_element_text_without_blocked_content(child))
        parts.append(child.tail or "")
    return "".join(parts)
