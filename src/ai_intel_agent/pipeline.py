from __future__ import annotations

from ai_intel_agent.domain import SampleStory
from ai_intel_agent.persistence import SampleStoryRepository, create_database_engine
from ai_intel_agent.sample import build_sample_story


def persist_sample_story(database_url: str) -> SampleStory:
    sample = build_sample_story()
    engine = create_database_engine(database_url)
    try:
        SampleStoryRepository(engine).persist(sample)
    finally:
        engine.dispose()
    return sample
