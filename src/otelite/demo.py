"""Built-in demo load: fills a fresh instance so the page shows something in seconds.

Pure stdlib and hand-built OTLP/JSON, so the zipapp keeps its zero dependency
promise. Started with the --demo flag; posts to our own ingest endpoints on a
daemon thread and dies with the process.
"""

import http.client
import json
import random
import time
from contextlib import closing
from datetime import datetime, timezone

SERVICES = {
    "frontend": ["GET /", "GET /product/42"],
    "checkout": ["POST /checkout", "POST /pay"],
}

SENTRY_KEY = "demokey"
SENTRY_PROJECT = "1"
DEMO_RELEASE = "demo-1.0.0"


def post(host, port, path, payload, headers=None):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    request_headers = {"Content-Type": "application/json"}
    request_headers.update(headers or {})
    with closing(http.client.HTTPConnection(host, port, timeout=5)) as conn:
        conn.request("POST", path, body=body, headers=request_headers)
        response = conn.getresponse()
        response.read()
        return response.status


def trace_batch(service, route, rng, now_ns, elapsed_ms, error):
    trace_id = rng.randbytes(16).hex()
    root_id = rng.randbytes(8).hex()
    child_id = rng.randbytes(8).hex()
    status = {"code": 2, "message": "boom"} if error else {"code": 1}
    spans = [
        {
            "traceId": trace_id,
            "spanId": root_id,
            "name": f"{service} {route}",
            "kind": 2,
            "startTimeUnixNano": str(now_ns - elapsed_ms * 1_000_000),
            "endTimeUnixNano": str(now_ns),
            "attributes": [{"key": "http.route", "value": {"stringValue": route}}],
            "status": status,
        },
        {
            "traceId": trace_id,
            "spanId": child_id,
            "parentSpanId": root_id,
            "name": "db query",
            "startTimeUnixNano": str(now_ns - (elapsed_ms - 2) * 1_000_000),
            "endTimeUnixNano": str(now_ns - 3_000_000),
            "status": status if error else {"code": 1},
        },
    ]
    return (
        {
            "resourceSpans": [
                {
                    "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": service}}]},
                    "scopeSpans": [{"spans": spans}],
                }
            ]
        },
        trace_id,
        root_id,
    )


def log_batch(service, trace_id, root_id, error, elapsed_ms):
    severity = "ERROR" if error else "INFO"
    body = f"{route_line(service)} -> {'500' if error else '200'} in {elapsed_ms}ms"
    return {
        "resourceLogs": [
            {
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": service}}]},
                "scopeLogs": [
                    {
                        "logRecords": [
                            {
                                "timeUnixNano": str(time.time_ns()),
                                "severityNumber": 17 if error else 9,
                                "severityText": severity,
                                "body": {"stringValue": body},
                                "traceId": trace_id,
                                "spanId": root_id,
                            }
                        ]
                    }
                ],
            }
        ]
    }


def route_line(service):
    return f"{service} request"


# two fixed exception shapes so the errors panel groups them like Sentry would;
# the trace context is what joins an error to the failing trace on the page
ERROR_SHAPES = [
    (
        "ZeroDivisionError",
        "division by zero in cart totals",
        [
            {"filename": "app/cart.py", "function": "totals", "lineno": 28, "in_app": True},
            {"filename": "app/cart.py", "function": "render", "lineno": 9, "in_app": True},
        ],
    ),
    (
        "ConnectionRefusedError",
        "payment gateway refused connection",
        [
            {"filename": "app/pay.py", "function": "charge", "lineno": 63, "in_app": True},
            {"filename": "socket.py", "function": "create_connection", "lineno": 200},
        ],
    ),
]


def sentry_envelope(service, trace_id, root_id, rng, port):
    kind, value, frames = rng.choice(ERROR_SHAPES)
    event = {
        "event_id": rng.randbytes(16).hex(),
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "level": "error",
        "platform": "python",
        "logger": "demo",
        "server_name": service,
        "release": DEMO_RELEASE,
        "environment": "dev",
        "message": value,
        "exception": {"values": [{"type": kind, "value": value, "stacktrace": {"frames": frames}}]},
        "contexts": {"trace": {"trace_id": trace_id, "span_id": root_id}},
        "tags": {"route": service},
    }
    header = {
        "event_id": event["event_id"],
        "dsn": f"http://{SENTRY_KEY}@127.0.0.1:{port}/{SENTRY_PROJECT}",
        "sdk": {"name": "otelite-demo", "version": "1.0"},
    }
    body = (
        json.dumps(header, separators=(",", ":")).encode()
        + b"\n"
        + json.dumps({"type": "event"}, separators=(",", ":")).encode()
        + b"\n"
        + json.dumps(event, separators=(",", ":")).encode()
        + b"\n"
    )
    headers = {
        "Content-Type": "application/x-sentry-envelope",
        "X-Sentry-Auth": f"Sentry sentry_version=7, sentry_client=demo/1.0, sentry_key={SENTRY_KEY}",
    }
    return f"/api/{SENTRY_PROJECT}/envelope/", body, headers


def metric_batch(service, elapsed_ms, served):
    return {
        "resourceMetrics": [
            {
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": service}}]},
                "scopeMetrics": [
                    {
                        "metrics": [
                            {
                                "name": "demo.request.latency",
                                "unit": "ms",
                                "data": {
                                    "gauge": {
                                        "dataPoints": [
                                            {"timeUnixNano": str(time.time_ns()), "asDouble": float(elapsed_ms)}
                                        ]
                                    }
                                },
                            },
                            {
                                "name": "demo.requests",
                                "unit": "1",
                                "data": {
                                    "sum": {
                                        "dataPoints": [{"timeUnixNano": str(time.time_ns()), "asInt": str(served)}],
                                        "isMonotonic": True,
                                        "aggregationTemporality": 2,
                                    }
                                },
                            },
                        ]
                    }
                ],
            }
        ]
    }


def serve(port, host="127.0.0.1", interval=0.3):
    rng = random.Random(11)
    served = 0
    while True:
        service = rng.choice(list(SERVICES))
        route = rng.choice(SERVICES[service])
        now_ns = time.time_ns()
        elapsed_ms = rng.randint(3, 60)
        error = rng.random() < 0.08
        served += 1
        batch, trace_id, root_id = trace_batch(service, route, rng, now_ns, elapsed_ms, error)
        post(host, port, "/v1/traces", batch)
        post(host, port, "/v1/logs", log_batch(service, trace_id, root_id, error, elapsed_ms))
        post(host, port, "/v1/metrics", metric_batch(service, elapsed_ms, served))
        if error:
            path, body, headers = sentry_envelope(service, trace_id, root_id, rng, port)
            post(host, port, path, body, headers)
        time.sleep(interval)
