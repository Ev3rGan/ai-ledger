# Legacy flow inventory

This inventory records the compatibility and historical paths reviewed for Issue #125. Runtime
code, versioned data, persisted history, and the current tests remain the source of truth. No
legacy flow is deleted by Issue #125: no candidate had all of static zero-dependency proof,
historical replay coverage, deterministic tests, and relevant runtime evidence. No deletion is a
deliberate evidence result, not unfinished cleanup.

| Classification | Flow or artifact | Current evidence and disposition |
| --- | --- | --- |
| Retain | Exact Digest Plan preparation, inspection, Story removal, approval, and follow-up retry | The protected Console and break-glass CLI call the same `EditorialWorkflow`. Repeated removal creates immutable versions; only the exact latest Plan can approve. |
| Retain | Zero-Story completion | Plans support 0-12 included Stories. Exact approval of zero records a durable no-publication outcome and creates no Digest, RSS item, or index follow-up. |
| Retain | Public URLs, RSS, history, and withdrawal behavior | `PublicContent` projects the existing publication records for Home, Digest, Archive, Story, Browse, Research entry, and RSS without exposing private rows. Existing URLs and historical publications remain compatible. |
| Retain | Accepted-knowledge Research boundary | Research continues to read Evidence Spans supporting accepted, published Stories. It must not read unreviewed, rejected, withdrawn, or operator-only material. |
| Repair completed in #125 | Production frontend build | The production image now runs locked Vite builds in a pinned Node builder stage and copies only generated assets into the final Python runtime. CI rejects committed-asset drift and proves Node tools are absent from the runtime image. |
| Repair completed in #125 | Public/Operator edge boundary | Caddy now has distinct Host blocks: Public rejects Operator paths and caches hashed public assets immutably; Operator rejects public paths and remains `no-store`. The application repeats Host, Session, Cookie, CSRF, Origin, and CSP enforcement. |
| Repair completed in #125 | Current product and operator guidance | Bilingual README and runbooks now describe the protected Console, arbitrary repeated Story removal, exact latest approval, zero-Story no-publication, and durable index follow-up. |
| Repair retained | Direct Story acceptance/rejection and direct Digest preview/publication | The command names remain fail-closed compatibility surfaces. Tests prove they refuse to bypass exact Plan approval; current operator documentation does not advertise them as a supported path. |
| Archive indexed | Early MVP grilling checkpoint | The original file remains unchanged under `docs/design/`; `docs/archive/README.md` extracts and points to durable decisions while marking superseded quota, source-count, direct-review, and administrator-surface language as historical. |
| Archive indexed | M1-M5 release records and research evidence | Keep them under the archive and research indexes so past decisions remain auditable without becoming present runtime instructions. |
| Verify before deletion | Direct `story accept`, `story reject`, `digest preview`, and `digest publish` handlers | Static search still finds CLI registrations and tests that assert their fail-closed compatibility behavior. Removal needs its own issue, caller proof, historical replay, and replacement acceptance. |
| Verify before deletion | Legacy publication readers, withdrawal behavior, and compatibility fields | Persisted historical publications and withdrawal cases still use these readers. Replay representative production-shaped history before changing them. |
| Verify before deletion | Collector, historical-data read, and preview paths | Their compatibility and live operational callers were not proven absent in #125. Preserve them and record future evidence rather than guessing. |
| Out of cleanup scope | Published history, old Plans, Audit, Withdrawal, source data, and ignored reports | These artifacts are provenance or user-owned output. They must not be removed by legacy cleanup. |

## Static dependency evidence

Repository search at the #125 base found the retired CLI names registered in
`src/ai_intel_agent/cli.py`, a persistence-level refusal for direct Story acceptance, and tests in
`tests/test_m3_editorial_digest_plan.py` that require the compatibility commands to fail closed.
The original grilling checkpoint has no runtime caller, but it retains decision provenance and is
therefore indexed rather than moved or deleted. Legacy publication readers and collector paths
remain connected to historical data and current tests.

The earlier #119 inventory said, “No legacy flow is deleted by Issue #119” and classified
“One-to-twelve Story quantity and Publisher warning.” Those phrases are preserved here only to
make the documentation transition auditable. ADR 0011 and the current runtime supersede the old
quantity wording with 0-12 Plans and durable no-publication.

## Source-count interpretation

The versioned Source Profile file and runtime status output define the current universe. The four
public Feed URLs used by the original M1 live gate remain useful historical evidence, but they must
not be described as the complete active universe. Any future count change must update the
versioned profile contract, its tests, and current runbooks together.

## Deletion gate

**Deletion candidate:** none in Issue #125. Every reviewed flow failed at least one required
evidence gate and is classified above for retention, repair, archival, or later verification.

Before deleting a candidate, record the owning issue, replacement path, repository-wide static
dependency search, historical replay coverage, deterministic tests, and relevant runtime evidence.
If any one is missing, retain and label the compatibility surface for another verification pass.
