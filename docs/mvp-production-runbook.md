# Public multi-source v2 production runbook

This is the supported production procedure for the M1 service lineage, the collector over the
current versioned Source Profile universe, and the exact Digest Plan editorial/publication loop.
It keeps both the public and read-only Operator Hosts behind Caddy automatic HTTPS and PostgreSQL
reachable only on an internal Compose network. The two Hosts share one Python Web process, while
the application rejects every Operator route, Session, and static entry on the public Host.

The public edge network is pinned to `172.31.255.0/24`, with its dynamic allocation range limited
to `172.31.255.128/25` and Caddy fixed at `172.31.255.2`. Keeping `.2` outside the dynamic range
prevents Web or another dynamically addressed service from taking Caddy's address first. Web has
no published host port and configures Uvicorn to trust forwarded headers only from that Caddy
address; do not widen this to arbitrary clients or all container addresses.

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
/etc/ai-ledger-m1/qualifications/   safe SHA-bound Provider qualification reports
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
- `github-oauth-client-secret`

Create them as root in `/etc/ai-ledger-m1/secrets`, never in the checkout, release env file, shell
arguments, Compose environment, or image. Generate the database password and anonymous salt with
`openssl rand`; enter the Provider key and GitHub OAuth client secret through a non-echoing prompt
or the host's secret manager. Register the exact callback URL
`https://<AI_INTEL_OPERATOR_DOMAIN>/operator/oauth/callback` in the GitHub OAuth App. Record only
stable numeric GitHub user IDs in `AI_INTEL_OPERATOR_GITHUB_USER_IDS`; account logins are display
metadata and must not be used for authorization.
Create a dedicated host group with numeric GID 10001, then set the directory and files to
`root:10001` with modes `0750` and `0640`. Compose adds only that supplementary group to the
services which need a mounted secret; each service receives only its own required secret files.
Do not print their contents during setup or acceptance.

Set `AI_INTEL_PROVIDER_MONTHLY_BUDGET_CENTS` no higher than `50000`. This is the
application's internal estimated-spend ceiling; the Provider invoice remains authoritative.
Set `AI_INTEL_PROVIDER_REQUEST_RESERVATION_CENTS` to exactly `100` cents. That amount is a
temporary concurrency hold, not booked spend. Web and Scheduler atomically acquire it before
HTTP, replace it with a micro-USD estimate from the response's prompt/cache/completion token usage
at the versioned peak price, and release it when no metered response was produced. Missing or
invalid usage fails closed by booking the full hold. The migration preserves the previous fixed
reservation counter as `legacy_reserved_cents` for audit but excludes it from the active ceiling,
because it did not represent actual spend.
The file-backed `collect-gemini` and `collect-sources` operator commands detect this production
contract and use the same ledger; neither can bypass the cap.

## Real Provider release qualification

Deterministic CI deliberately uses mocked Providers and cannot qualify a release. The same CI
classifies the files changed by every PR. A Provider-facing PR automatically runs the protected
**live Provider qualification** job after deterministic quality checks pass; an unrelated PR
records that no paid qualification is required. The job verifies the exact open same-repository PR
HEAD, waits for `provider-acceptance` approval, reads its `DEEPSEEK_API_KEY`, runs the versioned
production-shaped Research corpus once against the real Provider, and uploads a safe report. Fork
PRs never receive the secret. Use GitHub Actions' failed-job retry for transient failures.

This is the only pre-deployment real-Provider qualification stage. There is no second post-merge
qualification and no scheduled Provider-drift run. Configure `provider-acceptance` for `main` and
`refs/pull/*/merge`, list the trusted collaborators as reviewers, and leave self-review enabled so
either a collaborator or a sole maintainer can authorize their own bounded run. Approval protects
the secret and paid call; it is not an independent code-review requirement.

Download the successful workflow artifact without editing it and install the JSON report as
`/etc/ai-ledger-m1/qualifications/<pr-head>.json`, owned by root and mode `0600`. Set
`AI_INTEL_PROVIDER_QUALIFICATION_FILE` in the candidate release file to that absolute path. The
report contains only the tested PR commit, qualified-source and contract hashes, route/model
identifiers, a timestamp, bounded cost/attempt metadata, and per-case pass/fail metadata; it
excludes credentials, prompts, evidence, and model answer text.

`validate`, `start`, and `upgrade` fail before image pull, backup, migration, or service mutation
when the report is missing, failed, mocked, bound to different Provider-qualified source or
contract content, names a route or model not approved by that checkout, or omits any required
corpus observation. Squash merging may change the commit SHA, so deployment compares the
versioned qualified-source content hash rather than requiring the release SHA to equal the tested
PR SHA. Unrelated changes may reuse the report while that content is unchanged. This gate does not
replace the post-deploy anonymous browser acceptance; it prevents changed Provider behavior from
first reaching the real Provider only after deployment.

For the first rollout of this gate, run the new checkout's `operate.sh validate` explicitly before
using an older installed operator, then install/use the new operator. Older operator code cannot
retroactively enforce a gate it does not contain. Later upgrades enforce the report normally.

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
Caddy. Use `start` only when no current release is recorded; an existing installation must use
`upgrade`. If initial startup fails after creating runtime resources, the operator removes those
unrecorded containers and a newly created, correctly labeled project edge while retaining the
database volume. It preserves any edge network that existed before the attempt or is not labeled as
owned by this Compose project. The Scheduler holds a
PostgreSQL advisory lock, so a second production Scheduler exits before collection. `status`
shows database readiness and persisted recent Scheduler state through the private container CLI.
`operator source-status --production` additionally reports each approved source's recent result,
cursor, health, and body-valid Document Versions pending draft generation. The separate Operator
Host provides an authenticated, read-only HTTP view of this state, Scheduler status, Editorial
plans, completion/index follow-up, Stories, Claims, Evidence, and raw Document Versions.

The lock-holding database session is monitored every two seconds, including while source or
Provider I/O is in progress. A replacement Scheduler holds a five-second activation grace. If a
PostgreSQL restart drops the old session, the old dedicated Scheduler process exits before the
replacement can collect; Docker then keeps exactly one effective worker running.

Only Caddy publishes host ports 80 and 443. PostgreSQL has no host port. Caddy terminates both
configured HTTPS names and overwrites the
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
set and waits for health. `validate` checks Caddy in a read-only, networkless, non-privileged
one-off container after dropping all capabilities and adding back only `NET_BIND_SERVICE`, which
the pinned Caddy binary's file capability requires even for config validation. It never attaches a
candidate container to the running Compose project. `upgrade` validates and
pulls the candidate first, then requires the existing edge to be either the verified legacy
`172.19.0.0/16` network with no dynamic range or the pinned `172.31.255.0/24` network with exact
dynamic range `172.31.255.128/25`. A missing, uninspectable, extra, or mismatched subnet/range
contract aborts before backup or outage. After that preflight it creates and verifies a local and
off-host logical backup. When it finds the verified legacy contract, it rechecks that exact state
and enters one bounded outage: remove only the
current Caddy, Web, and Scheduler containers, remove the old edge network, then let the candidate
Compose bundle create the pinned edge and wait for health. PostgreSQL and the backup service stay
on the unchanged private database network. After candidate health succeeds, `upgrade` verifies the
new edge subnet and dynamic range again before changing the release records. Any detach, network
removal, migration, candidate health, or target network-contract failure leaves the release records
unchanged and attempts to recreate the recorded current release and its edge connectivity. Because
the legacy Compose file did not pin IPAM, recovery explicitly recreates its verified `172.19.0.0/16`
edge with the Compose project/network labels before restarting it; this makes a later candidate
retry deterministic instead of depending on Docker's next free dynamic subnet. M1-M3
migrations are additive, so `rollback` runs the previous
application image against the forward-compatible database without destructive schema downgrade;
if that target is unhealthy, it restores the recorded current release without swapping records.

Before validation, mount an authorized off-host filesystem or object-storage gateway at
`AI_INTEL_OFFSITE_BACKUP_DIR`; `validate` refuses an ordinary host directory. The backup service
writes a verified custom-format logical backup immediately on start and then at the configured
interval, copies it to that required mount, verifies the copied archive, and only then reports
success. It deletes local and off-host files older than the retention window. `restore-isolated` targets only
the `restore-postgres` profile, which has its own volume and internal network and is not connected
to Web, Scheduler, Caddy, or the production database.

Docker stores each service stream as JSON and rotates it at 10 MiB with five files. Caddy and the
application emit JSON records. `audit-no-secrets` compares the four injected values against
tracked repository content, captured service logs, and the saved image layers while printing
only pass/fail.

## Public-host user action and live acceptance

Infrastructure ownership remains with the user. After purchasing or selecting the Linux host:

1. Install Docker Engine and Compose v2 from the operating-system/vendor-supported repository.
2. Provision an off-host backup destination, mount it at `AI_INTEL_OFFSITE_BACKUP_DIR`, and verify
   `mountpoint --quiet "$AI_INTEL_OFFSITE_BACKUP_DIR"`. This task cannot create or authorize the
   user's storage account.
3. Add DNS `A` records for both `AI_INTEL_DOMAIN` and `AI_INTEL_OPERATOR_DOMAIN`, each
   pointing to the host's public IPv4 address.
4. Permit inbound TCP 22 from the operator's trusted address and TCP 80/443 from the public
   internet; permit UDP 443 if HTTP/3 is desired. Deny public TCP 5432.
5. Inject the four secret files named above, place the frozen checkout/release file in the
   documented paths, then run `validate` and `start`.
6. Verify both domains resolve to the host, HTTP redirects to HTTPS, and both certificates are
   valid. On the public Host, verify Home → Digest → Story, Browse, RSS, and Research are reachable
   and `/health/ready` is 404 at the public boundary. On the Operator Host, complete GitHub OAuth
   with an allowlisted numeric user ID and verify Dashboard, Plan history/detail, Evidence links,
   and raw Document Version inspection are read-only; verify public routes are 404 on that Host.
7. Run the bounded Research acceptance with a dedicated anonymous client: one supported answer
   up to the recorded limit, then an `anonymous-allowance-exhausted` refusal. Confirm the
   Provider call counter does not increase for the excess request.
8. Record Digest/Story URLs and database row counts, run `restart`, and confirm both are unchanged.
9. Run `backup`, confirm the same verified basename exists at the off-host destination, then run
   `restore-isolated`, `rollback`, `status`, and `audit-no-secrets`; record only
   commit/image digests, timestamps, public URLs, row counts, and pass/fail results.

Any missing real domain/certificate, public browser path, Provider counter proof, restart
persistence, isolated restore, rollback, or secret audit makes live M1 acceptance incomplete.

## Historical four-feed live-source acceptance gate

The original M1 live-source probe used only these public discovery URLs:

- `https://the-decoder.com/feed/`
- `https://techcrunch.com/category/artificial-intelligence/feed/`
- `https://huggingface.co/blog/feed.xml`
- `https://www.qbitai.com/feed/`

That probe is historical evidence and included a Source Profile that is now retired. Its immutable
baseline remains linked from the
[historical evidence index](archive/README.md); it is not current activation policy. Ordinary
repository validation makes no live request to the four URLs above.

The current versioned universe contains 18 enabled Source Profiles across the core and supplemental
groups, plus one disabled authorization-required profile. Treat
`src/ai_intel_agent/data/source_profiles.v1.json` and `operator source-status --production` as the
current source-count authorities; the four URLs above remain a bounded historical acceptance set.

Do not run `collect-sources`, start a live backfill, or let the M2 Scheduler reach a collection
slot until the supervisor explicitly authorizes the applicable live-acceptance gate. Do not rerun
the historical four-feed probe as current activation policy. After authorization, use the enabled
versioned Source Profiles and record URLs, response status/behavior, counters, and identifiers—not
raw fetched bodies or credentials. Verify each source follows its versioned discovery and body
policy; one forced source failure leaves useful results from the other sources; and a same-key
replay plus a new-key unchanged-cursor run creates no duplicates. Run
`operator source-status --production` before and after collection and preserve the M1 acceptance
record above as history only. A missing real source observation, real budgeted Provider draft, idempotency
proof, source-isolation proof, or exact Candidate-to-Evidence provenance leaves M2 live acceptance
incomplete.

## M4 frozen Release Candidate

Freeze one candidate before any live acceptance. The checkout must be clean at one reviewed
40-character commit, the image must be built from that checkout, and the release file must pin
the resulting `@sha256:` digest. Record `AI_INTEL_SCHEDULE_BACKFILL_LIMIT=5` (or a smaller
reviewed value) in that immutable release file. Keep the previous release file and image
available until M4 acceptance and the public preview are complete.

Run `validate` before the change. Use `upgrade`, not an ad-hoc Compose invocation, when a current
release exists:

```bash
export AI_INTEL_STATE_DIR=/etc/ai-ledger-m1/state
bash deploy/m1/operate.sh validate /etc/ai-ledger-m1/releases/<candidate>.env
bash deploy/m1/operate.sh upgrade /etc/ai-ledger-m1/releases/<candidate>.env
bash deploy/m1/operate.sh status
```

`upgrade` refuses an invalid release, pulls and verifies it without joining the running project,
requires the exact legacy-or-fixed subnet/range preflight, creates the verified pre-change backup,
migrates the verified legacy edge only inside the bounded outage described above, migrates the
database, waits for all required services, verifies the target subnet/range, and records
current/previous only after the candidate is healthy. An already pinned edge is left intact, so a
retry or later rollback/upgrade cycle does not rebuild it. Do not edit a recorded release file or
checkout in place. A rebuilt image or changed configuration is a new candidate. The candidate
`upgrade` command deliberately does not dispatch to the recorded older operator: this ensures the
new pre-migration backup guard and edge migration recovery are active on the first M3-to-M4
upgrade. `validate` pulls the pinned application image and verifies that its
`org.opencontainers.image.revision` label exactly matches `AI_INTEL_RELEASE`; status reports that
validated commit. Lifecycle commands also override ambient release-contract variables with the
values from the recorded release file and fix the Compose project name, so an exported shell
variable cannot substitute a different image, release, domain, database, budget, or schedule.
Release validation rejects a scheduled limit outside `1-5`, and both production Scheduler and
manual collection refuse a requested limit above that recorded value.

## Normal operator loop

`operate.sh operator` is the supported private CLI boundary. It executes inside the recorded
Web container and therefore reads and writes the same PostgreSQL state that public Web and
Research use. It remains the mutation boundary; the separate Operator Host exposes only the
authenticated read projection and logout.

For a single production Research diagnostic, run:

```bash
bash deploy/m1/operate.sh operator operator research answer --production \
  'Who published the WebGPU kernel library?'
```

This command uses the current release's production PostgreSQL accepted knowledge, Retrieval
backends, Research execution, DeepSeek route, output validation, and aggregate monthly Provider
budget. It deliberately omits only the per-client anonymous daily allowance, so it neither consumes
nor resets a visitor's allowance. Every Provider attempt still acquires the configured temporary
hold in the shared PostgreSQL ledger and stops before HTTP when settled spend plus live holds reach
the cap. Successful responses settle from Provider usage; unmetered failures release the hold. The
command accepts exactly one question, has the same 500-character input limit as public Research,
and returns only the validated answer, public citation data, or the structured refusal/error. It has
no batch, budget-bypass, API-key, anonymous-client, or raw-Provider-output option. A completed
answer or structured refusal exits successfully; an execution result with `status: failed`
prints its structured error and exits non-zero.

Start each operation by saving the secret-free status JSON:

```bash
bash deploy/m1/operate.sh operator operator status --production
bash deploy/m1/operate.sh operator operator source-status --production
bash deploy/m1/operate.sh operator operator retrieval status --production
bash deploy/m1/operate.sh operator story list --state unreviewed
```

The combined status reports the deployed commit, database readiness, Scheduler state and next
execution, latest multi-source Collection Run, all enabled source health snapshots, pending review
count, and latest published Digest. It never reports the database URL, credentials, source body,
Evidence text, or Provider response.

Inspect every pending Story before asking the Editorial Agent to prepare one immutable Plan:

```bash
bash deploy/m1/operate.sh operator story show <stable-key>
bash deploy/m1/operate.sh operator digest plan prepare --date <Asia-Shanghai-date>
```

The prepare output records the Plan ID, immutable content hash, current-state hash, source and
scheduler snapshots, Story decisions, ordering, and blockers. Re-open that exact Plan before the
single approval action, then supply the displayed content hash unchanged:

```bash
bash deploy/m1/operate.sh operator digest plan show <plan-id>
bash deploy/m1/operate.sh operator digest plan remove <plan-id> <story-stable-key> \
  --reason <operator-reason> \
  --actor production-operator
bash deploy/m1/operate.sh operator digest plan approve <plan-id> \
  --content-hash <displayed-sha256> \
  --actor production-operator
bash deploy/m1/operate.sh operator operator retrieval index --production
bash deploy/m1/operate.sh operator operator retrieval status --production --require-hybrid
```

Removing a Story creates a new immutable Plan version with lineage and audit metadata; it never
rewrites the inspected Plan or calls the Provider again. Re-open the returned Plan and approve only
its new ID and content hash. Superseded Plans cannot be approved or published. The supported
quantity is 1-12 Stories, while fewer than three Publishers remains visible as a non-blocking
warning.

Direct `story accept`, `story reject`, `digest preview`, and `digest publish` are retired
compatibility surfaces and are not a supported production workflow. Do not bypass the exact Plan
workflow with direct publication commands.

Research detects accepted published Documents that are newer than the active retrieval generation,
reports `documents_pending_index`, and disables stale semantic/entity candidates while retaining the
current PostgreSQL lexical projection. The incremental index command restores full Hybrid behavior
without replacing the active profile. After publication and indexing, rerun status and inspect the
public HTTPS Home, linked Digest, every selected
Story's expandable Evidence and canonical source, Browse, RSS, and Research. The database and
public projection, not CLI prose copied into an acceptance report, are the source of truth.

## Bounded backfill to incremental schedule

Initial backfill and subsequent scheduled collection use the enabled versioned Source Profiles,
body gate, cursor, canonical identity, operation-key idempotency, and Provider budget. A backfill
requires an explicit supervisor-approved unique operation key and a limit no greater than the
recorded scheduled limit:

```bash
bash deploy/m1/operate.sh operator collect-sources \
  --operation-key m4-backfill:<supervisor-recorded-identifier> \
  --backfill-limit <1-5>
```

Record the before/after status and Provider-call allowance. Replaying the same key must return the
same Collection Run without new logical records. Stop manual backfill when the approved content
and Provider budget target is reached. Do not reset cursors or manufacture rows to reach a count.

The Scheduler then owns ongoing collection at 06:00 and 18:00 Asia/Shanghai. At the next real
window, record status before and after, confirm a new slot-derived operation key, confirm only
eligible unseen entries were processed up to the frozen per-source limit, and compare Candidate,
Document Version, Story, Claim, and Evidence counts for duplicates. Keep the Scheduler active
across the entire window; do not substitute a manual invocation for this acceptance.

## Failure operations

### Source failure

A source-level `invalid-format`, `access-blocked`, or `temporary-failure` is not a service outage.
Use combined status and logs to verify the other Source Profiles completed, public historical
Digest/Story URLs remain available, and Research still reads only accepted published knowledge.
Never replace a blocked article body with Feed summary text, bypass an access control, edit a
cursor, or add any Source Profile outside the active versioned set. A temporary source may retry
at the next scheduled window; a
supervisor-approved manual retry must use a new recorded operation key and the bounded limit.

### Provider failure or budget refusal

A Provider error or budget refusal must leave acquired body-valid Documents and pending draft
work visible without publishing partial generated content. The supported operational signal is a
healthy or completed source result with `pending_drafts` above zero and no corresponding new
draft; do not expect source health to identify a Provider incident. Save the before/after combined
status, confirm Web and prior publications remain healthy, and have the supervisor distinguish an
outage from an exhausted approved budget at the existing secret/Provider boundary. Do not print a
credential, Provider response, or ledger internals, and do not raise a budget or change Provider
routes during incident handling. After the existing Provider route is healthy and the supervisor
authorizes paid work, let the next schedule retry pending drafts or run one bounded new-key
collection. Verify `pending_drafts` returns to zero only after a persisted draft exists, then
review the generated Story/Claim/Evidence normally; never hand-promote a Feed summary or
unverified model response.

### Service failure

Use `status` and `logs`, then `restart`. Preserve the PostgreSQL volume and public URLs. Do not use
Compose commands that remove volumes. If restart cannot restore health, use application rollback
below; restore data only from a verified backup and only after diagnosing a data failure.

## Backup, isolated restore, and application rollback

Create a manual checkpoint before a risky operator action even though `upgrade` already requires
one:

```bash
bash deploy/m1/operate.sh backup
bash deploy/m1/operate.sh restore-isolated ai-ledger-<UTC timestamp>.dump
```

Accept the backup only when the command reports a verified local archive and verified off-host
copy with the same basename. `restore-isolated` must target the dedicated restore volume and
internal restore network; compare schema head and recorded row counts there without connecting
Web, Scheduler, or Caddy to it. Never test restoration over the production database.

Application rollback is:

```bash
bash deploy/m1/operate.sh rollback
bash deploy/m1/operate.sh status
```

Rollback activates the recorded previous immutable bundle without a destructive schema
downgrade. Verify Home, the published Digest and Story URLs, database row counts, Scheduler next
run, and anonymous allowance boundary. If the old application cannot operate against the current
forward-compatible schema, stop and restore service from the candidate; do not improvise a schema
downgrade.

Schema revision 0014 adds immutable Digest Plan lineage that 0013 cannot represent. An
`0014 → 0013` downgrade is available only before any derived Digest Plan exists. Once Story
removal creates one, the migration refuses downgrade even if it was never approved or published;
do not delete Plan history to bypass that guard. The downgrade also refuses an active published
Digest that violates 0013's eight-Story or three-Publisher contract until that Digest is withdrawn.
After lineage exists, roll the application forward against the 0014 schema or restore a verified
pre-0014 backup into the isolated restore path.

## M4 live acceptance record

The supervisor owns secret injection, Provider budget authorization, and timing. Record only
commit/image digests, protocol/profile versions, timestamps, public URLs, non-secret operation
keys, row counts, status/result codes, and pass/fail observations. Do not record environment
values, source bodies, Evidence text, model responses, or anonymous-client identifiers.

M4 is incomplete until one frozen candidate proves all of the following in the same deployed
state:

- the enabled versioned Source Profiles and body gate, with no retired profile in scheduler or status output;
- real DeepSeek draft preparation and the exact Digest Plan review/approval loop;
- one public Digest that exactly matches the approved immutable Plan;
- anonymous HTTPS Home → Digest → Story/source, Browse, RSS, and Research;
- supported Research, insufficient-Evidence refusal, and allowance rejection with no excess
  Provider call;
- one real scheduled window with incremental work, no duplicate logical records, and isolated
  source failure;
- restart persistence, verified isolated restore, application rollback, status output, and
  secret audit.

Keep the final accepted public version running for the anonymous in-app Browser preview. Do not
commit, push, open or merge a PR, remove acceptance resources, or replace that version until the
user confirms the preview.
