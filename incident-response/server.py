import json
import os
import re
import shlex
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException


DATA_DIR = Path(os.getenv("INCIDENT_DATA_DIR", "data/incidents"))
LOKI_URL = os.getenv("LOKI_URL", "http://localhost:3100").rstrip("/")
TEMPO_URL = os.getenv("TEMPO_URL", "http://localhost:3200").rstrip("/")
DEFAULT_WINDOW_SECONDS = 5 * 60
ASSISTANT_ENABLED = os.getenv("INCIDENT_ASSISTANT_ENABLED", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
ASSISTANT_COMMAND = shlex.split(os.getenv("INCIDENT_ASSISTANT_COMMAND", "copilot"))
WORKSPACE_DIR = Path(os.getenv("INCIDENT_WORKSPACE_DIR", "/workspace"))
app = FastAPI(title="Incident Response")


def incident_path(incident_id: str) -> Path:
    try:
        parsed_id = UUID(incident_id)
    except ValueError as error:
        raise HTTPException(404, "Incident not found") from error
    return DATA_DIR / f"{parsed_id}.json"


def extract_endpoints(payload: dict[str, Any]) -> list[str]:
    endpoints = set()
    for alert in payload.get("alerts", []):
        labels = alert.get("labels") or {}
        annotations = alert.get("annotations") or {}
        endpoint = annotations.get("endpoint") or labels.get("http_route")
        if endpoint:
            endpoints.add(str(endpoint))
    return sorted(endpoints)


def extract_window_seconds(payload: dict[str, Any]) -> int:
    for alert in payload.get("alerts", []):
        annotations = alert.get("annotations") or {}
        match = re.fullmatch(r"(\d+)(s|m|h)", str(annotations.get("time_window", "")))
        if match:
            value = int(match.group(1))
            scale = {"s": 1, "m": 60, "h": 3600}[match.group(2)]
            return min(max(value * scale, 1), 24 * 3600)
    return DEFAULT_WINDOW_SECONDS


def get_json(url: str) -> dict[str, Any]:
    with urlopen(url, timeout=3) as response:
        return json.load(response)


def query_loki(endpoint: str | None, start_ns: int, end_ns: int) -> list[dict[str, Any]]:
    query = '{service_name="order-tracker"}'
    if endpoint:
        query += f" | http_route={json.dumps(endpoint)}"
    params = urlencode(
        {
            "query": query,
            "start": start_ns,
            "end": end_ns,
            "direction": "backward",
            "limit": 100,
        }
    )
    result = get_json(f"{LOKI_URL}/loki/api/v1/query_range?{params}")
    logs = []
    for stream in result.get("data", {}).get("result", []):
        for timestamp, line in stream.get("values", []):
            logs.append(
                {"timestamp": timestamp, "line": line, "labels": stream.get("stream", {})}
            )
    return logs


def query_tempo(endpoint: str | None, start: int, end: int) -> list[dict[str, Any]]:
    params = [
        ("start", start),
        ("end", end),
        ("limit", 20),
        ("tags", "service.name=order-tracker"),
    ]
    if endpoint:
        params.append(("tags", f"http.route={endpoint}"))
    result = get_json(f"{TEMPO_URL}/api/search?{urlencode(params)}")
    return result.get("traces", [])


def collect_endpoint_evidence(
    endpoint: str | None, start_ns: int, end_ns: int
) -> dict[str, Any]:
    start_seconds = start_ns // 1_000_000_000
    end_seconds = end_ns // 1_000_000_000
    evidence: dict[str, Any] = {"endpoint": endpoint}
    for key, query, args in (
        ("logs", query_loki, (endpoint, start_ns, end_ns)),
        ("traces", query_tempo, (endpoint, start_seconds, end_seconds)),
    ):
        try:
            evidence[key] = query(*args)
        except (OSError, TimeoutError, URLError, ValueError) as error:
            evidence[key] = {"error": str(error)}
    return evidence


def write_incident(record: dict[str, Any], path: Path) -> None:
    temporary_path = path.with_suffix(".tmp")
    temporary_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    temporary_path.replace(path)


def start_assistant(record: dict[str, Any]) -> dict[str, Any]:
    if not ASSISTANT_ENABLED:
        return {"state": "disabled"}
    if not any(os.getenv(key) for key in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")):
        return {"state": "not_started", "error": "Set GH_TOKEN to authenticate Copilot CLI."}
    if not ASSISTANT_COMMAND or not WORKSPACE_DIR.is_dir():
        return {"state": "not_started", "error": "Assistant command or workspace is unavailable."}

    incident_file = DATA_DIR / f"{record['id']}.json"
    prompt = (
        f"Investigate Grafana incident {record['id']}. Read {incident_file} for the alert, "
        "affected endpoint, time window, logs, and traces. Inspect this repository, identify "
        "the most likely cause, implement a focused fix if supported by the evidence, and run "
        "relevant tests. Do not modify the incident archive. Summarize findings and changed "
        "files in your final response."
    )
    command = [
        *ASSISTANT_COMMAND,
        "-C",
        str(WORKSPACE_DIR),
        "--add-dir",
        str(DATA_DIR),
        "-p",
        prompt,
        "--allow-all-tools",
        "--no-ask-user",
        "--no-auto-update",
        "--mode",
        "autopilot",
        "--max-autopilot-continues",
        "3",
        "--secret-env-vars",
        "GH_TOKEN",
        "COPILOT_GITHUB_TOKEN",
        "GITHUB_TOKEN",
    ]
    log_path = DATA_DIR / f"{record['id']}.assistant.log"
    try:
        with log_path.open("ab") as output:
            process = subprocess.Popen(
                command,
                cwd=WORKSPACE_DIR,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    except OSError as error:
        return {"state": "not_started", "error": str(error)}
    return {"state": "started", "pid": process.pid, "log_file": str(log_path)}


@app.get("/healthz")
def health():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return {"status": "ok"}


@app.post("/alerts", status_code=202)
def receive_alert(payload: dict[str, Any]):
    if not isinstance(payload.get("alerts", []), list):
        raise HTTPException(422, "alerts must be a list")

    incident_id = str(uuid4())
    received_at = time.time_ns()
    window_seconds = extract_window_seconds(payload)
    start_ns = received_at - window_seconds * 1_000_000_000
    endpoints = extract_endpoints(payload)
    record = {
        "id": incident_id,
        "received_at": datetime.fromtimestamp(
            received_at / 1_000_000_000, timezone.utc
        ).isoformat(),
        "status": payload.get("status", "unknown"),
        "endpoints": endpoints,
        "evidence_window": {
            "start": datetime.fromtimestamp(
                start_ns / 1_000_000_000, timezone.utc
            ).isoformat(),
            "end": datetime.fromtimestamp(
                received_at / 1_000_000_000, timezone.utc
            ).isoformat(),
            "duration_seconds": window_seconds,
        },
        "evidence": [
            collect_endpoint_evidence(endpoint, start_ns, received_at)
            for endpoint in endpoints or [None]
        ],
        "payload": payload,
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = incident_path(incident_id)
    write_incident(record, path)
    is_firing = payload.get("status") == "firing" or any(
        alert.get("status") == "firing" for alert in payload.get("alerts", [])
    )
    record["assistant"] = (
        start_assistant(record) if is_firing else {"state": "skipped", "reason": "resolved"}
    )
    write_incident(record, path)
    return {
        "incident_id": incident_id,
        "status": "accepted",
        "assistant": record["assistant"]["state"],
    }


@app.get("/incidents")
def list_incidents():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    incidents = []
    for path in sorted(DATA_DIR.glob("*.json"), reverse=True):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        incidents.append(
            {
                "id": record["id"],
                "received_at": record["received_at"],
                "status": record["status"],
                "endpoints": record["endpoints"],
            }
        )
    return incidents


@app.get("/incidents/{incident_id}")
def get_incident(incident_id: str):
    path = incident_path(incident_id)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise HTTPException(404, "Incident not found") from error
    except json.JSONDecodeError as error:
        raise HTTPException(500, "Incident record is invalid") from error