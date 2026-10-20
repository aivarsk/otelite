#!/usr/bin/env python3
"""Fill otelite with realistic-looking telemetry.

Traces go through the real OTEL Python SDK (real span objects, real timing,
real context propagation) and are translated to OTLP/JSON by a custom
exporter, because otelite is JSON-first and protobuf ingest is a later
decision. Metrics and logs are built as OTLP/JSON directly and correlated
with the active SDK span context.

Usage: gen.py --url http://127.0.0.1:4318 --seconds 60 --rps 20
"""

import argparse
import json
import random
import time
import urllib.request

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import Status, StatusCode

try:
    from opentelemetry.trace import NonRecordingSpan, set_span_in_parent_context
except ImportError:  # very old SDKs; cross-service calls degrade to local children
    NonRecordingSpan = None
    set_span_in_parent_context = None

SERVICES = {
    "frontend": ["GET /", "GET /product/42", "POST /cart/add"],
    "checkout": ["POST /checkout", "POST /pay"],
    "inventory": ["GET /stock/42", "PUT /stock/42"],
}
SERVICE_WEIGHTS = [("frontend", 50), ("checkout", 30), ("inventory", 20)]


def post(url, payload):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(request, timeout=10).read()


def attr_value(value):
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [attr_value(v) for v in value]}}
    return {"stringValue": str(value)}


def kv(key, value):
    return {"key": key, "value": attr_value(value)}


def group_spans(resource_attrs, scope_name, scope_version, spans):
    return {
        "resource": {"attributes": [kv(k, v) for k, v in resource_attrs.items()]},
        "scopeSpans": [
            {
                "scope": {"name": scope_name, "version": scope_version},
                "spans": spans,
            }
        ],
    }


class JsonTraceExporter(SpanExporter):
    """Translates SDK spans to OTLP/JSON and POSTs them to otelite."""

    def __init__(self, url):
        self.url = url + "/v1/traces"

    def export(self, spans):
        groups = {}
        for span in spans:
            resource_attrs = dict(span.resource.attributes) if span.resource else {}
            scope = span.instrumentation_scope
            scope_key = (scope.name if scope else "", scope.version if scope else "")
            key = (json.dumps(resource_attrs, sort_keys=True, default=str), *scope_key)
            otlp_span = {
                "traceId": f"{span.context.trace_id:032x}",
                "spanId": f"{span.context.span_id:016x}",
                "name": span.name,
                "kind": int(getattr(span.kind, "value", span.kind)),
                "startTimeUnixNano": str(span.start_time or 0),
                "endTimeUnixNano": str(span.end_time or 0),
                "attributes": [kv(k, v) for k, v in (span.attributes or {}).items()],
                "events": [
                    {
                        "timeUnixNano": str(event.timestamp),
                        "name": event.name,
                        "attributes": [kv(k, v) for k, v in (event.attributes or {}).items()],
                    }
                    for event in span.events or []
                ],
                "status": {
                    "code": 2 if span.status.status_code == StatusCode.ERROR else 1
                },
            }
            if span.parent is not None:
                otlp_span["parentSpanId"] = f"{span.parent.span_id:016x}"
            groups.setdefault(key, [resource_attrs, scope_key[0], scope_key[1], []])[3].append(otlp_span)
        payload = {
            "resourceSpans": [
                group_spans(resource, scope_name, scope_version, spans_list)
                for resource, scope_name, scope_version, spans_list in groups.values()
            ]
        }
        post(self.url, payload)
        return SpanExportResult.SUCCESS


def now_ns():
    return time.time_ns()


def emit_log(url, service, severity, body, span_context):
    number = {"DEBUG": 5, "INFO": 9, "WARN": 13, "ERROR": 17}[severity]
    record = {
        "timeUnixNano": str(now_ns()),
        "severityNumber": number,
        "severityText": severity,
        "body": {"stringValue": body},
        "attributes": [kv("log.source", "generator")],
    }
    if span_context is not None:
        record["traceId"] = f"{span_context.trace_id:032x}"
        record["spanId"] = f"{span_context.span_id:016x}"
    payload = {
        "resourceLogs": [
            {
                "resource": {"attributes": [kv("service.name", service)]},
                "scopeLogs": [{"scope": {"name": "generator"}, "logRecords": [record]}],
            }
        ]
    }
    post(url + "/v1/logs", payload)


class Metrics:
    def __init__(self, url):
        self.url = url + "/v1/metrics"
        self.requests = {service: 0 for service in SERVICES}
        self.errors = {service: 0 for service in SERVICES}
        self.cpu = {service: 5.0 for service in SERVICES}
        self.heap = {service: 40.0 for service in SERVICES}
        self._latencies = []

    def count(self, service, error):
        self.requests[service] += 1
        if error:
            self.errors[service] += 1

    def latency(self, service, elapsed_ms):
        self._latencies.append((now_ns() // 1_000_000, service, elapsed_ms))

    def _gauge(self, name, unit, service, value):
        return {
            "name": name,
            "unit": unit,
            "data": {
                "gauge": {
                    "dataPoints": [
                        {
                            "timeUnixNano": str(now_ns()),
                            "asDouble": round(value, 3),
                        }
                    ]
                }
            },
        }

    def _sum(self, name, unit, service, value):
        return {
            "name": name,
            "unit": unit,
            "data": {
                "sum": {
                    "dataPoints": [
                        {
                            "timeUnixNano": str(now_ns()),
                            "asInt": str(value),
                        }
                    ],
                    "isMonotonic": True,
                    "aggregationTemporality": 2,
                }
            },
        }

    def flush(self):
        resource_metrics = []
        for service in SERVICES:
            self.cpu[service] = min(max(self.cpu[service] + random.uniform(-3, 3), 1), 90)
            self.heap[service] = min(max(self.heap[service] + random.uniform(-2, 2), 20), 95)
            metrics = [
                self._gauge("process.cpu.utilization", "1", service, self.cpu[service]),
                self._gauge("process.memory.utilization", "1", service, self.heap[service]),
                self._sum("requests.served", "1", service, self.requests[service]),
                self._sum("requests.failed", "1", service, self.errors[service]),
            ]
            resource_metrics.append(
                {
                    "resource": {"attributes": [kv("service.name", service)]},
                    "scopeMetrics": [{"scope": {"name": "generator"}, "metrics": metrics}],
                }
            )
        # per-request latency gives the charts real subsecond density
        by_service = {}
        for ts, service, value in self._latencies:
            by_service.setdefault(service, []).append((ts, value))
        for service, points in by_service.items():
            resource_metrics.append(
                {
                    "resource": {"attributes": [kv("service.name", service)]},
                    "scopeMetrics": [
                        {
                            "scope": {"name": "generator"},
                            "metrics": [
                                {
                                    "name": "request.latency",
                                    "unit": "ms",
                                    "data": {
                                        "gauge": {
                                            "dataPoints": [
                                                {"timeUnixNano": str(ts * 1_000_000), "asDouble": value}
                                                for ts, value in points
                                            ]
                                        }
                                    },
                                }
                            ],
                        }
                    ],
                }
            )
        self._latencies = []
        post(self.url, {"resourceMetrics": resource_metrics})


def pick_service(rng):
    return rng.choices([name for name, _ in SERVICE_WEIGHTS], [w for _, w in SERVICE_WEIGHTS])[0]


def child_work(tracer, name, work_ms, error_rate, rng):
    with tracer.start_as_current_span(name) as span:
        time.sleep(work_ms / 1000)
        if rng.random() < error_rate:
            span.set_status(Status(StatusCode.ERROR))
            span.add_event(
                "exception",
                {"exception.type": "ValueError", "exception.message": f"{name} blew up"},
            )
            return True
        return False


def run_request(tracer_for, rng, service, route):
    tracer = tracer_for(service)
    method = route.split()[0]
    started = time.time()
    error = False
    with tracer.start_as_current_span(f"{service} {route}") as span:
        context = span.get_span_context()
        span.set_attributes({"http.method": method, "http.route": route.split(" ", 1)[1]})
        if service == "frontend" and set_span_in_parent_context is not None and rng.random() < 0.3:
            parent_context = set_span_in_parent_context(NonRecordingSpan(context))
            with tracer_for("checkout").start_as_current_span("checkout POST /checkout", context=parent_context) as checkout_span:
                checkout_span.set_attribute("http.method", "POST")
                error = child_work(tracer, "db charge", rng.uniform(4, 25), 0.08, rng)
        else:
            if rng.random() < 0.6:
                error = child_work(tracer, "db query", rng.uniform(2, 18), 0.05, rng)
            if rng.random() < 0.4:
                child_work(tracer, "cache lookup", rng.uniform(0.5, 3), 0.01, rng)
        status = 500 if error else 200
        span.set_attribute("http.status_code", status)
        elapsed_ms = int((time.time() - started) * 1000)
        emit_log(
            URL,
            service,
            "ERROR" if error else "INFO",
            f"{route} -> {status} in {elapsed_ms}ms",
            context,
        )
    return error, elapsed_ms


def main():
    global URL
    parser = argparse.ArgumentParser(description="Generate realistic telemetry into otelite")
    parser.add_argument("--url", default="http://127.0.0.1:4318")
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--rps", type=float, default=20)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    URL = args.url

    rng = random.Random(args.seed)
    tracers = {}

    def tracer_for(service):
        if service not in tracers:
            provider = TracerProvider(
                resource=Resource.create({"service.name": service, "deployment.environment.name": "gen"})
            )
            provider.add_span_processor(SimpleSpanProcessor(JsonTraceExporter(URL)))
            tracers[service] = provider.get_tracer("otelite-generator")
        return tracers[service]

    metrics = Metrics(URL)
    total = int(args.seconds * args.rps)
    deadline = time.time() + args.seconds
    interval = 1.0 / args.rps
    sent = 0
    next_metrics = time.time() + 1

    print(f"generating ~{total} requests at {args.rps} rps for {args.seconds}s into {URL}")
    while time.time() < deadline and sent < total:
        service = pick_service(rng)
        route = rng.choice(SERVICES[service])
        error, elapsed = run_request(tracer_for, rng, service, route)
        metrics.count(service, error)
        metrics.latency(service, elapsed)
        sent += 1
        if time.time() >= next_metrics:
            metrics.flush()
            next_metrics = time.time() + 1
        if sent % max(int(args.rps) * 10, 1) == 0:
            print(f"  {sent}/{total} requests")
        time.sleep(interval)
    metrics.flush()
    print(f"done: {sent} requests, open http://localhost:4318/ (or wherever otelite listens)")


if __name__ == "__main__":
    main()
