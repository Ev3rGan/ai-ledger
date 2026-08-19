# ADR 0009: Deferred event-level semantic deduplication

- Status: deferred
- Scope: deciding whether several Documents describe one real-world Story

## Context

The current collection path already enforces canonical-URL, content-hash, cursor, and operation-key
idempotency. Event-level semantic deduplication is a different judgment: related reports can cover
one event, a material follow-up, or genuinely different events. A premature similarity rule could
erase provenance or merge distinct Claims.

## Alternatives

- Add embedding similarity and automatically merge cross-publisher reports now.
- Add a title/entity heuristic and accept false merges.
- Defer semantic merging while retaining deterministic identity and complete provenance.

## Decision

Event-level semantic deduplication and autonomous cross-publisher Story merging are deferred from
v2.1-v2.2. Exact canonical URL, content hash, operation key, and cursor idempotency remain
mandatory. Related accepted Stories may remain separate until an independently testable event
identity contract exists.

## Accepted tradeoff

Readers and operators may see more than one Story about related activity, and editorial review may
do some grouping manually. The system avoids silently collapsing updates, disagreements, or
independent Evidence Spans into one record.

## Revisit trigger

Revisit after measuring duplicate-event frequency on an approved, labeled bilingual corpus that
distinguishes same event, follow-up event, and related-but-separate event. The proposal must define
false-merge limits, human correction, immutable provenance, and deterministic regression tests.
