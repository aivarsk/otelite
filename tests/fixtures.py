"""Shared test fixtures: OTLP/JSON payloads and Sentry events.

One canonical copy of every request payload, imported by the integration
test files. Not named test_*.py, so unittest discovery never collects it.
"""

import json
import time

TRACE_ID = "5b8efff798038103d269b633813fc60c"
SPAN_ID = "eee19b7ec3c1b174"
PARENT_ID = "f2e78b4f7c3b2a19"

SENTRY_AUTH = {"X-Sentry-Auth": "Sentry sentry_version=7, sentry_client=test/1.0, sentry_key=deadbeef"}
SENTRY_EVENT_ID = "11112222333344445555666677778888"
SENTRY_FRAMES = [
    {"filename": "app/x.py", "function": "boom", "lineno": 7, "in_app": True},
    {"filename": "lib/y.py", "function": "call", "lineno": 42},
]


def traces_payload():
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "checkout"}},
                        {"key": "deployment.environment.name", "value": {"stringValue": "dev"}},
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "my.app", "version": "1.2.3"},
                        "spans": [
                            {
                                "traceId": TRACE_ID,
                                "spanId": SPAN_ID,
                                "parentSpanId": PARENT_ID,
                                "name": "POST /checkout",
                                "kind": 2,
                                "startTimeUnixNano": "1729328732051000000",
                                "endTimeUnixNano": 1729328732099000000,
                                "attributes": [
                                    {"key": "http.route", "value": {"stringValue": "/checkout"}},
                                    {"key": "retries", "value": {"intValue": "3"}},
                                    {"key": "ok", "value": {"boolValue": True}},
                                    {
                                        "key": "tags",
                                        "value": {"arrayValue": {"values": [{"stringValue": "a"}, {"intValue": 1}]}},
                                    },
                                ],
                                "events": [
                                    {
                                        "timeUnixNano": 1729328732060000000,
                                        "name": "cart.loaded",
                                        "attributes": [{"key": "cart.id", "value": {"intValue": 42}}],
                                    }
                                ],
                                "links": [{"traceId": TRACE_ID, "spanId": PARENT_ID}],
                                "status": {"code": "STATUS_CODE_ERROR", "message": "boom"},
                                "droppedAttributesCount": 1,
                            }
                        ],
                    }
                ],
            }
        ]
    }


def metrics_payload():
    return {
        "resourceMetrics": [
            {
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "checkout"}}]},
                "scopeMetrics": [
                    {
                        "metrics": [
                            {
                                "name": "checkout.requests",
                                "description": "requests",
                                "unit": "1",
                                "data": {
                                    "sum": {
                                        "dataPoints": [
                                            {
                                                "timeUnixNano": "1729328732051000000",
                                                "asInt": "42",
                                                "attributes": [{"key": "route", "value": {"stringValue": "/checkout"}}],
                                            }
                                        ],
                                        "isMonotonic": True,
                                        "aggregationTemporality": 2,
                                    }
                                },
                            },
                            {
                                "name": "queue.depth",
                                "data": {
                                    "gauge": {"dataPoints": [{"timeUnixNano": "1729328732051000000", "asDouble": 3.5}]}
                                },
                            },
                            {
                                "name": "latency.histogram",
                                "data": {"histogram": {"dataPoints": [{"timeUnixNano": "1729328732051000000"}]}},
                            },
                        ]
                    }
                ],
            }
        ]
    }


def logs_payload():
    return {
        "resourceLogs": [
            {
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "checkout"}}]},
                "scopeLogs": [
                    {
                        "scope": {"name": "my.app"},
                        "logRecords": [
                            {
                                "timeUnixNano": 1729328732051000000,
                                "severityNumber": 17,
                                "severityText": "ERROR",
                                "body": {"stringValue": "checkout failed for cart 42"},
                                "attributes": [{"key": "cart.id", "value": {"intValue": 42}}],
                                "traceId": TRACE_ID.upper(),
                                "spanId": SPAN_ID.upper(),
                            },
                            {
                                "observedTimeUnixNano": 1729328732100000000,
                                "severityNumber": 9,
                                "severityText": "INFO",
                                "body": {"stringValue": "recovered"},
                            },
                        ],
                    }
                ],
            }
        ]
    }


def sentry_event(event_id=SENTRY_EVENT_ID, kind="ValueError", value="bad input", fingerprint=None):
    event = {
        "event_id": event_id,
        "timestamp": time.time(),
        "level": "error",
        "platform": "python",
        "release": "test-1.0",
        "environment": "ci",
        "message": value,
        "exception": {"values": [{"type": kind, "value": value, "stacktrace": {"frames": SENTRY_FRAMES}}]},
        "contexts": {"trace": {"trace_id": TRACE_ID, "span_id": SPAN_ID}},
    }
    if fingerprint:
        event["fingerprint"] = fingerprint
    return event


def sentry_envelope(event, key="deadbeef"):
    header = {"event_id": event["event_id"], "dsn": f"http://{key}@127.0.0.1/1"}
    return (
        json.dumps(header).encode()
        + b"\n"
        + json.dumps({"type": "event"}).encode()
        + b"\n"
        + json.dumps(event).encode()
        + b"\n"
    )


def hour_range():
    now_ms = int(time.time() * 1000)
    return {"from": now_ms - 3600_000, "to": now_ms + 60_000}
