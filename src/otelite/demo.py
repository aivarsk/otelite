"""Built-in demo load: fills a fresh instance so the page shows something in seconds.

Pure stdlib and hand-built OTLP/JSON, so the zipapp keeps its zero dependency
promise. Started with the --demo flag; posts to our own ingest endpoints on a
daemon thread and dies with the process.
"""

import http.client
import json
import random
import time

SERVICES = {
    "frontend": ["GET /", "GET /product/42"],
    "checkout": ["POST /checkout", "POST /pay"],
}


def post(host, port, path, payload):
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request("POST", path, body=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    response = conn.getresponse()
    response.read()
    conn.close()
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
        time.sleep(interval)
