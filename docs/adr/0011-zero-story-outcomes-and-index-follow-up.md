# ADR 0011: Record zero-Story outcomes and durable index follow-up

- Status: accepted and implemented
- Scope: the v2.2 Editorial completion boundary
- Amends: ADR 0007

Issue 122 establishes that an exact latest Digest Plan may contain zero included Stories. Approval
still applies to one immutable Plan once, but it now commits one immutable Editorial Outcome: a
zero-Story Plan records no publication and creates no Digest, while a non-empty Plan publishes the
unchanged Digest. This amends ADR 0007's 1-12 Story range and its assumption that every approval
publishes; the one-human-approval boundary, version checks, evidence visibility, and auditability
remain unchanged.

A new publication outcome after migration 0015 is installed also records a queued Retrieval Index
Follow-Up in the approval transaction. The migration backfills older published approvals as
Editorial Outcomes without inventing retroactive follow-ups. The existing Scheduler may coalesce
queued follow-ups into one full accepted-knowledge index replacement. A failed rebuild leaves the
previous active index and the published Digest intact and is explicitly retryable; a no-publication
outcome creates no follow-up. This separates the public commit from rebuild availability without
losing the durable obligation or weakening the atomic publication boundary.

## Consequences

Zero-Story days become durable, replayable decisions rather than fake empty Digests. Publication
does not wait for index construction, so readers may temporarily use the previous complete index;
operators can distinguish queued, running, failed, and succeeded follow-up states and retry failure
without republishing.
