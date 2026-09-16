from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date, datetime
from functools import lru_cache
from importlib.resources import files

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ai_intel_agent.publication import PublicStory

_TEMPLATE_ROOT = files("ai_intel_agent").joinpath("templates")
_STATIC_ROOT = files("ai_intel_agent").joinpath("static")
_ENVIRONMENT = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_ROOT)),
    autoescape=select_autoescape(enabled_extensions=("html", "xml"), default_for_string=True),
    trim_blocks=True,
    lstrip_blocks=True,
)


def render_public_page(template_name: str, *, page_name: str, **context: object) -> str:
    template = _ENVIRONMENT.get_template(template_name)
    return template.render(
        page_name=page_name,
        frontend_assets=_frontend_assets(page_name),
        **context,
    )


@lru_cache(maxsize=2)
def _frontend_assets(page_name: str) -> dict[str, object]:
    if page_name not in {"browse", "research"}:
        return {"module": None, "css": ()}
    manifest_path = _STATIC_ROOT.joinpath(".vite/manifest.json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        entry = manifest[f"src/{page_name}.js"]
        module = _asset_url(entry["file"])
        css = tuple(_asset_url(path) for path in _entry_css(manifest, entry))
    except (FileNotFoundError, KeyError, TypeError, ValueError):
        return {"module": None, "css": ()}
    return {"module": module, "css": css}


def _entry_css(manifest: dict[str, object], entry: object) -> tuple[str, ...]:
    discovered: list[str] = []
    visited: set[str] = set()

    def visit(candidate: object) -> None:
        if not isinstance(candidate, dict):
            raise TypeError("Frontend manifest entry must be an object")
        for imported_key in candidate.get("imports", ()):
            if not isinstance(imported_key, str) or imported_key in visited:
                continue
            visited.add(imported_key)
            visit(manifest[imported_key])
        for path in candidate.get("css", ()):
            if not isinstance(path, str):
                raise TypeError("Frontend manifest CSS path must be a string")
            if path not in discovered:
                discovered.append(path)

    visit(entry)
    return tuple(discovered)


def _asset_url(path: object) -> str:
    if not isinstance(path, str) or path.startswith(("/", ".")) or ".." in path:
        raise ValueError("Frontend manifest contains an unsafe asset path")
    return f"/assets/{path}"


def render_story_cards(
    stories: tuple[PublicStory, ...],
    *,
    story_url: Callable[[str], str],
) -> str:
    template = _ENVIRONMENT.get_template("fragments/story_cards.html")
    return template.render(stories=stories, story_url=story_url)


def display_date(value: date | datetime | None) -> str:
    if value is None:
        return "时间未知"
    if isinstance(value, datetime):
        return value.date().isoformat()
    return value.isoformat()


_ENVIRONMENT.filters["display_date"] = display_date
