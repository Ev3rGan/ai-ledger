[English](README.md) | [简体中文](README.zh-CN.md)

# AI Ledger

一份可追溯证据的 AI 日报，把获批的公开来源转化为经审核的 Digest 与带引用的 Research。

**[打开正式 Public Demo](https://bench-tencent-hk.ai-ledger.cn/)**

## 产品闭环

**获批来源** → **采集 + 正文门禁** → **DeepSeek 草稿** → **人类/Agent 编辑边界** → **Digest + 公共知识** → **带引用的 Research**

采集与草稿生成有明确边界并可追溯。Operator 保留直接审核 Story 与发布 Digest 的控制权。采用辅助编排时，Editorial Agent 生成一份完整、版本化、不可变的 Digest Plan；operator 一次批准这份 exact plan，批准事务接受其中纳入的 Story 并发布未改动的 Digest。Agent 不会自行发布，也不能修改已批准计划。

生产 Scheduler 在 **Asia/Shanghai 每日 06:00 与 18:00** 运行。它自动采集符合条件的新材料并准备可追溯草稿，但不自动发布；只有 operator 审阅并批准一份 exact Digest Plan 后，新的公开日报才会出现。

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

M1-M4 能力已部署；可用状态与代码树集成状态分开记录。

| 能力 | 产品边界 | 可用状态 |
| --- | --- | --- |
| 版本化来源组合 | 八个核心 Profile 加十个有边界的补充 Profile，逐来源记录角色、故障隔离、正文或结构化数据门禁、cursor 与可重放 operation key；机器之心在取得正式授权前保持停用 | 已部署（M2） |
| 可追溯草稿 | 获批的 DeepSeek route 生成 Story、Claim 与精确 Evidence Span 草稿，但不能接受或发布 | 已部署 |
| 编辑批准 | Operator 可直接审核，也可一次批准 Agent 生成的不可变 Digest Plan；Agent 不自动发布，每次批准都绑定 exact plan | 已部署（M3） |
| 已接受知识 Hybrid | pgvector 中的 MiniLM 向量与 PostgreSQL FTS、exact Entity candidates 共同 Fusion，再交给唯一 mMARCO reranker；模型不可用时显式回退 | 已部署（M4） |
| 高级 Research | Query Intent 区分 simple lookup、comparison、timeline 与 bounded multi-hop；entity/dimension 或语义子问题 Evidence Set 相互隔离，严格时间语义与引用校验 fail closed，有界编排通过 SSE 输出进度且不暴露 hidden reasoning | 已部署（M5） |
| 公共投影 | Home、Digest、Story、Browse、RSS 与当前可用的 Research surface 只暴露已发布知识，不暴露 operator 控件或 hidden reasoning | 已部署；高级 Research 以上述可用状态为准 |
| 可复现运维 | 锁定的本地与生产 runbook 定义启动、迁移、状态、备份、恢复、回滚与验收边界 | 已部署 |

## 运行模型

| 职责 | 自动化部分 | 人类边界 |
| --- | --- | --- |
| 采集与起草 | Scheduler 每日采集两次；Adapter 执行来源策略，DeepSeek 准备可追溯草稿 | Operator 调查降级来源，并决定是否需要一次有边界的重试 |
| 编排与发布 | Editorial Agent 提出一份包含排序、摘要、Topic、排除项和异常标记的完整计划 | 一次明确批准只发布该不可变计划；存在 blocking anomaly 时禁止发布 |
| 回答 | Research Agent 只检索已接受知识，执行有界编排，验证每个实质性引用，并拒绝证据不足的问题 | 读者决定问题；Agent 不能实时浏览公网，也不能静默扩大范围 |
| 运维 | status、health、backup、restore-isolated、restart、upgrade 与 rollback 均有已提交的命令边界 | 发布锁定 exact merge SHA 与 immutable image digest；秘密和破坏性恢复仍由 operator 控制 |

## 路线图

| 里程碑 | 范围 | 状态 |
| --- | --- | --- |
| [#70 M1](https://github.com/Ev3rGan/ai-ledger/issues/70) | Repository productization and design-decision archive | 已交付 |
| [#71 M2](https://github.com/Ev3rGan/ai-ledger/issues/71) | Focused source portfolio | 已交付 |
| [#72 M3](https://github.com/Ev3rGan/ai-ledger/issues/72) | Editorial Agent Digest Plan | 已交付 |
| [#73 M4](https://github.com/Ev3rGan/ai-ledger/issues/73) | MiniLM Hybrid Retrieval and mMARCO | 已交付 |
| [#74 M5](https://github.com/Ev3rGan/ai-ledger/issues/74) | Comparison, timeline, and multi-hop Research | 已交付 |

五个里程碑均已跨过发布门禁，并在官方演示站点可用。

## 学习本项目

中文优先的 [Learning Guide](docs/guide/README.md) 把产品闭环连接到领域对象、真实代码与安全的本地观察方式。

| 章节 | 回答的问题 |
| --- | --- |
| [01 · 产品闭环](docs/guide/01-product-loop.md) | 公开信息如何变成带引用的回答？ |
| [02 · 领域与数据模型](docs/guide/02-domain-and-data-model.md) | 哪些记录保存 provenance 与发布状态？ |
| [03 · 仓库导览](docs/guide/03-repository-tour.md) | 每项职责位于哪里？ |
| [04 · Agent/人类边界](docs/guide/04-agent-human-boundaries.md) | 自动化可以准备什么，什么必须审批？ |
| [05 · 检索与 Research](docs/guide/05-retrieval-and-research.md) | 当前 Hybrid retrieval 与高级 Research 如何保持可引用和有边界？ |

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

Apache-2.0
