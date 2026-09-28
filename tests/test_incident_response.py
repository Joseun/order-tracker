import importlib.util
import json
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


def test_firing_alert_starts_copilot_headlessly(client, monkeypatch, tmp_path):
    launched = []

    class Process:
        pid = 4321

    def fake_popen(command, **kwargs):
        launched.append((command, kwargs))
        return Process()

    monkeypatch.setattr(incident_response, "ASSISTANT_ENABLED", True)
    monkeypatch.setattr(incident_response, "ASSISTANT_COMMAND", ["copilot"])
    monkeypatch.setattr(incident_response, "WORKSPACE_DIR", tmp_path)
    monkeypatch.setattr(incident_response.subprocess, "Popen", fake_popen)
    monkeypatch.setenv("GH_TOKEN", "test-token")

    response = client.post(
        "/alerts",
        json={"status": "firing", "alerts": [{"status": "firing"}]},
    )

    assert response.json()["assistant"] == "started"
    incident = client.get(f"/incidents/{response.json()['incident_id']}").json()
    assert incident["assistant"]["pid"] == 4321
    command, options = launched[0]
    assert "--allow-all-tools" in command
    assert "--no-ask-user" in command
    assert "--mode" in command and command[command.index("--mode") + 1] == "autopilot"
    assert options["cwd"] == tmp_path
    assert options["stdin"] is incident_response.subprocess.DEVNULL


def test_missing_github_token_records_assistant_not_started(client, monkeypatch):
    monkeypatch.setattr(incident_response, "ASSISTANT_ENABLED", True)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    response = client.post("/alerts", json={"status": "firing", "alerts": []})

    incident = client.get(f"/incidents/{response.json()['incident_id']}").json()
    assert incident["assistant"]["state"] == "not_started"