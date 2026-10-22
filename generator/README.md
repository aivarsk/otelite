# Telemetry generator

Fills otelite with realistic-looking telemetry so the viewer page has
something to show.

`run.sh` does everything: creates `generator/.venv`, installs
`opentelemetry-sdk` into it (dev-only; `sentry_send.py` adds `sentry-sdk`
the same way), starts otelite from source on port 4318 with data in
`/tmp/otelite-generator`, and generates load.

```bash
./generator/run.sh                 # 60s at 20 rps
./generator/run.sh --seconds 300 --rps 50
OTELITE_PORT=14318 ./generator/run.sh
```

Traces go through the real OTEL SDK (span objects, timing, context) and
are translated to OTLP/JSON at export time, because otelite speaks
OTLP/HTTP JSON only. Metrics and logs are built as OTLP/JSON directly and
correlated with the active SDK span context, so the trace-to-logs join in
the viewer works.

Sentry errors go through the real Sentry Python SDK:
`generator/.venv/bin/pip install sentry-sdk` once, then
`python3 generator/sentry_send.py --url http://127.0.0.1:4318`. It captures
real exceptions with the DSN pointed at otelite, using the OTEL
instrumenter so the error events share trace ids with the exported OTLP
traces and the page's error-to-waterfall join is exercised against the
real client.

Ctrl+C (or `kill`) stops otelite; the data directory stays for inspection.

`stress.py` hammers one ingest endpoint and reports throughput, latency,
and how much actually landed (it cross-checks against `/perl/stats`):
`python3 generator/stress.py --signal traces|metrics|logs --seconds 20
--concurrency 20`.
