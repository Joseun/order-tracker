"""Run one read-only SQL query against the orders database and print JSON rows."""

import json
import os
import sqlite3
import sys
import time
from pathlib import Path


DB_PATH = Path(os.getenv("ORDER_DB_PATH", "/orders/orders.db"))
MAX_ROWS = 200
TIMEOUT_SECONDS = 5
DENIED_ACTIONS = {sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH}


def authorize(action, *_):
    return sqlite3.SQLITE_DENY if action in DENIED_ACTIONS else sqlite3.SQLITE_OK


def run_query(sql: str, db_path: Path = DB_PATH) -> dict:
    if not db_path.is_file():
        raise FileNotFoundError(f"Orders database not found at {db_path}")
    db = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        db.execute("PRAGMA query_only = ON")
        db.set_authorizer(authorize)
        deadline = time.monotonic() + TIMEOUT_SECONDS
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
        cursor = db.execute(sql)
        columns = [column[0] for column in cursor.description or []]
        rows = cursor.fetchmany(MAX_ROWS + 1)
    finally:
        db.close()
    return {
        "columns": columns,
        "rows": [list(row) for row in rows[:MAX_ROWS]],
        "truncated": len(rows) > MAX_ROWS,
    }


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: db_query.py \"SELECT ...\"")
    try:
        print(json.dumps(run_query(sys.argv[1]), default=str))
    except (sqlite3.Error, OSError) as error:
        sys.exit(f"Query failed: {error}")
