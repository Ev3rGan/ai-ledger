# 04 · Agent 与人类边界

## 它解决的产品问题

模型适合把正文整理成结构化候选，但不应自行把候选变成公共事实或决定每日版面。AI Ledger 把“生成建议”和“授权发布”分开，使 Provider 故障、提示变化或编辑分歧不会绕过审核。

## 核心对象与术语

| 对象/角色 | 权限边界 |
| --- | --- |
| Draft Provider | 从 body-valid Document Version 准备 Story/Claim/Evidence Span 草稿；不能 accept 或 publish |
| Operator | 检查 Document Version 正文和 Evidence Span，接受或拒绝 Story，决定 Digest 顺序与 introduction，并显式发布 |
| Audit Event | 记录 actor、action、subject、时间与状态变化 |
| Editorial Agent（M3 计划） | 只能生成一份完整、版本化、不可变的 Digest Plan |
| Administrator（M3 计划） | 一次批准 exact plan；计划改变必须重新批准 |

## 数据或控制流

当前控制流是：`body-valid Document Version → Provider draft → unreviewed Story → operator inspect → accept/reject → Digest preview → explicit publish → public projection`。Provider 输出即使结构正确也只停在 `unreviewed`。

M3 计划把它收敛为 `eligible unreviewed Stories → Editorial Agent → complete Digest Plan → one administrator approval → accept the exact plan and publish`。这一次批准同时接受计划中选定的 Story 内容并发布未改动的 Digest；Agent 不自动发布，不逐条申请零散批准，也不能在批准后暗改计划。该边界记录在 [ADR 0007](../adr/0007-editorial-approval-boundary.md)。

## 真实代码入口

- [`multisource_collection.py`](../../src/ai_intel_agent/multisource_collection.py)：`DraftProvider` protocol 及草稿边界。
- [`editorial.py`](../../src/ai_intel_agent/editorial.py)：review state、Digest preview/publication 与 Audit Event。
- [`persistence.py`](../../src/ai_intel_agent/persistence.py)：持久化 review 与 publish transaction。
- [`cli.py`](../../src/ai_intel_agent/cli.py)：`story list/show/accept/reject` 和 `digest preview/publish` operator surface。
- [ADR 0007](../adr/0007-editorial-approval-boundary.md)：未来 Digest Plan 的完整决策记录。

## 如何本地运行或观察

命令帮助不会连接数据库或 Provider，可先观察人工动作的输入契约；确定性 Digest 测试验证状态门禁：

```powershell
uv run ai-intel-agent story accept --help
uv run ai-intel-agent digest publish --help
uv run --extra dev pytest tests/test_m3_digest.py -q
```

真正执行 accept/publish 前，必须按[本地 runbook](../mvp-local-runbook.md)确认使用同一 commit 与隔离数据库。生产操作只遵循[生产 runbook](../mvp-production-runbook.md)和独立授权。
