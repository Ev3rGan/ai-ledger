# AI Ledger 学习指南

这是一条中文优先、保留必要英文术语的项目学习路线。它不按测试数量介绍项目，而是从读者能看到的产品出发，逐步连接到领域对象、代码边界和安全运行方式。

## 它解决的产品问题

AI Ledger 横跨采集、证据、编辑、发布与 Research。只看根 README 容易知道“它是什么”却不知道“为什么这样分层”；直接读代码又容易把 Candidate、Document、Story 和 Digest 混为一谈。本指南在两者之间提供稳定入口。

## 核心对象与术语

| 术语 | 在学习路线中的作用 |
| --- | --- |
| Product Loop | 从获批来源到带引用 Research 的完整价值链 |
| Domain Model | 为 Source Definition、Document Version、Story、Claim、Evidence Span 与 Digest 定义一致含义 |
| Code Entry | 能落到当前仓库的模块、CLI 或测试入口，不是未来设想 |
| Runbook | 承载详细配置、operator 流程、拓扑和验收步骤 |
| ADR | 记录已接受决策、tradeoff 与 revisit trigger |

## 数据或控制流

建议按顺序阅读：

1. [产品闭环](01-product-loop.md)：先看到输入、门禁、人工决策与公共输出。
2. [领域与数据模型](02-domain-and-data-model.md)：再理解这些阶段保存什么记录。
3. [仓库导览](03-repository-tour.md)：把职责映射到目录和模块。
4. [Agent/人类边界](04-agent-human-boundaries.md)：区分自动准备与授权决策。
5. [检索与 Research](05-retrieval-and-research.md)：理解当前检索以及 M4-M5 的计划边界。

## 真实代码入口

- 领域词汇从 [`CONTEXT.md`](../../CONTEXT.md) 与 [`domain.py`](../../src/ai_intel_agent/domain.py) 开始。
- 产品控制入口在 [`cli.py`](../../src/ai_intel_agent/cli.py)，公共页面入口在 [`web.py`](../../src/ai_intel_agent/web.py)。
- PostgreSQL 映射与 repository seam 在 [`persistence.py`](../../src/ai_intel_agent/persistence.py)。
- 详细运行边界由[本地 runbook](../mvp-local-runbook.md)与[生产 runbook](../mvp-production-runbook.md)负责。

## 如何本地运行或观察

先做不访问真实来源或 Provider 的导航观察：

```powershell
uv run ai-intel-agent --help
uv run --extra dev pytest tests/test_m1_product_repository_contract.py -q
```

需要启动本地产品时，按[本地 runbook](../mvp-local-runbook.md)准备隔离的本地 PostgreSQL 和进程配置；不要把生产数据库或凭据当作学习环境。
