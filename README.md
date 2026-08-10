# AI Intelligence Agent Template

A compact Python template for demonstrating AI Agent engineering skills through an automated AI intelligence pipeline.

This project is inspired by a Rebabel-style content workflow: collect candidate AI news, normalize untrusted web content, retrieve memory, cluster duplicate stories, rank by novelty and authority, draft grounded briefs, verify claims, and export a reviewable daily report.

## What This Shows

- Agent harness design
- Context engineering and structured prompts
- Content memory instead of user memory
- Retrieval-friendly story records
- Duplicate detection and novelty ranking
- Claim-level evidence placeholders
- Human-in-the-loop publishing workflow

## Quick Start

```powershell
uv sync --locked --python 3.12 --extra ch3
.\.venv\Scripts\activate
ai-intel-agent run --sample
```

If `uv` is not on PATH, install it first or call the same command through the uv executable available on your machine.

## Project Flow

```text
scheduled trigger
  -> source collection
  -> normalization and sanitization
  -> memory retrieval
  -> clustering and deduplication
  -> ranking
  -> evidence research
  -> editor drafting
  -> claim verification
  -> daily report export
  -> optional publishing
```

## Repository Layout

```text
src/ai_intel_agent/
  agents.py       # collector, editor, verifier placeholders
  models.py       # typed story, evidence, brief, report models
  pipeline.py     # end-to-end orchestration
  memory.py       # simple local content memory
  cli.py          # command line interface
examples/
  sample_sources.json
```

## Environment

Copy `.env.example` to `.env` when adding real provider integrations. Do not commit secrets.

```powershell
copy .env.example .env
```

The current implementation runs without API keys by using deterministic placeholder logic. Replace the TODO sections with OpenAI, search, vector database, WordPress, or messaging integrations as the project evolves.

## License

MIT
