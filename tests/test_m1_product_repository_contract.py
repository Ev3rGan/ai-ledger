import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_DEMO_URL = "https://bench-tencent-hk.ai-ledger.cn/"
MILESTONE_ISSUES = (70, 71, 72, 73, 74)
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
                    "## 🌱 Why AI Ledger",
                    "## ✨ What works today",
                    "## 🚀 Explore the product",
                    "## ⚡ Run a deterministic sample",
                    "## 🧭 How information becomes public knowledge",
                    "## 🛡️ Trust and operating boundaries",
                    "## 📚 Documentation",
                    "## 🧪 Development",
                    "## License",
                ),
            ),
        "README.zh-CN.md": (
            "[English](README.md)",
                "[简体中文](README.zh-CN.md)",
                (
                    "## 🌱 为什么需要 AI Ledger",
                    "## ✨ 当前可用能力",
                    "## 🚀 体验产品",
                    "## ⚡ 运行确定性样例",
                    "## 🧭 信息如何成为公共知识",
                    "## 🛡️ 信任与运行边界",
                    "## 📚 文档",
                    "## 🧪 开发",
                    "## License",
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
        assert readme.count("```") == 8

    assert _markdown_link_targets(_read("README.md")) == _markdown_link_targets(
        _read("README.zh-CN.md")
    )


def test_bilingual_milestone_links_defer_current_health_to_ci() -> None:
    english = _read("README.md")
    chinese = _read("README.zh-CN.md")
    capability_sections = (
        _section(english, "## ✨ What works today", "## 🚀 Explore the product"),
        _section(chinese, "## ✨ 当前可用能力", "## 🚀 体验产品"),
    )

    for issue_number in MILESTONE_ISSUES:
        issue_url = f"https://github.com/Ev3rGan/ai-ledger/issues/{issue_number}"
        for section in capability_sections:
            assert issue_url in section

    english_capabilities, chinese_capabilities = capability_sections
    assert "M1–M5 product scopes and release records" in english_capabilities
    assert "M1–M5 的产品范围与发布记录" in chinese_capabilities
    assert "Current build health is reported by [CI]" in english_capabilities
    assert "当前构建健康度以 [CI]" in chinese_capabilities
    for section in capability_sections:
        assert "M1-M4" not in section
        assert "[x]" not in section.casefold()
        assert "~~" not in section


def test_bilingual_sample_quickstart_states_its_postgres_boundary() -> None:
    english = _read("README.md")
    chinese = _read("README.zh-CN.md")

    assert "persists its publication to the configured PostgreSQL database" in english
    assert "持久化到已配置的 PostgreSQL" in chinese
    for readme in (english, chinese):
        assert "AI_INTEL_DATABASE_URL" in readme
        assert "uv run ai-intel-agent run --sample --output reports\\daily.md" in readme


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
