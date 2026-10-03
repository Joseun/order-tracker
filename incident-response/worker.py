"""Always-on worker that runs Claude Code for incidents queued by the receiver."""

import json
import os
import shutil
import socket
import sys
import time
from pathlib import Path

import claude_runner


DATA_DIR = claude_runner.DATA_DIR
POLL_SECONDS = float(os.getenv("INCIDENT_WORKER_POLL_SECONDS", "2"))
HEARTBEAT_FILE = Path(os.getenv("INCIDENT_WORKER_HEARTBEAT", "/tmp/incident-worker.heartbeat"))
STALE_SECONDS = claude_runner.AGENT_TIMEOUT_SECONDS + 120


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def read_record(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def assistant_state(record: dict | None) -> str | None:
    return (record or {}).get("assistant", {}).get("state")


def claim(incident_id: str) -> bool:
    try:
        descriptor = os.open(
            DATA_DIR / f"{incident_id}.claim", os.O_CREAT | os.O_EXCL | os.O_WRONLY
        )
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w") as claim_file:
        claim_file.write(socket.gethostname())
    return True


def claim_next() -> str | None:
    paths = sorted(DATA_DIR.glob("*.json"), key=lambda path: path.stat().st_mtime)
    for path in paths:
        record = read_record(path)
        if assistant_state(record) == "queued" and claim(record["id"]):
            return record["id"]
    return None


def fail_stale_jobs() -> None:
    now = time.time()
    for claim_path in DATA_DIR.glob("*.claim"):
        if now - claim_path.stat().st_mtime < STALE_SECONDS:
            continue
        record = read_record(claim_path.with_suffix(".json"))
        if assistant_state(record) == "running":
            claude_runner.update_incident(
                record["id"],
                claude_runner.finished("failed", error="Worker stopped before finishing."),
            )


def setup_error() -> str | None:
    if not os.getenv("CLAUDE_CODE_OAUTH_TOKEN"):
        return "Set CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token` to enable the agent."
    if not shutil.which(claude_runner.CLAUDE_COMMAND):
        return "Claude Code CLI is unavailable."
    if not claude_runner.WORKSPACE_DIR.is_dir():
        return f"Workspace {claude_runner.WORKSPACE_DIR} is unavailable."
    return None


def run_once() -> str | None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    HEARTBEAT_FILE.touch()
    fail_stale_jobs()
    incident_id = claim_next()
    if incident_id:
        log(f"Running agent for incident {incident_id}")
        claude_runner.run_agent(incident_id)
        log(f"Finished incident {incident_id}")
    return incident_id


def main() -> None:
    error = setup_error()
    if error:
        log(f"{error} Queued incidents stay queued until the worker restarts with it.")
    while True:
        if error:
            HEARTBEAT_FILE.touch()
        elif run_once():
            continue
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
