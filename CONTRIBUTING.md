# Contributing

Thank you for contributing to AI Ledger.

## Before you start

- Search existing issues before opening a new one.
- Use an issue to agree on non-trivial changes before implementation.
- Report security problems privately as described in [SECURITY.md](SECURITY.md).
- Keep pull requests focused; do not include secrets, `.env`, or generated reports.

## Local setup

This project uses Python 3.12 and `uv`:

```powershell
uv sync --locked --python 3.12 --extra ch3
```

Before opening a pull request, run:

```powershell
uv run --extra dev pytest
uv run --extra dev ruff check .
uv run ai-intel-agent run --sample --output reports\daily.md
```

If `uv` is not on `PATH`, use the executable documented in `AGENTS.md`.

## Pull requests

- Link the issue the change addresses, using `Closes #<number>` when appropriate.
- Describe the intended behavior, non-goals, and verification performed.
- Update user-facing or architecture documentation when behavior changes.
- Respond to review feedback with a new commit rather than rewriting shared history.
