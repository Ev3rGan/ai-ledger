# 03 · 仓库导览

## 它解决的产品问题

产品闭环跨越 CLI、领域规则、持久化、Web 与文档。如果不知道职责在哪一层，修复一个页面问题可能误改采集规则，研究实验也可能被误认为生产能力。本章提供从用户入口到实现与证据的地图。

## 核心对象与术语

| 路径 | 主要职责 |
| --- | --- |
| `src/ai_intel_agent/` | 可执行 package；CLI、collection、editorial、publication、Web、Research |
| `src/ai_intel_agent/data/` | 版本化 Source Profile、Provider protocol、retrieval corpus/profile 等资源 |
| `alembic/` | PostgreSQL schema migration 与 sole-head 演进 |
| `tests/` | 业务规则、故障路径、Web/CLI 与 repository contract |
| `docs/guide/`、`docs/adr/` | 学习入口与 accepted decision |
| `docs/research/`、`docs/archive/` | 次级研究资料与历史 provenance，不是当前 runtime 配置 |
| `docker/`、`deploy/` | 由 runbook 管理的本地数据库与发布边界 |

## 数据或控制流

用户或 scheduler 从 [`cli.py`](../../src/ai_intel_agent/cli.py) 进入；CLI 调用 collection/editorial/runtime service，service 使用 [`domain.py`](../../src/ai_intel_agent/domain.py) 与 [`persistence.py`](../../src/ai_intel_agent/persistence.py)；[`web.py`](../../src/ai_intel_agent/web.py) 通过 publication/research repository 读取 bounded public view。版本化 JSON 资源约束来源、模型与 protocol，Alembic 约束数据库形状，tests 验证跨层契约。

文档采用分层导航：根 README 是稳定海报，guide 解释概念，runbook 承载操作，ADR 解释决策，research/archive 保存证据。

## 真实代码入口

- [`cli.py`](../../src/ai_intel_agent/cli.py)：`ai-intel-agent` 及 operator/story/digest 子命令。
- [`multisource_collection.py`](../../src/ai_intel_agent/multisource_collection.py)：多来源 collection slice。
- [`editorial.py`](../../src/ai_intel_agent/editorial.py) 与 [`publication.py`](../../src/ai_intel_agent/publication.py)：审核、发布和公共读模型。
- [`runtime.py`](../../src/ai_intel_agent/runtime.py)：本地/服务生命周期、scheduler 与配置边界。
- [`tests/`](../../tests/)：从文件名定位相应产品 seam 的确定性验证。

## 如何本地运行或观察

先看 CLI surface，再运行仓库入口契约：

```powershell
uv run ai-intel-agent --help
uv run --extra dev pytest tests/test_m1_product_repository_contract.py -q
```

需要了解某条命令的环境、side effect 和停止方式时，不要从函数名推断；查阅[本地 runbook](../mvp-local-runbook.md)或[生产 runbook](../mvp-production-runbook.md)。
