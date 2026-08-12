# Grilling checkpoint: AI intelligence MVP

This checkpoint records the shared understanding reached through Q149 and closes the decision frontier required to write the current MVP specification. It is a design checkpoint, not an implementation specification, and it does not execute a to-spec workflow.

## 1. Resolved MVP decisions

### Product boundary and audience

- Build a real, usable portfolio and personal-production product for AI developers and technical practitioners, primarily serving visitors in mainland China.
- Operate as a small public website with anonymous visitors and one administrator, not as a multi-tenant SaaS.
- Cover broad AI intelligence rather than only Agent engineering.
- Keep two core product modules: a deterministic Intelligence workflow and a bounded Research Agent.
- Provide home, Digest, Story, Browse, Research, and administrator experiences in a text-first technical editorial design with light and dark modes.
- Exclude registration, persistent profiles, comments, likes, subscriptions, billing, and social features.

### Topics, Stories, and publication

- Use Topics for Models, Research, Products and Tools, Industry and Infrastructure, Business, Applications, Policy and Safety, and Community.
- Give each Story one primary Topic and optional secondary Topics. Treat Focus and Trend as Editorial Labels and expose no catch-all Other Topic.
- Treat 20-30 accepted Stories per day as an upper bound, never a quota.
- Generate one administrator-reviewed Digest per day. Its Asia/Shanghai Editorial Window runs from 06:00 on the previous day through 05:59 on the publication day.
- Run collection at 06:00 and 18:00. Evening material normally feeds the next Digest.
- Review Stories before composing a Digest. Only accepted Stories may enter the published Digest.
- Keep unreviewed Stories off the home page and out of Digests; expose them in Browse only through an explicit filter and visible automated/unreviewed status.
- Publish one Digest RSS item per day with a short introduction, selected Story titles, and internal links, but no raw source text, complete Evidence, unreviewed Stories, or Research Answers.
- Label generated summaries, System Analysis, Research Answers, and exports as AI-generated or AI-organized.
- Present evidence states rather than model confidence percentages: reviewed, automated and unreviewed, multi-source agreement, single source, source conflict, insufficient evidence, corrected, and AI-service degraded.

### Acquisition and source policy

- Combine configured Publisher and Source Definition whitelists with controlled queries for paper repositories, GitHub, policy sites, and public communities.
- Prefer official APIs, RSS/Atom, and structured interfaces, then static HTTP extraction, source-specific adapters, optional managed extraction, and browser fallback.
- Keep MCP replaceable and outside the core scheduled production dependency; use it for evaluation, diagnosis, or later tool integration.
- Collect Chinese and English source material, preserve original language and content where permitted, and produce a normalized Chinese presentation layer.
- Backfill ten days on first deployment.
- Activate sources in two waves. The first wave covers OpenAI News, Anthropic News, Google AI, Google DeepMind, Meta AI, Microsoft Research, NVIDIA Generative AI, Hugging Face Blog, DeepSeek Changelog, Qwen Blog, GitHub AI and ML, GitHub Changelog, arXiv AI queries, curated GitHub Releases, Machine Heart, and Qbitai. The second wave adds selected professional media, public communities, policy sources, and GitHub discovery only after backfill validation.
- Require each Source Definition to pass an activation review covering access method, permitted paths, robots and terms checks, storage and public-excerpt policy, language, Topic scope, authority baseline, Hong Kong reachability, parser fixtures, and pause conditions.
- Separate curated GitHub Release monitoring from new-repository discovery. Discovery results remain review-only leads until ownership, maintenance, license, activity, and independent interest are verified.
- Enforce HTTPS, redirect validation, private-network blocking, response limits, MIME checks, timeouts, and untrusted-content handling for every fetch path.
- Keep raw extracted text private. Public pages show AI-marked summaries, bounded excerpts, and links to originals.

### Domain, provenance, and lifecycle

- Use the domain language in `CONTEXT.md`, including Publisher, Source Definition, Candidate, Document, Document Version, Collection Run, Story, Story Revision, Claim, Evidence Span, Evidence Role, Chunk, Entity, Digest, Digest Revision, Correction Notice, Community Signal, Editorial Window, Query Intent, Evidence Set, and Research Answer.
- Model one bounded real-world event as one Story. Reposts and reports about the same event merge; materially new actions or outcomes become related Stories.
- Use deterministic URL, canonical URL, and content-hash duplicate removal. Treat cross-Publisher semantic grouping as a suggested cluster requiring review when the event boundary is ambiguous.
- Do not silently change the membership of an accepted or published Story. New Documents, Claims, or conflicts create a draft Story Revision when they materially change published content.
- Keep completed Collection Runs immutable. A retry is a linked new Collection Run, partial source failure does not fail successful sources, and pausing a Source Definition never deletes accepted knowledge.
- Keep separate lifecycles for Collection Runs, Documents, Stories, and Digests.
- Treat Claims as atomic, independently verifiable propositions. Compound statements are split; source-backed facts remain distinct from System Analysis.
- Make Claim-level provenance first class. Evidence Spans support, contradict, qualify, or duplicate Claims and record a Claim-relative Primary, Independent, Secondary, or Community Evidence Role.
- Anchor Evidence Spans to immutable Document Versions. Chunks remain rebuildable retrieval artifacts and never become Evidence merely because they were retrieved.
- Preserve substantive published changes as immutable Story Revisions or Digest Revisions with public Correction Notices. Minor non-factual fixes need no notice; new real-world actions remain related Stories.
- Preserve event, publication, first-seen, update, extraction, and editorial times with distinct meanings.
- Use typed Entities and retain administrator review for ambiguous identity matches. Advanced automatic merge/split calibration is not part of the MVP.
- Keep normalized Entity relationships in PostgreSQL without a graph database.
- Keep Authority, Relevance, Impact, Novelty, Popularity, Confidence, Support Strength, and Evidence Role separate. Popularity may influence ordering and Trend selection but cannot bypass eligibility or evidence gates.

### Knowledge and Research Agent

- Use PostgreSQL as the system of record, pgvector for semantic retrieval, and PostgreSQL full-text search for lexical retrieval. Do not add a separate vector or graph database.
- Index Story representations and type-aware Document Chunks with complete version and provenance metadata.
- Use deterministic visibility and metadata filters followed by parallel FTS, pgvector, and exact-entity retrieval, reciprocal-rank fusion, local reranking, and evidence-coverage checks.
- Keep weights, thresholds, and candidate counts in versioned Retrieval Profiles rather than prompts.
- Implement constrained Agentic RAG with simple, comparative or timeline, and complex-research execution levels.
- Use rule-first intent routing with a low-cost model fallback, bounded iterations, fixed budgets, query decomposition, counter-evidence checks, and citation verification.
- Permit the public Research Agent to read only curated knowledge. Live web search and direct web citation are forbidden.
- Support factual retrieval, multi-source synthesis, comparisons, timelines, change and trend analysis, developer-impact analysis, conflict detection, report organization, and technology-stack-aware tool recommendations.
- When a visitor omits their stack, show a dated popular-tools overview rather than claiming personalized suitability.
- Treat public-community material as Community Signals. It may support claims about discussion, experience, or controversy but cannot alone establish underlying product, performance, financing, or safety facts.
- Keep anonymous sessions without long-term user memory. Persist application-owned traces rather than durable LangGraph request checkpoints.
- Show citations and structured execution facts, but never hidden reasoning, prompts, or sensitive tool parameters.

### Models, evaluation policy, and budget

- Provide real DeepSeek and Kimi adapters behind a capability-oriented Model Gateway. OpenAI is outside the MVP because no official API key is available.
- Treat DeepSeek as the default candidate family and Kimi as a quality challenger; choose task routes through evaluation rather than provider claims.
- Keep model IDs, prompts, schemas, budgets, retries, and fallbacks in versioned configuration.
- Require typed structured output, local schema validation, one controlled repair attempt, controlled fallback, and adapter contract tests.
- Select models per task through critical quality gates followed by quality, latency, and cost comparison. Low price or an aggregate score cannot compensate for a critical factual, citation, structure, or abstention failure.
- Apply layered verification: all Stories receive deterministic, evidence, and economical-model checks; Focus, conflict, or low-confidence cases may receive stronger or cross-provider verification.
- Cap all usage-metered external APIs together at USD 100 per month. This includes LLM and managed extraction usage; infrastructure remains a separate USD 15-30 monthly budget.
- Apply the agreed 70, 85, 95, and 100 percent degradation thresholds to aggregate external API spend.
- Start without provider keys in deterministic sample mode and never fabricate live capability.

### Administration, observability, and retention

- Authenticate the sole administrator through GitHub OAuth and an allowlisted numeric GitHub user ID, with no shared-password fallback.
- Organize administrator work into an exception-first Story review queue, a Digest composer, and an operations area for source health, pause/resume, backfill, linked retry Runs, costs, errors, and degradation.
- Audit human edits, review actions, publication, correction, source-state changes, and reprocessing.
- Preserve structured collection, Story, model, retrieval, citation, cost, latency, and error traces in PostgreSQL and emit JSON logs without requiring a paid observability platform.
- Retain anonymous question and answer bodies for seven days and aggregate metrics for 90 days. Store no direct IP address; use an irreversible rate-limit identifier.

### Architecture, deployment, and delivery

- Use a modular Python monolith with domain, acquisition, intelligence, knowledge, editorial, research, models, web, jobs, and observability modules.
- Use ordinary Python workflows for acquisition, intelligence, and editorial work. Use LangGraph only inside the Research module.
- Keep SQLAlchemy mappings, provider SDK types, FastAPI types, and LangGraph state out of the domain module.
- Use SQLAlchemy 2, Alembic, PostgreSQL, and pgvector without shallow repository wrappers for every table.
- Replace the placeholder classes rather than wrapping them. Preserve `ai-intel-agent run --sample` only as the deterministic offline acceptance entry point.
- Start Alembic with one clean target-domain baseline migration; do not encode placeholder types or compatibility tables.
- Deliver in vertical slices: deterministic domain and persistence foundation; real reviewed Digest; hybrid retrieval; bounded Research Agent; source and evaluation expansion; then production deployment and recovery hardening.
- Gate slices with deterministic acceptance tests, pre-migration backup, idempotent jobs, previous application images, feature isolation, structured observability, and human smoke tests. Use expand-migrate-contract for later destructive schema changes.
- Deploy as one small Hong Kong service. Benchmark a 4 GB VM that self-hosts Web, worker, and PostgreSQL/pgvector, with daily off-machine logical backups retained for at least seven days.

## 2. Implementation-stage research, benchmarks, and evaluation

The following are implementation work, not unresolved MVP Spec decisions. Their current numeric thresholds and candidate rankings are hypotheses to validate and version.

### Source activation and extraction

- Confirm every selected feed, endpoint, query, schedule, language, Topic mapping, robots rule, term, storage permission, and public-excerpt policy.
- Test all sources from the selected Hong Kong region and record latency, 403/429 behavior, extraction quality, update frequency, and duplicate rate.
- Build the agreed 60-URL extraction corpus across English, Chinese, dynamic, technically structured, and PDF pages.
- Compare HTTP plus Trafilatura, Playwright plus Trafilatura, Firecrawl Scrape, and Tavily Extract. Validate metadata accuracy, body completeness, boilerplate, provenance anchoring, repeatability, latency, reliability, and cost.
- Treat the provisional Q144 values, including 98 percent metadata accuracy and 95 percent body success, as research hypotheses rather than fixed Spec acceptance values. Untraceable or synthesized text remains ineligible for Evidence regardless of aggregate benchmark score.
- Select at most one managed extraction fallback after free-tier evaluation and fit it inside the aggregate external API budget.
- Validate the current curated GitHub Release candidates and repository-specific filters against historical output before activation. The current research shortlist is LangGraph, LlamaIndex, CrewAI, PydanticAI, OpenAI Agents Python, MCP Python and TypeScript SDKs, Transformers, vLLM, Ollama, LiteLLM, Promptfoo, and Langfuse; it remains operational configuration, not a permanent product boundary.

### Model, retrieval, and scoring evaluation

- Exercise real Kimi and DeepSeek credentials, configured model IDs, quotas, schema behavior, billed prices, latency, retries, and fallback paths.
- Run the agreed workflow and Research Agent evaluation suites before assigning default routes.
- Build human-approved development and frozen test sets. Q149 becomes an evaluation implementation ticket: model assistance may draft annotations, but the administrator approves gold Evidence, required points, rejection conditions, and failure labels.
- Benchmark the selected multilingual embedding and reranking candidates on cross-language retrieval, exact technical entities, Evidence Span recall, memory, indexing throughput, and production-class CPU latency.
- Measure corpus-specific, type-aware Chunk sizes, overlap, retrieval candidate counts, fusion weights, reranking depth, and evidence-coverage thresholds.
- Calibrate Eligibility separately from Ranking. Preserve raw features and versioned scorecards for Relevance, Impact, Novelty, Popularity, Authority, Confidence, and Support Strength.
- Calibrate API allocation, per-request complexity budgets, anonymous rate limits, and degradation behavior from measured cost and traffic rather than design guesses.

### Infrastructure and operational research

- Benchmark the Hong Kong candidates for mainland-user latency, SSE stability, source and provider outbound access, GitHub OAuth, resource pressure, and total cost; then select the provider.
- Measure source-text and backup volume before deciding long-term object-storage needs.
- Verify daily logical backups by restoring an empty database and rebuilding pgvector indexes; record RPO and RTO.
- Choose exact dependency versions, deployment configuration, DNS, TLS, secret management, alerting, rollout, and rollback procedures during implementation.
- Verify Docker Desktop, Docker CLI PATH visibility, and engine availability in the actual development session.

## 3. Launch gates

The MVP may be implemented before all gates pass, but it must not be presented as a live production service until they do.

- Every active Source Definition has documented access, robots, terms, storage, excerpt, language, Topic, health, and pause policies.
- Fetch security passes redirect, SSRF, private-network, response-size, MIME, timeout, and untrusted-content tests.
- The real pipeline completes collection, normalization, Story review, Claim and Evidence creation, correction, Digest publication, and RSS generation without fabricated provider behavior.
- Published Claims and Research Answers have reproducible citations to eligible Evidence Spans; unsupported or conflicting questions produce visible qualification or abstention.
- Summary, retrieval, reasoning, citation, conflict, and abstention evaluation gates pass using the versioned human-approved evaluation sets. Exact numeric thresholds are set from implementation evidence.
- Anonymous rate limiting, seven-day body retention, 90-day aggregate retention, irreversible rate-limit identifiers, and trace redaction are verified.
- GitHub OAuth callback, administrator allowlist, session security, TLS, secrets, and public/admin authorization are verified in the production environment.
- Aggregate external API metering and the 70/85/95/100 percent degradation ladder are exercised without taking historical Digests offline.
- Daily off-machine backups retain at least seven recoverable copies, and an empty-environment restoration drill succeeds.
- Mainland latency, SSE, source reachability, model API reachability, and monthly infrastructure cost are measured on the selected Hong Kong host.
- Deployment-specific privacy, generative-content labeling, algorithm, news-information, filing, and retention obligations receive qualified review before public launch.

## 4. Outside the MVP

- Multi-tenancy, teams, billing, subscriptions, public registration, social interactions, and high availability.
- Public live-web research, direct public web citation, or allowing visitors to modify the knowledge base or publish content.
- Long-term anonymous-user memory or persistent personal profiles.
- OpenAI API integration without an official project key.
- Separate vector databases, graph databases, or a knowledge-graph product surface.
- Multi-region deployment, mainland deployment, load balancers, dedicated workers, and managed high-availability PostgreSQL.
- Public raw-content APIs, full-source redistribution, and unrestricted exports.
- Personal-author blogs in the initial source policy.
- Advanced automatic Entity merge/split calibration; the MVP retains typed Entities and administrator review for ambiguous cases.
- Fully autonomous cross-Publisher Story merging or silent mutation of published Stories.
- Treating benchmark hypotheses, current model IDs, cloud prices, chunk values, ranking weights, source permissions, or traffic assumptions as permanent product decisions.

The frontier that can change the current MVP's user behavior, scope, or difficult-to-reverse architecture is empty. Remaining uncertainty is assigned to implementation research, evaluation, or launch gates rather than additional grilling questions.
