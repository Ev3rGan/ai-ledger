# Multilingual Retrieval Profile calibration

- Parent Spec: #1
- Implementation ticket: #6
- Corpus version: `retrieval-calibration-2026-08-13.v1`
- Corpus SHA-256: `280aa6eee581e331bd624c38312fb281c70dc41557b87cde087ec221128084ce`
- Candidate configuration: `retrieval-candidates-2026-08-13.v1`
- Candidate configuration SHA-256: `ff52a214bd875adc843c9537ada72bb88e45456c1f0dfd785f127a07ee830b5f`
- Runtime: `fastembed-0.8.0-onnx-cpu`
- Generated at: `2026-08-13T04:02:45.433214+00:00`
- Candidate combinations: 16

## Selected Retrieval Profile

- Profile ID: `retrieval-profile-2026-08-13.v1-cb8758f22d7c`
- Embedding: `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (384 dimensions, mean pooling)
- Reranker: `BAAI/bge-reranker-base`
- Chunk profile: `evidence-windows`
- Fusion profile: `semantic-heavy-rrf`
- Cross-language retrieval Recall@5: 100.0%
- Exact technical-Entity retrieval Recall@5: 100.0%
- Evidence Span Recall@5: 87.5%
- Declared model size: 1.26 GiB
- Index throughput: 48.1 Chunks/s
- Median query latency: 678.84 ms
- P95 query latency: 757.61 ms
- Peak process RSS: 2211.2 MiB

Selection is fail-closed: each recall threshold must pass before worst-category and mean recall are compared. Quality ties prefer smaller declared model size, higher index throughput, lower median and P95 query latency, lower RSS, then stable ID.

## Candidate results

| Candidate | Cross-language | Exact Entity | Evidence Span | Gates | Model GiB | Chunks/s | P50 ms | P95 ms | RSS MiB |
| --- | ---: | ---: | ---: | :---: | ---: | ---: | ---: | ---: | ---: |
| `multilingual-minilm-l12-v2__no-reranker-control__compact-windows__balanced-rrf` | 75.0% | 100.0% | 75.0% | PASS | 0.22 | 42.0 | 9.83 | 14.71 | 2077.3 |
| `multilingual-minilm-l12-v2__bge-reranker-base__compact-windows__balanced-rrf` | 100.0% | 100.0% | 75.0% | PASS | 1.26 | 42.0 | 655.99 | 762.51 | 2169.2 |
| `multilingual-minilm-l12-v2__no-reranker-control__compact-windows__semantic-heavy-rrf` | 75.0% | 100.0% | 75.0% | PASS | 0.22 | 42.0 | 9.83 | 12.71 | 2169.2 |
| `multilingual-minilm-l12-v2__bge-reranker-base__compact-windows__semantic-heavy-rrf` | 100.0% | 100.0% | 75.0% | PASS | 1.26 | 42.0 | 689.21 | 828.42 | 2172.4 |
| `multilingual-minilm-l12-v2__no-reranker-control__evidence-windows__balanced-rrf` | 75.0% | 100.0% | 75.0% | PASS | 0.22 | 48.1 | 9.02 | 11.74 | 2179.3 |
| `multilingual-minilm-l12-v2__bge-reranker-base__evidence-windows__balanced-rrf` | 100.0% | 100.0% | 87.5% | PASS | 1.26 | 48.1 | 938.55 | 1067.42 | 2210.2 |
| `multilingual-minilm-l12-v2__no-reranker-control__evidence-windows__semantic-heavy-rrf` | 75.0% | 100.0% | 75.0% | PASS | 0.22 | 48.1 | 9.10 | 12.08 | 2210.2 |
| `multilingual-minilm-l12-v2__bge-reranker-base__evidence-windows__semantic-heavy-rrf` | 100.0% | 100.0% | 87.5% | PASS | 1.26 | 48.1 | 678.84 | 757.61 | 2211.2 |
| `multilingual-mpnet-base-v2__no-reranker-control__compact-windows__balanced-rrf` | 75.0% | 100.0% | 62.5% | FAIL | 1.00 | 19.0 | 21.19 | 23.65 | 3097.3 |
| `multilingual-mpnet-base-v2__bge-reranker-base__compact-windows__balanced-rrf` | 100.0% | 100.0% | 75.0% | PASS | 2.04 | 19.0 | 433.13 | 536.69 | 3098.1 |
| `multilingual-mpnet-base-v2__no-reranker-control__compact-windows__semantic-heavy-rrf` | 75.0% | 100.0% | 62.5% | FAIL | 1.00 | 19.0 | 21.03 | 23.58 | 3098.1 |
| `multilingual-mpnet-base-v2__bge-reranker-base__compact-windows__semantic-heavy-rrf` | 100.0% | 100.0% | 75.0% | PASS | 2.04 | 19.0 | 447.33 | 542.47 | 3098.4 |
| `multilingual-mpnet-base-v2__no-reranker-control__evidence-windows__balanced-rrf` | 75.0% | 100.0% | 75.0% | PASS | 1.00 | 13.2 | 19.99 | 22.26 | 3101.2 |
| `multilingual-mpnet-base-v2__bge-reranker-base__evidence-windows__balanced-rrf` | 100.0% | 100.0% | 87.5% | PASS | 2.04 | 13.2 | 657.29 | 753.40 | 3101.4 |
| `multilingual-mpnet-base-v2__no-reranker-control__evidence-windows__semantic-heavy-rrf` | 75.0% | 100.0% | 75.0% | PASS | 1.00 | 13.2 | 20.17 | 22.42 | 3101.4 |
| `multilingual-mpnet-base-v2__bge-reranker-base__evidence-windows__semantic-heavy-rrf` | 100.0% | 100.0% | 87.5% | PASS | 2.04 | 13.2 | 848.11 | 1018.48 | 3101.6 |

## CPU resources

- Logical CPU count: 24
- Configured ONNX threads: 4

| Role | Candidate | Model | Declared size GiB | Load ms | RSS after load MiB |
| --- | --- | --- | ---: | ---: | ---: |
| Reranker | `bge-reranker-base` | `BAAI/bge-reranker-base` | 1.04 | 1453.05 | 1452.9 |
| Embedding | `multilingual-minilm-l12-v2` | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | 0.22 | 731.42 | 1977.8 |
| Embedding | `multilingual-mpnet-base-v2` | `sentence-transformers/paraphrase-multilingual-mpnet-base-v2` | 1.00 | 1372.93 | 2904.4 |

RSS values are calibration-process working sets. ONNX allocations can be retained between candidate phases, so declared model size and an isolated deployment measurement remain necessary for capacity planning.

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
- The command does not connect to the application database, Browse, or Research behavior and does not introduce a vector database.
- Chunk sizes, candidate counts, fusion weights, reranking depth, and thresholds are temporary, versioned calibration outputs that can be replaced by a later run.
