# otelite

*The garage edition of OTEL. A small, disposable OTEL backend for test and dev.*

A small OpenTelemetry backend for test and dev. Speaks OTLP/HTTP, stores
traces and metrics in one SQLite file, logs as JSONL on disk, and shows
them together on one small page. Python. No cluster, no vendor, no card on
file.

## Quickstart

```bash
curl -fsSL https://otelite.io/install.sh | bash
```

Point your OTEL SDK at `http://localhost:4318` and open the same address
in a browser. That is the whole setup.

Rather not pipe install.sh? Download the latest `otelite-*.pyz` from the
releases page and run `python3 otelite-*.pyz`. Empty page on first run?
`otelite --demo` fills it with sample telemetry.

```bash
docker run --rm -p 4318:4318 ghcr.io/aivarsk/otelite:latest
# first run, empty page? add --demo
docker build -t otelite . && docker run -p 4318:4318 otelite
# or: docker compose up
```

## What you get

- `POST /v1/traces`, `/v1/metrics`, `/v1/logs` (OTLP/HTTP JSON, gzip accepted, 200 `{}` on acceptance)
- traces and metrics in one SQLite file, logs as JSONL files you grep; the files are the API
- one page for everything: trace list with waterfall, metric picker with charts, raw logs, one shared time range you can brush on any chart
- read APIs named for the garage: `/strace` for traces, `/awk` for metrics, `/grep` for logs, `/perl` for the per-service report
- zero runtime dependencies, standard library only, ships as a single zipapp
- measured on a laptop: 3,500+ requests per second, 20,000+ records, one process
- `otelite --demo` fills a fresh instance with sample telemetry so the page is never empty on first run

## Small on purpose

No alerts, no dashboards-as-product, no retention machinery, no high
availability. Eventual consistency, garage grade. No fsync: if it dies, it
dies, and you start a new instance in seconds. When an instance grows too
big, seal it, keep it as history, spawn a fresh one.

## File formats (the API)

Traces and metrics live in `otelite.sqlite`, logs in `logs/<day>/<service>.jsonl`.
The DDL in `src/otelite/db.py` is the spec, with the format notes as
comments on it; the shape in one breath:

- `spans`: hot columns (trace/span/parent ids as blobs, service, times,
  status) plus attrs, events, and links as JSON text. Indexed by trace id,
  service plus time, and time.
- `metrics`: dictionary tables for names and attribute sets, a raw
  `samples` table keyed by (ms timestamp, metric, attributes, service).
  No rollups, charts read raw rows.
- log lines: one flattened JSON record per line, nanosecond timestamps,
  ids kept as hex strings so `grep <trace-id>` is the trace join.

Formats are stable for a given `user_version`; bumps recreate empty, this
is a dev tool.

## Development

```bash
make build    # dist/otelite-<version>.pyz
make test     # 45 tests
make lint     # ruff + mypy
./generator/run.sh            # demo data via the real OTEL SDK
python3 generator/stress.py --signal traces --seconds 20
```
