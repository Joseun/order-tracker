# Order Tracker

A small order tracking app for the AI Dev Tools Zoomcamp observability homework. It includes a web page, API, tests, and a Docker Compose observability stack with OpenTelemetry, Prometheus, Loki, Tempo, and Grafana.

The main user flow is creating an order and checking its status. Three sample orders are created on first startup.

## Run it

You need Docker with Compose. To run the tests, you also need Python 3.11+ and `uv`.

```bash
GH_TOKEN="$(gh auth token)" docker compose up --build -d --wait
```

Open <http://127.0.0.1:8000>. The API is at `/api/orders`, and the health check is at `/healthz`. Grafana is at <http://127.0.0.1:3000> (default login `admin` / `admin`) with the **Order Tracker Requests** dashboard provisioned. Alert incidents are listed at <http://127.0.0.1:8001/incidents>; open `/incidents/{incident_id}` for the original Grafana payload and the related Loki logs and Tempo traces. Set `GRAFANA_ADMIN_PASSWORD` to change the local Grafana password. Order, observability, and incident data are stored in Docker volumes.

The app sends OTLP metrics, logs, and traces to the Collector. The Collector exports metrics for Prometheus to scrape, logs to Loki, and traces to Tempo. Grafana sends alert notifications to the incident receiver's `POST /alerts` endpoint on port `8001`. Collector debug output can be inspected with `docker compose logs otel-collector`.

For firing alerts, the receiver starts GitHub Copilot CLI in headless autopilot mode in the mounted repository. The session reads its incident record from the data volume, and its output is saved to the incident's `.assistant.log` file. A GitHub CLI login with Copilot access is required; `GH_TOKEN` is passed only to the receiver and is marked as secret for assistant tool subprocesses. Set `INCIDENT_ASSISTANT_ENABLED=false` to disable automatic runs.

If port 8000 is occupied, set `ORDER_TRACKER_PORT`; if port 3000 is occupied, set `GRAFANA_PORT`. For example:

```bash
ORDER_TRACKER_PORT=18080 GRAFANA_PORT=13000 docker compose up --build -d --wait
```

Run tests with `uv run --frozen pytest -q`. Stop the stack with `docker compose down`. Add `-v` only if you also want to delete the order and observability data.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | Web page |
| GET | `/healthz` | Database health check |
| GET | `/api/orders` | List orders |
| POST | `/api/orders` | Create an order |
| GET | `/api/orders/{id}` | Check an order |
| PATCH | `/api/orders/{id}` | Change an order status |

The app uses SQLite to keep setup small. Run one app container at a time. The course exercise is about detecting and handling an incident, not scaling the database.
