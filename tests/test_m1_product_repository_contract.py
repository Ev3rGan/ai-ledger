import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_DEMO_URL = "https://bench-tencent-hk.ai-ledger.cn/"
ROADMAP_ISSUES = {
    70: "Repository productization and design-decision archive",
    71: "Focused source portfolio",
    72: "Editorial Agent Digest Plan",
    73: "MiniLM Hybrid Retrieval and mMARCO",
    74: "Comparison, timeline, and multi-hop Research",
}
GUIDE_PATHS = (
    "docs/guide/README.md",
    "docs/guide/01-product-loop.md",
    "docs/guide/02-domain-and-data-model.md",
    "docs/guide/03-repository-tour.md",
    "docs/guide/04-agent-human-boundaries.md",
    "docs/guide/05-retrieval-and-research.md",
)


def _read(relative_path: str) -> str:
    return (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")


def _section(document: str, heading: str, next_heading: str) -> str:
    start = document.index(heading)
    end = document.index(next_heading, start)
    return document[start:end]


def _markdown_link_targets(document: str) -> tuple[str, ...]:
    return tuple(
        match.group("target")
        for match in re.finditer(r"(?<!!)\[[^\]]+\]\((?P<target>[^)]+)\)", document)
    )


def test_bilingual_readmes_share_stable_product_portal_structure() -> None:
    readmes = {
        "README.md": (
            "[English](README.md)",
            "[简体中文](README.zh-CN.md)",
            (
                "## Product Loop",
                "## Public Surfaces",
                "## What Works Today",
                "## Roadmap",
                "## Learn the Project",
                "## Documentation Map",
                "## Repository Tree",
                "## Quick Start",
                "## Scope and Safety",
            ),
        ),
        "README.zh-CN.md": (
            "[English](README.md)",
            "[简体中文](README.zh-CN.md)",
            (
                "## 产品闭环",
                "## 公共页面",
                "## 当前可用能力",
                "## 路线图",
                "## 学习本项目",
                "## 文档地图",
                "## 仓库结构",
                "## 快速开始",
                "## 范围与安全边界",
            ),
        ),
    }

    for relative_path, (english_link, chinese_link, headings) in readmes.items():
        readme = _read(relative_path)
        assert english_link in readme
        assert chinese_link in readme
        assert PUBLIC_DEMO_URL in readme
        assert tuple(readme.index(heading) for heading in headings) == tuple(
            sorted(readme.index(heading) for heading in headings)
        )
        assert readme.count("```") == 2

    assert _markdown_link_targets(_read("README.md")) == _markdown_link_targets(
        _read("README.zh-CN.md")
    )


def test_bilingual_roadmaps_link_the_same_issues_and_tell_status_truth() -> None:
    english = _read("README.md")
    chinese = _read("README.zh-CN.md")
    roadmap_sections = (
        _section(english, "## Roadmap", "## Learn the Project"),
        _section(chinese, "## 路线图", "## 学习本项目"),
    )

    for issue_number, title in ROADMAP_ISSUES.items():
        issue_url = f"https://github.com/Ev3rGan/ai-ledger/issues/{issue_number}"
        for roadmap in roadmap_sections:
            assert issue_url in roadmap
            assert title in roadmap

    english_roadmap, chinese_roadmap = roadmap_sections
    assert (
        "Integrated release candidate; #74 owns exact-SHA production acceptance"
        in english_roadmap
    )
    assert (
        "Integrated release candidate；#74 负责 exact-SHA production acceptance"
        in chinese_roadmap
    )
    assert english_roadmap.count("| Delivered |") == 4
    assert chinese_roadmap.count("| 已交付 |") == 4
    assert (
        "M5 is not production accepted until the exact merged SHA completes the acceptance "
        "owned by #74."
        in english_roadmap
    )
    assert (
        "M5 在 #74 完成 exact merged SHA 验收前，不得表述为 production accepted。"
        in chinese_roadmap
    )
    for roadmap in roadmap_sections:
        assert "[x]" not in roadmap.casefold()
        assert "~~" not in roadmap


def test_learning_guide_and_documentation_map_are_complete() -> None:
    required_guide_sections = (
        "## 它解决的产品问题",
        "## 核心对象与术语",
        "## 数据或控制流",
        "## 真实代码入口",
        "## 如何本地运行或观察",
    )
    docs_index = _read("docs/README.md")
    docs_headings = (
        "## Learning Guide",
        "## Product Operations",
        "## Architecture and Policy",
        "## Research and Evaluation",
        "## Archive",
    )

    assert tuple(docs_index.index(heading) for heading in docs_headings) == tuple(
        sorted(docs_index.index(heading) for heading in docs_headings)
    )
    assert "[Learning Guide](guide/README.md)" in docs_index

    for relative_path in GUIDE_PATHS:
        path = REPOSITORY_ROOT / relative_path
        assert path.is_file()
        guide = path.read_text(encoding="utf-8")
        assert all(section in guide for section in required_guide_sections)


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

    for relative_path in required_paths:
        path = REPOSITORY_ROOT / relative_path
        assert path.is_file()

    readme = _read("README.md")
    for relative_path in required_paths[:4]:
        assert relative_path in readme

    for relative_path in required_paths[4:]:
        decision = _read(relative_path)
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
        content = _read(relative_path).casefold()
        assert all(phrase not in content for phrase in forbidden_current_portfolio_phrases)
