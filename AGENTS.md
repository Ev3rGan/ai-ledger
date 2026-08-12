# Agent Instructions

## Scope and Layout

- **This AGENTS.md applies to:** `D:\AgentDev\ai-agent-dev` and all subdirectories.
- **Project type:** single Python package, not a monorepo.
- **Runtime package:** `src/ai_intel_agent`
- **Tests:** `tests`
- **CLI entry point:** `ai-intel-agent`
- **Generated reports:** `reports/*.md` are ignored by Git.

## Repository

- **GitHub:** `https://github.com/Ev3rGan/ai-ledger`
- Use the configured `origin` remote and `main` default branch for repository operations.

### GitHub CLI authentication

Use the repository's installed GitHub CLI at `D:\AgentDev\gh\bin\gh.exe`. Authentication is stored in the Windows keyring and must be verified before Issue or remote operations:

```powershell
$gh = 'D:\AgentDev\gh\bin\gh.exe'
& $gh auth status --hostname github.com
& $gh api user --jq .login
```

A successful status reports the active `Ev3rGan` account and `keyring`; the API command must return the expected login. After a successful browser/device login, configure Git's HTTPS credential helper once:

```powershell
& $gh auth login --hostname github.com --git-protocol https --web --skip-ssh-key
& $gh auth setup-git --hostname github.com
```

If `gh auth status` reports an invalid token or the API returns `401`, re-authenticate with the same installed `gh.exe` in a visible desktop PowerShell window, then rerun both verification commands. A restricted Codex process may be unable to access the user's Windows keyring; treat that as a credential-context problem, not proof that the token was revoked, and run GitHub writes in an approved desktop-user context. Never copy tokens into `GH_TOKEN`, repository files, `.env`, or plaintext configuration. Re-login is normally needed only after authorization is revoked, the keyring is cleared, the Windows user changes, or GitHub invalidates the token.

## Agent skills

### Issue tracker

Track work in GitHub Issues using `gh`. See `docs/agents/issue-tracker.md`.

### Triage labels

Use the five default Matt Pocock triage labels. See `docs/agents/triage-labels.md`.

### Domain docs

Use the single-context layout with root `CONTEXT.md` and `docs/adr/`. See `docs/agents/domain.md`.

## Environment

- This project uses Python 3.12.
- The project environment is the repository-local `.venv`.
- The environment is managed by `uv` from the repository root:

```powershell
uv sync --locked --python 3.12 --extra ch3
```

- Do not assume `python`, `py`, or `uv` is available on `PATH`. If `uv` is missing, use:

```powershell
& "$env:APPDATA\Python\Python312\Scripts\uv.exe" sync --locked --python 3.12 --extra ch3
```

## Verification

Run these from the repository root before considering code changes complete:

```powershell
uv run --extra dev pytest
uv run --extra dev ruff check .
uv run ai-intel-agent run --sample --output reports\daily.md
```

Use the full `uv.exe` path from the Environment section if `uv` is not on `PATH`.

## Conventions

- Keep project instructions short and scoped to facts needed for future agents to work correctly.
- Keep secrets in `.env`; commit only `.env.example`.
- Use `pyproject.toml` and `uv.lock` for dependency changes.

## Common Pitfalls

- Running raw `python`, `py`, or global `pip` may fail on this machine.
- Creating a new virtual environment with `python -m venv` bypasses the locked `uv` environment.
- The CLI is a subcommand app: use `ai-intel-agent run --sample`, not `ai-intel-agent --sample`.

## Do Not

- Do not commit API keys, GitHub passwords, tokens, or `.env`.
- Do not use or request a GitHub password for publishing. Use `gh auth login`, SSH, or a Personal Access Token.
- Do not use a Python executable or virtual environment from another repository.
