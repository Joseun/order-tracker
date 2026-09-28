FROM node:22-bookworm-slim

COPY --from=ghcr.io/astral-sh/uv:0.8.22 /uv /uvx /bin/
RUN apt-get update \
	&& apt-get install -y --no-install-recommends python3 git ca-certificates \
	&& rm -rf /var/lib/apt/lists/*
RUN npm install --global @github/copilot@1.0.89
RUN git config --global --add safe.directory /workspace
WORKDIR /service
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --python /usr/bin/python3
COPY incident-response/server.py ./server.py
ENV INCIDENT_DATA_DIR=/data
ENV UV_PROJECT_ENVIRONMENT=/service/.venv
EXPOSE 8001
CMD ["uv", "run", "--no-sync", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8001"]