# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.

## Before exploring, read these

- **`CONTEXT.md`** at the repo root, or
- **`CONTEXT-MAP.md`** at the repo root if it exists; it points at one `CONTEXT.md` per context. Read each one relevant to the topic.
- **`docs/adr/`** for ADRs that touch the area being changed. In multi-context repos, also check `src/<context>/docs/adr/` for context-scoped decisions.

If these files do not exist, proceed silently. The `/domain-modeling` skill, reached through `/grill-with-docs` and `/improve-codebase-architecture`, creates them lazily when terms or decisions are resolved.

## File structure

This repository uses the single-context layout:

```text
/
|-- CONTEXT.md
|-- docs/adr/
|   |-- 0001-example-decision.md
|   `-- 0002-another-decision.md
`-- src/
```

The presence of `CONTEXT-MAP.md` would indicate a future multi-context layout with context-specific `CONTEXT.md` and ADR directories.

## Use the glossary's vocabulary

When output names a domain concept in an issue title, refactor proposal, hypothesis, or test name, use the term defined in `CONTEXT.md`. Do not drift to synonyms the glossary explicitly avoids.

If the required concept is missing from the glossary, reconsider whether the term belongs to the project or record the gap for `/domain-modeling`.

## Flag ADR conflicts

Surface any conflict with an existing ADR instead of silently overriding it:

> Contradicts ADR-0007 (event-sourced orders), but worth reopening because...
