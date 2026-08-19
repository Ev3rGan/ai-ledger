[English](README.md) | [简体中文](README.zh-CN.md)

# AI Ledger

An evidence-grounded AI daily that turns approved public sources into reviewed Digests and cited Research.

**[Open the official Public Demo](https://bench-tencent-hk.ai-ledger.cn/)**

## Product Loop

**Approved sources** → **collection + article-body gate** → **DeepSeek draft** → **human/Agent editorial boundary** → **Digest + public knowledge** → **cited Research**

Collection and drafting are bounded and traceable. Today, an operator accepts or rejects each Story and explicitly publishes each Digest. The planned Editorial Agent may prepare one complete Digest Plan, but an administrator must approve that exact plan once; the Agent never publishes by itself.

## Public Surfaces

| Surface | Route | What readers get |
| --- | --- | --- |
| Home | `/` | The latest published Digest, highlights, coverage, and recent editions |
| Digest | `/digests/<date>` | One reviewed daily composition of accepted Stories |
| Story | `/stories/<stable-key>` | Claims, exact Evidence Spans, and original-source links |
| Browse | `/browse` | Published Stories filtered by keyword, publisher, Topic, or date |
| RSS | `/rss` and `/rss.xml` | A subscription page and machine-readable feed |
| Research | `/research` | Accepted-knowledge answers with clickable citations or an explicit refusal |

## What Works Today

| Capability | Current v2 behavior |
| --- | --- |
| Bounded acquisition | Versioned Source Profiles, isolated source failures, canonical identity, article-body quality gates, cursors, and replay-safe operation keys |
| Traceable drafting | The approved DeepSeek route prepares Story, Claim, and exact Evidence Span drafts but cannot accept or publish them |
| Editorial control | Operators inspect drafts, accept or reject Stories, preview ordered Digests, and publish explicitly with audit events |
| Public knowledge | Home, Digest, Story, Browse, RSS, and accepted-only Research expose published knowledge without operator controls |
| Safe Research | Bounded retrieval cites Story/Claim/Evidence Span identities and fails closed for unsupported questions or invalid Provider output |
| Reproducible operation | Locked local and production runbooks define startup, migration, status, backup, restore, rollback, and acceptance boundaries |

## Roadmap

| Milestone | Scope | Status |
| --- | --- | --- |
| [#70 M1](https://github.com/Ev3rGan/ai-ledger/issues/70) | Repository productization and design-decision archive | Implementation/review complete; Git/release/production acceptance pending |
| [#71 M2](https://github.com/Ev3rGan/ai-ledger/issues/71) | Focused source portfolio | Planned |
| [#72 M3](https://github.com/Ev3rGan/ai-ledger/issues/72) | Editorial Agent Digest Plan | Planned |
| [#73 M4](https://github.com/Ev3rGan/ai-ledger/issues/73) | MiniLM Hybrid Retrieval and mMARCO | Planned |
| [#74 M5](https://github.com/Ev3rGan/ai-ledger/issues/74) | Comparison, timeline, and multi-hop Research | Planned |

A milestone is marked complete only after production acceptance of its exact merged SHA; implementation or review alone is not release completion.

## Learn the Project

The Chinese-first [Learning Guide](docs/guide/README.md) connects the product loop to domain objects, code, and safe local observation.

| Chapter | Question it answers |
| --- | --- |
| [01 · Product Loop](docs/guide/01-product-loop.md) | How does public information become a cited answer? |
| [02 · Domain and Data Model](docs/guide/02-domain-and-data-model.md) | Which records preserve provenance and publication state? |
| [03 · Repository Tour](docs/guide/03-repository-tour.md) | Where does each responsibility live? |
| [04 · Agent/Human Boundaries](docs/guide/04-agent-human-boundaries.md) | What may automation prepare, and what requires approval? |
| [05 · Retrieval and Research](docs/guide/05-retrieval-and-research.md) | What is live today, and what changes in M4-M5? |

## Documentation Map

| Area | Start here |
| --- | --- |
| Product and learning | [Documentation index](docs/README.md) · [Learning Guide](docs/guide/README.md) |
| Operations | [Local runbook](docs/mvp-local-runbook.md) · [Production runbook](docs/mvp-production-runbook.md) |
| Architecture and decisions | [Domain model](CONTEXT.md) · [ADR index](docs/adr/README.md) |
| Research and evaluation | [Research index](docs/research/README.md) |
| Historical provenance | [Archive index](docs/archive/README.md) |

## Repository Tree

- `src/ai_intel_agent/` — runtime package, CLI, collection, editorial, publication, Web, and Research
- `alembic/` — versioned PostgreSQL schema migrations
- `tests/` — deterministic product, policy, and repository contracts
- `docs/guide/` — learning path from product behavior to code
- `docs/adr/` — accepted architecture and roadmap decisions
- `docs/research/` and `docs/archive/` — secondary research and historical evidence
- `deploy/` and `docker/` — deployment and local database boundaries described by the runbooks

## Quick Start

After providing the process-only configuration described in the [local runbook](docs/mvp-local-runbook.md):

```powershell
uv sync --locked --python 3.12 --extra ch3
uv run ai-intel-agent start-local
```

Use the runbooks for operator commands, topology, validation, and shutdown; the root README intentionally stays stable and high-level.

## Scope and Safety

This public repository documents interfaces and decisions, not secrets, private conversations, hidden reasoning, or sensitive production values. Live source, Provider, database, deployment, and production-acceptance actions require separate authorization.

## License

MIT
