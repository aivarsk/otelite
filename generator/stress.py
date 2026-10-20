#!/usr/bin/env python3
"""Ingestion stress test for one signal.

Pure stdlib so the client never becomes the bottleneck excuse. Hammers one
endpoint with N concurrent keep-alive connections for a fixed time, then
cross-checks against /perl/stats how much actually landed after the writer
drains.

Usage:
  python3 generator/stress.py --signal traces --seconds 20 --concurrency 20
  python3 generator/stress.py --signal metrics --batch 50
  python3 generator/stress.py --signal logs --url http://127.0.0.1:4319
"""

import argparse
import http.client
import json
import random
import statistics
import threading
import time
import urllib.parse
import urllib.request
from collections import Counter


def _rid(rng, nbytes):
    return rng.randbytes(nbytes).hex()


def build_traces(n, rng):
    now = time.time_ns()
    trace_id = _rid(rng, 16)
    spans = [
        {
            "traceId": trace_id if i % 4 else _rid(rng, 16),
            "spanId": _rid(rng, 8),
            "name": "stress /endpoint",
            "kind": 2,
            "startTimeUnixNano": str(now - 50_000_000),
            "endTimeUnixNano": now,
            "attributes": [{"key": "i", "value": {"intValue": str(i)}}],
        }
        for i in range(n)
    ]
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "stress"}}]},
                "scopeSpans": [{"spans": spans}],
            }
        ]
    }


def build_metrics(n, rng):
    now = time.time_ns()
    req = rng.randbytes(4).hex()  # unique label set per request so points never collide on the samples key
    metrics = []
    for name, kind in (("stress.cpu", "gauge"), ("stress.heap", "gauge"), ("stress.requests", "sum")):
        points = []
        for i in range(n // 3 + 1):
            point = {
                "timeUnixNano": str(now - i * 1_000_000),
                "asDouble": rng.random() * 100,
                "attributes": [{"key": "req", "value": {"stringValue": req}}],
            }
            if kind == "sum":
                point["asInt"] = str(i)
                point.pop("asDouble")
            points.append(point)
        body = {"dataPoints": points}
        if kind == "sum":
            body["isMonotonic"] = True
            body["aggregationTemporality"] = 2
        metrics.append({"name": name, "unit": "1", "data": {kind: body}})
    return {"resourceMetrics": [{"scopeMetrics": [{"metrics": metrics}]}]}


def build_logs(n, rng):
    now = time.time_ns()
    records = [
        {
            "timeUnixNano": str(now - i),
            "severityNumber": 9,
            "severityText": "INFO",
            "body": {"stringValue": f"stress log line {i} lorem ipsum dolor sit amet"},
            "traceId": _rid(rng, 16),
        }
        for i in range(n)
    ]
    return {
        "resourceLogs": [
            {
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "stress"}}]},
                "scopeLogs": [{"logRecords": records}],
            }
        ]
    }


BUILDERS = {"traces": build_traces, "metrics": build_metrics, "logs": build_logs}
PATHS = {"traces": "/v1/traces", "metrics": "/v1/metrics", "logs": "/v1/logs"}
STAT_KEYS = {"traces": "spans", "metrics": "samples", "logs": "logs"}


def stats_totals(url):
    parsed = urllib.parse.urlsplit(url)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=10)
    conn.request("GET", "/perl/stats?from=0&to=4102444800000")
    response = conn.getresponse()
    data = json.loads(response.read())
    conn.close()
    return sum(service[STAT_KEYS[args.signal]] for service in data["services"])


def worker(host, port, path, make_payload, stop, results):
    conn = http.client.HTTPConnection(host, port, timeout=30)
    while not stop.is_set():
        payload = make_payload()
        started = time.perf_counter()
        try:
            conn.request("POST", path, body=payload, headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            response.read()
            status = response.status
        except Exception:
            status = -1
        results.append((status, time.perf_counter() - started))
    conn.close()


def percentile(sorted_values, pct):
    if not sorted_values:
        return 0.0
    index = min(int(len(sorted_values) * pct), len(sorted_values) - 1)
    return sorted_values[index]


def main():
    global args
    parser = argparse.ArgumentParser(description="Stress one otelite ingest endpoint")
    parser.add_argument("--signal", required=True, choices=sorted(BUILDERS))
    parser.add_argument("--url", default="http://127.0.0.1:4318")
    parser.add_argument("--seconds", type=float, default=20)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--batch", type=int, default=10)
    parser.add_argument("--seed", type=int, default=3)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    parsed = urllib.parse.urlsplit(args.url)

    def make_payload():
        return json.dumps(BUILDERS[args.signal](args.batch, rng), separators=(",", ":")).encode()

    stop = threading.Event()
    results = []

    before = stats_totals(args.url)
    threads = [
        threading.Thread(target=worker, args=(parsed.hostname, parsed.port, PATHS[args.signal], make_payload, stop, results), daemon=True)
        for _ in range(args.concurrency)
    ]
    started = time.perf_counter()
    for thread in threads:
        thread.start()
    time.sleep(args.seconds)
    stop.set()
    for thread in threads:
        thread.join(timeout=10)
    wall = time.perf_counter() - started

    statuses = Counter(status for status, _ in results)
    latencies = sorted(dt for _, dt in results)
    sent = len(results) * args.batch
    ok = statuses.get(200, 0)
    time.sleep(3)  # let the writer drain
    landed = stats_totals(args.url) - before

    print(f"signal:      {args.signal} ({args.batch} records/request, {args.concurrency} connections, {args.seconds:.0f}s)")
    print(f"requests:    {len(results)}  ({len(results) / wall:.0f} req/s)")
    print(f"records:     {sent} sent, {landed} landed after drain")
    print(f"throughput:  {sent / wall:.0f} records/s client-side")
    print(f"statuses:    {dict(statuses)}")
    print(
        "latency ms:  min {:.1f}  p50 {:.1f}  p95 {:.1f}  p99 {:.1f}  max {:.1f}".format(
            latencies[0] * 1000 if latencies else 0,
            percentile(latencies, 0.50) * 1000,
            percentile(latencies, 0.95) * 1000,
            percentile(latencies, 0.99) * 1000,
            latencies[-1] * 1000 if latencies else 0,
        )
    )


if __name__ == "__main__":
    main()
