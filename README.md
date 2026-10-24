# otelite

*The garage edition of OTEL. A small, disposable OTEL backend for test and dev.*

A small OpenTelemetry backend for test and dev. Speaks OTLP/HTTP and
Sentry's error protocol on the same port, stores traces and metrics in
one SQLite file, logs and Sentry errors as JSONL on disk, and shows
them together on one small page. Python. No cluster, no vendor, no card
on file.

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
- Sentry error events on the same port: `POST /api/<project>/store/` or `/api/<project>/envelope/`, any project id, any or no key, nothing verified; errors land as JSONL grouped by exception
- traces and metrics in one SQLite file, logs and Sentry errors as JSONL files you grep; the files are the API
- one page for everything: trace list with waterfall, metric picker with charts, raw logs, Sentry error groups with the trace join, one shared time range you can brush on any chart
- read APIs named for the garage: `/strace` for traces, `/awk` for metrics, `/grep` for logs and errors, `/perl` for the per-service report
- zero runtime dependencies, standard library only, ships as a single zipapp
- measured on a laptop: 3,500+ requests per second, 20,000+ records, one process
- `otelite --demo` fills a fresh instance with sample telemetry so the page is never empty on first run

## Sentry errors

Any Sentry SDK sends to otelite once the DSN points at it. In
`http://mykey@localhost:4318/42` the `mykey` part is the public key and `42`
is the project id; both are labels, they end up in the file name
(`errors/<day>/42-mykey.jsonl`), nothing is verified:

```bash
export SENTRY_DSN=http://mykey@localhost:4318/42
```

```python
import sentry_sdk

sentry_sdk.init(dsn="http://mykey@localhost:4318/42", traces_sample_rate=1.0)
```

Two parameters matter for the page:

- `traces_sample_rate` (Python) / `tracesSampleRate` (JavaScript): error
  events only carry a trace id when tracing is on, and the trace id is
  what joins an error to its waterfall on the page.
- `instrumenter="otel"` (sentry-python): run Sentry on top of your OTEL
  tracer provider and point the OTLP exporter at otelite as well. Error
  events then share trace ids with the OTLP traces, and clicking an error
  opens the exact trace it came from.

Scope is error events only: transactions, replays, release health, source
maps, alerts, and the issue workflow stay with Sentry. Test and CI produce
errors; otelite keeps those, grouped by exception kind, in the run's own
data directory, next to the traces and logs from the same run.

## Sealed runs

Archive the data directory at the end of a CI run and anyone holding the
artifact can browse exactly what was collected, read-only:

```bash
tar czf run.tgz -C "$OTELITE_DATA_DIR" .
# or: zip -r run.zip "$OTELITE_DATA_DIR"

otelite --archive run.tgz    # read-only viewer on http://localhost:4318
```

The page, the read APIs, and `grep` work as usual; every OTLP and Sentry
ingestion call is refused with 403.

Browser SDKs: responses carry no CORS headers yet, so a browser SDK needs
same-origin serving or a local proxy. Server-side SDKs (Python, Node, Go,
Rust) just work.

## Small on purpose

No alerts, no dashboards-as-product, no retention machinery, no high
availability. Eventual consistency, garage grade. No fsync: if it dies, it
dies, and you start a new instance in seconds. When an instance grows too
big, seal it, keep it as history, spawn a fresh one.

## File formats (the API)

Traces and metrics live in `otelite.sqlite`, logs in `logs/<day>/<service>.jsonl`,
Sentry errors in `errors/<day>/<project>-<key>.jsonl`.
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
- error lines: the whole Sentry event JSON under `event` plus flattened
  `kind` (exception type), `message`, `release`, and `group` (client
  fingerprint or type+stacktrace hash, 16 hex chars). Same grep join.

Formats are stable for a given `user_version`; bumps recreate empty, this
is a dev tool.

## Development

```bash
make build    # dist/otelite-<version>.pyz
make test     # 79 tests
make lint     # ruff + mypy
./generator/run.sh            # demo data via the real OTEL SDK
python3 generator/stress.py --signal traces --seconds 20
```
