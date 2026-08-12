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

## Verification

The dev environment bundles an isolated PostgreSQL/pgvector test server. The acceptance test
starts it, applies the migration, invokes the CLI twice, and removes its data afterward. Set
`TEST_DATABASE_URL` only when you want the same test to use an existing disposable database.

```powershell
uv run --extra dev pytest
uv run --extra dev ruff check .
uv run ai-intel-agent run --sample --output reports\daily.md
```

The sample slice intentionally does not include Web pages, administrator review, real sources,
model providers, or placeholder tables for later work.

## Repository layout

```text
src/ai_intel_agent/
  domain.py       # persistence-independent domain records
  sample.py       # fixed clock, fake source adapter, and sample data
  persistence.py  # SQLAlchemy mappings and idempotent repository
  pipeline.py     # application operation
  cli.py          # supported CLI transport
alembic/          # clean PostgreSQL/pgvector baseline migration
tests/            # CLI-to-database acceptance test
```

## License

MIT
