# Historical evidence index

This index keeps prior delivery evidence discoverable without presenting it as current runtime
policy. The underlying tracked records remain in their original locations; no Git history or
provenance was rewritten to create this index.

## Source and extraction evidence

- [First-wave source activation audit](../research/first-wave-source-activation-audit-2026-08-12.md)
- [Document extraction benchmark](../research/document-extraction-benchmark-2026-08-12.md)

AI Business was an active Source Profile at the frozen pre-M1 baseline. Its former manifest and
site-specific runtime policy remain directly inspectable in the immutable
[`31f5349` Source Profile snapshot](https://github.com/Ev3rGan/ai-ledger/blob/31f5349e076ae37a9cf0d1440a77ab250b3c9905/src/ai_intel_agent/data/source_profiles.v1.json)
and
[`31f5349` collection implementation](https://github.com/Ev3rGan/ai-ledger/blob/31f5349e076ae37a9cf0d1440a77ab250b3c9905/src/ai_intel_agent/multisource_collection.py).
The matching
[`31f5349` production runbook](https://github.com/Ev3rGan/ai-ledger/blob/31f5349e076ae37a9cf0d1440a77ab250b3c9905/docs/mvp-production-runbook.md)
preserves the former scheduler, status, and live-acceptance wording.
That evidence explains the retirement; it is not an activation instruction. The current boundary
is defined by the tracked manifest at `HEAD` and the
[source portfolio decision](../adr/0008-source-portfolio-boundary.md).

## Model and retrieval evidence

- [Model-routing evaluation](../research/model-routing-evaluation-2026-08-13.md)
- [Multilingual retrieval calibration](../research/multilingual-retrieval-calibration-2026-08-13.md)

## Runtime delivery evidence

- [Runtime benchmark protocol](../research/hong-kong-runtime-benchmark-protocol-2026-08-13.md)
- [First-node recommendation](../research/hong-kong-runtime-first-node-recommendation-2026-08-13.md)
- [Runtime qualification](../research/tencent-lighthouse-hk-qualification-2026-08-13.md)
- [Dated purchase options](../research/hong-kong-server-purchase-options-2026-08-13.md)

Architecture decisions that still govern the product live under [`docs/adr/`](../adr/) rather
than this historical index.
