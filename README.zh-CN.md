[English](README.md) | [简体中文](README.zh-CN.md)

# AI Ledger

一份可追溯证据的 AI 日报，把获批的公开来源转化为经审核的 Digest 与带引用的 Research。

**[打开正式 Public Demo](https://bench-tencent-hk.ai-ledger.cn/)**

## 产品闭环

**获批来源** → **采集 + 正文门禁** → **DeepSeek 草稿** → **人类/Agent 编辑边界** → **Digest + 公共知识** → **带引用的 Research**

采集与草稿生成有明确边界并可追溯。当前由 operator 逐条接受或拒绝 Story，并显式发布 Digest。计划中的 Editorial Agent 只能准备一份完整 Digest Plan，管理员须一次批准这份精确计划；Agent 不会自行发布。

## 公共页面

| 页面 | 路由 | 读者看到什么 |
| --- | --- | --- |
| Home | `/` | 最新已发布 Digest、重点、来源覆盖与近期版本 |
| Digest | `/digests/<date>` | 一份由已接受 Story 组成并经审核的每日内容 |
| Story | `/stories/<stable-key>` | Claim、精确 Evidence Span 与原始来源链接 |
| Browse | `/browse` | 按关键词、Publisher、Topic 或日期筛选的已发布 Story |
| RSS | `/rss` 与 `/rss.xml` | 订阅说明页与机器可读 Feed |
| Research | `/research` | 仅基于已接受知识、带可点击引用的回答，或明确拒答 |

## 当前可用能力

| 能力 | 当前 v2 行为 |
| --- | --- |
| 有边界的采集 | 版本化 Source Profile、来源故障隔离、canonical identity、文章正文质量门禁、cursor 与可重放 operation key |
| 可追溯草稿 | 获批的 DeepSeek route 生成 Story、Claim 与精确 Evidence Span 草稿，但不能接受或发布 |
| 编辑控制 | Operator 检查草稿、接受或拒绝 Story、预览有序 Digest，并通过 audit event 显式发布 |
| 公共知识 | Home、Digest、Story、Browse、RSS 与仅用已接受知识的 Research 不暴露 operator 控件 |
| 安全 Research | 有边界的检索引用 Story/Claim/Evidence Span 身份；不支持的问题或无效 Provider 输出会 fail closed |
| 可复现运维 | 锁定的本地与生产 runbook 定义启动、迁移、状态、备份、恢复、回滚与验收边界 |

## 路线图

| 里程碑 | 范围 | 状态 |
| --- | --- | --- |
| [#70 M1](https://github.com/Ev3rGan/ai-ledger/issues/70) | Repository productization and design-decision archive | 实现与审查已完成；Git、发布与生产验收待完成 |
| [#71 M2](https://github.com/Ev3rGan/ai-ledger/issues/71) | Focused source portfolio | 计划中 |
| [#72 M3](https://github.com/Ev3rGan/ai-ledger/issues/72) | Editorial Agent Digest Plan | 计划中 |
| [#73 M4](https://github.com/Ev3rGan/ai-ledger/issues/73) | MiniLM Hybrid Retrieval and mMARCO | 计划中 |
| [#74 M5](https://github.com/Ev3rGan/ai-ledger/issues/74) | Comparison, timeline, and multi-hop Research | 计划中 |

只有完成某里程碑 exact merged SHA 的生产验收后，才能把它标记为完成；仅实现或审查完成不等于发布完成。

## 学习本项目

中文优先的 [Learning Guide](docs/guide/README.md) 把产品闭环连接到领域对象、真实代码与安全的本地观察方式。

| 章节 | 回答的问题 |
| --- | --- |
| [01 · 产品闭环](docs/guide/01-product-loop.md) | 公开信息如何变成带引用的回答？ |
| [02 · 领域与数据模型](docs/guide/02-domain-and-data-model.md) | 哪些记录保存 provenance 与发布状态？ |
| [03 · 仓库导览](docs/guide/03-repository-tour.md) | 每项职责位于哪里？ |
| [04 · Agent/人类边界](docs/guide/04-agent-human-boundaries.md) | 自动化可以准备什么，什么必须审批？ |
| [05 · 检索与 Research](docs/guide/05-retrieval-and-research.md) | 当前能力是什么，M4-M5 将改变什么？ |

## 文档地图

| 区域 | 从这里开始 |
| --- | --- |
| 产品与学习 | [文档总索引](docs/README.md) · [Learning Guide](docs/guide/README.md) |
| 运行与操作 | [本地 runbook](docs/mvp-local-runbook.md) · [生产 runbook](docs/mvp-production-runbook.md) |
| 架构与决策 | [领域模型](CONTEXT.md) · [ADR 索引](docs/adr/README.md) |
| 研究与评测 | [Research 索引](docs/research/README.md) |
| 历史 provenance | [Archive 索引](docs/archive/README.md) |

## 仓库结构

- `src/ai_intel_agent/` — runtime package、CLI、采集、编辑、发布、Web 与 Research
- `alembic/` — 版本化 PostgreSQL schema migration
- `tests/` — 确定性的产品、策略与仓库契约
- `docs/guide/` — 从产品行为走向代码的学习路线
- `docs/adr/` — 已接受的架构与路线图决策
- `docs/research/` 与 `docs/archive/` — 次级研究材料与历史证据
- `deploy/` 与 `docker/` — runbook 描述的部署和本地数据库边界

## 快速开始

先按[本地 runbook](docs/mvp-local-runbook.md)注入仅属于进程的配置：

```powershell
uv sync --locked --python 3.12 --extra ch3
uv run ai-intel-agent start-local
```

Operator 命令、拓扑、验证与停止方式都由 runbook 承载；根 README 有意保持稳定与高度概括。

## 范围与安全边界

公开仓库记录接口与决策，不记录密钥、私密对话、hidden reasoning 或生产敏感值。真实来源、Provider、数据库、部署和生产验收操作都需要独立授权。

## License

MIT
