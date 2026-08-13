FROM python:3.12.11-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.8.11 /uv /uvx /bin/

RUN apt-get update \
    && apt-get install --yes --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/runtime-benchmark
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev

USER 65532:65532
EXPOSE 8080

ENTRYPOINT [".venv/bin/python", "-m", "ai_intel_agent.runtime_workload"]
