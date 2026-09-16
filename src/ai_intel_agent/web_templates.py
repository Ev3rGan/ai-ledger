from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from importlib.resources import files

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ai_intel_agent.publication import PublicStory

_TEMPLATE_ROOT = files("ai_intel_agent").joinpath("templates")
_ENVIRONMENT = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_ROOT)),
    autoescape=select_autoescape(enabled_extensions=("html", "xml"), default_for_string=True),
    trim_blocks=True,
    lstrip_blocks=True,
)


def render_public_page(template_name: str, *, page_name: str, **context: object) -> str:
    template = _ENVIRONMENT.get_template(template_name)
    return template.render(page_name=page_name, **context)


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
