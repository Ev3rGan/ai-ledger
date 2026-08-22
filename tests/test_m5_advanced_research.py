from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
import pytest

from ai_intel_agent.accepted_knowledge import (
    AcceptedKnowledgeHit,
    AcceptedKnowledgeResult,
    RetrievalFault,
    RetrievalQuery,
    RetrievalTrace,
)
from ai_intel_agent.domain import EvidenceRelation, EvidenceRole
from ai_intel_agent.research import (
    DeepSeekResearchProvider,
    ResearchBudgetExceeded,
    ResearchEvidence,
    ResearchEvidenceMetadata,
    ResearchEvidenceSet,
    ResearchEvidenceTimes,
    ResearchRepository,
    ResearchTaskType,
    ResearchTimeSemantic,
    interpret_query_intent,
    load_research_protocol,
    stream_research_events,
)


@pytest.mark.parametrize(
    ("question", "expected_task_type"),
    (
        ("Gemini 3.6 Flash 有什么更新？", ResearchTaskType.SIMPLE_LOOKUP),
        (
            "比较 OpenAI 和 Anthropic 在模型发布、融资方面的进展",
            ResearchTaskType.COMPARISON,
        ),
        ("按时间线梳理 Gemini 3.6 Flash 的发布历程", ResearchTaskType.TIMELINE),
        (
            "OpenAI 发布新模型后如何影响开发者采用？",
            ResearchTaskType.MULTI_HOP,
        ),
    ),
)
def test_query_intent_distinguishes_the_four_bounded_research_tasks(
    question: str,
    expected_task_type: ResearchTaskType,
) -> None:
    intent = interpret_query_intent(question)

    assert intent.task_type is expected_task_type


def test_comparison_intent_records_entities_time_scope_dimensions_and_budget() -> None:
    intent = interpret_query_intent(
        "比较 OpenAI 和 Anthropic 在 2025 至 2026 年的模型发布、融资方面的进展"
    )
    protocol = load_research_protocol()

    assert intent.task_type is ResearchTaskType.COMPARISON
    assert intent.entities == ("OpenAI", "Anthropic")
    assert intent.time_range.start == datetime(2025, 1, 1, tzinfo=UTC)
    assert intent.time_range.end == datetime(2027, 1, 1, tzinfo=UTC)
    assert intent.scope == "accepted-published-knowledge"
    assert intent.dimensions == ("模型发布", "融资")
    assert intent.budget.maximum_iterations == 1
    assert intent.budget.maximum_retrieval_calls == 4
    assert intent.budget.maximum_evidence_items <= protocol.maximum_evidence_items
    assert intent.budget.maximum_output_tokens == protocol.maximum_output_tokens
    assert intent.budget.maximum_elapsed_seconds == protocol.maximum_elapsed_seconds


def test_generic_comparison_records_one_explicit_default_dimension() -> None:
    intent = interpret_query_intent("比较 OpenAI 和 Anthropic 的主要变化")

    assert intent.task_type is ResearchTaskType.COMPARISON
    assert intent.entities == ("OpenAI", "Anthropic")
    assert intent.dimensions == ("主要差异",)


def test_comparison_intent_records_explicit_chinese_entities() -> None:
    intent = interpret_query_intent("比较百度和阿里巴巴在模型发布方面的进展")

    assert intent.task_type is ResearchTaskType.COMPARISON
    assert intent.entities == ("百度", "阿里巴巴")
    assert intent.dimensions == ("模型发布",)


def _id(name: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"m5-advanced-research:{name}")


def _hit(identity: str, *, claim_text: str) -> AcceptedKnowledgeHit:
    return AcceptedKnowledgeHit(
        story_id=_id(f"{identity}:story"),
        story_stable_key=f"m5:{identity}",
        story_headline=f"M5 {identity}",
        claim_id=_id(f"{identity}:claim"),
        claim_text=claim_text,
        evidence_span_id=_id(f"{identity}:evidence"),
        exact_text=f"Evidence for {identity}.",
        chunk_id=None,
    )


def _result(
    query: RetrievalQuery,
    *hits: AcceptedKnowledgeHit,
    faults: tuple[RetrievalFault, ...] = (),
) -> AcceptedKnowledgeResult:
    return AcceptedKnowledgeResult(
        query=query,
        hits=hits,
        matching_story_ids=tuple(hit.story_id for hit in hits),
        trace=RetrievalTrace(
            lexical=(),
            semantic=(),
            entity=(),
            fusion=(),
            final=(),
            faults=faults,
        ),
    )


class FixtureAcceptedKnowledge:
    def __init__(
        self,
        hits: dict[str, AcceptedKnowledgeHit],
        *,
        faults: tuple[RetrievalFault, ...] = (),
    ) -> None:
        self.hits = hits
        self.faults = faults
        self.queries: list[RetrievalQuery] = []

    def retrieve(self, query: RetrievalQuery) -> AcceptedKnowledgeResult:
        self.queries.append(query)
        matches = tuple(hit for entity, hit in self.hits.items() if entity in query.text)
        return _result(query, *matches, faults=self.faults)


class ExactQueryAcceptedKnowledge:
    def __init__(self, hits: dict[str, AcceptedKnowledgeHit]) -> None:
        self.hits = hits
        self.queries: list[RetrievalQuery] = []

    def retrieve(self, query: RetrievalQuery) -> AcceptedKnowledgeResult:
        self.queries.append(query)
        hit = self.hits.get(query.text)
        return _result(query, *((hit,) if hit is not None else ()))


class FixtureEvidenceMetadata:
    def __init__(self, values: dict[UUID, ResearchEvidenceMetadata]) -> None:
        self.values = values
        self.calls: list[tuple[UUID, ...]] = []

    def load(
        self,
        evidence_span_ids: tuple[UUID, ...],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[UUID, ResearchEvidenceMetadata]:
        del timeout_seconds
        self.calls.append(evidence_span_ids)
        return {
            identifier: self.values[identifier]
            for identifier in evidence_span_ids
            if identifier in self.values
        }


class CrossDimensionComparisonProvider:
    def stream(self, evidence_set: object):
        citations = [
            {
                "story_id": str(item.story_id),
                "claim_id": str(item.claim_id),
                "evidence_span_id": str(item.evidence_span_id),
            }
            for item in evidence_set.evidence
        ]
        support = [
            {
                "statement": "模型发布维度被错误地绑定到全部证据。",
                "citations": citations,
                "requirement_ids": [
                    requirement.identifier
                    for requirement in evidence_set.requirements
                    if requirement.dimension == "模型发布"
                ],
                "dimension": "模型发布",
                "time_semantic": None,
            },
            {
                "statement": "融资维度也被错误地绑定到全部证据。",
                "citations": citations,
                "requirement_ids": [
                    requirement.identifier
                    for requirement in evidence_set.requirements
                    if requirement.dimension == "融资"
                ],
                "dimension": "融资",
                "time_semantic": None,
            },
        ]
        yield json.dumps(
            {
                "answer": "\n".join(item["statement"] for item in support),
                "support": support,
            },
            ensure_ascii=False,
        )


class DiagnosticFailureProvider:
    def __init__(self, failure: str) -> None:
        self.failure = failure
        self.output = ""

    def stream(self, evidence_set: object):
        if self.failure == "invalid-json":
            self.output = "SENSITIVE-PROVIDER-PAYLOAD{"
            yield self.output
            return

        evidence = evidence_set.evidence[0]
        citation = {
            "story_id": str(evidence.story_id),
            "claim_id": str(evidence.claim_id),
            "evidence_span_id": str(evidence.evidence_span_id),
        }
        support = [
            {
                "statement": "Cursor 的敏感比较结论。",
                "citations": [citation],
                "requirement_ids": ["requirement-1"],
                "dimension": "代码托管定位",
                "time_semantic": None,
            },
            {
                "statement": "GitHub 的敏感比较结论。",
                "citations": [citation],
                "requirement_ids": ["requirement-2"],
                "dimension": "代码托管定位",
                "time_semantic": None,
            },
        ]
        self.output = json.dumps(
            {
                "answer": "\n".join(item["statement"] for item in support),
                "support": support,
            },
            ensure_ascii=False,
        )
        yield self.output


@pytest.mark.parametrize(
    ("failure", "expected_code", "expected_stage", "expected_cited_story_count"),
    (
        ("invalid-json", "invalid-json", "json-parse", None),
        (
            "collapsed-distinct-stories",
            "collapsed-distinct-stories",
            "distinct-story-validation",
            1,
        ),
    ),
)
def test_provider_failure_diagnostic_capture_is_sanitized_and_publicly_unchanged(
    caplog: pytest.LogCaptureFixture,
    failure: str,
    expected_code: str,
    expected_stage: str,
    expected_cited_story_count: int | None,
) -> None:
    question = "比较 Cursor 和 GitHub 在代码托管定位方面的敏感诊断问题？"
    shared_hit = AcceptedKnowledgeHit(
        story_id=_id("sensitive-story"),
        story_stable_key="https://sensitive.invalid/story",
        story_headline="敏感 Story 标题",
        claim_id=_id("sensitive-claim"),
        claim_text="敏感 Claim 文本",
        evidence_span_id=_id("sensitive-evidence"),
        exact_text="敏感 Evidence 文本",
        chunk_id=None,
    )
    repository = ResearchRepository(
        retrieval=ExactQueryAcceptedKnowledge(
            {
                "Cursor 代码托管定位": shared_hit,
                "GitHub 代码托管定位": shared_hit,
            }
        ),
        metadata_loader=FixtureEvidenceMetadata({}),
    )
    provider = DiagnosticFailureProvider(failure)

    with caplog.at_level(logging.WARNING, logger="ai_intel_agent.research"):
        events = list(
            stream_research_events(
                question,
                repository=repository,
                provider=provider,
            )
        )

    assert [event for event, _ in events] == [
        "status",
        "status",
        "status",
        "error",
        "done",
    ]
    assert [
        payload["state"]
        for event, payload in events
        if event == "status"
    ] == ["retrieving", "evidence-assembled", "generating"]
    assert events[-2:] == [
        (
            "error",
            {
                "version": "research-sse-2026-08-15.v1",
                "code": "provider-failed",
                "message": "Research Provider 输出未通过验证。",
            },
        ),
        (
            "done",
            {
                "version": "research-sse-2026-08-15.v1",
                "status": "failed",
            },
        ),
    ]
    public_events = json.dumps(events, ensure_ascii=False)
    assert "[DEBUG-M5-G-CAPTURE]" not in public_events
    assert "failure_code" not in public_events
    assert "validation_stage" not in public_events

    records = [
        record
        for record in caplog.records
        if record.getMessage().startswith("[DEBUG-M5-G-CAPTURE] ")
    ]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    diagnostic = json.loads(
        records[0].getMessage().removeprefix("[DEBUG-M5-G-CAPTURE] ")
    )
    assert diagnostic == {
        "cited_distinct_story_count": expected_cited_story_count,
        "evidence_count": 1,
        "failure_code": expected_code,
        "per_requirement_story_counts": [1, 1],
        "requirement_count": 2,
        "requirement_story_intersection_count": 1,
        "retrieved_distinct_story_count": 1,
        "task_type": "comparison",
        "validation_stage": expected_stage,
    }
    captured = records[0].getMessage()
    sensitive_values = (
        question,
        provider.output,
        shared_hit.story_stable_key,
        shared_hit.story_headline,
        shared_hit.claim_text,
        shared_hit.exact_text,
        str(shared_hit.story_id),
        str(shared_hit.claim_id),
        str(shared_hit.evidence_span_id),
        "Cursor 的敏感比较结论。",
        "GitHub 的敏感比较结论。",
    )
    assert all(value not in captured for value in sensitive_values)


def test_comparison_evidence_set_preserves_dimensions_claim_qualifiers_and_roles() -> None:
    openai_model = _hit(
        "openai-model",
        claim_text="OpenAI 发布了限定为预览版的模型。",
    )
    openai_funding = _hit(
        "openai-funding",
        claim_text="OpenAI 完成了有明确日期限定的融资。",
    )
    anthropic_model = _hit(
        "anthropic-model",
        claim_text="Anthropic 发布了正式可用的模型。",
    )
    anthropic_funding = _hit(
        "anthropic-funding",
        claim_text="Anthropic 披露了另一轮融资。",
    )
    hits = {
        "OpenAI 模型发布": openai_model,
        "OpenAI 融资": openai_funding,
        "Anthropic 模型发布": anthropic_model,
        "Anthropic 融资": anthropic_funding,
    }
    retrieval = ExactQueryAcceptedKnowledge(hits)
    metadata = FixtureEvidenceMetadata(
        {
            openai_model.evidence_span_id: ResearchEvidenceMetadata(
                evidence_role=EvidenceRole.PRIMARY,
                evidence_relation=EvidenceRelation.SUPPORTS,
                evidence_publisher="OpenAI",
            ),
            openai_funding.evidence_span_id: ResearchEvidenceMetadata(
                evidence_role=EvidenceRole.INDEPENDENT,
                evidence_relation=EvidenceRelation.SUPPORTS,
                evidence_publisher="Financial Filing",
            ),
            anthropic_model.evidence_span_id: ResearchEvidenceMetadata(
                evidence_role=EvidenceRole.INDEPENDENT,
                evidence_relation=EvidenceRelation.SUPPORTS,
                evidence_publisher="Independent Lab",
            ),
            anthropic_funding.evidence_span_id: ResearchEvidenceMetadata(
                evidence_role=EvidenceRole.SECONDARY,
                evidence_relation=EvidenceRelation.SUPPORTS,
                evidence_publisher="Finance Desk",
            ),
        }
    )
    intent = interpret_query_intent(
        "比较 OpenAI 和 Anthropic 在模型发布、融资方面的进展"
    )

    evidence_set = ResearchRepository(
        retrieval=retrieval,
        metadata_loader=metadata,
    ).retrieve_intent(intent)

    assert [query.text for query in retrieval.queries] == list(hits)
    assert all(query.filters == retrieval.queries[0].filters for query in retrieval.queries)
    assert [
        (requirement.entity, requirement.dimension, requirement.evidence_keys)
        for requirement in evidence_set.requirements
    ] == [
        (
            "OpenAI",
            "模型发布",
            ((openai_model.story_id, openai_model.claim_id, openai_model.evidence_span_id),),
        ),
        (
            "OpenAI",
            "融资",
            ((openai_funding.story_id, openai_funding.claim_id, openai_funding.evidence_span_id),),
        ),
        (
            "Anthropic",
            "模型发布",
            (
                (
                    anthropic_model.story_id,
                    anthropic_model.claim_id,
                    anthropic_model.evidence_span_id,
                ),
            ),
        ),
        (
            "Anthropic",
            "融资",
            (
                (
                    anthropic_funding.story_id,
                    anthropic_funding.claim_id,
                    anthropic_funding.evidence_span_id,
                ),
            ),
        ),
    ]
    assert evidence_set.missing_requirement_ids == ()
    assert tuple(item.claim_text for item in evidence_set.evidence) == (
        "OpenAI 发布了限定为预览版的模型。",
        "OpenAI 完成了有明确日期限定的融资。",
        "Anthropic 发布了正式可用的模型。",
        "Anthropic 披露了另一轮融资。",
    )
    assert tuple(item.evidence_role for item in evidence_set.evidence) == (
        EvidenceRole.PRIMARY,
        EvidenceRole.INDEPENDENT,
        EvidenceRole.INDEPENDENT,
        EvidenceRole.SECONDARY,
    )
    assert evidence_set.intent is intent

    events = list(
        stream_research_events(
            intent.question,
            repository=ResearchRepository(
                retrieval=ExactQueryAcceptedKnowledge(hits),
                metadata_loader=metadata,
            ),
            provider=CrossDimensionComparisonProvider(),
        )
    )
    assert next(payload for event, payload in events if event == "error")["code"] == (
        "provider-failed"
    )


def test_timeline_evidence_set_keeps_five_distinct_time_semantics() -> None:
    gemini = _hit("gemini", claim_text="Gemini 3.6 Flash 已正式发布。")
    times = ResearchEvidenceTimes(
        event=datetime(2025, 1, 10, 8, tzinfo=UTC),
        source_publication=datetime(2025, 1, 11, 8, tzinfo=UTC),
        discovery=datetime(2025, 1, 12, 8, tzinfo=UTC),
        editorial=datetime(2025, 1, 13, 8, tzinfo=UTC),
        digest_publication=datetime(2025, 1, 14, 8, tzinfo=UTC),
    )
    retrieval = FixtureAcceptedKnowledge({"Gemini 3.6 Flash": gemini})
    metadata = FixtureEvidenceMetadata(
        {
            gemini.evidence_span_id: ResearchEvidenceMetadata(
                evidence_role=EvidenceRole.PRIMARY,
                evidence_relation=EvidenceRelation.SUPPORTS,
                evidence_publisher="Google",
                times=times,
            )
        }
    )
    intent = interpret_query_intent(
        "按时间线梳理 Gemini 3.6 Flash 在 2025 年的发布历程"
    )

    evidence_set = ResearchRepository(
        retrieval=retrieval,
        metadata_loader=metadata,
    ).retrieve_intent(intent)

    assert len(retrieval.queries) == 1
    assert intent.time_semantic is ResearchTimeSemantic.ALL
    assert retrieval.queries[0].filters.time_semantics == (
        "event",
        "source-publication",
        "discovery",
        "editorial",
        "digest-publication",
    )
    assert retrieval.queries[0].filters.time_from == intent.time_range.start
    assert retrieval.queries[0].filters.time_to == intent.time_range.end
    assert retrieval.queries[0].filters.occurred_from is None
    assert retrieval.queries[0].filters.occurred_to is None
    assert evidence_set.evidence[0].times == times
    assert tuple(evidence_set.evidence[0].times.as_labeled_values()) == (
        ("event", times.event),
        ("source-publication", times.source_publication),
        ("discovery", times.discovery),
        ("editorial", times.editorial),
        ("digest-publication", times.digest_publication),
    )


class FullySupportedTimelineProvider:
    def stream(self, evidence_set: object):
        evidence = evidence_set.evidence[0]
        citation = {
            "story_id": str(evidence.story_id),
            "claim_id": str(evidence.claim_id),
            "evidence_span_id": str(evidence.evidence_span_id),
        }
        labels = (
            ("event", "事件时间已记录。"),
            ("source-publication", "来源发布时间已记录。"),
            ("discovery", "发现时间已记录。"),
            ("editorial", "编辑时间已记录。"),
            ("digest-publication", "Digest 发布时间已记录。"),
        )
        support = [
            {
                "statement": statement,
                "citations": [citation],
                "requirement_ids": ["requirement-1"],
                "dimension": None,
                "time_semantic": label,
            }
            for label, statement in labels
        ]
        yield json.dumps(
            {
                "answer": "\n".join(item["statement"] for item in support),
                "support": support,
            },
            ensure_ascii=False,
        )


def test_timeline_output_labels_each_time_semantic_without_conflation() -> None:
    gemini = _hit("timeline-output", claim_text="Gemini 3.6 Flash 已正式发布。")
    times = ResearchEvidenceTimes(
        event=datetime(2025, 1, 10, 8, tzinfo=UTC),
        source_publication=datetime(2025, 1, 11, 8, tzinfo=UTC),
        discovery=datetime(2025, 1, 12, 8, tzinfo=UTC),
        editorial=datetime(2025, 1, 13, 8, tzinfo=UTC),
        digest_publication=datetime(2025, 1, 14, 8, tzinfo=UTC),
    )

    explicit_retrieval = FixtureAcceptedKnowledge(
        {"Gemini 3.6 Flash": gemini}
    )
    explicit_intent = interpret_query_intent(
        "按来源发布时间梳理 Gemini 3.6 Flash 在 2025 年的发布历程"
    )
    explicit_evidence_set = ResearchRepository(
        retrieval=explicit_retrieval,
        metadata_loader=FixtureEvidenceMetadata(
            {
                gemini.evidence_span_id: ResearchEvidenceMetadata(
                    evidence_role=EvidenceRole.PRIMARY,
                    evidence_relation=EvidenceRelation.SUPPORTS,
                    evidence_publisher="Google",
                    times=times,
                )
            }
        ),
    ).retrieve_intent(explicit_intent)

    assert explicit_intent.time_semantic is ResearchTimeSemantic.SOURCE_PUBLICATION
    assert explicit_retrieval.queries[0].filters.time_semantics == (
        "source-publication",
    )
    assert explicit_retrieval.queries[0].filters.time_from == explicit_intent.time_range.start
    assert explicit_retrieval.queries[0].filters.time_to == explicit_intent.time_range.end
    assert explicit_retrieval.queries[0].filters.occurred_from is None
    assert explicit_retrieval.queries[0].filters.occurred_to is None
    assert explicit_evidence_set.evidence[0].times.source_publication == (
        times.source_publication
    )

    events = list(
        stream_research_events(
            "按时间线梳理 Gemini 3.6 Flash 在 2025 年的发布历程",
            repository=ResearchRepository(
                retrieval=FixtureAcceptedKnowledge(
                    {"Gemini 3.6 Flash": gemini}
                ),
                metadata_loader=FixtureEvidenceMetadata(
                    {
                        gemini.evidence_span_id: ResearchEvidenceMetadata(
                            evidence_role=EvidenceRole.PRIMARY,
                            evidence_relation=EvidenceRelation.SUPPORTS,
                            evidence_publisher="Google",
                            times=times,
                        )
                    }
                ),
            ),
            provider=FullySupportedTimelineProvider(),
        )
    )

    citation = next(payload for event, payload in events if event == "citation")
    assert citation["statement_indexes"] == [1, 2, 3, 4, 5]
    assert citation["statement_support"] == [
        {"statement_index": 1, "dimension": None, "time_semantic": "event"},
        {
            "statement_index": 2,
            "dimension": None,
            "time_semantic": "source-publication",
        },
        {"statement_index": 3, "dimension": None, "time_semantic": "discovery"},
        {"statement_index": 4, "dimension": None, "time_semantic": "editorial"},
        {
            "statement_index": 5,
            "dimension": None,
            "time_semantic": "digest-publication",
        },
    ]
    assert citation["times"] == {
        label: value.isoformat()
        for label, value in times.as_labeled_values()
    }
    assert events[-1][1]["status"] == "answered"


class NeverCalledProvider:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def stream(self, evidence_set: object):
        self.calls.append(evidence_set)
        raise AssertionError("Provider must not run without every required hop")


def test_multi_hop_refuses_before_provider_when_intermediate_support_is_missing() -> None:
    first_hop = _hit("openai-orion", claim_text="OpenAI 发布了 Orion。")
    retrieval = FixtureAcceptedKnowledge({"OpenAI": first_hop})
    repository = ResearchRepository(
        retrieval=retrieval,
        metadata_loader=FixtureEvidenceMetadata({}),
    )
    provider = NeverCalledProvider()

    events = list(
        stream_research_events(
            "OpenAI 发布了 Orion；Orion 的新架构如何影响开发者部署？",
            repository=repository,
            provider=provider,
        )
    )

    assert len(retrieval.queries) == 2
    assert [event for event, _ in events] == ["status", "refusal", "done"]
    assert events[1][1]["reason"] == "missing-intermediate-evidence"
    assert events[1][1]["missing_requirements"] == ["requirement-2"]
    assert events[-1][1]["status"] == "refused"
    assert provider.calls == []


def test_causal_multi_hop_question_decomposes_into_two_bounded_requirements() -> None:
    cause = _hit("model-release", claim_text="模型已经发布。")
    effect = _hit("developer-deployment", claim_text="开发者部署方式发生变化。")
    retrieval = FixtureAcceptedKnowledge(
        {"模型发布": cause, "开发者部署": effect}
    )
    intent = interpret_query_intent("模型发布后如何影响开发者部署？")

    evidence_set = ResearchRepository(
        retrieval=retrieval,
        metadata_loader=FixtureEvidenceMetadata({}),
    ).retrieve_intent(intent)

    assert [query.text for query in retrieval.queries] == ["模型发布", "开发者部署"]
    assert [requirement.label for requirement in evidence_set.requirements] == [
        "hop-1",
        "hop-2",
    ]
    assert evidence_set.missing_requirement_ids == ()


def test_multi_hop_never_falls_back_to_one_whole_question_retrieval() -> None:
    cause = _hit("generic-cause", claim_text="AI 模型能力已经变化。")
    effect = _hit("generic-effect", claim_text="医疗行业采用方式受到影响。")
    generic_retrieval = FixtureAcceptedKnowledge(
        {"AI 模型": cause, "医疗行业": effect}
    )

    evidence_set = ResearchRepository(
        retrieval=generic_retrieval,
        metadata_loader=FixtureEvidenceMetadata({}),
    ).retrieve_intent(interpret_query_intent("AI 模型如何影响医疗行业？"))

    assert [query.text for query in generic_retrieval.queries] == [
        "AI 模型",
        "医疗行业",
    ]
    assert len(evidence_set.requirements) == 2

    opaque_retrieval = FixtureAcceptedKnowledge({})
    provider = NeverCalledProvider()
    events = list(
        stream_research_events(
            "多跳研究 OpenAI",
            repository=ResearchRepository(
                retrieval=opaque_retrieval,
                metadata_loader=FixtureEvidenceMetadata({}),
            ),
            provider=provider,
        )
    )

    assert opaque_retrieval.queries == []
    assert [event for event, _ in events] == ["status", "refusal", "done"]
    assert events[1][1]["reason"] == "unsupported-decomposition"
    assert provider.calls == []


class FullySupportedMultiHopProvider:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def stream(self, evidence_set: object):
        self.calls.append(evidence_set)
        first, second = evidence_set.evidence
        output = {
            "answer": "OpenAI 发布了 Orion。\n新架构随后影响了开发者部署。",
            "support": [
                {
                    "statement": "OpenAI 发布了 Orion。",
                    "citations": [
                        {
                            "story_id": str(first.story_id),
                            "claim_id": str(first.claim_id),
                            "evidence_span_id": str(first.evidence_span_id),
                        }
                    ],
                    "requirement_ids": ["requirement-1"],
                    "dimension": None,
                    "time_semantic": None,
                },
                {
                    "statement": "新架构随后影响了开发者部署。",
                    "citations": [
                        {
                            "story_id": str(second.story_id),
                            "claim_id": str(second.claim_id),
                            "evidence_span_id": str(second.evidence_span_id),
                        }
                    ],
                    "requirement_ids": ["requirement-2"],
                    "dimension": None,
                    "time_semantic": None,
                },
            ],
        }
        yield json.dumps(output, ensure_ascii=False)


def test_multi_hop_returns_concise_supported_answer_progress_and_public_citations() -> None:
    first_hop = _hit("openai-orion", claim_text="OpenAI 发布了 Orion。")
    second_hop = _hit(
        "orion-deployment",
        claim_text="Orion 的新架构改变了开发者部署方式。",
    )
    retrieval = FixtureAcceptedKnowledge(
        {"OpenAI": first_hop, "开发者部署": second_hop}
    )
    repository = ResearchRepository(
        retrieval=retrieval,
        metadata_loader=FixtureEvidenceMetadata({}),
    )
    provider = FullySupportedMultiHopProvider()

    events = list(
        stream_research_events(
            "OpenAI 发布了 Orion；Orion 的新架构如何影响开发者部署？",
            repository=repository,
            provider=provider,
        )
    )

    states = [payload["state"] for event, payload in events if event == "status"]
    assert states == [
        "retrieving",
        "evidence-assembled",
        "generating",
        "verifying-citations",
    ]
    assert events[0][1]["intent"]["task_type"] == "multi-hop"
    assert events[0][1]["intent"]["budget"]["maximum_retrieval_calls"] == 4
    assert len(retrieval.queries) == 2
    assert len(retrieval.queries) <= 4
    answer = "".join(
        str(payload["text"])
        for event, payload in events
        if event == "answer.delta"
    )
    assert answer == "OpenAI 发布了 Orion。\n新架构随后影响了开发者部署。"
    citations = [payload for event, payload in events if event == "citation"]
    assert len(citations) == 2
    assert [payload["statement_indexes"] for payload in citations] == [[1], [2]]
    assert all(payload["story_url"].startswith("/stories/") for payload in citations)
    assert all("#claim-" in str(payload["claim_url"]) for payload in citations)
    assert all("#evidence-" in str(payload["evidence_url"]) for payload in citations)
    public_stream = json.dumps(events, ensure_ascii=False)
    assert "reasoning" not in public_stream.casefold()
    assert "retrieval_queries" not in public_stream
    assert len(provider.calls) == 1
    assert events[-1][1]["status"] == "answered"


def test_research_graph_enforces_and_exposes_the_actual_iteration_budget() -> None:
    retrieval = FixtureAcceptedKnowledge(
        {
            f"步骤{label}": _hit(f"step-{index}", claim_text=f"步骤{label}有依据。")
            for index, label in enumerate("一二三", start=1)
        }
    )
    repository = ResearchRepository(
        retrieval=retrieval,
        metadata_loader=FixtureEvidenceMetadata({}),
    )
    provider = NeverCalledProvider()

    events = list(
        stream_research_events(
            "步骤一确认 A；步骤二确认 B；步骤三如何影响 C？",
            repository=repository,
            provider=provider,
        )
    )

    assert [query.text for query in retrieval.queries] == [
        "步骤一确认 A",
        "步骤二确认 B",
    ]
    assert [event for event, _ in events] == ["status", "error", "done"]
    assert events[1][1]["code"] == "execution-budget-exceeded"
    assert events[1][1]["limit"] == "iterations"
    assert events[1][1]["iterations"] == 2
    assert events[-1][1]["status"] == "failed"
    assert provider.calls == []


class FullySupportedComparisonProvider:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def stream(self, evidence_set: object):
        self.calls.append(evidence_set)
        evidence_by_key = {item.citation_key: item for item in evidence_set.evidence}
        support = []
        for dimension in evidence_set.intent.dimensions:
            requirements = [
                requirement
                for requirement in evidence_set.requirements
                if requirement.dimension == dimension
            ]
            requirement_evidence = [
                evidence_by_key[key]
                for requirement in requirements
                for key in requirement.evidence_keys
            ]
            unique_evidence = tuple(
                {
                    item.citation_key: item
                    for item in requirement_evidence
                }.values()
            )
            support.append(
                {
                    "statement": f"{dimension}维度只报告对应公开证据能够支持的差异。",
                    "citations": [
                        {
                            "story_id": str(item.story_id),
                            "claim_id": str(item.claim_id),
                            "evidence_span_id": str(item.evidence_span_id),
                        }
                        for item in unique_evidence
                    ],
                    "requirement_ids": [
                        requirement.identifier for requirement in requirements
                    ],
                    "dimension": dimension,
                    "time_semantic": None,
                }
            )
        yield json.dumps(
            {
                "answer": "\n".join(item["statement"] for item in support),
                "support": support,
            },
            ensure_ascii=False,
        )


def test_retrieval_fallback_is_explicit_and_keeps_supported_public_citations() -> None:
    openai = _hit("fallback-openai", claim_text="OpenAI 发布了预览版模型。")
    anthropic = _hit("fallback-anthropic", claim_text="Anthropic 发布了正式版模型。")
    retrieval = FixtureAcceptedKnowledge(
        {"OpenAI": openai, "Anthropic": anthropic},
        faults=(
            RetrievalFault(stage="embedding", code="embedding-unavailable"),
            RetrievalFault(stage="reranker", code="reranker-failed"),
        ),
    )
    metadata = FixtureEvidenceMetadata(
        {
            openai.evidence_span_id: ResearchEvidenceMetadata(
                evidence_role=EvidenceRole.PRIMARY,
                evidence_relation=EvidenceRelation.SUPPORTS,
                evidence_publisher="OpenAI",
            ),
            anthropic.evidence_span_id: ResearchEvidenceMetadata(
                evidence_role=EvidenceRole.INDEPENDENT,
                evidence_relation=EvidenceRelation.SUPPORTS,
                evidence_publisher="Independent Lab",
            ),
        }
    )
    provider = FullySupportedComparisonProvider()

    events = list(
        stream_research_events(
            "比较 OpenAI 和 Anthropic 在模型发布、融资方面的进展",
            repository=ResearchRepository(
                retrieval=retrieval,
                metadata_loader=metadata,
            ),
            provider=provider,
        )
    )

    states = [payload["state"] for event, payload in events if event == "status"]
    assert states == [
        "retrieving",
        "retrieval-degraded",
        "evidence-assembled",
        "generating",
        "verifying-citations",
    ]
    degraded = next(
        payload
        for event, payload in events
        if event == "status" and payload["state"] == "retrieval-degraded"
    )
    assert degraded["fallback"] == "fts-exact-entity-fusion"
    assert degraded["faults"] == [
        {"stage": "embedding", "code": "embedding-unavailable"},
        {"stage": "reranker", "code": "reranker-failed"},
    ]
    citations = [payload for event, payload in events if event == "citation"]
    assert [payload["evidence_role"] for payload in citations] == [
        "primary",
        "independent",
    ]
    assert all("chunk" not in json.dumps(payload).casefold() for payload in citations)
    assert events[-1][1]["status"] == "answered"
    assert len(provider.calls) == 1


class SequenceClock:
    def __init__(self, *values: float) -> None:
        self.values = iter(values)
        self.last = values[-1]

    def __call__(self) -> float:
        self.last = next(self.values, self.last)
        return self.last


class ManualClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class DeadlineAwareAcceptedKnowledge:
    def __init__(self, clock: ManualClock, hit: AcceptedKnowledgeHit) -> None:
        self.clock = clock
        self.hit = hit
        self.timeouts: list[float | None] = []

    def retrieve(self, query: RetrievalQuery) -> AcceptedKnowledgeResult:
        self.timeouts.append(query.timeout_seconds)
        self.clock.advance(20.0)
        return _result(query, self.hit)


class DeadlineAwareEvidenceMetadata:
    def __init__(self, clock: ManualClock) -> None:
        self.clock = clock
        self.timeouts: list[float | None] = []

    def load(
        self,
        evidence_span_ids: tuple[UUID, ...],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[UUID, ResearchEvidenceMetadata]:
        del evidence_span_ids
        self.timeouts.append(timeout_seconds)
        self.clock.advance(timeout_seconds or 0.0)
        return {}


def test_retrieval_and_metadata_share_the_hard_elapsed_time_budget() -> None:
    clock = ManualClock()
    hit = _hit("hard-deadline", claim_text="模型已经发布。")
    retrieval = DeadlineAwareAcceptedKnowledge(clock, hit)
    metadata = DeadlineAwareEvidenceMetadata(clock)
    provider = NeverCalledProvider()

    events = list(
        stream_research_events(
            "模型有什么更新？",
            repository=ResearchRepository(
                retrieval=retrieval,
                metadata_loader=metadata,
            ),
            provider=provider,
            clock=clock,
        )
    )

    assert retrieval.timeouts == [pytest.approx(45.0)]
    assert metadata.timeouts == [pytest.approx(25.0)]
    assert next(payload for event, payload in events if event == "error") == {
        "version": load_research_protocol().sse_contract_version,
        "code": "execution-budget-exceeded",
        "message": "Research 已达到执行时间上限，未生成答案。",
        "limit": "elapsed-time",
        "iterations": 1,
    }
    assert provider.calls == []


def test_multi_hop_stops_retrieval_and_provider_at_the_elapsed_time_budget() -> None:
    first_hop = _hit("timed-first", claim_text="第一跳有公开依据。")
    second_hop = _hit("timed-second", claim_text="第二跳也有公开依据。")
    retrieval = FixtureAcceptedKnowledge(
        {"OpenAI": first_hop, "开发者部署": second_hop}
    )
    provider = NeverCalledProvider()

    events = list(
        stream_research_events(
            "OpenAI 发布了 Orion；Orion 的新架构如何影响开发者部署？",
            repository=ResearchRepository(
                retrieval=retrieval,
                metadata_loader=FixtureEvidenceMetadata({}),
            ),
            provider=provider,
            clock=SequenceClock(0.0, 1.0, 46.0),
        )
    )

    assert len(retrieval.queries) == 1
    assert [event for event, _ in events] == ["status", "error", "done"]
    assert events[1][1]["code"] == "execution-budget-exceeded"
    assert events[1][1]["limit"] == "elapsed-time"
    assert provider.calls == []


def test_deepseek_retries_share_one_remaining_elapsed_time_budget() -> None:
    requests: list[httpx.Request] = []

    def retryable_failure(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503, request=request)

    evidence_set = ResearchEvidenceSet(
        question="Gemini 3.6 Flash 有什么更新？",
        evidence=(
            ResearchEvidence(
                story_id=_id("provider-budget:story"),
                story_stable_key="provider-budget-story",
                story_headline="Provider budget Story",
                claim_id=_id("provider-budget:claim"),
                claim_text="Gemini 3.6 Flash 已正式发布。",
                evidence_span_id=_id("provider-budget:evidence"),
                exact_text="Gemini 3.6 Flash is generally available.",
            ),
        ),
        provider_timeout_seconds=45.0,
    )
    with httpx.Client(transport=httpx.MockTransport(retryable_failure)) as client:
        provider = DeepSeekResearchProvider(
            client,
            api_key="fixture-provider-key",
            sleeper=lambda _: None,
            clock=SequenceClock(0.0, 1.0, 46.0),
        )
        with pytest.raises(ResearchBudgetExceeded, match="elapsed-time"):
            tuple(provider.stream(evidence_set))

    assert len(requests) == 1


class FailingAcceptedKnowledge:
    def __init__(self) -> None:
        self.calls = 0

    def retrieve(self, query: RetrievalQuery) -> AcceptedKnowledgeResult:
        self.calls += 1
        raise RuntimeError("forced accepted-knowledge retrieval failure")


def test_retrieval_failure_returns_structured_sse_without_calling_provider() -> None:
    retrieval = FailingAcceptedKnowledge()
    provider = NeverCalledProvider()

    events = list(
        stream_research_events(
            "Gemini 3.6 Flash 有什么更新？",
            repository=ResearchRepository(
                retrieval=retrieval,
                metadata_loader=FixtureEvidenceMetadata({}),
            ),
            provider=provider,
        )
    )

    assert [event for event, _ in events] == ["status", "error", "done"]
    assert events[1][1] == {
        "version": "research-sse-2026-08-15.v1",
        "code": "retrieval-failed",
        "message": "Research 检索失败，未生成答案。",
    }
    assert retrieval.calls == 1
    assert provider.calls == []
