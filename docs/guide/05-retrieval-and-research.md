# 05 · 检索与 Research

## 它解决的产品问题

Research 不能把相似文本直接包装成答案：它必须只使用已接受、已发布且可引用的证据，并在证据不足时拒答。同时，未来的跨语言、精确实体和比较类问题需要比当前 lexical retrieval 更强的召回与排序能力。

## 核心对象与术语

| 术语 | 当前或计划中的含义 |
| --- | --- |
| Evidence Set | 当前从 published Digest 中的 accepted Story、Claim 和支持性 Evidence Span 组装的有界输入 |
| PostgreSQL FTS | 当前 production path 的 lexical retrieval |
| SSE contract | `status`、answer delta、citation、refusal/error 与 `done` 的公共流式边界 |
| MiniLM Hybrid | M4 计划：lexical、semantic 与 exact-entity candidates 的 Fusion，不是当前 live retrieval |
| mMARCO reranker | M4 计划中唯一获批的 reranker；不满足资源/健康门禁时显式回退到无 reranker Fusion |
| Comparison/timeline/multi-hop | M5 计划的结构化 Query Intent 与有预算执行，不是当前能力 |

## 数据或控制流

当前流是：`question → PostgreSQL FTS → accepted + published supporting Evidence Spans → bounded Evidence Set → approved DeepSeek Research route → schema/citation validation → SSE answer`。没有 Evidence Span、Provider 不可用、额度耗尽或输出验证失败都会 fail closed；`Evidence Role = Community` 的 Evidence Span 不用于建立事实回答。

M4 只改变 retrieval/ranking seam：`lexical + MiniLM semantic + exact Entity → weighted Fusion of Chunks → optional sole mMARCO rerank → top-k ranked Chunks`。它不会放宽 accepted-only、published-only、citation 或 refusal 边界。模型决策与 fallback 见 [ADR 0010](../adr/0010-minilm-mmarco-retrieval.md)。

## 真实代码入口

- [`research.py`](../../src/ai_intel_agent/research.py)：当前 FTS query、accepted/published filter、Provider validation、citation 与 refusal。
- [`web.py`](../../src/ai_intel_agent/web.py)：`/research` 与 `/research/answer` SSE surface。
- [`retrieval_calibration.py`](../../src/ai_intel_agent/retrieval_calibration.py)：fixed-corpus calibration、Chunk/Fusion/profile 与 human-approval guard。
- [`retrieval_profile.v1.json`](../../src/ai_intel_agent/data/retrieval_profile.v1.json)：当前已策展的 retrieval profile 证据；不等于 M4 runtime 已上线。
- [ADR 0010](../adr/0010-minilm-mmarco-retrieval.md)：MiniLM、mMARCO 与 explicit fallback 的 decision/tradeoff/revisit trigger。

## 如何本地运行或观察

用确定性测试区分“当前 Research contract”和“未来 retrieval profile”，不调用真实 Provider：

```powershell
uv run --extra dev pytest tests/test_mvp_research.py tests/test_retrieval_calibration.py -q
uv run ai-intel-agent calibrate-retrieval --help
```

公共 Research 的浏览器验收、真实 Provider 计数和 allowance 证明属于[本地 runbook](../mvp-local-runbook.md)或[生产 runbook](../mvp-production-runbook.md)定义的授权阶段，不应在普通学习或文档验证中执行。
