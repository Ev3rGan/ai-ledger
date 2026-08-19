from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_readme_leads_from_product_to_operation_and_architecture() -> None:
    readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")

    headings = (
        "## What the product does",
        "## What readers see",
        "## Run it locally",
        "## Operator workflow",
        "## Architecture and decisions",
        "## Research, evaluation, and historical evidence",
    )
    positions = tuple(readme.index(heading) for heading in headings)

    assert positions == tuple(sorted(positions))


def test_repository_wayfinding_and_decision_records_are_complete() -> None:
    required_paths = (
        "docs/README.md",
        "docs/research/README.md",
        "docs/archive/README.md",
        "docs/adr/README.md",
        "docs/adr/0007-editorial-approval-boundary.md",
        "docs/adr/0008-source-portfolio-boundary.md",
        "docs/adr/0009-event-level-semantic-deduplication.md",
        "docs/adr/0010-minilm-mmarco-retrieval.md",
    )
    required_decision_sections = (
        "## Context",
        "## Alternatives",
        "## Decision",
        "## Accepted tradeoff",
        "## Revisit trigger",
    )

    readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
    for relative_path in required_paths:
        path = REPOSITORY_ROOT / relative_path
        assert path.is_file()
        assert relative_path in readme or relative_path.startswith("docs/adr/000")

    for relative_path in required_paths[4:]:
        decision = (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")
        assert all(section in decision for section in required_decision_sections)


def test_current_runtime_and_runbooks_do_not_advertise_retired_source_profile() -> None:
    current_product_paths = (
        "docs/mvp-local-runbook.md",
        "docs/mvp-production-runbook.md",
        "src/ai_intel_agent/data/source_profiles.v1.json",
        "src/ai_intel_agent/multisource_collection.py",
        "src/ai_intel_agent/cli.py",
    )

    forbidden_current_portfolio_phrases = (
        "aibusiness",
        "ai business",
        "five source",
        "five-source",
        "five profile",
        "five-profile",
    )

    for relative_path in current_product_paths:
        content = (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8").casefold()
        assert all(phrase not in content for phrase in forbidden_current_portfolio_phrases)


def test_readme_operator_examples_match_the_supported_cli_contract() -> None:
    readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")

    assert "--summary" in readme
    assert "--why-it-matters" in readme
    assert "--topic" in readme
    assert "--story" in readme
    assert "--introduction" in readme
    assert "--reason" not in readme
