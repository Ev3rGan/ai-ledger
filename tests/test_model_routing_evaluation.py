from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from ai_intel_agent.cli import app
from ai_intel_agent.model_routing_evaluation import (
    CRITICAL_GATES,
    EVALUATION_VERSION,
    TASK_CLASSES,
    CandidateTaskSummary,
    HttpModelEvaluationClient,
    ModelEvaluationConfigurationError,
    ModelEvaluationCredentials,
    ProviderResponse,
    TokenUsage,
    estimate_cost_usd,
    load_candidate_configuration,
    load_evaluation_corpus,
    load_protocol_configuration,
    maximum_request_cost_usd,
    run_model_routing_evaluation,
    score_response,
    select_task_routes,
)


def _valid_output(case_identifier: str) -> str:
    case = next(
        item
        for item in load_evaluation_corpus().cases
        if item.identifier == case_identifier
    )
    answers = {
        "classification-research-01": "",
        "chinese-summary-architecture-01": (
            "该应用以 PostgreSQL 为系统记录，使用 pgvector 和 PostgreSQL 全文检索做混合检索，"
            "通过模型网关接入 DeepSeek 与 Kimi，并部署在香港。"
        ),
        "claim-verification-live-web-01": (
            "该说法与证据矛盾：公共研究仅限已接受知识，提供商实时网络搜索已禁用。"
        ),
        "simple-question-editorial-window-01": (
            "摘要编辑窗口从前一天 06:00 开始，到发布当天 05:59 结束。"
        ),
        "simple-question-abstention-01": (
            "现有证据未提供每月匿名研究请求额度，因此无法确定。"
        ),
        "complex-reasoning-routing-policy-01": (
            "路由必须由评测驱动：DeepSeek 是经济型默认候选，Kimi 是质量与跨供应商挑战者。"
            "只有通过所有关键门槛的候选才能入选；廉价候选若失败就必须排除，不能用低成本补偿。"
        ),
    }
    return json.dumps(
        {
            "answer": answers[case_identifier],
            "label": case.gold.expected_label,
            "verdict": case.gold.expected_verdict,
            "abstained": case.gold.expected_abstained,
            "citations": list(case.gold.required_citations),
            "facts": case.gold.expected_facts,
        },
        ensure_ascii=False,
    )


def _approved_corpus():
    return load_evaluation_corpus()


class PassingClient:
    def __init__(self, *, finish_reason: str = "stop") -> None:
        self.calls: list[tuple[str, str]] = []
        self.finish_reason = finish_reason

    def complete(self, candidate, case) -> ProviderResponse:
        self.calls.append((candidate.identifier, case.identifier))
        return ProviderResponse(
            content=_valid_output(case.identifier),
            returned_model_id=candidate.model_id + "-returned",
            finish_reason=self.finish_reason,
            usage=TokenUsage(
                input_tokens=500,
                cached_input_tokens=100,
                output_tokens=100,
            ),
            latency_ms=25,
        )


def test_fixed_corpus_has_exact_human_approval_and_source_provenance() -> None:
    corpus = load_evaluation_corpus()

    assert corpus.version == EVALUATION_VERSION
    assert corpus.review_state == "human-approved"
    assert corpus.approved_by == "Ev3rGan"
    assert corpus.approved_at == "2026-08-13T10:51:08+08:00"
    assert len(corpus.content_sha256) == 64
    assert len(corpus.cases_sha256) == 64
    assert corpus.approved_cases_sha256 == corpus.cases_sha256
    assert corpus.cases_sha256 == (
        "8de5085eb331ce7ef869467583018a76598caa296bf3ce7221e76e7d2f616ca7"
    )
    assert {source.url.rsplit("/", 1)[-1] for source in corpus.approval_sources} == {
        "1",
        "5",
    }
    assert {case.task_class for case in corpus.cases} == set(TASK_CLASSES)
    assert len(corpus.cases) == 6
    assert any(case.gold.expected_abstained for case in corpus.cases)
    assert all(case.critical_gates == CRITICAL_GATES for case in corpus.cases)
    assert all(evidence.source_url for case in corpus.cases for evidence in case.context)


def test_protocol_versions_prompt_schema_retries_and_budgets() -> None:
    protocol = load_protocol_configuration()

    assert protocol.version.startswith("model-routing-protocol-")
    assert protocol.prompt_version.startswith("model-routing-prompt-")
    assert protocol.schema_version.startswith("model-routing-output-schema-")
    assert len(protocol.content_sha256) == 64
    assert "{output_schema}" in protocol.system_prompt_template
    assert "{fact_keys}" in protocol.user_prompt_template
    assert all(
        field["description"]
        for field in protocol.output_schema["fields"].values()
    )
    assert protocol.retry_policy.max_attempts == 2
    assert protocol.retry_policy.backoff_seconds == (0.5,)
    assert protocol.budget_policy.maximum_evaluation_cost_usd == 0.25
    assert protocol.budget_policy.monthly_external_api_cap_usd == 100.0
    assert protocol.route_rank_by[:3] == (
        "quality_score descending",
        "estimated_cost_usd ascending",
        "median_latency_ms ascending",
    )
    assert set(protocol.task_maximum_output_tokens) == set(TASK_CLASSES)


def test_contradictory_answer_cannot_pass_by_repeating_expected_terms() -> None:
    case = next(
        item
        for item in load_evaluation_corpus().cases
        if item.identifier == "chinese-summary-architecture-01"
    )
    raw_output = json.loads(_valid_output(case.identifier))
    raw_output["answer"] = (
        "系统没有采用 PostgreSQL，但仍提到 pgvector、全文检索、DeepSeek、Kimi 和香港。"
    )

    score = score_response(case, json.dumps(raw_output, ensure_ascii=False))

    assert score.gates["structure"].passed is True
    assert score.gates["factual"].passed is False
    assert "rejection pattern" in score.gates["factual"].detail
    assert score.passed_all_critical_gates is False


@pytest.mark.parametrize(
    "answer",
    [
        "该说法与证据矛盾：公共请求不允许使用实时网络，只能使用已接受知识。",
        "公共研究仅限已接受知识，公共请求不允许直接联网，因此该说法不成立。",
    ],
)
def test_claim_verification_accepts_explicitly_negated_live_web_permission(
    answer: str,
) -> None:
    case = next(
        item
        for item in load_evaluation_corpus().cases
        if item.identifier == "claim-verification-live-web-01"
    )
    raw_output = json.loads(_valid_output(case.identifier))
    raw_output["answer"] = answer

    score = score_response(case, json.dumps(raw_output, ensure_ascii=False))

    assert score.gates["factual"].passed is True
    assert score.passed_all_critical_gates is True


def test_claim_verification_rejects_positive_live_web_permission() -> None:
    case = next(
        item
        for item in load_evaluation_corpus().cases
        if item.identifier == "claim-verification-live-web-01"
    )
    raw_output = json.loads(_valid_output(case.identifier))
    raw_output["answer"] = "公共研究可以使用实时网络，现有证据支持该说法。"

    score = score_response(case, json.dumps(raw_output, ensure_ascii=False))

    assert score.gates["factual"].passed is False
    assert score.passed_all_critical_gates is False


@pytest.mark.parametrize(
    "answer",
    [
        (
            "路由必须由评测驱动，DeepSeek 是经济型默认候选，Kimi 是质量与跨供应商挑战者。"
            "候选必须通过所有关键门槛，不能只按价格选择。"
        ),
        (
            "路由必须由评测驱动，DeepSeek 是经济型默认候选，Kimi 是质量与跨供应商挑战者。"
            "候选必须通过所有关键门槛，不应仅依据成本选择。"
        ),
    ],
)
def test_complex_reasoning_accepts_rejection_of_price_only_routing(
    answer: str,
) -> None:
    case = next(
        item
        for item in load_evaluation_corpus().cases
        if item.identifier == "complex-reasoning-routing-policy-01"
    )
    raw_output = json.loads(_valid_output(case.identifier))
    raw_output["answer"] = answer

    score = score_response(case, json.dumps(raw_output, ensure_ascii=False))

    assert score.gates["factual"].passed is True
    assert score.passed_all_critical_gates is True


def test_complex_reasoning_rejects_price_only_routing() -> None:
    case = next(
        item
        for item in load_evaluation_corpus().cases
        if item.identifier == "complex-reasoning-routing-policy-01"
    )
    raw_output = json.loads(_valid_output(case.identifier))
    raw_output["answer"] = (
        "路由应由评测驱动并比较 DeepSeek 和 Kimi，但为了经济性可以只按价格选择，"
        "不必要求候选通过所有关键门槛。"
    )

    score = score_response(case, json.dumps(raw_output, ensure_ascii=False))

    assert score.gates["factual"].passed is False
    assert score.passed_all_critical_gates is False


def test_fact_values_treat_conjunction_and_comma_as_equivalent() -> None:
    case = next(
        item
        for item in load_evaluation_corpus().cases
        if item.identifier == "chinese-summary-architecture-01"
    )
    raw_output = json.loads(_valid_output(case.identifier))
    raw_output["facts"]["model_providers"] = "DeepSeek, Kimi"

    score = score_response(case, json.dumps(raw_output, ensure_ascii=False))

    assert score.gates["factual"].passed is True
    assert score.passed_all_critical_gates is True


def test_fact_values_reject_an_extra_provider_after_comma_normalization() -> None:
    case = next(
        item
        for item in load_evaluation_corpus().cases
        if item.identifier == "chinese-summary-architecture-01"
    )
    raw_output = json.loads(_valid_output(case.identifier))
    raw_output["facts"]["model_providers"] = "DeepSeek, Kimi, OpenAI"

    score = score_response(case, json.dumps(raw_output, ensure_ascii=False))

    assert score.gates["factual"].passed is False
    assert score.passed_all_critical_gates is False


def test_fact_values_accept_bounded_context_qualifiers() -> None:
    case = next(
        item
        for item in load_evaluation_corpus().cases
        if item.identifier == "claim-verification-live-web-01"
    )
    raw_output = json.loads(_valid_output(case.identifier))
    raw_output["facts"] = {
        "knowledge_boundary": "public Research restricted to accepted knowledge",
        "provider_web_search": "disabled for public requests",
    }

    score = score_response(case, json.dumps(raw_output, ensure_ascii=False))

    assert score.gates["factual"].passed is True
    assert score.passed_all_critical_gates is True


def test_missing_fact_accepts_an_explicit_empty_canonical_value() -> None:
    case = next(
        item
        for item in load_evaluation_corpus().cases
        if item.identifier == "simple-question-abstention-01"
    )
    raw_output = json.loads(_valid_output(case.identifier))
    raw_output["facts"]["monthly_anonymous_request_allowance"] = ""

    score = score_response(case, json.dumps(raw_output, ensure_ascii=False))

    assert score.gates["factual"].passed is True
    assert score.passed_all_critical_gates is True


def test_every_case_requires_citation_and_abstention_gates() -> None:
    case = load_evaluation_corpus().cases[0]
    raw_output = json.loads(_valid_output(case.identifier))
    raw_output["citations"] = []
    raw_output["abstained"] = True

    score = score_response(case, json.dumps(raw_output, ensure_ascii=False))

    assert tuple(score.gates) == CRITICAL_GATES
    assert score.gates["factual"].passed is True
    assert score.gates["citation"].passed is False
    assert score.gates["abstention"].passed is False
    assert score.passed_all_critical_gates is False


def test_strict_output_schema_requires_canonical_facts() -> None:
    case = load_evaluation_corpus().cases[0]
    raw_output = json.loads(_valid_output(case.identifier))
    raw_output.pop("facts")

    score = score_response(case, json.dumps(raw_output))

    assert score.gates["structure"].passed is False
    assert score.quality_score == 0


def test_bounded_fact_values_accept_equivalent_order_and_qualifiers() -> None:
    case = next(
        item
        for item in load_evaluation_corpus().cases
        if item.identifier == "simple-question-editorial-window-01"
    )
    raw_output = json.loads(_valid_output(case.identifier))
    raw_output["facts"] = {
        "window_start": "06:00 on the previous day",
        "window_end": "05:59 on the publication day",
    }

    score = score_response(case, json.dumps(raw_output, ensure_ascii=False))

    assert score.gates["factual"].passed is True
    assert score.passed_all_critical_gates is True


def test_bounded_fact_values_reject_partial_keyword_matches() -> None:
    case = next(
        item
        for item in load_evaluation_corpus().cases
        if item.identifier == "claim-verification-live-web-01"
    )
    raw_output = json.loads(_valid_output(case.identifier))
    raw_output["facts"]["knowledge_boundary"] = "knowledge"

    score = score_response(case, json.dumps(raw_output, ensure_ascii=False))

    assert score.gates["factual"].passed is False
    assert score.passed_all_critical_gates is False


@pytest.mark.parametrize("contradiction", ["not disabled", "disabled but enabled"])
def test_bounded_fact_values_reject_negated_or_conflicting_facts(
    contradiction: str,
) -> None:
    case = next(
        item
        for item in load_evaluation_corpus().cases
        if item.identifier == "claim-verification-live-web-01"
    )
    raw_output = json.loads(_valid_output(case.identifier))
    raw_output["facts"]["provider_web_search"] = contradiction

    score = score_response(case, json.dumps(raw_output, ensure_ascii=False))

    assert score.gates["factual"].passed is False
    assert score.passed_all_critical_gates is False


@pytest.mark.parametrize(
    ("fact_name", "contradiction"),
    [
        ("knowledge_boundary", "accepted knowledge only or live web"),
        ("provider_web_search", "disabled but permitted"),
    ],
)
def test_bounded_fact_values_reject_extra_conflicting_alternatives(
    fact_name: str,
    contradiction: str,
) -> None:
    case = next(
        item
        for item in load_evaluation_corpus().cases
        if item.identifier == "claim-verification-live-web-01"
    )
    raw_output = json.loads(_valid_output(case.identifier))
    raw_output["facts"][fact_name] = contradiction

    score = score_response(case, json.dumps(raw_output, ensure_ascii=False))

    assert score.gates["factual"].passed is False
    assert score.passed_all_critical_gates is False


def test_only_candidates_passing_every_gate_are_selectable() -> None:
    failing = CandidateTaskSummary(
        candidate_identifier="cheap-but-failing",
        task_class="classification",
        passed_all_critical_gates=False,
        quality_score=99.0,
        median_latency_ms=1,
        estimated_cost_usd=0.000001,
        cases_passed=0,
        cases_total=1,
    )
    passing = replace(
        failing,
        candidate_identifier="eligible",
        passed_all_critical_gates=True,
        quality_score=80.0,
        median_latency_ms=20,
        estimated_cost_usd=0.01,
        cases_passed=1,
    )

    routes = select_task_routes((failing, passing))

    assert routes["classification"] == passing
    assert routes["complex_reasoning"] is None


def test_equal_quality_route_prefers_cost_before_latency() -> None:
    baseline = CandidateTaskSummary(
        candidate_identifier="cheap-slower",
        task_class="classification",
        passed_all_critical_gates=True,
        quality_score=100.0,
        median_latency_ms=50,
        estimated_cost_usd=0.001,
        cases_passed=1,
        cases_total=1,
    )
    faster_expensive = replace(
        baseline,
        candidate_identifier="fast-expensive",
        median_latency_ms=10,
        estimated_cost_usd=0.01,
    )

    routes = select_task_routes((faster_expensive, baseline))

    assert routes["classification"] == baseline


def test_candidate_configuration_uses_current_provider_endpoints_and_models() -> None:
    configuration = load_candidate_configuration()

    assert {candidate.provider for candidate in configuration.candidates} == {
        "deepseek",
        "kimi",
    }
    assert {candidate.model_id for candidate in configuration.candidates} == {
        "deepseek-v4-flash",
        "deepseek-v4-pro",
        "kimi-k2.6",
    }
    assert all(candidate.maximum_output_tokens == 4096 for candidate in configuration.candidates)
    assert next(
        item for item in configuration.candidates if item.provider == "kimi"
    ).base_url == "https://api.moonshot.cn/v1"


def test_credentials_follow_candidate_environment_variable_configuration() -> None:
    configuration = load_candidate_configuration()
    credentials = ModelEvaluationCredentials.from_environment(
        configuration=configuration,
        environment={
            "DEEPSEEK_API_KEY": "deepseek-secret",
            "KIMI_API_KEY": "kimi-secret",
        },
    )

    for candidate in configuration.candidates:
        expected = "kimi-secret" if candidate.provider == "kimi" else "deepseek-secret"
        assert credentials.for_candidate(candidate) == expected

    with pytest.raises(ModelEvaluationConfigurationError, match="KIMI_API_KEY"):
        ModelEvaluationCredentials.from_environment(
            configuration=configuration,
            environment={"DEEPSEEK_API_KEY": "present"},
        )


def test_cost_estimate_normalizes_native_provider_prices_to_usd() -> None:
    candidate = next(
        item
        for item in load_candidate_configuration().candidates
        if item.provider == "kimi"
    )

    estimate = estimate_cost_usd(
        candidate,
        TokenUsage(input_tokens=1_000_000, cached_input_tokens=250_000, output_tokens=100_000),
    )

    expected_native = 0.25 * 1.1 + 0.75 * 6.5 + 0.1 * 27.0
    assert estimate.total_native == pytest.approx(expected_native)
    assert estimate.total_usd == pytest.approx(expected_native / 7.2)


def test_http_boundary_uses_versioned_contract_and_normalizes_usage() -> None:
    configuration = load_candidate_configuration()
    protocol = load_protocol_configuration()
    candidate = configuration.candidates[0]
    case = load_evaluation_corpus().cases[0]
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers["Authorization"]
        seen["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "returned-model",
                "choices": [
                    {
                        "message": {"content": _valid_output(case.identifier)},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 50,
                    "prompt_cache_hit_tokens": 20,
                    "completion_tokens": 10,
                },
            },
        )

    credentials = ModelEvaluationCredentials(
        values={"DEEPSEEK_API_KEY": "secret", "KIMI_API_KEY": "other"}
    )
    client = HttpModelEvaluationClient(
        credentials=credentials,
        protocol=protocol,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    response = client.complete(candidate, case)
    payload = seen["payload"]

    assert seen["authorization"] == "Bearer secret"
    assert isinstance(payload, dict)
    assert payload["max_tokens"] == 512
    assert protocol.prompt_version in payload["messages"][0]["content"]
    assert protocol.schema_version in payload["messages"][0]["content"]
    assert "primary_topic" in payload["messages"][1]["content"]
    assert response.returned_model_id == "returned-model"
    assert response.usage == TokenUsage(50, 20, 10)
    assert response.attempts == 1


def test_http_boundary_retries_versioned_transient_statuses() -> None:
    configuration = load_candidate_configuration()
    protocol = load_protocol_configuration()
    candidate = configuration.candidates[0]
    case = load_evaluation_corpus().cases[0]
    sleeps: list[float] = []
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, json={"error": "temporary"})
        return httpx.Response(
            200,
            json={
                "model": candidate.model_id,
                "choices": [
                    {
                        "message": {"content": _valid_output(case.identifier)},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    client = HttpModelEvaluationClient(
        credentials=ModelEvaluationCredentials(
            values={"DEEPSEEK_API_KEY": "secret", "KIMI_API_KEY": "other"}
        ),
        protocol=protocol,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=sleeps.append,
    )

    response = client.complete(candidate, case)

    assert response.attempts == 2
    assert calls == 2
    assert sleeps == [0.5]


def test_worst_case_budget_is_checked_before_any_provider_call(tmp_path: Path) -> None:
    protocol = load_protocol_configuration()
    constrained = replace(
        protocol,
        budget_policy=replace(
            protocol.budget_policy,
            maximum_evaluation_cost_usd=0.000001,
        ),
    )
    client = PassingClient()

    with pytest.raises(ModelEvaluationConfigurationError, match="worst-case"):
        run_model_routing_evaluation(
            tmp_path / "report.md",
            client=client,
            corpus=_approved_corpus(),
            protocol=constrained,
        )

    assert client.calls == []


def test_standalone_evaluation_reports_all_metrics_and_versions(tmp_path: Path) -> None:
    client = PassingClient()
    output = tmp_path / "model-routing.md"

    evaluation = run_model_routing_evaluation(
        output,
        client=client,
        corpus=_approved_corpus(),
        now=datetime(2026, 8, 12, tzinfo=UTC),
    )
    report = output.read_text(encoding="utf-8")

    assert len(client.calls) == 18
    assert len(evaluation.measurements) == 18
    assert all(evaluation.recommendations[task] is not None for task in TASK_CLASSES)
    assert evaluation.estimated_actual_cost_usd > 0
    assert evaluation.worst_case_reserved_usd <= evaluation.maximum_evaluation_cost_usd
    assert "Corpus SHA-256" in report
    assert "Evaluation protocol version" in report
    assert "Evaluation protocol SHA-256" in report
    assert "Prompt version" in report
    assert "Output schema version" in report
    assert "Worst-case reserved cost" in report
    assert "structure=PASS" in report
    assert "citation=PASS" in report
    assert "deepseek-v4-flash-returned" in report
    assert "initial project-specific route smoke evaluation" in report
    assert "each have one case; simple questions have two" in report
    assert "not general complex-reasoning ability" in report
    assert "该应用以 PostgreSQL" not in report


def test_non_stop_finish_reason_fails_structure_gate(tmp_path: Path) -> None:
    evaluation = run_model_routing_evaluation(
        tmp_path / "report.md",
        client=PassingClient(finish_reason="length"),
        corpus=_approved_corpus(),
    )

    assert all(
        not measurement.score.gates["structure"].passed
        for measurement in evaluation.measurements
    )
    assert all(route is None for route in evaluation.recommendations.values())


def test_maximum_request_cost_uses_task_and_retry_protocol() -> None:
    candidate = load_candidate_configuration().candidates[0]
    case = load_evaluation_corpus().cases[0]
    protocol = load_protocol_configuration()

    single_request = maximum_request_cost_usd(candidate, case, protocol)

    assert single_request > 0
    assert single_request * protocol.retry_policy.max_attempts < 0.01


def test_live_evaluation_refuses_unapproved_corpus_before_provider_calls(
    tmp_path: Path,
) -> None:
    client = PassingClient()
    corpus = load_evaluation_corpus()
    unapproved_corpus = replace(
        corpus,
        review_state="awaiting-human-approval",
        approved_by=None,
        approved_at=None,
        approved_cases_sha256=None,
    )

    with pytest.raises(ModelEvaluationConfigurationError, match="administrator approval"):
        run_model_routing_evaluation(
            tmp_path / "report.md",
            client=client,
            corpus=unapproved_corpus,
        )

    assert client.calls == []


def test_cli_exposes_standalone_evaluation_command() -> None:
    result = CliRunner().invoke(app, ["evaluate-model-routes", "--help"])

    assert result.exit_code == 0
    assert "versioned routing evaluation" in result.stdout
