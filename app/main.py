import os
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from opentelemetry import _logs, metrics, trace
from opentelemetry._logs import SeverityNumber
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor,
    ConsoleLogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field


DB_PATH = Path(os.getenv("ORDER_DB_PATH", "data/orders.db"))
STATUSES = {"received", "preparing", "shipped", "delivered"}
ROUTE = "/api/orders/{order_id}"
resource = Resource.create({"service.name": "order-tracker"})

trace_provider = TracerProvider(resource=resource)
if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
    span_exporter = OTLPSpanExporter()
    trace_provider.add_span_processor(BatchSpanProcessor(span_exporter))
else:
    span_exporter = ConsoleSpanExporter()
    trace_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
trace.set_tracer_provider(trace_provider)
tracer = trace.get_tracer("order_tracker")

if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
    metric_exporter = OTLPMetricExporter()
else:
    metric_exporter = ConsoleMetricExporter()
metric_reader = PeriodicExportingMetricReader(
    metric_exporter, export_interval_millis=10_000
)
metric_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
metrics.set_meter_provider(metric_provider)
meter = metrics.get_meter("order_tracker")
lookup_requests = meter.create_counter("order.lookup.requests", unit="{request}")
lookup_duration = meter.create_histogram("order.lookup.duration", unit="s")

logger_provider = LoggerProvider(resource=resource)
if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
    log_exporter = OTLPLogExporter()
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
else:
    log_exporter = ConsoleLogRecordExporter()
    logger_provider.add_log_record_processor(SimpleLogRecordProcessor(log_exporter))
_logs.set_logger_provider(logger_provider)
otel_logger = logger_provider.get_logger("order_tracker")


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    with connect() as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                customer TEXT NOT NULL,
                item TEXT NOT NULL,
                priority TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        if db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0:
            now = datetime.now(timezone.utc)
            previous_month_end = now.replace(day=1) - timedelta(days=1)
            for order in (
                ("standard-1001", "Avery", "Notebook", "standard", "received", now),
                ("express-1002", "Sam", "Headphones", "express", "preparing", previous_month_end),
                ("standard-1003", "Riley", "Water bottle", "standard", "shipped", now),
            ):
                db.execute(
                    "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)",
                    (*order[:5], order[5].isoformat()),
                )


def as_dict(row):
    return dict(row) if row else None


def order_detail(row):
    order = as_dict(row)
    if order["priority"] == "express":
        placed_at = datetime.fromisoformat(order["created_at"])
        estimated_at = placed_at + timedelta(days=2)
        order["estimated_delivery"] = estimated_at.date().isoformat()
    return order


class NewOrder(BaseModel):
    customer: str = Field(min_length=1, max_length=80)
    item: str = Field(min_length=1, max_length=120)
    priority: str = "standard"


class StatusUpdate(BaseModel):
    status: str


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    try:
        yield
    finally:
        trace_provider.force_flush()
        metric_provider.force_flush()
        logger_provider.force_flush()


app = FastAPI(title="Order Tracker", lifespan=lifespan)


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent.parent / "static" / "index.html")


@app.get("/healthz")
def health():
    with connect() as db:
        db.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/api/orders")
def list_orders():
    with connect() as db:
        rows = db.execute("SELECT * FROM orders ORDER BY created_at DESC").fetchall()
    return [as_dict(row) for row in rows]


@app.get("/api/orders/{order_id}")
def get_order(order_id: str):
    started_at = time.perf_counter()
    status_code = 500
    with tracer.start_as_current_span(
        "GET " + ROUTE,
        attributes={"http.request.method": "GET", "http.route": ROUTE},
    ) as span:
        try:
            result = load_order(order_id)
            status_code = 200
            return result
        except HTTPException as error:
            status_code = error.status_code
            if status_code >= 500:
                span.set_status(trace.Status(trace.StatusCode.ERROR))
            raise
        except Exception as error:
            span.record_exception(error)
            span.set_status(trace.Status(trace.StatusCode.ERROR))
            raise
        finally:
            attributes = {
                "http.route": ROUTE,
                "http.response.status_code": status_code,
            }
            span.set_attribute("http.response.status_code", status_code)
            lookup_requests.add(1, attributes)
            lookup_duration.record(time.perf_counter() - started_at, attributes)
            severity = SeverityNumber.INFO
            severity_text = "INFO"
            if status_code == 404:
                severity = SeverityNumber.WARN
                severity_text = "WARN"
            elif status_code >= 500:
                severity = SeverityNumber.ERROR
                severity_text = "ERROR"
            otel_logger.emit(
                severity_number=severity,
                severity_text=severity_text,
                body="Order lookup completed",
                attributes=attributes,
                event_name="order.lookup",
            )


def load_order(order_id: str):
    with connect() as db:
        row = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Order not found")
    return order_detail(row)


@app.post("/api/orders", status_code=201)
def create_order(order: NewOrder):
    if order.priority not in {"standard", "express"}:
        raise HTTPException(422, "Priority must be standard or express")
    order_id = str(uuid4())
    with connect() as db:
        db.execute(
            "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)",
            (order_id, order.customer, order.item, order.priority, "received",
             datetime.now(timezone.utc).isoformat()),
        )
    return load_order(order_id)


@app.patch("/api/orders/{order_id}")
def update_status(order_id: str, update: StatusUpdate):
    if update.status not in STATUSES:
        raise HTTPException(422, "Invalid status")
    with connect() as db:
        cursor = db.execute(
            "UPDATE orders SET status = ? WHERE id = ?",
            (update.status, order_id),
        )
    if cursor.rowcount == 0:
        raise HTTPException(404, "Order not found")
    return load_order(order_id)
