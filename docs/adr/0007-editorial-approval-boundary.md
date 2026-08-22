# ADR 0007: Editorial planning and one explicit approval

- Status: accepted and implemented
- Scope: the v2.1-v2.2 Editorial Agent boundary

## Context

The product prepares traceable Story drafts and supports direct operator review. The Editorial
Agent reduces composition work by preparing a complete proposal without allowing a model to
silently decide what becomes public.

## Alternatives

- Let an Agent accept Stories and auto-publish categories considered low risk.
- Add a conversational administrator Agent or administrator Web UI.
- Have an Editorial Agent prepare a complete, versioned proposal while preserving one explicit
  approval boundary.

## Decision

Each Editorial Agent proposal is one complete, versioned, immutable Digest Plan: 8-12 selected
Stories, ordering, summary, why-it-matters text, Topics, exclusions, and anomaly flags. The
persisted plan has a content identity that binds the approval to that exact proposal.

One administrative operator approves the exact plan once. Approval accepts its included Stories
and publishes the unchanged Digest as one controlled action. The Agent never approves or
publishes, and any changed proposal is a new plan version that requires a new approval. Public or
operator-facing output exposes the plan and its evidence, not hidden model reasoning.

## Accepted tradeoff

Publication still waits for a human action, and a revised plan costs another review. In return,
the public boundary stays legible, replayable, and attributable to an exact approved artifact.

## Revisit trigger

Revisit only through a separately approved product decision backed by an audit design, explicit
risk categories, rollback behavior, and evidence that the existing one-approval boundary is the
measured bottleneck. Operational convenience alone is insufficient.
