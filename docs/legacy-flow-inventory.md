# Legacy flow inventory

This inventory records the pre-M1 product paths that still appear in code, tests, or historical
documentation. Runtime code and versioned data remain the source of truth. No legacy flow is
deleted by Issue #119; deletion requires separate dependency proof and an issue that owns the
behavior change.

| Classification | Flow or artifact | Current evidence and disposition |
| --- | --- | --- |
| Retain | Exact Digest Plan preparation, inspection, and approval | `digest plan prepare`, `digest plan show`, and `digest plan approve --plan-hash ...` are the current human approval boundary. The approved immutable Plan controls publication membership and order. |
| Retain | Public URLs, RSS, history, and withdrawal behavior | `PublicContent` projects the existing publication records for Home, Digest, Archive, Story, Browse, Research entry, and RSS without exposing private rows. Existing URLs and historical publications remain compatible. |
| Retain | Accepted-knowledge Research boundary | Research continues to read accepted published Documents through the existing accepted-knowledge projection. It must not read unreviewed, rejected, or withdrawn private material. |
| Repair | Direct Story acceptance/rejection and direct Digest preview/publication guidance | These command handlers remain compatibility surfaces, but current operator documentation no longer presents them as the supported approval path. The exact Digest Plan loop is authoritative. |
| Repair | Eight-Story and three-publisher publication gates | These checks are legacy compatibility debt. [Issue #120](https://github.com/Ev3rGan/ai-ledger/issues/120) owns the quantity-policy change, so Issue #119 documents but does not alter them. |
| Repair | Old source-count statements | `source_profiles.v1.json` currently defines 19 profiles: 18 enabled profiles across the core and supplemental groups, plus one disabled authorization-required profile. The four-feed M1 live probe is historical acceptance evidence, not the current Source Profile universe. |
| Archive | Early M1 live-probe and grilling checkpoints | Preserve them as dated evidence until durable decisions have been extracted. They are historical records, not current operator instructions. |
| Archive | M1-M5 release records and research evidence | Keep them under the documentation archive and research indexes so past decisions remain auditable without being mistaken for present behavior. |
| Verify before deletion | Direct `story accept`, `story reject`, `digest preview`, and `digest publish` handlers | Prove every caller, test, deployment script, and historical-data reader has moved to the exact Plan workflow before removing a handler. Route this behavior change through its owning follow-up issue. |
| Verify before deletion | Legacy publication readers and compatibility fields | Replay historical publications and withdrawal cases before removing any field or adapter needed by persisted records. |
| Deletion candidate | Direct-publication command implementation after dependency proof | Delete only when static search, deterministic tests, and deployment-script review show no supported dependency and the replacement workflow is available in the same release. |
| Deletion candidate | Unreachable duplicate documentation | Delete only after links are migrated and every still-relevant decision is retained in a current guide, ADR, or dated archive record. |

## Source-count interpretation

The versioned Source Profile file and runtime status output define the current universe. The four
public Feed URLs used by the original M1 live gate remain useful historical evidence, but they must
not be described as the complete active universe. Any future count change must update the
versioned profile contract, its tests, and current runbooks together.

## Deletion gate

Before deleting a candidate, record the owning issue, replacement path, repository-wide dependency
search, historical replay coverage, and deterministic acceptance evidence. If any one of these is
missing, retain the compatibility surface and classify it for another verification pass.
