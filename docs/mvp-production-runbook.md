# MVP M1 production runbook

This is the supported Issue #54 M1 procedure extended by Issue #55's five-source M2 collector. It
keeps the existing v1 Web behind Caddy automatic HTTPS and PostgreSQL reachable only on an
internal Compose network. M2 does not change the public security boundary, allowance ledger,
backup/restore, rollback, secret handling, or add an administrator Web surface.

## Frozen release and host layout

Accept only a clean commit reviewed against its exact `origin/main` base. Build and publish one
application image from that commit, then record the registry digest rather than a mutable tag:

```bash
release="$(git rev-parse HEAD)"
docker build --build-arg "AI_INTEL_RELEASE=${release}" \
  --file deploy/m1/production.Dockerfile \
  --tag "REGISTRY/ai-ledger:${release}" .
docker push "REGISTRY/ai-ledger:${release}"
docker image inspect "REGISTRY/ai-ledger:${release}" --format '{{index .RepoDigests 0}}'
```

Use these root-owned host paths; none belongs inside the repository or image:

```text
/opt/ai-ledger/releases/<commit>/   frozen release checkout
/etc/ai-ledger-m1/releases/         non-secret release env files
/etc/ai-ledger-m1/secrets/          injected secret files, mode 0600
/etc/ai-ledger-m1/state/            current and previous release records
/var/backups/ai-ledger-m1/          logical backups, mode 0700 directory
/mnt/ai-ledger-m1-offsite/          mounted off-host backup target
```

Copy `deploy/m1/release.env.example` to
`/etc/ai-ledger-m1/releases/<commit>.env`. Replace only the image digest, commit SHA, domain, and
non-secret database/retention settings. Production validation rejects an image reference without
`@sha256:`.

The release file also records `AI_INTEL_RELEASE` and `AI_INTEL_RELEASE_DIR`. Validation requires
the directory to be an exact clean checkout at that commit. Lifecycle commands load Compose,
Caddy, and operational scripts from the recorded release directory, so rollback restores the
whole versioned deployment bundle rather than changing only the application image.

## Secret injection

The exact secret file names are:

- `database-password`
- `deepseek-api-key`
- `anonymous-id-salt`

Create them as root in `/etc/ai-ledger-m1/secrets`, never in the checkout, release env file, shell
arguments, Compose environment, or image. Generate the database password and anonymous salt with
`openssl rand`; enter the Provider key through a non-echoing prompt or the host's secret manager.
Create a dedicated host group with numeric GID 10001, then set the directory and files to
`root:10001` with modes `0750` and `0640`. Compose adds only that supplementary group to the
services which need a mounted secret; each service receives only its own required secret files.
Do not print their contents during setup or acceptance.

Set `AI_INTEL_PROVIDER_MONTHLY_BUDGET_CENTS` no higher than `10000`. Set
`AI_INTEL_PROVIDER_REQUEST_RESERVATION_CENTS` to a conservative upper bound for one request using
the current Provider price and configured maximum tokens. Web and Scheduler atomically reserve
that amount in the same PostgreSQL monthly ledger before every request attempt. The ledger never
refunds a reservation, so retries and failed calls remain safely counted and all production
metered calls stop before the configured aggregate cap can be exceeded.
The file-backed `collect-gemini` and `collect-sources` operator commands detect this production
contract and use the same ledger; neither can bypass the cap.

## Validate, start, and inspect

Docker Engine and the Compose v2 plugin are required. From the frozen release checkout:

```bash
export AI_INTEL_STATE_DIR=/etc/ai-ledger-m1/state
bash deploy/m1/operate.sh validate /etc/ai-ledger-m1/releases/<commit>.env
bash deploy/m1/operate.sh start /etc/ai-ledger-m1/releases/<commit>.env
bash deploy/m1/operate.sh status
```

`start` validates Caddy and Compose, pulls the recorded image digest, starts and waits for
PostgreSQL, migrates to the sole Alembic head, and then waits for Web, Scheduler, backup, and
Caddy. The Scheduler holds a
PostgreSQL advisory lock, so a second production Scheduler exits before collection. `status`
shows database readiness and persisted recent Scheduler state through the private container CLI.
`operator source-status --production` additionally reports each approved source's recent result,
cursor, health, and body-valid Document Versions pending draft generation. There is no operator
HTTP route.

The lock-holding database session is monitored every two seconds, including while source or
Provider I/O is in progress. A replacement Scheduler holds a five-second activation grace. If a
PostgreSQL restart drops the old session, the old dedicated Scheduler process exits before the
replacement can collect; Docker then keeps exactly one effective worker running.

Only Caddy publishes host ports 80 and 443. PostgreSQL has no host port. Caddy overwrites the
anonymous-client header used by the persistent daily Research allowance and blocks `/health/*`
at the public edge; container health checks use those endpoints internally.

## Lifecycle operations

Run all operations from the frozen release checkout with the same `AI_INTEL_STATE_DIR`:

```bash
bash deploy/m1/operate.sh stop
bash deploy/m1/operate.sh restart
bash deploy/m1/operate.sh status
bash deploy/m1/operate.sh logs
bash deploy/m1/operate.sh backup
bash deploy/m1/operate.sh restore-isolated ai-ledger-<UTC timestamp>.dump
bash deploy/m1/operate.sh upgrade /etc/ai-ledger-m1/releases/<new-commit>.env
bash deploy/m1/operate.sh rollback
bash deploy/m1/operate.sh audit-no-secrets
```

`stop` retains PostgreSQL, Caddy, and restore volumes. `restart` recreates the required service
set and waits for health. `upgrade` migrates before switching the recorded release and restores
the current release automatically if the candidate fails to become healthy. M1 migrations are
additive, so `rollback` runs the previous application image against the forward-compatible
database without destructive schema downgrade.

Before validation, mount an authorized off-host filesystem or object-storage gateway at
`AI_INTEL_OFFSITE_BACKUP_DIR`; `validate` refuses an ordinary host directory. The backup service
writes a verified custom-format logical backup immediately on start and then at the configured
interval, copies it to that required mount, verifies the copied archive, and only then reports
success. It deletes local and off-host files older than the retention window. `restore-isolated` targets only
the `restore-postgres` profile, which has its own volume and internal network and is not connected
to Web, Scheduler, Caddy, or the production database.

Docker stores each service stream as JSON and rotates it at 10 MiB with five files. Caddy and the
application emit JSON records. `audit-no-secrets` compares the three injected values against
tracked repository content, captured service logs, and the saved image layers while printing
only pass/fail.

## Public-host user action and live acceptance

Infrastructure ownership remains with the user. After purchasing or selecting the Linux host:

1. Install Docker Engine and Compose v2 from the operating-system/vendor-supported repository.
2. Provision an off-host backup destination, mount it at `AI_INTEL_OFFSITE_BACKUP_DIR`, and verify
   `mountpoint --quiet "$AI_INTEL_OFFSITE_BACKUP_DIR"`. This task cannot create or authorize the
   user's storage account.
3. Add one DNS `A` record for the chosen domain pointing to the host's public IPv4 address.
4. Permit inbound TCP 22 from the operator's trusted address and TCP 80/443 from the public
   internet; permit UDP 443 if HTTP/3 is desired. Deny public TCP 5432.
5. Inject the three secret files named above, place the frozen checkout/release file in the
   documented paths, then run `validate` and `start`.
6. Verify the domain resolves to the host, HTTP redirects to HTTPS, the certificate is valid,
   Home → Digest → Story, Browse, RSS, and Research are reachable, and `/health/ready` is 404 at
   the public boundary.
7. Run the bounded Research acceptance with a dedicated anonymous client: one supported answer
   up to the recorded limit, then an `anonymous-allowance-exhausted` refusal. Confirm the
   Provider call counter does not increase for the excess request.
8. Record Digest/Story URLs and database row counts, run `restart`, and confirm both are unchanged.
9. Run `backup`, confirm the same verified basename exists at the off-host destination, then run
   `restore-isolated`, `rollback`, `status`, and `audit-no-secrets`; record only
   commit/image digests, timestamps, public URLs, row counts, and pass/fail results.

Any missing real domain/certificate, public browser path, Provider counter proof, restart
persistence, isolated restore, rollback, or secret audit makes live M1 acceptance incomplete.

## M2 live-source acceptance gate

The bounded implementation probe on 2026-08-17 used only these public discovery URLs:

- `https://the-decoder.com/feed/`
- `https://techcrunch.com/category/artificial-intelligence/feed/`
- `https://huggingface.co/blog/feed.xml`
- `https://aibusiness.com/rss.xml`
- `https://www.qbitai.com/feed/`

All five behaved as XML/RSS Feed endpoints; representative public article reads on each approved
host exposed substantive HTML body content through ordinary access. No login, challenge bypass,
credentials, raw-body persistence, live database, or Provider call was used. This implementation
probe is evidence for the common adapter shape, not live acceptance.

Do not run `collect-sources`, start a live backfill, or let the M2 Scheduler reach a collection
slot until the supervisor explicitly authorizes `M2_LIVE_ACCEPTANCE_READY`. After authorization,
use only the five versioned Source Profiles and record URLs, response status/behavior, counters,
and identifiers—not raw fetched bodies or credentials. Verify TechCrunch discovery uses only its
AI category Feed; AI Business either creates a body-valid Document Version or records
`access-blocked`; one forced source failure leaves useful results from the other sources; and a
same-key replay plus a new-key unchanged-cursor run creates no duplicates. Run
`operator source-status --production` before and after collection and preserve the M1 acceptance
record above. A missing real Feed/article observation, real budgeted Provider draft, idempotency
proof, source-isolation proof, or exact Candidate-to-Evidence provenance leaves M2 live acceptance
incomplete.
