# otelite

*The garage edition of OpenTelemetry.*

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

## What you get

- `POST /v1/traces`, `/v1/metrics`, `/v1/logs` (OTLP/HTTP JSON, gzip accepted, 200 `{}` on acceptance)
- traces and metrics in one SQLite file, logs as JSONL files you grep; the files are the API
- one page for everything: trace list with waterfall, metric picker with charts, raw logs, one shared time range you can brush on any chart
- read APIs named for the garage: `/strace` for traces, `/awk` for metrics, `/grep` for logs, `/perl` for the per-service report
- zero runtime dependencies, standard library only, ships as a single zipapp
- measured on a laptop: 3,500+ requests per second, 20,000+ records, one process

## Small on purpose

No alerts, no dashboards-as-product, no retention machinery, no high
availability. Eventual consistency, garage grade. No fsync: if it dies, it
dies, and you start a new instance in seconds. When an instance grows too
big, seal it, keep it as history, spawn a fresh one.

## Development

```bash
make build    # dist/otelite-<version>.pyz
make test     # 45 tests
make lint     # ruff + mypy
./generator/run.sh            # demo data via the real OTEL SDK
python3 generator/stress.py --signal traces --seconds 20
```
