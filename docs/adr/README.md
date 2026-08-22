# Architecture decisions

These notes record current and deferred architectural decisions. They omit raw conversations,
hidden reasoning, credentials, and deployment-sensitive values. Each note states the evidence
that would justify reopening the decision.

## Current architecture

1. [Separate deterministic intelligence from bounded Research](0001-separate-deterministic-intelligence-from-bounded-research.md)
2. [Use PostgreSQL for Hybrid knowledge retrieval](0002-use-postgres-for-hybrid-knowledge-retrieval.md)
3. [Preserve Claim-level provenance](0003-preserve-claim-level-provenance.md)
4. [Restrict public Research to curated knowledge](0004-restrict-public-research-to-curated-knowledge.md)
5. [Deploy the MVP as a small Hong Kong service](0005-deploy-the-mvp-as-a-small-hong-kong-service.md)
6. [Preserve published revisions and corrections](0006-preserve-published-revisions-and-corrections.md)
7. [Editorial planning and one explicit approval](0007-editorial-approval-boundary.md)
8. [Focused source portfolio](0008-source-portfolio-boundary.md)
10. [MiniLM Hybrid retrieval and mMARCO reranking](0010-minilm-mmarco-retrieval.md)

## Deferred

9. [Deferred event-level semantic deduplication](0009-event-level-semantic-deduplication.md)

The implementation and its tests remain the source of truth for delivered behavior. A future
change to these boundaries requires a new or superseding decision; deferred work is not presented
as already delivered.
