# Tencent Lighthouse Hong Kong Qualification

Status: **evening window passed; daytime window pending**.

This is a budget-constrained single-node pilot for Tencent Cloud Lighthouse. It is not the
three-provider comparison required to close Issue #7, and neither result below is eligible to be
copied or relabelled as an AWS or Alibaba result.

## Frozen environment

- Source snapshot: the v2 runtime and image were built from the working-tree contents that were
  subsequently committed unchanged as `0da62daa6d977643c212428ae45ac0a52b186d14`. The commit did
  not yet exist when the evening probe ran; this distinction is retained for audit accuracy.
- Observer: `mainland-direct-59-64-129-184-01`
- Target: `https://bench-tencent-hk.ai-ledger.cn`
- Candidate: `tencent-lighthouse-hk`, 2 vCPU / 4 GB, Tencent metadata region `ap-hongkong`
- Workload image: `fef1ce10304c447a22778ccbb69fb2121e8d3e3aefb7e496b75931bb8aa358b3`
- PostgreSQL image: `38471f330eb885e04de130b768d6db4e10469e2311879c7e5c699f6d2d8a1c74`
- Protocol: `hong-kong-runtime-benchmark-2026-08-13.v2`, SHA-256
  `37a737ec29e0c2ac8991a174ffd93cb2a08976621e9e83a2cb86f94b812d91ef`
- Observed package price: CNY 95/month. The probe retained the pre-run protocol example's
  USD-normalized value of USD 13.20 and official Tencent price URL; record an explicit dated FX
  source before any formal cross-provider comparison.

## Invalid v1 diagnostic run

The first evening run at `2026-08-13T22:01:15+08:00` failed only the representative resource
probe. The workload image had Debian Bookworm's unversioned `postgresql-client`, which resolved
to `pg_dump 15.18`, while the fixed database image ran PostgreSQL 16.10. PostgreSQL rejects a
`pg_dump` client whose major version is older than the server. CPU, memory, disk, metadata,
network, SSE, egress, OAuth, and cost probes were not the cause.

The v1 file is retained only as diagnostic evidence:

- `reports/tencent-lighthouse-hk-evening-2026-08-13.json`
- File SHA-256: `beeeff31b2832feb95c421d557a62a61c47d7c1c7a283862580ee509fe87a732`

Protocol v2 installs `postgresql-client-16`, publishes a new workload version and image digest,
and cannot be mixed with v1 in the comparator. A real container regression verified `pg_dump
16.15` against PostgreSQL 16.10 with a 10,000-row dump/drop/restore before deployment.

## Evening v2 result

The formal v2 run at `2026-08-13T22:33:06+08:00` passed all 23 measurements. The result contains
no benchmark token.

| Dimension | Result |
| --- | --- |
| HTTPS health | 3/3; median 44.406 ms; worst 134.939 ms |
| SSE | 3/3 complete; first-event median 45.009 ms; worst completion 196.482 ms |
| GitHub Releases | 3/3; node median 106.754 ms; worst 430.847 ms |
| arXiv API | 3/3; node median 139.752 ms; worst 170.728 ms |
| DeepSeek API | 3/3 reachable; node median 143.797 ms; worst 167.923 ms |
| Kimi API | 3/3 reachable; node median 285.974 ms; worst 1273.572 ms |
| GitHub OAuth | 3/3; node median 479.341 ms; worst 747.413 ms |
| Resource gate | 2 vCPU; 3.633 GiB; 128 MiB memory touch; disk and CPU probes passed |
| PostgreSQL restore | dump 178.120 ms; restore 96.413 ms; 10,000 rows restored |
| Node identity | `ap-hongkong`; non-empty Tencent instance ID |
| Runtime health after probe | workload and PostgreSQL running; 0 restarts; no OOM; database healthy |

Evidence:

- `reports/tencent-lighthouse-hk-evening-v2-2026-08-13.json`
- File SHA-256: `4d2792d409fad9f565331119bc86bacb869edab68ccf010fd93cc8b7465767e1`

The frozen product thresholds are satisfied for this window: HTTPS median is at most 250 ms,
every HTTPS attempt is at most 500 ms, SSE first-event median is at most 500 ms, all streams are
complete, and every hard probe passed.

## Remaining qualification and cleanup

Run one daytime v2 qualification from the same observer and preserve it as
`reports/tencent-lighthouse-hk-daytime-v2-2026-08-14.json`. Before running, fail closed if the
observer, Git commit, protocol SHA-256, image SHA-256 values, node, or target changed.

Only if both v2 windows pass may the node be called "Tencent Lighthouse pilot qualified" and
retained as the provisional MVP host. Issue #7 remains open until its three-provider acceptance
criteria are changed or all three candidates are measured in the same 24-hour window.

After the daytime decision, stop and remove the benchmark Compose stack and disposable database
volume, delete or rotate the temporary benchmark token, and release the temporary Compose package
hold. Retain the server, TLS certificate, Nginx, DNS, both v2 JSON evidence files, and this report
unless a separate production deployment decision says otherwise.
