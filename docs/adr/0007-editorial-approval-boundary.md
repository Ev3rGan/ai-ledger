# ADR 0007: Editorial planning and one explicit approval

- Status: accepted and implemented
- Scope: the v2.1-v2.2 Editorial Agent boundary

## Context

The product prepares traceable Story drafts. The Editorial Agent reduces composition work by
preparing a complete proposal without allowing a model to silently decide what becomes public.
Earlier direct Story review and Digest publication commands are retired compatibility surfaces.

## Alternatives

- Let an Agent accept Stories and auto-publish categories considered low risk.
- Add a conversational administrator Agent or administrator Web UI.
- Have an Editorial Agent prepare a complete, versioned proposal while preserving one explicit
  approval boundary.

## Decision

Each Editorial Agent proposal is one complete, versioned, immutable Digest Plan: Story decisions,
ordering, summary, why-it-matters text, Topics, exclusions, and anomaly flags. The persisted plan
has a content identity that binds the approval to that exact proposal. An operator may remove an
included Story by deriving a new immutable Plan version without recalling the Provider. The
derivation records its predecessor, removed Story key, reason, and actor while preserving the
remaining Story, Claim, Evidence, and source facts. Plans support 1-12 included Stories; fewer than
three Publishers produces a visible non-blocking warning.

The derived Plan inherits `provider_identifier` and `protocol_version` from its predecessor. Those
fields identify the originating Agent proposal and the protocol used for its sole Provider call;
they do not claim that the operator derivation called, or was authored by, a newer Provider
protocol. The derivation metadata and immutable `digest-plan.story-removed` audit event identify
the local operator action separately.

One administrative operator approves the exact plan once. Approval accepts its included Stories
and publishes the unchanged Digest as one controlled action. Only the latest Plan version for the
publication date may be approved or published; predecessor Plans remain readable as immutable
history. The Agent never approves or publishes, and any changed proposal is a new plan version that
requires a new approval. Public or operator-facing output exposes the plan and its evidence, not
hidden model reasoning.

## Accepted tradeoff

Publication still waits for a human action, and a revised plan costs another review. In return,
the public boundary stays legible, replayable, and attributable to an exact approved artifact.
Once any derived Digest Plan exists, migration 0014 intentionally refuses a downgrade to 0013,
even if that Plan was never approved or published: 0013 cannot represent the immutable lineage,
and deleting it would violate this decision. Operational rollback must keep the forward-compatible
schema or restore a verified pre-0014 backup rather than discard Plan history.

## Revisit trigger

Revisit only through a separately approved product decision backed by an audit design, explicit
risk categories, rollback behavior, and evidence that the existing one-approval boundary is the
measured bottleneck. Operational convenience alone is insufficient.
