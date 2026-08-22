# 04 · Agent 与人类边界

## 它解决的产品问题

模型适合把正文整理成结构化候选，但不应自行把候选变成公共事实或决定每日版面。AI Ledger 把“生成建议”和“授权发布”分开，使 Provider 故障、提示变化或编辑分歧不会绕过审核。

## 核心对象与术语

| 对象/角色 | 权限边界 |
| --- | --- |
| Draft Provider | 从 body-valid Document Version 准备 Story/Claim/Evidence Span 草稿；不能 accept 或 publish |
| Editorial Agent | 为一个 publication date 生成一份完整、版本化、不可变的 Digest Plan；不能 accept 或 publish |
| Digest Plan | 固定 included/excluded/held Stories、顺序、文案、Topics、排除理由与 anomaly flags 的审批对象 |
| Administrative operator | 检查 exact plan 并执行一次显式批准；直接 Story review/publish 控件仍由 operator 掌握 |
| Audit Event | 记录 actor、action、subject、时间与状态变化，包括绑定 exact plan 的批准 |

## 数据或控制流

草稿控制流是：`body-valid Document Version → Provider draft → unreviewed Story`。Provider 输出即使结构正确也只停在 `unreviewed`，operator 仍可逐条 inspect、accept/reject、preview 和显式 publish。

当前辅助编排流是：`eligible unreviewed Stories → Editorial Agent → persisted immutable Digest Plan → operator inspects exact version → one explicit approval → accept included Stories + publish unchanged Digest`。批准事务只接受该计划固定的内容；blocking anomaly、过期版本或内容变化都要求新计划/新批准。Agent 不自动发布，不逐条申请零散批准，也不输出或持久化 hidden reasoning。该边界记录在 [ADR 0007](../adr/0007-editorial-approval-boundary.md)。

## 真实代码入口

- [`multisource_collection.py`](../../src/ai_intel_agent/multisource_collection.py)：`DraftProvider` protocol 及草稿边界。
- [`editorial.py`](../../src/ai_intel_agent/editorial.py)：`DigestPlan`、`EditorialPlanProvider`、plan preparation、anomaly 与内容哈希契约。
- [`persistence.py`](../../src/ai_intel_agent/persistence.py)：不可变 plan/approval records，以及绑定 exact plan 的原子 `approve_digest_plan` transaction。
- [`cli.py`](../../src/ai_intel_agent/cli.py)：`story` 直接审核命令与 `digest plan prepare/show/approve` operator 子组。
- [ADR 0007](../adr/0007-editorial-approval-boundary.md)：当前“一份计划、一次显式批准”的决策记录。

## 如何本地运行或观察

命令帮助不会连接数据库或 Provider，可先观察人工动作的输入契约；确定性 Digest 测试验证状态门禁：

```powershell
uv run ai-intel-agent story accept --help
uv run ai-intel-agent digest plan prepare --help
uv run ai-intel-agent digest plan show --help
uv run ai-intel-agent digest plan approve --help
uv run --extra dev pytest tests/test_m3_digest.py -q
```

真正执行 accept/publish 前，必须按[本地 runbook](../mvp-local-runbook.md)确认使用同一 commit 与隔离数据库。生产操作只遵循[生产 runbook](../mvp-production-runbook.md)和独立授权。
