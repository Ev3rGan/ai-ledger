from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from ai_intel_agent.cli import app

runner = CliRunner()

FIRST_WAVE_SOURCE_DEFINITIONS = (
    "OpenAI News",
    "Anthropic News",
    "Google AI",
    "Google DeepMind",
    "Meta AI",
    "Microsoft Research",
    "NVIDIA Generative AI",
    "Hugging Face Blog",
    "DeepSeek Changelog",
    "Qwen Blog",
    "GitHub AI and ML",
    "GitHub Changelog",
    "arXiv AI queries",
    "Curated GitHub Releases",
    "Machine Heart",
    "Qbitai",
)

REQUIRED_FINDINGS = (
    "Conclusion",
    "Entry point",
    "Language",
    "Topic scope",
    "Extraction adapter",
    "Cursor",
    "Health policy",
    "Robots findings",
    "Terms findings",
    "Storage policy",
    "Public excerpt policy",
    "Pause conditions",
)


def test_source_audit_cli_produces_complete_deterministic_report(tmp_path: Path) -> None:
    output_path = tmp_path / "source-activation-audit.md"

    first = runner.invoke(app, ["audit-sources", "--output", str(output_path)])
    first_report = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
    second = runner.invoke(app, ["audit-sources", "--output", str(output_path)])

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert output_path.read_text(encoding="utf-8") == first_report
    assert "Audited 16 first-wave Source Definitions" in first.output
    assert (
        "Conclusions: approved=6, metadata-only=5, blocked=2, needs-verification=3" in first_report
    )

    sections = first_report.split("\n## ")[1:]
    assert [section.partition("\n")[0] for section in sections] == list(
        FIRST_WAVE_SOURCE_DEFINITIONS
    )
    assert "maximum source excerpt: 280 Unicode characters" in first_report
    assert "maximum source excerpt: disabled" in first_report
    assert all(
        all(f"- {finding}:" in section for finding in REQUIRED_FINDINGS) for section in sections
    )
