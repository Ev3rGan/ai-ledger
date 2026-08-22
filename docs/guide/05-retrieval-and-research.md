# 05 · 检索与 Research

## 它解决的产品问题

Research 不能把相似文本直接包装成答案：它只使用已接受、已发布且可引用的 supporting Evidence，并在支持缺失、deadline 耗尽或 Provider 输出无效时 fail closed。当前 Hybrid retrieval 同时覆盖跨语言表达、lexical matching 与 exact technical Entity；高级 Research 在同一 visibility/citation 边界上处理 lookup、comparison、timeline 与 bounded multi-hop。

## 核心对象与术语

| 术语 | 当前含义 |
| --- | --- |
| Query Intent | 记录 task type、entities、time range/semantic、accepted-knowledge scope 与 iterations/retrieval/evidence/time/output budgets |
| Evidence Set | 从 published Digest 中 accepted Story、Claim 与 supporting Evidence Span 组装；同时记录 requirement support、缺失支持、Evidence Role 与时间语义 |
| Accepted-knowledge Hybrid | PostgreSQL FTS、pgvector 中的 MiniLM semantic candidates 与 exact Entity candidates 进入确定性 Fusion |
| mMARCO reranker | 唯一获批的 query-time reranker；普通加载/推理故障回退到原 Fusion 顺序，deadline exhaustion 不回退 |
| SSE contract | `status`、validated answer delta、可点击 citation、refusal/error 与 `done`；不公开 hidden reasoning |
| Advanced task types | simple lookup、entity×dimension comparison、五类时间语义 timeline，以及至少两步的 bounded multi-hop |

## 数据或控制流

当前流是：`question → Query Intent → bounded Research state graph → one or more accepted-knowledge Hybrid calls → Evidence Set + missing-support check → sole approved DeepSeek route → schema/statement/citation validation → SSE answer`。comparison 把每个 entity×requested dimension 绑定到具体 Evidence identities；timeline 为每条 statement 保留 event、source publication、discovery、editorial 或 Digest publication 语义；multi-hop 缺少中间 Evidence 时明确拒答。

Chunks 只是可重建的检索产物，公共引用始终回到 Story/Claim/Evidence Span。普通 Embedding/Reranker 故障可使用显式 deterministic fallback；unsupported decomposition、缺失支持、retrieval failure、Provider failure 与 hard-budget exhaustion 都不会输出未经验证的 token 或 citation。当前范围不包含 live Web、Provider web-search tools、多 Provider 路由、conflict/counter-evidence search 或 durable anonymous memory。模型决策与 fallback 见 [ADR 0010](../adr/0010-minilm-mmarco-retrieval.md)。

## 真实代码入口

- [`accepted_knowledge.py`](../../src/ai_intel_agent/accepted_knowledge.py)：Chunk/index generation、FTS/pgvector/exact-Entity candidates、Fusion、mMARCO、visibility 与 fallback。
- [`research.py`](../../src/ai_intel_agent/research.py)：Query Intent、bounded state graph、Evidence Set、time semantics、Provider/citation validation 与 refusal。
- [`web.py`](../../src/ai_intel_agent/web.py)：`/research` 与 `/research/answer` SSE surface。
- [`accepted_knowledge_retrieval.v1.json`](../../src/ai_intel_agent/data/accepted_knowledge_retrieval.v1.json)：当前 pinned artifacts、Chunk/Fusion/rerank/top-k runtime contract。
- [`research_protocol.v1.json`](../../src/ai_intel_agent/data/research_protocol.v1.json)：当前 route、SSE schema 与 hard execution budgets。
- [ADR 0010](../adr/0010-minilm-mmarco-retrieval.md)：MiniLM、mMARCO 与 explicit fallback 的当前 decision/tradeoff/revisit trigger。

## 已归档的 Gate 证据

- [Multilingual Retrieval Profile calibration](../research/multilingual-retrieval-calibration-2026-08-13.md)：固定双语 corpus 上的 MiniLM、Chunk 与 Fusion calibration 基线。
- [DeepSeek and Kimi Model Routing Evaluation](../research/model-routing-evaluation-2026-08-13.md)：固定、人工批准 cases 上的 route、citation 与 abstention gate 证据。

这些报告是可复现的历史 Gate evidence，不是当前 integrated release candidate 的 production acceptance。

## 如何本地运行或观察

用确定性测试观察当前 Hybrid/Research contract，不调用真实 Provider：

```powershell
uv run --extra dev pytest tests/test_m5_advanced_research.py tests/test_mvp_research.py tests/test_m4_hybrid_retrieval.py -q
uv run ai-intel-agent calibrate-retrieval --help
```

公共 Research 的浏览器验收、真实 Provider 计数和 allowance 证明属于[本地 runbook](../mvp-local-runbook.md)或[生产 runbook](../mvp-production-runbook.md)定义的授权阶段，不应在普通学习或文档验证中执行。
