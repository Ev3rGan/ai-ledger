# AI Ledger

AI Ledger is a public, evidence-grounded AI daily. It collects from a bounded source portfolio,
turns body-valid documents into traceable draft Stories, keeps publication under operator
control, and serves accepted knowledge as daily Digests, individual Stories, Browse, RSS, and
bounded Research.

The repository is organized product-first. Start here for the running product and supported
operations, then use the [documentation map](docs/README.md) for architecture and runbooks. The
[research and evaluation](docs/research/README.md) and
[historical evidence](docs/archive/README.md) areas are retained as secondary, indexed material.

## What the product does

The operating loop is deliberately small and auditable:

1. A scheduler collects eligible entries at 06:00 and 18:00 Asia/Shanghai from versioned Source
   Profiles. Canonical URLs, content hashes, cursors, and operation keys make retries safe.
2. Feed text is discovery metadata only. A Story draft is prepared only after the article body
   passes access, canonical-location, and body-quality gates.
3. The DeepSeek draft route produces traceable Story, Claim, and exact Evidence Span candidates. It
   cannot accept or publish them.
4. An operator inspects each draft, accepts or rejects it, previews the dated Digest, and
   publishes explicitly.
5. Public pages and RSS expose only published knowledge. Research retrieves only accepted
   knowledge, cites Story, Claim, and Evidence Span records, and refuses unsupported questions.

One failed source remains isolated from the rest of a Collection Run. The system never bypasses
login, paywall, CAPTCHA, robots, consent, or anti-bot controls, and it does not use live Web
retrieval to answer public Research questions.

## What readers see

The public service exposes these stable surfaces:

| Surface | Route | Reader result |
| --- | --- | --- |
| Home | `/` | The current published Digest, source coverage, highlights, and recent Digests |
| Digest | `/digests/<date>` | One reviewed daily composition of accepted Stories |
| Story | `/stories/<stable-key>` | A published Story with Claims, exact Evidence Spans, and original-source links |
| Browse | `/browse` | Published Stories filtered by keyword, publisher, Topic, or date |
| RSS | `/rss` and `/rss.xml` | A human-readable subscription page and machine-readable feed |
| Research | `/research` | SSE answers grounded only in accepted knowledge, with clickable citations |

The public Home page describes reader-facing intelligence. Internal Agent mechanics and operator
controls remain in repository documentation and the private CLI.

## Run it locally

Use Python 3.12, Docker Desktop, PostgreSQL/pgvector through the supplied Compose boundary, and
the repository-local locked environment:

```powershell
uv sync --locked --python 3.12 --extra ch3
uv run ai-intel-agent start-local
```

Before `start-local`, inject `AI_INTEL_DATABASE_URL` and `DEEPSEEK_API_KEY` into the supervising
process. Do not place credentials in tracked files or command arguments. The command starts the
loopback database, migrates to the sole Alembic head, then owns the local Web and twice-daily
source scheduler until `Ctrl+C`. See the [local runbook](docs/mvp-local-runbook.md) for the exact
process boundary, URLs, shutdown behavior, and acceptance procedure.

For a provider-free deterministic smoke run against a configured PostgreSQL database:

```powershell
uv run alembic upgrade head
uv run ai-intel-agent run --sample --output reports\daily.md
```

Generated `reports/*.md` files are local artifacts and are ignored by Git.

## Operator workflow

Keep the service running and use the same commit and database for collection, review, and
publication:

```powershell
uv run ai-intel-agent operator status
uv run ai-intel-agent operator source-status
uv run ai-intel-agent collect-sources --operation-key <recorded-key>
uv run ai-intel-agent story list
uv run ai-intel-agent story show <stable-key>
uv run ai-intel-agent story accept <stable-key> `
  --summary <reviewed-summary> --why-it-matters <reviewed-significance> `
  --topic <Topic> --actor <operator>
uv run ai-intel-agent story reject <stable-key> --actor <operator>
uv run ai-intel-agent digest preview --date <Asia-Shanghai-date> `
  --story <first-key> --story <second-key>
uv run ai-intel-agent digest publish --date <Asia-Shanghai-date> `
  --introduction <reviewed-introduction> `
  --story <first-key> --story <second-key> --actor <operator>
```

`collect-sources` is a live source and Provider operation. Run it only against an authorized
database with an approved Provider budget. The [production runbook](docs/mvp-production-runbook.md)
defines the secret-file contract, immutable release bundle, backup, rollback, and production
operator commands.

### Current source boundary

The twice-daily multi-source scheduler currently activates four Source Profiles: THE DECODER,
TechCrunch AI, Hugging Face Blog, and QbitAI. Gemini API Release Notes remain supported through
the dedicated Gemini collection path. AI Business is retired from active profiles, scheduling,
status output, and site-specific handling; 36kr is not active.

The approved v2.1-v2.2 target portfolio is eight active Source Definitions: Gemini API Release
Notes, THE DECODER, TechCrunch AI, Hugging Face Blog, QbitAI, OpenAI News through its official
News/RSS boundary, GitHub Trending as a Community Signal, and Hugging Face Daily Papers through
the official Hub interface. Machine Heart is a ninth conditional definition and stays disabled
until a formally authorized entry point exists. Later milestones must activate that target
incrementally; this milestone adds no source.

Whitelisting permits only a bounded acquisition attempt at the approved entry point. It never
permits authentication, challenge bypass, arbitrary crawling, or treating Community Signals as
factual support.

### Agent responsibilities

- The collection path applies deterministic source, access, body-quality, idempotency, and
  provenance rules; the DeepSeek route prepares drafts but cannot accept or publish them.
- The human operator owns Story acceptance/rejection, Digest ordering, and publication.
- The public Research path uses one bounded Provider route over accepted knowledge only and must
  cite or refuse.
- A later Editorial Agent will prepare one complete versioned Digest Plan. It will not publish;
  one administrator approval of the exact plan remains required.

## Architecture and decisions

```text
approved sources -> Scheduler -> PostgreSQL/pgvector <- Operator CLI
                                |                       |
                                v                       v
                    draft Story/Claim/Evidence Span -> accepted Digest
                                |                       |
                                +---- Web + RSS --------+
                                         |
                                  accepted-only Research
```

Production runs on one versioned Linux-host bundle: Caddy owns public HTTPS; separate Web and
singleton Scheduler services share private PostgreSQL/pgvector; backup and isolated restore stay
off the public network; Docker secret files hold credentials; rollback activates the previous
immutable application bundle without rewriting publication history.

Use the [domain model](CONTEXT.md), existing [architecture decisions](docs/adr/),
[local runbook](docs/mvp-local-runbook.md), and
[production runbook](docs/mvp-production-runbook.md) for implementation and operations.

### Design decisions and Future Work

The [decision index](docs/adr/README.md) contains concise, approved pre-ticket records for:

- [Editorial planning and one explicit approval](docs/adr/0007-editorial-approval-boundary.md)
- [The focused source portfolio and retired entries](docs/adr/0008-source-portfolio-boundary.md)
- [Deferring event-level semantic deduplication](docs/adr/0009-event-level-semantic-deduplication.md)
- [MiniLM Hybrid retrieval and the sole mMARCO reranker](docs/adr/0010-minilm-mmarco-retrieval.md)

Near-term delivery follows Parent #69: M2 completes the focused source portfolio, M3 adds the
complete Digest Plan and one-approval flow, M4 adds MiniLM Hybrid retrieval plus mMARCO with an
explicit fallback, and M5 adds comparison, timeline, and bounded multi-hop Research. Each
milestone requires deterministic local validation before exact-merge-SHA production acceptance.

## Research, evaluation, and historical evidence

Research tools, fixed corpora, benchmark protocols, and their safety/cost boundaries are indexed
under [docs/research/README.md](docs/research/README.md). Earlier source audits, extraction and
model-route studies, runtime qualification, calibration evidence, and retired-profile provenance
remain discoverable through [docs/archive/README.md](docs/archive/README.md). They support current
decisions but are not the product landing page or current runtime configuration.

## Verification

From the repository root:

```powershell
uv run --extra dev pytest
uv run --extra dev ruff check .
uv lock --check
uv run alembic heads
uv run ai-intel-agent run --sample --output reports\daily.md
```

Live source collection, real Provider calls, public Browser checks, and production deployment are
separate authorized acceptance actions, not ordinary repository verification.

## License

MIT
