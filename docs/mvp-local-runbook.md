# Local MVP runbook

This runbook is the supported Windows-local path for Issue #48. It integrates the existing
M1-M3 commands; it does not replace their collection, editorial, publication, Web, or Research
behavior.

## Prerequisites and process-only configuration

- Windows PowerShell, Docker Desktop, Python 3.12, and the repository-local locked `uv`
  environment.
- A clean checkout at the commit being accepted.
- Exactly these environment keys injected into the launched process by the supervising session:
  `AI_INTEL_DATABASE_URL` and `DEEPSEEK_API_KEY`.
- `AI_INTEL_DATABASE_URL` must use PostgreSQL on `localhost` or `127.0.0.1` and include a
  database, user, password, and the free host port that the managed container will publish.

`start-local` reads only its current process environment. It also sets Compose's
`COMPOSE_DISABLE_ENV_FILE=1`, so Compose does not load `.env`. Neither value is printed or placed
in a command argument. Do not paste either value into logs, reports, shell history, or
Git-tracked files.

From the repository root, the single supported start command is:

```powershell
uv run ai-intel-agent start-local
```

Use the full `uv.exe` path documented in `AGENTS.md` if `uv` is not on `PATH`. The command:

1. starts the `ai-ledger-mvp` Compose PostgreSQL/pgvector service and waits for health;
2. applies every Alembic migration to `head`;
3. starts `schedule-gemini` for 06:00 and 18:00 Asia/Shanghai;
4. starts the formal `serve` command on `http://127.0.0.1:8000`;
5. remains in the foreground as the owner of the Web and scheduler processes.

The PostgreSQL container is loopback-only. Press `Ctrl+C` in the owning terminal to request a
graceful stop from both Windows child-process groups and stop the Compose database safely. A child
that does not exit in ten seconds is force-stopped. The named database volume is retained for the
next run. Do not close the terminal or Docker Desktop as a substitute for `Ctrl+C`.

```text
STOPPED -> DATABASE_STARTING -> DATABASE_HEALTHY -> MIGRATED
        -> SCHEDULER_AND_WEB_RUNNING -> STOPPING -> STOPPED
```

Any startup or child-process failure follows the same `STOPPING -> STOPPED` cleanup path. If the
owning process is forcibly killed, use Docker Desktop to stop only the container in the
`ai-ledger-mvp` Compose project; do not remove its volume.

## Operator acceptance from the same commit and database

Keep `start-local` running in its owning terminal. In supervisor-launched operator subprocesses
with only the environment keys needed by each command, run:

```powershell
uv run ai-intel-agent collect-gemini
uv run ai-intel-agent story list
uv run ai-intel-agent story show <stable-key>
uv run ai-intel-agent story accept <stable-key> --actor m4-operator
uv run ai-intel-agent digest preview --date <Asia-Shanghai-date>
uv run ai-intel-agent digest publish --date <Asia-Shanghai-date> --actor m4-operator
```

Observe these public URLs through a real browser session:

- `http://127.0.0.1:8000/`
- the linked `/digests/<date>` page
- the linked `/stories/<stable-key>` page
- `http://127.0.0.1:8000/browse`
- `http://127.0.0.1:8000/rss.xml`
- `http://127.0.0.1:8000/research`

Ask one question directly supported by the published Claim and exact Evidence. Verify the SSE
answer and click its Story, Claim, and Evidence links. Then ask an unrelated unsupported question
and verify explicit insufficient-Evidence refusal with zero citations. Run `collect-gemini` again
against the same database and verify the summary and operator views show no duplicate Candidate,
Document Version, Story, Claim, or Evidence for unchanged source content.

## Acceptance record

Record metadata and pass/fail observations, never source bodies, model responses, secrets, or the
database URL:

- exact clean commit and branch;
- Compose image tag and Alembic head;
- Gemini source-contract, draft-protocol, model-route, and Research protocol versions;
- dated live source section identity and canonical public URL;
- collection-run identity and idempotency counters;
- Story stable key plus Claim and Evidence UUIDs;
- Digest date and public Home, Digest, Story, Browse, RSS, and Research URLs;
- supported-question SSE result and clickable citation identities;
- unsupported-question refusal and zero-citation observation;
- pass/fail for every item, shutdown result, final Git status, and any anomaly.

Any missing live source, real DeepSeek, browser, public-page, SSE, citation, refusal, idempotency,
or shutdown observation makes the complete acceptance result `FAIL`.
