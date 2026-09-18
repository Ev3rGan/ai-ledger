FROM node:22.23.2-bookworm-slim@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5 AS frontend-build

WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./frontend/
RUN npm --prefix frontend ci
COPY frontend ./frontend
RUN npm --prefix frontend run build

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
COPY --from=frontend-build /build/src/ai_intel_agent/static ./src/ai_intel_agent/static
COPY --from=frontend-build /build/src/ai_intel_agent/operator_static ./src/ai_intel_agent/operator_static
RUN apt-get update \
    && apt-get install --yes --no-install-recommends git git-lfs \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir uv==0.12.3 \
    && uv sync --locked --no-dev --no-editable --extra retrieval

USER 10001:10001
ENTRYPOINT ["ai-intel-agent"]
