# Multilingual Retrieval Profile calibration

- Parent Spec: #1
- Implementation ticket: #6
- Corpus version: `retrieval-calibration-2026-08-13.v1`
- Corpus SHA-256: `9141b3d0b2e9f6a8fd4124cd833b2ff06a7a21c5ce4dfdf737eb4eca9919898a`
- Approved fixtures SHA-256: `f637f643f597509e756727dc542a84c1baffd8b8e691a91e4df661e187b2d3c9`
- Human approval: `Ev3rGan` at `2026-08-13T14:25:22+08:00`
- Candidate configuration: `retrieval-candidates-2026-08-13.v1`
- Candidate configuration SHA-256: `b398e2b656ca5ae9cecc493f33d5af87b3acb47434e38c113a404d38e8c054ec`
- Runtime: `fastembed-0.8.0-onnx-cpu`
- Generated at: `2026-08-13T06:26:50.078141+00:00`
- Candidate combinations: 16

## Selected Retrieval Profile

- Profile ID: `retrieval-profile-2026-08-13.v1-c607cdb27815`
- Embedding: `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (384 dimensions, mean pooling)
- Reranker: `BAAI/bge-reranker-base`
- Chunk profile: `evidence-windows`
- Fusion profile: `semantic-heavy-rrf`
- Cross-language retrieval Recall@5: 100.0%
- Exact technical-Entity retrieval Recall@5: 100.0%
- Evidence Span Recall@5: 87.5%
- Declared model size: 1.26 GiB
- Index throughput (offline preparation): 43.7 Chunks/s
- Median query latency: 914.61 ms
- P95 query latency: 1001.95 ms
- Calibration-process peak RSS: 3103.1 MiB

Selection is fail-closed: each recall threshold must pass before worst-category and mean recall are compared. Quality ties prefer smaller declared model size, higher offline index-preparation throughput, lower median and P95 query latency, then stable ID. Process RSS is a run-level diagnostic and is not used to compare candidates.

## Candidate results

| Candidate | Cross-language | Exact Entity | Evidence Span | Gates | Model GiB | Index-prep Chunks/s | P50 ms | P95 ms |
| --- | ---: | ---: | ---: | :---: | ---: | ---: | ---: | ---: |
| `multilingual-minilm-l12-v2__no-reranker-control__compact-windows__balanced-rrf` | 75.0% | 100.0% | 75.0% | PASS | 0.22 | 55.0 | 8.03 | 11.92 |
| `multilingual-minilm-l12-v2__bge-reranker-base__compact-windows__balanced-rrf` | 100.0% | 100.0% | 75.0% | PASS | 1.26 | 55.0 | 453.62 | 565.75 |
| `multilingual-minilm-l12-v2__no-reranker-control__compact-windows__semantic-heavy-rrf` | 75.0% | 100.0% | 75.0% | PASS | 0.22 | 55.0 | 8.36 | 11.85 |
| `multilingual-minilm-l12-v2__bge-reranker-base__compact-windows__semantic-heavy-rrf` | 100.0% | 100.0% | 75.0% | PASS | 1.26 | 55.0 | 611.11 | 740.57 |
| `multilingual-minilm-l12-v2__no-reranker-control__evidence-windows__balanced-rrf` | 75.0% | 100.0% | 75.0% | PASS | 0.22 | 43.7 | 7.84 | 11.63 |
| `multilingual-minilm-l12-v2__bge-reranker-base__evidence-windows__balanced-rrf` | 100.0% | 100.0% | 87.5% | PASS | 1.26 | 43.7 | 985.65 | 1091.03 |
| `multilingual-minilm-l12-v2__no-reranker-control__evidence-windows__semantic-heavy-rrf` | 75.0% | 100.0% | 75.0% | PASS | 0.22 | 43.7 | 7.53 | 11.58 |
| `multilingual-minilm-l12-v2__bge-reranker-base__evidence-windows__semantic-heavy-rrf` | 100.0% | 100.0% | 87.5% | PASS | 1.26 | 43.7 | 914.61 | 1001.95 |
| `multilingual-mpnet-base-v2__no-reranker-control__compact-windows__balanced-rrf` | 75.0% | 100.0% | 62.5% | FAIL | 1.00 | 12.9 | 31.46 | 40.38 |
| `multilingual-mpnet-base-v2__bge-reranker-base__compact-windows__balanced-rrf` | 100.0% | 100.0% | 75.0% | PASS | 2.04 | 12.9 | 649.73 | 895.69 |
| `multilingual-mpnet-base-v2__no-reranker-control__compact-windows__semantic-heavy-rrf` | 75.0% | 100.0% | 62.5% | FAIL | 1.00 | 12.9 | 31.79 | 41.25 |
| `multilingual-mpnet-base-v2__bge-reranker-base__compact-windows__semantic-heavy-rrf` | 100.0% | 100.0% | 75.0% | PASS | 2.04 | 12.9 | 628.62 | 754.21 |
| `multilingual-mpnet-base-v2__no-reranker-control__evidence-windows__balanced-rrf` | 75.0% | 100.0% | 75.0% | PASS | 1.00 | 9.6 | 30.14 | 39.29 |
| `multilingual-mpnet-base-v2__bge-reranker-base__evidence-windows__balanced-rrf` | 100.0% | 100.0% | 87.5% | PASS | 2.04 | 9.6 | 852.74 | 1078.11 |
| `multilingual-mpnet-base-v2__no-reranker-control__evidence-windows__semantic-heavy-rrf` | 75.0% | 100.0% | 75.0% | PASS | 1.00 | 9.6 | 29.95 | 39.36 |
| `multilingual-mpnet-base-v2__bge-reranker-base__evidence-windows__semantic-heavy-rrf` | 100.0% | 100.0% | 87.5% | PASS | 2.04 | 9.6 | 813.75 | 989.58 |

## CPU resources

- Logical CPU count: 24
- Configured ONNX threads: 4
- Calibration-process peak RSS: 3103.1 MiB

| Role | Candidate | Model | Declared size GiB | Load ms | RSS after load MiB |
| --- | --- | --- | ---: | ---: | ---: |
| Reranker | `bge-reranker-base` | `BAAI/bge-reranker-base` | 1.04 | 1946.38 | 1453.5 |
| Embedding | `multilingual-minilm-l12-v2` | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | 0.22 | 772.70 | 1979.8 |
| Embedding | `multilingual-mpnet-base-v2` | `sentence-transformers/paraphrase-multilingual-mpnet-base-v2` | 1.00 | 2083.06 | 2906.1 |

RSS values are calibration-process diagnostics. ONNX allocations can be retained between candidate phases, so RSS is not treated as a candidate-comparable score. Declared model sizes and an isolated deployment measurement remain necessary for capacity planning.

## Versioned candidates

- Runtime: [fastembed 0.8.x](https://qdrant.github.io/fastembed/) with `CPUExecutionProvider`.

| Role | Candidate | Model | Pooling | License | Source |
| --- | --- | --- | --- | --- | --- |
| Embedding | `multilingual-minilm-l12-v2` | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | mean | apache-2.0 | [model card](https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2) |
| Embedding | `multilingual-mpnet-base-v2` | `sentence-transformers/paraphrase-multilingual-mpnet-base-v2` | mean | apache-2.0 | [model card](https://huggingface.co/sentence-transformers/paraphrase-multilingual-mpnet-base-v2) |
| Reranker | `no-reranker-control` | `none` | n/a | n/a | n/a |
| Reranker | `bge-reranker-base` | `BAAI/bge-reranker-base` | n/a | mit | [model card](https://huggingface.co/BAAI/bge-reranker-base) |

## Scope and interpretation

- The fixed corpus is synthetic and project-specific; these results are not a general model leaderboard.
- Chunks remain rebuildable retrieval artifacts and are never treated as Evidence. Evidence Span recall requires a retrieved Chunk to contain the exact anchored span.
- Offline index preparation includes passage embedding plus lexical-term and exact-Entity posting construction. Query timing runs lexical, semantic, and exact-Entity channels concurrently before deterministic fusion.
- Per Issue #6 non-goals, the command does not connect to the application database or change Browse/Research. PostgreSQL FTS/pgvector persistence, visibility filters, and production tracing belong to the later hybrid Browse slice; this command does not introduce a vector database.
- Chunk sizes, candidate counts, fusion weights, reranking depth, and thresholds are temporary, versioned calibration outputs that can be replaced by a later run.
