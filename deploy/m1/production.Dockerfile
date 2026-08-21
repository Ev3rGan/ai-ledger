FROM python:3.12.10-slim-bookworm@sha256:fd95fa221297a88e1cf49c55ec1828edd7c5a428187e67b5d1805692d11588db

ARG AI_INTEL_RELEASE
LABEL org.opencontainers.image.source="https://github.com/Ev3rGan/ai-ledger" \
      org.opencontainers.image.revision="${AI_INTEL_RELEASE}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AI_INTEL_PROJECT_ROOT=/opt/ai-ledger \
    PATH=/opt/ai-ledger/.venv/bin:${PATH}

RUN groupadd --gid 10001 ai-intel \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin ai-intel

WORKDIR /opt/ai-ledger
COPY pyproject.toml uv.lock README.md alembic.ini ./
COPY alembic ./alembic
COPY src ./src
RUN apt-get update \
    && apt-get install --yes --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir uv==0.12.3 \
    && uv sync --locked --no-dev --no-editable --extra retrieval

USER 10001:10001
ENTRYPOINT ["ai-intel-agent"]
