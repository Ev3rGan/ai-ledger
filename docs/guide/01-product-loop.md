# 01 · 产品闭环

## 它解决的产品问题

公开 AI 信息很多，但“抓到一段文字”不等于形成可信情报。AI Ledger 需要回答三个问题：正文是否真的可用、结论是否能追溯到证据、内容是否经过明确的发布决策。产品闭环把这三个门禁串成读者最终看到的 Digest 与 Research。

## 核心对象与术语

| 对象 | 作用 |
| --- | --- |
| Source Definition | 领域中的获批发现入口及其采集策略 |
| Source Profile | 当前多来源 collector 的版本化配置；限定 host、path、策略与 cursor，并映射为 Source Definition |
| Candidate | 刚发现、尚未证明正文有效的线索 |
| Document Version | 某个 canonical location 在一次观察中的不可变正文快照 |
| Story / Claim / Evidence Span | 事件、可独立核验的陈述，以及来自特定 Document Version 的精确证据 |
| Digest | 由已接受 Story 组成并显式发布的每日内容 |
| Research Answer | 只基于已接受公共知识生成、带可追溯 citation 的回答 |

## 数据或控制流

| 阶段 | 主要门禁 | 产物 |
| --- | --- | --- |
| Discovery | Source Profile 边界、Feed 元数据、cursor | Candidate |
| Acquisition | canonical identity、访问约束、文章正文质量 | Document Version |
| Drafting | 有预算且版本锁定的 DeepSeek route；只生成草稿 | Story、Claim、Evidence Span 草稿 |
| Editorial | Operator 检查证据并接受/拒绝；预览排序后显式发布 | 已接受 Story、published Digest、Audit Event |
| Public projection | 只读取已发布知识 | Home、Digest、Story、Browse、RSS |
| Research | 只检索已接受且已发布的支持性 Evidence Span；无证据时拒答 | SSE answer 与 Story/Claim/Evidence Span citation |

一个来源失败只影响该来源的 collection result；Provider 失败不能把未验证内容推进为已接受知识。

## 真实代码入口

- [`multisource_collection.py`](../../src/ai_intel_agent/multisource_collection.py)：Source Profile、Feed discovery、article body gate、幂等 collection 与 draft boundary。
- [`editorial.py`](../../src/ai_intel_agent/editorial.py)：Story review、Digest composition/publication 与 audit event。
- [`publication.py`](../../src/ai_intel_agent/publication.py)：published-only 公共投影。
- [`web.py`](../../src/ai_intel_agent/web.py)：Home、Digest、Story、Browse、RSS 与 Research routes。
- [`research.py`](../../src/ai_intel_agent/research.py)：accepted-only retrieval、Provider validation、citation 与 refusal。

## 如何本地运行或观察

用确定性 fake 观察每个阶段，不接真实来源或 Provider：

```powershell
uv run --extra dev pytest tests/test_m2_multisource_collection.py tests/test_m3_digest.py tests/test_mvp_publication.py tests/test_mvp_research.py -q
```

需要看完整本地页面与 operator 流程时，遵循[本地 runbook](../mvp-local-runbook.md)；其中明确区分普通本地启动和需要额外授权的 live collection。
