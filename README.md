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
intended.

The sample slice intentionally does not include Web pages, administrator review, real sources,
model providers, or placeholder tables for later work.

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
  cli.py          # supported CLI transport
alembic/          # clean PostgreSQL/pgvector baseline migration
tests/            # CLI-to-database acceptance test
```

## License

MIT
