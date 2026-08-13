# DeepSeek and Kimi Model Routing Evaluation

- Evaluation version: `model-routing-evaluation-2026-08-12.v1`
- Candidate configuration version: `model-routing-candidates-2026-08-12.v1`
- Corpus review state: `human-approved`
- Corpus SHA-256: `97027a1e3c45cac30e1349a34904706290b6527c650d3c193a10b384affeca31`
- Approved cases SHA-256: `8de5085eb331ce7ef869467583018a76598caa296bf3ce7221e76e7d2f616ca7`
- Corpus approved by: `Ev3rGan`
- Corpus approved at: `2026-08-13T10:51:08+08:00`
- Approval method: Administrator explicitly approved the exact cases SHA-256, including gold facts, rejection conditions, answer bounds, citations, and failure gates.
- Evaluation protocol version: `model-routing-protocol-2026-08-12.v1`
- Evaluation protocol SHA-256: `7b052d195a67a7c79f480d295abe4cabfb88b321ac69cb7c39b4fdf97f93f49e`
- Prompt version: `model-routing-prompt-2026-08-12.v1`
- Output schema version: `model-routing-output-schema-2026-08-12.v1`
- Route ranking: quality descending, cost ascending, latency ascending, stable candidate identifier
- Run at: `2026-08-13T02:56:30.784754+00:00`
- Provider pricing checked at: `2026-08-12`
- Worst-case reserved cost: `$0.127552` USD of `$0.250000` per-run budget
- Estimated actual cost: `$0.011005` USD
- Approval sources: [https://github.com/Ev3rGan/ai-ledger/issues/1](https://github.com/Ev3rGan/ai-ledger/issues/1) at `2026-08-11T19:07:21Z`, [https://github.com/Ev3rGan/ai-ledger/issues/5](https://github.com/Ev3rGan/ai-ledger/issues/5) at `2026-08-12T03:48:20Z`

## Critical gates

Candidates are eligible only when every gate passes. Quality, latency, or cost never compensates for a failed gate.

- `structure`: strict JSON object with exactly the approved fields.
- `factual`: bounded canonical facts, verdicts, labels, language, and contradiction patterns.
- `citation`: every required Evidence identifier is present and no invented identifier appears.
- `abstention`: the model answers or abstains exactly when the frozen case requires it.

## Route recommendations

| Task class | Selected candidate | Quality | Median latency | Estimated USD | Eligibility |
| --- | --- | ---: | ---: | ---: | --- |
| `classification` | `deepseek:v4-pro` | 100.0 | 1816 ms | $0.000262 | PASS |
| `chinese_summarization` | `deepseek:v4-pro` | 100.0 | 2218 ms | $0.000213 | PASS |
| `claim_verification` | `deepseek:v4-pro` | 100.0 | 1861 ms | $0.000146 | PASS |
| `simple_question` | `deepseek:v4-flash` | 100.0 | 1144 ms | $0.000076 | PASS |
| `complex_reasoning` | `kimi:k2.6-cn` | 100.0 | 36401 ms | $0.005047 | PASS |

## Candidate task comparison

| Candidate | Task class | Critical gates | Cases | Quality | Median latency | Estimated USD |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| `deepseek:v4-flash` | `classification` | FAIL | 0/1 | 66.7 | 1299 ms | $0.000084 |
| `deepseek:v4-flash` | `chinese_summarization` | FAIL | 0/1 | 90.0 | 1827 ms | $0.000069 |
| `deepseek:v4-flash` | `claim_verification` | FAIL | 0/1 | 87.5 | 1345 ms | $0.000050 |
| `deepseek:v4-flash` | `simple_question` | PASS | 2/2 | 100.0 | 1144 ms | $0.000076 |
| `deepseek:v4-flash` | `complex_reasoning` | FAIL | 0/1 | 88.9 | 5540 ms | $0.000278 |
| `deepseek:v4-pro` | `classification` | PASS | 1/1 | 100.0 | 1816 ms | $0.000262 |
| `deepseek:v4-pro` | `chinese_summarization` | PASS | 1/1 | 100.0 | 2218 ms | $0.000213 |
| `deepseek:v4-pro` | `claim_verification` | PASS | 1/1 | 100.0 | 1861 ms | $0.000146 |
| `deepseek:v4-pro` | `simple_question` | PASS | 2/2 | 100.0 | 1950 ms | $0.000234 |
| `deepseek:v4-pro` | `complex_reasoning` | FAIL | 0/1 | 88.9 | 13876 ms | $0.001172 |
| `kimi:k2.6-cn` | `classification` | PASS | 1/1 | 100.0 | 1714 ms | $0.000600 |
| `kimi:k2.6-cn` | `chinese_summarization` | PASS | 1/1 | 100.0 | 4450 ms | $0.000842 |
| `kimi:k2.6-cn` | `claim_verification` | PASS | 1/1 | 100.0 | 4025 ms | $0.000878 |
| `kimi:k2.6-cn` | `simple_question` | PASS | 2/2 | 100.0 | 2934 ms | $0.001054 |
| `kimi:k2.6-cn` | `complex_reasoning` | PASS | 1/1 | 100.0 | 36401 ms | $0.005047 |

## Case results

| Candidate | Returned model | Case | Gates | Failure details | Quality | Latency | Attempts | Tokens in/cached/out | Cost | Finish |
| --- | --- | --- | --- | --- | ---: | ---: | ---: | --- | ---: | --- |
| `deepseek:v4-flash` | `deepseek-v4-flash` | `classification-research-01` | structure=PASS, factual=FAIL, citation=PASS, abstention=PASS | label must be Research; canonical fact keys and bounded values must match: expected={'primary_topic': 'Research'}, actual={'primary_topic': 'Models'} | 66.7 | 1299 ms | 1 | 482/0/60 | 0.000084 USD ($0.000084) | stop |
| `deepseek:v4-flash` | `deepseek-v4-flash` | `chinese-summary-architecture-01` | structure=PASS, factual=FAIL, citation=PASS, abstention=PASS | verdict must be supported | 90.0 | 1827 ms | 1 | 530/384/169 | 0.000069 USD ($0.000069) | stop |
| `deepseek:v4-flash` | `deepseek-v4-flash` | `claim-verification-live-web-01` | structure=PASS, factual=FAIL, citation=PASS, abstention=PASS | canonical fact keys and bounded values must match: expected={'knowledge_boundary': 'accepted knowledge only', 'provider_web_search': 'disabled'}, actual={'knowledge_boundary': 'Restrict public Research to accepted knowledge', 'provider_web_search': 'disabled for public requests'} | 87.5 | 1345 ms | 1 | 487/384/122 | 0.000050 USD ($0.000050) | stop |
| `deepseek:v4-flash` | `deepseek-v4-flash` | `simple-question-editorial-window-01` | structure=PASS, factual=PASS, citation=PASS, abstention=PASS | none | 100.0 | 1304 ms | 1 | 474/384/103 | 0.000043 USD ($0.000043) | stop |
| `deepseek:v4-flash` | `deepseek-v4-flash` | `simple-question-abstention-01` | structure=PASS, factual=PASS, citation=PASS, abstention=PASS | none | 100.0 | 985 ms | 1 | 471/384/71 | 0.000033 USD ($0.000033) | stop |
| `deepseek:v4-flash` | `deepseek-v4-flash` | `complex-reasoning-routing-policy-01` | structure=PASS, factual=FAIL, citation=PASS, abstention=PASS | canonical fact keys and bounded values must match: expected={'deepseek_role': 'economical default candidate family', 'kimi_role': 'quality and cross-provider challenger', 'route_basis': 'evaluation-driven', 'eligibility_rule': 'pass every critical gate'}, actual={'deepseek_role': 'economical default candidate family', 'eligibility_rule': 'candidate must pass every critical gate', 'kimi_role': 'quality and cross-provider challenger', 'route_basis': 'evaluation results on critical gates, quality, latency, and cost'} | 88.9 | 5540 ms | 1 | 641/0/672 | 0.000278 USD ($0.000278) | stop |
| `deepseek:v4-pro` | `deepseek-v4-pro` | `classification-research-01` | structure=PASS, factual=PASS, citation=PASS, abstention=PASS | none | 100.0 | 1816 ms | 1 | 482/0/60 | 0.000262 USD ($0.000262) | stop |
| `deepseek:v4-pro` | `deepseek-v4-pro` | `chinese-summary-architecture-01` | structure=PASS, factual=PASS, citation=PASS, abstention=PASS | none | 100.0 | 2218 ms | 1 | 530/384/170 | 0.000213 USD ($0.000213) | stop |
| `deepseek:v4-pro` | `deepseek-v4-pro` | `claim-verification-live-web-01` | structure=PASS, factual=PASS, citation=PASS, abstention=PASS | none | 100.0 | 1861 ms | 1 | 487/384/115 | 0.000146 USD ($0.000146) | stop |
| `deepseek:v4-pro` | `deepseek-v4-pro` | `simple-question-editorial-window-01` | structure=PASS, factual=PASS, citation=PASS, abstention=PASS | none | 100.0 | 2197 ms | 1 | 474/384/105 | 0.000132 USD ($0.000132) | stop |
| `deepseek:v4-pro` | `deepseek-v4-pro` | `simple-question-abstention-01` | structure=PASS, factual=PASS, citation=PASS, abstention=PASS | none | 100.0 | 1704 ms | 1 | 471/384/72 | 0.000102 USD ($0.000102) | stop |
| `deepseek:v4-pro` | `deepseek-v4-pro` | `complex-reasoning-routing-policy-01` | structure=PASS, factual=FAIL, citation=PASS, abstention=PASS | canonical fact keys and bounded values must match: expected={'deepseek_role': 'economical default candidate family', 'kimi_role': 'quality and cross-provider challenger', 'route_basis': 'evaluation-driven', 'eligibility_rule': 'pass every critical gate'}, actual={'deepseek_role': 'economical default candidate family', 'eligibility_rule': 'only candidates that pass every critical gate are selected', 'kimi_role': 'quality and cross-provider challenger', 'route_basis': 'evaluation-driven'} | 88.9 | 13876 ms | 1 | 641/0/1027 | 0.001172 USD ($0.001172) | stop |
| `kimi:k2.6-cn` | `kimi-k2.6` | `classification-research-01` | structure=PASS, factual=PASS, citation=PASS, abstention=PASS | none | 100.0 | 1714 ms | 1 | 465/0/48 | 0.004319 CNY ($0.000600) | stop |
| `kimi:k2.6-cn` | `kimi-k2.6` | `chinese-summary-architecture-01` | structure=PASS, factual=PASS, citation=PASS, abstention=PASS | none | 100.0 | 4450 ms | 1 | 506/256/154 | 0.006065 CNY ($0.000842) | stop |
| `kimi:k2.6-cn` | `kimi-k2.6` | `claim-verification-live-web-01` | structure=PASS, factual=PASS, citation=PASS, abstention=PASS | none | 100.0 | 4025 ms | 1 | 470/0/121 | 0.006322 CNY ($0.000878) | stop |
| `kimi:k2.6-cn` | `kimi-k2.6` | `simple-question-editorial-window-01` | structure=PASS, factual=PASS, citation=PASS, abstention=PASS | none | 100.0 | 2667 ms | 1 | 457/256/79 | 0.003721 CNY ($0.000517) | stop |
| `kimi:k2.6-cn` | `kimi-k2.6` | `simple-question-abstention-01` | structure=PASS, factual=PASS, citation=PASS, abstention=PASS | none | 100.0 | 3202 ms | 1 | 455/256/85 | 0.003870 CNY ($0.000538) | stop |
| `kimi:k2.6-cn` | `kimi-k2.6` | `complex-reasoning-routing-policy-01` | structure=PASS, factual=PASS, citation=PASS, abstention=PASS | none | 100.0 | 36401 ms | 1 | 540/256/1267 | 0.036337 CNY ($0.005047) | stop |

## Versioned candidate configuration

| Candidate | Provider model | Thinking tasks | Native prices per 1M tokens (hit/miss/output) | Source |
| --- | --- | --- | --- | --- |
| `deepseek:v4-flash` | `deepseek-v4-flash` (`DeepSeek-V4-Flash-0731`) | complex_reasoning | 0.0028/0.14/0.28 USD | [official pricing](https://api-docs.deepseek.com/quick_start/pricing) |
| `deepseek:v4-pro` | `deepseek-v4-pro` (`DeepSeek-V4-Pro`) | complex_reasoning | 0.003625/0.435/0.87 USD | [official pricing](https://api-docs.deepseek.com/quick_start/pricing) |
| `kimi:k2.6-cn` | `kimi-k2.6` (`Kimi-K2.6`) | complex_reasoning | 1.1/6.5/27 CNY | [official pricing](https://platform.moonshot.cn/docs/pricing/chat-k26) |

## Interpretation limits

- This v1 corpus is an initial project-specific route smoke evaluation, not a general model leaderboard.
- Classification, Chinese summarization, Claim verification, and complex reasoning each have one case; simple questions have two. One failure therefore makes that candidate's whole task route ineligible.
- The complex-reasoning case measures application of the approved routing policy, not general complex-reasoning ability.
- Recommendations apply only to the frozen corpus and versioned prompts/configuration above.
- Versioned evaluation conversion only; provider invoices remain authoritative.
- Provider invoices, returned usage, model availability, and prices must be rechecked before reruns.
- No evaluated model is connected to the production application by this command.
