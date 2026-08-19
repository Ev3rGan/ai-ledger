# ADR 0007: Editorial planning and one explicit approval

- Status: approved Future Work
- Scope: the v2.1-v2.2 Editorial Agent boundary

## Context

The current product prepares traceable Story drafts, then requires an operator to inspect and
accept or reject each Story before previewing and publishing a Digest. The next iteration should
reduce composition work without allowing a model to silently decide what becomes public.

## Alternatives

- Let an Agent accept Stories and auto-publish categories considered low risk.
- Add a conversational administrator Agent or administrator Web UI.
- Have an Editorial Agent prepare a complete, versioned proposal while preserving one explicit
  approval boundary.

## Decision

The Editorial Agent may generate only one complete immutable Digest Plan: 8-12 selected Stories,
ordering, summary, why-it-matters text, Topics, exclusions, and anomaly flags. One administrator
approval accepts the exact plan and publishes the Digest. The Agent never publishes, and any
changed plan requires a new approval. M1 records this boundary but does not implement the Agent.

## Accepted tradeoff

Publication still waits for a human action, and a revised plan costs another review. In return,
the public boundary stays legible, replayable, and attributable to an exact approved artifact.

## Revisit trigger

Revisit only through a separately approved product decision backed by an audit design, explicit
risk categories, rollback behavior, and evidence that the existing one-approval boundary is the
measured bottleneck. Operational convenience alone is insufficient.
