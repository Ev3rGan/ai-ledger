from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

import ai_intel_agent.cli as cli_module
from ai_intel_agent.cli import app
from ai_intel_agent.research import (
    DeepSeekResearchProvider,
    ResearchBudgetExceeded,
    ResearchEvidenceSet,
)
from ai_intel_agent.research_provider_qualification import (
    load_research_provider_qualification_corpus,
    qualified_source_sha256,
    run_research_provider_qualification,
)

runner = CliRunner()


def test_qualified_source_identity_is_stable_and_content_sensitive(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.txt"
    nested = tmp_path / "nested" / "second.txt"
    nested.parent.mkdir()
    first.write_bytes(b"alpha\r\n")
    nested.write_text("beta", encoding="utf-8")
    paths = ("nested/second.txt", "first.txt")

    original = qualified_source_sha256(tmp_path, paths)
    reordered = qualified_source_sha256(tmp_path, tuple(reversed(paths)))
    first.write_bytes(b"alpha\n")
    normalized_line_endings = qualified_source_sha256(tmp_path, paths)
    nested.write_text("changed", encoding="utf-8")

    assert original == reordered
    assert original == normalized_line_endings
    assert len(original) == 64
    assert original != qualified_source_sha256(tmp_path, paths)


class AbstainingProvider:
    def stream(self, evidence_set: object) -> Iterator[str]:
        yield json.dumps({"answer": None, "citations": []})


class PassingMockProvider:
    last_returned_model_id = "deepseek-v4-pro"

    def __init__(self) -> None:
        self.calls = 0

    def stream(self, evidence_set: ResearchEvidenceSet) -> Iterator[str]:
        self.calls += 1
        question = evidence_set.question
        evidence = evidence_set.evidence[0]
        if "量子芯片" in question:
            payload: dict[str, object] = {"answer": None, "citations": []}
        else:
            answer = (
                "Hugging Face 发布了 WebGPU 内核库和 Fleet 浏览器基准工具。"
                if "已发布知识" in question
                else "Hugging Face 发布了 WebGPU 内核库。"
            )
            payload = {
                "answer": answer,
                "citations": [
                    {
                        "story_id": str(evidence.story_id),
                        "claim_id": str(evidence.claim_id),
                        "evidence_span_id": str(evidence.evidence_span_id),
                    }
                ],
            }
        yield json.dumps(payload, ensure_ascii=False)


def test_qualification_fails_when_provider_abstains_from_supported_case() -> None:
    qualification = run_research_provider_qualification(
        provider=AbstainingProvider(),
        revision="a" * 40,
        qualified_source_sha256="1" * 64,
        now=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
    )

    assert qualification.status == "failed"
    supported = next(
        result
        for result in qualification.results
        if result.case_identifier == "hugging-face-published-facts"
    )
    assert supported.expected_status == "answered"
    assert supported.observed_status == "refused"
    assert supported.failure_code == "abstention-mismatch"


def test_mocked_provider_can_never_produce_release_qualification() -> None:
    provider = PassingMockProvider()

    qualification = run_research_provider_qualification(
        provider=provider,
        revision="b" * 40,
        qualified_source_sha256="2" * 64,
        execution_mode="mocked-provider",
        now=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
    )
    payload = qualification.as_dict()

    assert provider.calls == 7
    assert qualification.status == "non-qualifying"
    assert qualification.qualified_source_sha256 == "2" * 64
    assert all(result.passed for result in qualification.results)
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "谁发布了" not in serialized
    assert "Evidence" not in serialized
    assert "Hugging Face 发布了 WebGPU 内核库。" not in serialized


def test_live_pr_qualification_binds_the_qualified_source_content() -> None:
    qualification = run_research_provider_qualification(
        provider=PassingMockProvider(),
        revision="e" * 40,
        qualified_source_sha256="f" * 64,
        execution_mode="live-provider",
        now=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
    )

    assert qualification.status == "passed"
    assert qualification.commit_sha == "e" * 40
    assert qualification.qualified_source_sha256 == "f" * 64
    assert qualification.as_dict()["qualified_source_sha256"] == "f" * 64
    assert "target_kind" not in qualification.as_dict()


def test_live_qualification_command_fails_closed_without_deepseek_credential(
    tmp_path: Path,
) -> None:
    output = tmp_path / "qualification.json"

    result = runner.invoke(
        app,
        [
            "evaluate-research-provider",
            "--revision",
            "c" * 40,
            "--output",
            str(output),
        ],
        env={"DEEPSEEK_API_KEY": "", "DEEPSEEK_API_KEY_FILE": ""},
    )

    assert result.exit_code == 2
    assert "DEEPSEEK_API_KEY" in result.output
    assert not output.exists()


def test_live_adapter_rejects_qualification_input_over_its_cost_bound() -> None:
    case = load_research_provider_qualification_corpus().cases[0]

    def unexpected_request(_: httpx.Request) -> httpx.Response:
        pytest.fail("over-budget qualification input reached the Provider")

    with httpx.Client(transport=httpx.MockTransport(unexpected_request)) as client:
        provider = DeepSeekResearchProvider(
            client,
            api_key="fixture-deepseek-key",
            maximum_input_tokens=1,
        )

        with pytest.raises(ResearchBudgetExceeded, match="provider-input"):
            list(
                provider.stream(
                    ResearchEvidenceSet(question=case.question, evidence=case.evidence)
                )
            )


def test_live_qualification_command_writes_only_safe_sha_bound_results(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "qualification.json"
    provider = PassingMockProvider()
    monkeypatch.setattr(
        cli_module,
        "DeepSeekResearchProvider",
        lambda *args, **kwargs: provider,
    )

    result = runner.invoke(
        app,
        [
            "evaluate-research-provider",
            "--revision",
            "d" * 40,
            "--output",
            str(output),
        ],
        env={"DEEPSEEK_API_KEY": "fixture-secret", "DEEPSEEK_API_KEY_FILE": ""},
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(output.read_text(encoding="utf-8"))
    corpus = load_research_provider_qualification_corpus()
    expected_source_sha = qualified_source_sha256(
        Path(__file__).parents[1], corpus.qualified_source_paths
    )
    assert payload["status"] == "passed"
    assert payload["execution_mode"] == "live-provider"
    assert payload["commit_sha"] == "d" * 40
    assert payload["qualified_source_sha256"] == expected_source_sha
    assert "target_kind" not in payload
    assert payload["protocol_sha256"]
    assert payload["corpus_sha256"]
    assert payload["maximum_provider_attempts"] == 14
    assert payload["worst_case_reserved_cost_usd"] <= 0.10
    serialized = output.read_text(encoding="utf-8")
    assert "fixture-secret" not in serialized
    assert "谁发布了" not in serialized
    assert "Hugging Face 发布了 WebGPU 内核库。" not in serialized


def test_provider_facing_pull_requests_run_one_protected_live_gate() -> None:
    project_root = Path(__file__).parents[1]
    workflow = (
        project_root / ".github" / "workflows" / "provider-qualification.yml"
    ).read_text(encoding="utf-8")
    deterministic_ci = (project_root / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "workflow_call:" in workflow
    assert "workflow_dispatch:" in workflow
    assert "pull_request_number:" in workflow
    assert "pull/$pullRequestNumber/head" in workflow
    assert "environment: provider-acceptance" in workflow
    assert "secrets.DEEPSEEK_API_KEY" in workflow
    assert "evaluate-research-provider" in workflow
    assert "schedule:" not in workflow
    assert "merged-revision" not in workflow
    assert "push:" not in workflow
    assert "uses: ./.github/workflows/provider-qualification.yml" in deterministic_ci
    assert "github.event.pull_request.head.sha" in deterministic_ci
    assert "name: provider-acceptance" in deterministic_ci
    assert "git diff --name-only" in deterministic_ci
    assert "git diff --quiet" not in deterministic_ci
    qualified_paths = set(
        load_research_provider_qualification_corpus().qualified_source_paths
    )
    assert {
        "src/ai_intel_agent/data/model_routing_evaluation.v1.json",
        "src/ai_intel_agent/data/model_routing_protocol.v1.json",
    } <= qualified_paths
    for qualified_path in qualified_paths:
        assert qualified_path in deterministic_ci
    assert "deterministic tests (mocked providers only)" in deterministic_ci
