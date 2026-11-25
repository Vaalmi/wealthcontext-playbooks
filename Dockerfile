# No server keeps an access log: it would keep a path and a query as they were sent.
FROM ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336 AS uv

FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS base
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 PATH="/app/.venv/bin:$PATH"
COPY pyproject.toml uv.lock ./

FROM base AS backend
RUN uv sync --frozen --no-install-project --only-group server --no-cache
COPY backend ./backend
# The backend alone has the held-out cases, to compare a bundle with and to test a proposal.
COPY cases ./cases
# The firm's sources as committed: what the recorded cycle's people hand in. The recordings,
# so that the page can say before any reply whether a replay's was scripted.
COPY sources ./sources
COPY recordings ./recordings
COPY --chown=10001:10001 playbooks ./playbooks
RUN mkdir /data && chown 10001:10001 /data
USER 10001:10001
CMD ["python", "-m", "uvicorn", "--factory", "backend.main:create_app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header", "--no-access-log"]

FROM base AS triage_agent
RUN uv sync --frozen --no-install-project --only-group agent --no-cache
COPY modelkit ./modelkit
COPY triage_agent ./triage_agent
COPY recordings ./recordings
USER 10001:10001
CMD ["python", "-m", "uvicorn", "--factory", "triage_agent.entrypoint:create_app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header", "--no-access-log"]

# No case, no source, no playbook, and only the recording of its own.
FROM base AS curator
RUN uv sync --frozen --no-install-project --only-group agent --no-cache
COPY modelkit ./modelkit
COPY curator ./curator
COPY recordings/scripted-curation.json ./recordings/scripted-curation.json
COPY recordings/first-cycle-curation.json ./recordings/first-cycle-curation.json
USER 10001:10001
CMD ["python", "-m", "uvicorn", "--factory", "curator.entrypoint:create_app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header", "--no-access-log"]
