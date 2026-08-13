from __future__ import annotations

from ai_intel_agent.domain import SampleDigestPublication, SampleStory
from ai_intel_agent.editorial import review_and_publish_digest
from ai_intel_agent.persistence import (
    SampleEditorialRepository,
    SampleStoryRepository,
    create_database_engine,
)
from ai_intel_agent.sample import (
    FakeAdministrator,
    FixedClock,
    build_sample_stories,
    build_sample_story,
)


def persist_sample_story(database_url: str) -> SampleStory:
    sample = build_sample_story()
    engine = create_database_engine(database_url)
    try:
        SampleStoryRepository(engine).persist(sample)
    finally:
        engine.dispose()
    return sample


def publish_sample_digest(database_url: str) -> SampleDigestPublication:
    clock = FixedClock()
    publication = review_and_publish_digest(
        build_sample_stories(clock),
        administrator=FakeAdministrator(),
        clock=clock,
    )
    engine = create_database_engine(database_url)
    try:
        SampleEditorialRepository(engine).persist(publication)
    finally:
        engine.dispose()
    return publication
