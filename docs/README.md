# Documentation map

Use this page after the product overview in the repository [README](../README.md).

## Operate the product

- [Local MVP runbook](mvp-local-runbook.md): locked setup, local Web and scheduler lifecycle,
  operator flow, and safe shutdown.
- [Production runbook](mvp-production-runbook.md): immutable release bundle, private database,
  secret files, status, backup, isolated restore, and rollback.
- [Domain model](../CONTEXT.md): the vocabulary for Source Definitions, Documents, Stories,
  Claims, Evidence Spans, Digests, and Research.

## Understand the architecture and policy

- [Architecture decisions](adr/) define the current collection, evidence, retrieval, Research,
  production, and publication boundaries.
- [Design decisions and Future Work](adr/README.md) records the approved pre-ticket choices
  for the v2.1-v2.2 roadmap and the evidence that would reopen them.

## Secondary evidence

- [Research and evaluation](research/README.md) explains the reproducible tools and routes to
  their versioned protocols and reports.
- [Historical evidence](archive/README.md) preserves earlier delivery and retired-profile
  provenance without treating it as current product configuration.

Repository governance notes for coding agents remain under `docs/agents/`; they are maintainer
instructions, not product or operator documentation.
