FROM node:22-bookworm-slim

COPY --from=ghcr.io/astral-sh/uv:0.8.22 /uv /uvx /bin/
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN npm install --global @anthropic-ai/claude-code@2.1.288
RUN git config --global --add safe.directory '*'
WORKDIR /service
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --python /usr/bin/python3
COPY incident-response/server.py incident-response/claude_runner.py incident-response/worker.py incident-response/db_query.py ./
ENV INCIDENT_DATA_DIR=/data
ENV INCIDENT_WORKSPACE_DIR=/workspace
ENV INCIDENT_WORKTREES_DIR=/worktrees
ENV CLAUDE_CODE_COMMAND=claude
EXPOSE 8001
CMD ["uv", "run", "--no-sync", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8001"]