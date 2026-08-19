# 02 · 领域与数据模型

## 它解决的产品问题

如果把网页、事件、摘要和证据都叫“文章”，系统就无法说明某句话来自哪个版本、是否被审核、何时进入 Digest。领域模型用不同对象保存 discovery、source artifact、event intelligence、evidence 与 publication state，使修订、拒绝和引用都能追溯。

## 核心对象与术语

| 层 | 核心对象 | 不应混淆 |
| --- | --- | --- |
| Acquisition | Candidate、Document、Document Version、Collection Run | Candidate 不是 Story；Document Version 不是编辑版本 |
| Intelligence | Story、Claim、Evidence Span、Evidence Role/Relation/State | Evidence Span 不是可重建的 retrieval Chunk |
| Editorial | Story Review State、Digest、Digest Revision、Audit Event | Digest 不是 Feed 或 Collection Run |
| Research | Query Intent、Evidence Set、Research Answer | Research Answer 不是无来源 Chat 输出 |

完整 ubiquitous language 以 [`CONTEXT.md`](../../CONTEXT.md) 为准。

## 数据或控制流

`Candidate → Document Version → Story → Claim → Evidence Span` 保存从线索到证据的 provenance。Story 经过 review 后才能进入 `Digest`；Digest 发布后，公共投影与 Research 才能读取它。`Collection Run`、per-source result、Source Profile state 与 cursor 记录采集控制面，`Audit Event` 记录编辑状态转换。

数据库中的 SQLAlchemy Record 是持久化映射；领域 dataclass 表达业务状态。不要把数据库 row、public projection 和领域对象当成同一层接口。

## 真实代码入口

- [`domain.py`](../../src/ai_intel_agent/domain.py)：枚举与不可变领域 dataclass。
- [`persistence.py`](../../src/ai_intel_agent/persistence.py)：SQLAlchemy Record、repository 与状态转换。
- [`alembic/versions/`](../../alembic/versions/)：schema 如何随产品切片演进。
- [`publication.py`](../../src/ai_intel_agent/publication.py)：内部记录到 bounded public projection 的转换。
- [`CONTEXT.md`](../../CONTEXT.md)：术语的定义、层次和 avoid-list。

## 如何本地运行或观察

先用领域和 publication 测试观察对象如何创建、持久化与过滤：

```powershell
uv run --extra dev pytest tests/test_sample_story.py tests/test_mvp_publication.py -q
```

这些测试只应连接测试隔离的数据库边界。若要手动查看本地状态，请先按[本地 runbook](../mvp-local-runbook.md)建立本地数据库，不要连接共享或生产数据库。
