import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient


SERVICE_PATH = Path(__file__).parents[1] / "incident-response" / "server.py"
SPEC = importlib.util.spec_from_file_location("incident_response_server", SERVICE_PATH)
incident_response = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = incident_response
SPEC.loader.exec_module(incident_response)

AGENT_PATH = Path(__file__).parents[1] / "incident-response" / "claude_runner.py"
AGENT_SPEC = importlib.util.spec_from_file_location("incident_response_agent", AGENT_PATH)
incident_agent = importlib.util.module_from_spec(AGENT_SPEC)
sys.modules[AGENT_SPEC.name] = incident_agent
AGENT_SPEC.loader.exec_module(incident_agent)
sys.modules["claude_runner"] = incident_agent


def load_service_module(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).parents[1] / "incident-response" / filename
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


incident_worker = load_service_module("incident_response_worker", "worker.py")
db_query = load_service_module("incident_response_db_query", "db_query.py")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(incident_response, "DATA_DIR", tmp_path)
    monkeypatch.setattr(incident_response, "ASSISTANT_ENABLED", False)
    monkeypatch.setattr(
        incident_response,
        "query_loki",
        lambda endpoint, start, end: [
            {"line": "Order lookup completed", "labels": {"http_route": endpoint}}
        ],
    )
    monkeypatch.setattr(
        incident_response,
        "query_tempo",
        lambda endpoint, start, end: [{"traceID": "trace-123"}],
    )
    return TestClient(incident_response.app)


def test_receive_and_read_back_grafana_alert(client, tmp_path):
    payload = {
        "receiver": "incident-response",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": "Order lookup 5xx responses"},
                "annotations": {"endpoint": "/api/orders/{order_id}"},
            }
        ],
    }

    response = client.post("/alerts", json=payload)

    assert response.status_code == 202
    incident_id = response.json()["incident_id"]
    stored = json.loads((tmp_path / f"{incident_id}.json").read_text())
    assert stored["payload"] == payload
    assert stored["endpoints"] == ["/api/orders/{order_id}"]
    assert stored["evidence_window"]["duration_seconds"] == 300
    assert stored["evidence"][0]["logs"][0]["line"] == "Order lookup completed"
    assert stored["evidence"][0]["traces"][0]["traceID"] == "trace-123"
    assert client.get(f"/incidents/{incident_id}").json() == stored
    assert client.get("/incidents").json()[0]["id"] == incident_id


def test_rejects_non_list_alerts(client):
    response = client.post("/alerts", json={"alerts": "invalid"})

    assert response.status_code == 422


def test_unknown_incident_is_not_found(client):
    response = client.get("/incidents/not-a-uuid")

    assert response.status_code == 404


def test_loki_query_filters_endpoint_as_structured_metadata(monkeypatch):
    urls = []
    monkeypatch.setattr(incident_response, "get_json", lambda url: urls.append(url) or {})

    incident_response.query_loki("/api/orders/{order_id}", 1, 2)

    query = parse_qs(urlparse(urls[0]).query)["query"][0]
    assert query == '{service_name="order-tracker"} | http_route="/api/orders/{order_id}"'


def test_firing_alert_is_queued_for_worker(client, monkeypatch):
    monkeypatch.setattr(incident_response, "ASSISTANT_ENABLED", True)

    response = client.post(
        "/alerts",
        json={"status": "firing", "alerts": [{"status": "firing"}]},
    )

    assert response.json()["assistant"] == "queued"
    incident_id = response.json()["incident_id"]
    assert client.get(f"/incidents/{incident_id}").json()["assistant"] == {"state": "queued"}
    assert client.get("/incidents").json()[0]["assistant"] == "queued"


def test_resolved_alert_is_not_queued(client, monkeypatch):
    monkeypatch.setattr(incident_response, "ASSISTANT_ENABLED", True)

    response = client.post("/alerts", json={"status": "resolved", "alerts": []})

    assert response.json()["assistant"] == "skipped"


@pytest.fixture
def worker_data(tmp_path, monkeypatch):
    monkeypatch.setattr(incident_worker, "DATA_DIR", tmp_path)
    monkeypatch.setattr(incident_agent, "DATA_DIR", tmp_path)
    monkeypatch.setattr(incident_worker, "HEARTBEAT_FILE", tmp_path / "heartbeat")
    return tmp_path


def write_record(directory, incident_id, state):
    record = {"id": incident_id, "assistant": {"state": state}}
    (directory / f"{incident_id}.json").write_text(json.dumps(record))


def test_worker_claims_each_queued_incident_once(worker_data, monkeypatch):
    queued = "11111111-1111-1111-1111-111111111111"
    write_record(worker_data, queued, "queued")
    write_record(worker_data, "22222222-2222-2222-2222-222222222222", "completed")
    ran = []
    monkeypatch.setattr(incident_agent, "run_agent", ran.append)

    assert incident_worker.run_once() == queued
    assert incident_worker.run_once() is None
    assert ran == [queued]
    assert (worker_data / "heartbeat").exists()


def test_worker_fails_stale_running_job(worker_data):
    incident_id = "33333333-3333-3333-3333-333333333333"
    write_record(worker_data, incident_id, "running")
    claim = worker_data / f"{incident_id}.claim"
    claim.write_text("old-worker")
    stale = incident_worker.time.time() - incident_worker.STALE_SECONDS - 1
    incident_worker.os.utime(claim, (stale, stale))

    incident_worker.fail_stale_jobs()

    record = json.loads((worker_data / f"{incident_id}.json").read_text())
    assert record["assistant"]["state"] == "failed"


def test_worker_reports_missing_claude_token(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    assert "CLAUDE_CODE_OAUTH_TOKEN" in incident_worker.setup_error()


@pytest.fixture
def orders_db(tmp_path):
    path = tmp_path / "orders.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT)")
    db.executemany("INSERT INTO orders (status) VALUES (?)", [("pending",), ("shipped",)])
    db.commit()
    db.close()
    return path


def test_db_query_returns_rows(orders_db):
    result = db_query.run_query("SELECT id, status FROM orders ORDER BY id", orders_db)

    assert result == {
        "columns": ["id", "status"],
        "rows": [[1, "pending"], [2, "shipped"]],
        "truncated": False,
    }


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE orders SET status = 'lost'",
        "DELETE FROM orders",
        "ATTACH DATABASE 'other.db' AS other",
    ],
)
def test_db_query_rejects_changes(orders_db, sql):
    with pytest.raises(sqlite3.Error):
        db_query.run_query(sql, orders_db)

    assert db_query.run_query("SELECT count(*) FROM orders", orders_db)["rows"] == [[2]]


def test_claude_command_is_headless_and_restricted(tmp_path):
    command = incident_agent.build_claude_command("Investigate the incident.")

    assert "--print" in command
    assert "--restricted" in command
    assert "--permission-prompts" in command
    assert command[command.index("--permission-prompts") + 1] == "none"
    assert "--dangerously-skip-permissions" not in command
    assert any(
        item == "Bash(env -u CLAUDE_CODE_OAUTH_TOKEN uv run --frozen --group dev pytest -q)"
        for item in command
    )
    assert incident_agent.DB_QUERY_TOOL in command
    assert "Bash" not in command[command.index("--allowedTools") + 1 :]