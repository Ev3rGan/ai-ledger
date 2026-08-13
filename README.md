# AI Intelligence Agent

The first vertical slice of a traceable AI intelligence application. Its deterministic sample
workflow persists one Candidate, immutable Document Version, Story, atomic Claim, anchored
Evidence Span, and Structured Trace in PostgreSQL/pgvector.

## Quick start

Use Python 3.12 and the repository-local locked environment:

```powershell
uv sync --locked --python 3.12 --extra ch3
copy .env.example .env
```

Create the PostgreSQL database named in `.env`, then apply the baseline migration:

```powershell
uv run alembic upgrade head
uv run ai-intel-agent run --sample
```

The sample uses a fixed Asia/Shanghai clock, fixed source data, and deterministic identifiers.
Running it again leaves exactly one corresponding set of records and writes the same report.

Audit the versioned first-wave Source Definitions without network access or production
credentials:

```powershell
uv run ai-intel-agent audit-sources --output reports\source-activation-audit.md
```

The audit records each official entry point, language and Topic scope, robots and terms
findings, private-storage and public-excerpt policy, pause conditions, and a conservative
activation conclusion. Refresh the underlying evidence before activating a Source Definition
whose conclusion is `needs-verification`.

Add `FIRECRAWL_API_KEY` and `TAVILY_API_KEY` to the untracked `.env`, install Chromium once,
then run the live versioned Document extraction benchmark over the fixed 60-URL corpus:

```powershell
uv run playwright install chromium
uv run ai-intel-agent benchmark-extraction --output reports\document-extraction-benchmark.md
```

The benchmark compares HTTP plus Trafilatura, Playwright plus Trafilatura, Firecrawl, and
Tavily across body extraction, body completeness, metadata, noise, provenance anchoring,
repeatability, reliability, latency, and cost. Each URL/path pair runs twice by default.
Provider calls use credits; raw extracted bodies stay in memory and are not written to the
report. The command recommends at most one managed fallback and keeps rewritten extractor
output ineligible for Evidence. It does not activate production Source Definitions or
Collection Runs.

Add `DEEPSEEK_API_KEY` and a Kimi China-platform `KIMI_API_KEY` to `.env`, then run the
standalone model-routing evaluation over the frozen, human-approved corpus:

```powershell
uv run ai-intel-agent evaluate-model-routes --output reports\model-routing-evaluation.md
```

The command compares the versioned DeepSeek V4 Flash, DeepSeek V4 Pro, and Kimi K2.6
candidates on classification, Chinese summarization, Claim verification, simple questions,
and complex reasoning. Every case applies strict structure, factual, citation, and abstention
gates before comparing quality, latency, and token-based cost. A failed critical gate makes a
candidate ineligible regardless of aggregate score. Model IDs, endpoints, thinking routes,
prices, the CNY-to-USD evaluation conversion, human-approval provenance, prompt, output schema,
retry policy, route ranking, per-task token limits, per-run cost budget, and gold criteria are
versioned inputs. Eligible candidates rank by quality, then cost, then latency, preserving
DeepSeek as the economical default when quality is tied. The command checks the worst-case
request budget before making any provider call; provider invoices remain authoritative. This
evaluation does not connect any model to the production application.

The frozen v1 corpus is an initial route smoke evaluation: classification, Chinese
summarization, Claim verification, and complex reasoning each have one case, while simple
questions have two. A single failed case therefore makes its whole task route ineligible. The
complex-reasoning case measures application of the approved routing policy; it does not measure
general complex-reasoning ability. Treat recommendations as project-specific starting routes,
not broad model-capability conclusions.

Benchmark the three Hong Kong runtime candidates with the same representative container and
fixed mainland observer. Build and run the workload using
`docker/runtime-benchmark.Dockerfile`, then capture one versioned JSON artifact per candidate:

```powershell
uv run ai-intel-agent benchmark-runtime probe --help
uv run ai-intel-agent benchmark-runtime compare --help
```

The probe covers public HTTPS and SSE, node-side source, model API, and OAuth egress, a bounded
CPU/memory/disk container workload, and dated cost evidence. It sends no model credentials or
billed model requests. The comparator requires all three configured candidates to use the same
protocol, workload image SHA-256, and observer before it emits a report or recommendation. See
[`docs/research/hong-kong-runtime-benchmark-protocol-2026-08-13.md`](docs/research/hong-kong-runtime-benchmark-protocol-2026-08-13.md)
for the complete reproducible procedure.

## Verification

The dev environment bundles an isolated PostgreSQL/pgvector test server. The acceptance test
starts it, applies the migration, invokes the CLI twice, and removes its data afterward. Set
`TEST_DATABASE_URL` only when you want the same test to use an existing disposable database.

```powershell
uv run --extra dev pytest
uv run --extra dev ruff check .
uv run ai-intel-agent run --sample --output reports\daily.md
uv run ai-intel-agent audit-sources --output reports\source-activation-audit.md
```

Run `benchmark-extraction` separately when a live, credit-consuming benchmark refresh is
intended. Run `evaluate-model-routes` separately when a live, token-billed model evaluation
refresh is intended.

The sample slice intentionally does not include Web pages, administrator review, real sources,
production model-provider integration, or placeholder tables for later work.

## Repository layout

```text
src/ai_intel_agent/
  domain.py       # persistence-independent domain records
  sample.py       # fixed clock, fake source adapter, and sample data
  persistence.py  # SQLAlchemy mappings and idempotent repository
  pipeline.py     # application operation
  source_audit.py # versioned first-wave Source Definition activation audit
  extraction_corpus.py # fixed 60-URL benchmark corpus
  extraction_benchmark.py # fixed-corpus Document extraction benchmark
  model_routing_evaluation.py # frozen-corpus DeepSeek/Kimi route evaluation
  runtime_benchmark.py # fixed Hong Kong node probes and comparison
  runtime_workload.py # token-protected representative container workload
  data/model_routing_evaluation.v1.json # human-approved gold cases and gates
  data/model_routing_candidates.v1.json # versioned models, endpoints, and prices
  data/model_routing_protocol.v1.json # versioned prompt, schema, retries, and budgets
  cli.py          # supported CLI transport
alembic/          # clean PostgreSQL/pgvector baseline migration
tests/            # CLI-to-database acceptance test
docker/runtime-benchmark.Dockerfile # benchmark-only workload image
```

## License

MIT
