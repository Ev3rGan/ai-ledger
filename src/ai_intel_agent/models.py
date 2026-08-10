from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl


class SourceCandidate(BaseModel):
    title: str
    url: HttpUrl
    source: str
    published_at: datetime | None = None
    raw_text: str


class NormalizedDocument(BaseModel):
    title: str
    url: HttpUrl
    source: str
    published_at: datetime | None
    text: str
    extracted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class StoryCluster(BaseModel):
    cluster_id: str
    headline: str
    documents: list[NormalizedDocument]
    novelty_score: float
    authority_score: float


class Evidence(BaseModel):
    claim: str
    source_url: HttpUrl
    support: str
    confidence: Literal["low", "medium", "high"] = "medium"


class Brief(BaseModel):
    section: Literal["Focus", "Trends", "Tools"]
    title: str
    summary: str
    evidence: list[Evidence]
    needs_human_review: bool = True


class DailyReport(BaseModel):
    title: str
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    briefs: list[Brief]

    def to_markdown(self) -> str:
        lines = [f"# {self.title}", "", f"Generated at: {self.generated_at.isoformat()}", ""]
        for brief in self.briefs:
            lines.extend([f"## {brief.section}: {brief.title}", "", brief.summary, ""])
            for item in brief.evidence:
                lines.append(f"- Evidence ({item.confidence}): {item.claim} [{item.source_url}]")
            lines.append("")
        return "\n".join(lines).strip() + "\n"