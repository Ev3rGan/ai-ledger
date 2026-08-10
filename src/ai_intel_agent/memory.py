from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher

from ai_intel_agent.models import NormalizedDocument


@dataclass
class ContentMemory:
    """Small in-memory story index used as a placeholder for RAG/vector storage."""

    documents: list[NormalizedDocument] = field(default_factory=list)

    def add_many(self, documents: list[NormalizedDocument]) -> None:
        self.documents.extend(documents)

    def find_similar(self, document: NormalizedDocument, limit: int = 3) -> list[NormalizedDocument]:
        scored = [
            (SequenceMatcher(None, document.title.lower(), old.title.lower()).ratio(), old)
            for old in self.documents
            if old.url != document.url
        ]
        scored.sort(key=lambda item: item[0], reverse=True)
        return [doc for score, doc in scored[:limit] if score >= 0.55]
