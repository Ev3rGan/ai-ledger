from __future__ import annotations

from ai_intel_agent.agents import (
    ClaimVerifier,
    ContentNormalizer,
    EditorAgent,
    SourceScout,
    StoryClusterer,
)
from ai_intel_agent.memory import ContentMemory
from ai_intel_agent.models import DailyReport


def run_daily_report(sample: bool = False) -> DailyReport:
    scout = SourceScout()
    normalizer = ContentNormalizer()
    memory = ContentMemory()
    clusterer = StoryClusterer()
    editor = EditorAgent()
    verifier = ClaimVerifier()

    candidates = scout.collect(sample=sample)
    documents = normalizer.normalize(candidates)
    clusters = clusterer.cluster(documents, memory)
    briefs = verifier.keep_supported(editor.draft(clusters))
    memory.add_many(documents)

    return DailyReport(title="AI Intelligence Daily", briefs=briefs)