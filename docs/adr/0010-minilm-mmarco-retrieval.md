# ADR 0010: MiniLM Hybrid retrieval and mMARCO reranking

- Status: implemented and production-qualified for the pinned M4 profile
- Scope: accepted-knowledge retrieval for v2.2

## Context

PostgreSQL full-text search cannot by itself cover bilingual wording and exact technical Entities.
The project has a human-approved fixed bilingual corpus and a calibrated multilingual MiniLM
profile. The archived capacity-aware gate compared that control with one alternate Embedding and
a compact multilingual reranker without changing the approved fixtures; see the
[multilingual retrieval calibration](../research/multilingual-retrieval-calibration-2026-08-13.md).

The fixed evidence was `retrieval-calibration-2026-08-13.v1`, corpus SHA-256
`9141b3d0b2e9f6a8fd4124cd833b2ff06a7a21c5ce4dfdf737eb4eca9919898a`, with approved fixtures
SHA-256 `f637f643f597509e756727dc542a84c1baffd8b8e691a91e4df661e187b2d3c9`.

## Alternatives

- Keep PostgreSQL FTS only.
- Replace MiniLM with the tested multilingual E5-small candidate.
- Keep the former BGE reranker or route among several rerankers.
- Treat a second Embedding as a hot fallback despite requiring a full vector rebuild.
- Keep MiniLM, add the one qualified mMARCO reranker, and retain a model-free fallback.

## Decision

The primary Embedding is pinned
`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` through the FastEmbed Qdrant ONNX
revision `faf4aa4225822f3bc6376869cb1164e8e3feedd0`; its optimized artifact SHA-256 is
`634d0f66c29dc934c8fa72b8a4fe91dd4d420a22f1d82a241058d4316e659a99`.

The sole reranker is pinned `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1` AVX2 UINT8 at revision
`1427fd652930e4ba29e8149678df786c240d8825`; its artifact SHA-256 is
`6c2513767fb63d008a4377bef7a7a3555433d9436342bb53e35a3a72ffc52d4b`.

Hybrid retrieval fuses MiniLM pgvector similarity, PostgreSQL FTS, and exact-Entity matches before
reranking the deterministic top eight to a final top five. An ordinary load or inference failure
returns the unchanged Fusion order. PostgreSQL FTS plus exact-Entity retrieval remains the
model-free runtime fallback; there is no hot second Embedding. Query-time deadlines propagate
through connection acquisition and ONNX inference: deadline exhaustion fails closed instead of
being swallowed as an ordinary model fallback.

The E5 candidate failed the fixed project recall gates, while the former BGE reranker did not fit
the shared-node risk budget. Official model-card facts alone do not constitute project approval.
The archived calibration is project-specific Research Gate evidence, not the production
acceptance record. M4's completed exact-SHA production gate qualified only the pinned profile and
implementation described here.

## Accepted tradeoff

MiniLM has a short effective input limit and therefore requires token-aware Chunks. The mMARCO
reranker materially improves the fixed-corpus recall result but adds latency and memory pressure.
Changing vector spaces still requires a rebuild, so the fallback favors reduced semantic recall
over pretending a second model can take over instantly.

## Revisit trigger

Revisit if an expanded human-approved corpus no longer meets the recall gates or the pinned
artifacts become unavailable or incompatible. Any model, profile, configuration, or implementation
tree change reopens the corresponding calibration and exact-SHA production gates. A failed mMARCO
activation falls back to Fusion order; it does not authorize an ad hoc model tournament.
