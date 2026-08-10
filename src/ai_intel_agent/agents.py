from __future__ import annotations

import re
from hashlib import sha1

from ai_intel_agent.memory import ContentMemory
from ai_intel_agent.models import Brief, Evidence, NormalizedDocument, SourceCandidate, StoryCluster

_PROMPT_INJECTION_PATTERNS = [
    re.compile(r"ignore (all )?previous instructions", re.IGNORECASE),
    re.compile(r"system prompt", re.IGNORECASE),
    re.compile(r"developer message", re.IGNORECASE),
]


class SourceScout:
    def collect(self, sample: bool = False) -> list[SourceCandidate]:
        if not sample:
            raise NotImplementedError("TODO: connect RSS, search APIs, GitHub, papers, or curated sources.")

        return [
            SourceCandidate(
                title="OpenAI releases a new agent evaluation note",
                url="https://openai.com/blog/agent-evals",
                source="OpenAI",
                raw_text="OpenAI published guidance on measuring agent reliability with task traces.",
            ),
            SourceCandidate(
                title="Agent memory patterns improve retrieval quality",
                url="https://huggingface.co/blog/agent-memory-patterns",
                source="Hugging Face",
                raw_text="A technical post compares structured memory, vector retrieval, and reranking.",
            ),
            SourceCandidate(
                title="OpenAI releases a new agent evaluation note",
                url="https://example.com/duplicate-agent-evals",
                source="Example Wire",
                raw_text="A repost summarizes OpenAI guidance on agent reliability and trace evaluation.",
            ),
        ]


class ContentNormalizer:
    def normalize(self, candidates: list[SourceCandidate]) -> list[NormalizedDocument]:
        return [
            NormalizedDocument(
                title=item.title.strip(),
                url=item.url,
                source=item.source.strip(),
                published_at=item.published_at,
                text=self._sanitize(item.raw_text),
            )
            for item in candidates
        ]

    def _sanitize(self, text: str) -> str:
        cleaned = text.strip()
        for pattern in _PROMPT_INJECTION_PATTERNS:
            cleaned = pattern.sub("[removed untrusted instruction]", cleaned)
        return cleaned


class StoryClusterer:
    def cluster(self, documents: list[NormalizedDocument], memory: ContentMemory) -> list[StoryCluster]:
        clusters: list[StoryCluster] = []
        visited: set[str] = set()
        for document in documents:
            key = str(document.url)
            if key in visited:
                continue

            related = [document, *memory.find_similar(document)]
            related.extend(
                candidate
                for candidate in documents
                if candidate.url != document.url and self._looks_duplicate(document, candidate)
            )
            unique = {str(item.url): item for item in related}
            visited.update(unique)
            clusters.append(
                StoryCluster(
                    cluster_id=sha1(document.title.encode("utf-8")).hexdigest()[:12],
                    headline=document.title,
                    documents=list(unique.values()),
                    novelty_score=1.0 / max(len(unique), 1),
                    authority_score=self._authority(document.source),
                )
            )
        return clusters

    def _looks_duplicate(self, left: NormalizedDocument, right: NormalizedDocument) -> bool:
        left_words = set(left.title.lower().split())
        right_words = set(right.title.lower().split())
        overlap = len(left_words & right_words) / max(len(left_words | right_words), 1)
        return overlap >= 0.6

    def _authority(self, source: str) -> float:
        trusted = {"openai": 1.0, "anthropic": 0.95, "hugging face": 0.9}
        return trusted.get(source.lower(), 0.55)


class EditorAgent:
    def draft(self, clusters: list[StoryCluster]) -> list[Brief]:
        ranked = sorted(clusters, key=lambda item: (item.authority_score, item.novelty_score), reverse=True)
        briefs: list[Brief] = []
        for cluster in ranked[:5]:
            primary = cluster.documents[0]
            evidence = Evidence(
                claim=primary.title,
                source_url=primary.url,
                support=primary.text[:240],
                confidence="medium" if len(cluster.documents) == 1 else "high",
            )
            briefs.append(
                Brief(
                    section="Focus" if cluster.authority_score >= 0.95 else "Trends",
                    title=cluster.headline,
                    summary=(
                        f"{primary.source} reported: {primary.text} "
                        "TODO: replace this deterministic draft with an LLM editor prompt."
                    ),
                    evidence=[evidence],
                )
            )
        return briefs


class ClaimVerifier:
    def keep_supported(self, briefs: list[Brief]) -> list[Brief]:
        supported: list[Brief] = []
        for brief in briefs:
            if brief.evidence and all(item.support for item in brief.evidence):
                supported.append(brief)
        return supported
