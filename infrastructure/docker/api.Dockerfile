# API and worker image. Build context: repository root.
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
# PostgreSQL 17 client tools (from the PostgreSQL project's apt repository) for `agentic-sdlc backup`;
# pg_dump must be at least as new as the server it dumps.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git util-linux ca-certificates curl gnupg \
 && install -d /usr/share/postgresql-common/pgdg \
 && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
      | gpg --dearmor -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.gpg \
 && . /etc/os-release \
 && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.gpg] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client-17 \
 && apt-get purge -y curl gnupg && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY services/api/pyproject.toml services/api/alembic.ini ./services/api/
COPY services/api/src ./services/api/src
COPY services/api/migrations ./services/api/migrations
COPY skills ./skills
COPY samples ./samples
COPY LICENSE THIRD_PARTY_NOTICES.md /usr/share/doc/agentic-sdlc/
# The sandbox runs acceptance and regression suites with the image's own interpreter.
RUN pip install ./services/api "pytest>=8.4"
# The sandbox uid used by the namespace backend. The worker needs CAP_SYS_ADMIN for unshare
# (see docker-compose.yml); the API container does not.
RUN useradd --system --uid 64000 --no-create-home --shell /usr/sbin/nologin sandbox \
 && useradd --system --uid 10001 --create-home app
WORKDIR /app/services/api
# /data is a shared volume: the API (uid 10001) stores uploads, the worker (root, for namespaces)
# writes workspaces and staging. The group-writable setgid directory keeps both able to write.
RUN mkdir -p /data && chown app:app /data && chmod 2775 /data
ENV DATA_DIR=/data SKILLS_DIR=/app/skills FIXTURES_DIR=/app/samples SANDBOX_PYTHON=/usr/local/bin/python \
    MIGRATIONS_DIR=/app/services/api/migrations
EXPOSE 8000
CMD ["uvicorn", "agentic_sdlc.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
