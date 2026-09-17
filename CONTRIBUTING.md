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

## Frontend assets

Browse, Research, and the Operator Console use a locked Vue/Vite workspace under
`frontend/`. Node is required only to test and build these browser entry points;
it is not part of the production runtime. The Operator SPA starts at
`frontend/src/operator.js`, uses `frontend/vite.operator.config.js`, and is loaded
by `src/ai_intel_agent/templates/operator.html`.

From the repository root, install exactly the locked packages, run the frontend
tests, and rebuild the hashed assets with:

```powershell
npm --prefix frontend ci
npm --prefix frontend test
npm --prefix frontend run build
```

The build refreshes the committed Vite manifests and hashed files under both
`src/ai_intel_agent/static/` and `src/ai_intel_agent/operator_static/`. Include
the matching generated changes whenever public or Operator frontend source changes
so the Python application and packaged wheel serve the reviewed build.

The production Dockerfile repeats `npm ci` and both Vite builds in a pinned Node
builder stage. Only the generated static directories cross into the final Python
stage; Node, npm, the frontend sources, and `node_modules` are not runtime content.
CI rebuilds the committed assets and rejects drift before building that image.

## Pull requests

- Link the issue the change addresses, using `Closes #<number>` when appropriate.
- Describe the intended behavior, non-goals, and verification performed.
- Update user-facing or architecture documentation when behavior changes.
- Respond to review feedback with a new commit rather than rewriting shared history.
