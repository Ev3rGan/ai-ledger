# Research and evaluation

These tools answer bounded engineering questions. They do not activate production sources,
change public behavior, connect a model to the product, or replace exact-merge-SHA acceptance.
Generated `reports/*.md` outputs remain untracked.

## Source policy audit

```powershell
uv run ai-intel-agent audit-sources --output reports\source-activation-audit.md
```

The offline command renders the versioned first-wave policy manifest. Historical rationale is in
[the source activation audit](first-wave-source-activation-audit-2026-08-12.md). Refresh external
facts before using an old conclusion for a new activation decision.

## Document extraction benchmark

```powershell
uv run playwright install chromium
uv run ai-intel-agent benchmark-extraction --output reports\document-extraction-benchmark.md
```

This live, credit-aware benchmark compares the supported extraction paths over its fixed corpus.
It is not a collection command and never makes rewritten text eligible as an Evidence Span. See the
[benchmark record](document-extraction-benchmark-2026-08-12.md).

## Model-route evaluation

```powershell
uv run ai-intel-agent evaluate-model-routes --output reports\model-routing-evaluation.md
```

The command uses the frozen human-approved corpus and may incur Provider cost. It evaluates
candidate routes without changing application routing. See the
[evaluation record](model-routing-evaluation-2026-08-13.md).

## Retrieval calibration

```powershell
uv run --extra retrieval ai-intel-agent calibrate-retrieval `
  --output reports\retrieval-calibration.md `
  --profile-output src\ai_intel_agent\data\retrieval_profile.v1.json
```

Calibration requires approval matching the exact corpus fixtures hash. The original comparison
is preserved in [multilingual retrieval calibration](multilingual-retrieval-calibration-2026-08-13.md);
the approved v2.2 choice is summarized in the
[MiniLM/mMARCO decision](../adr/0010-minilm-mmarco-retrieval.md).

## Runtime qualification

Use `ai-intel-agent benchmark-runtime probe --help` and `compare --help` to inspect the versioned
workflow before running it. Start with the
[benchmark protocol](hong-kong-runtime-benchmark-protocol-2026-08-13.md); the
[first-node recommendation](hong-kong-runtime-first-node-recommendation-2026-08-13.md),
[qualification record](tencent-lighthouse-hk-qualification-2026-08-13.md), and
[purchase snapshot](hong-kong-server-purchase-options-2026-08-13.md) are dated evidence, not
current pricing or production state.
