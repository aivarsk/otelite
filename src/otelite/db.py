import hashlib
import json
import logging
import sqlite3

log = logging.getLogger("otelite")

PRAGMAS = [
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = OFF",
    "PRAGMA busy_timeout = 5000",
    "PRAGMA wal_autocheckpoint = 0",
    "PRAGMA foreign_keys = OFF",
]

SPANS_DDL = """
CREATE TABLE IF NOT EXISTS spans (
  rowid        INTEGER PRIMARY KEY AUTOINCREMENT,
  trace_id     BLOB NOT NULL,              -- 16 bytes
  span_id      BLOB NOT NULL,              -- 8 bytes
  parent_id    BLOB,                       -- 8 bytes, NULL for roots
  name         TEXT NOT NULL,
  kind         INTEGER NOT NULL DEFAULT 0,
  status_code  INTEGER NOT NULL DEFAULT 0,
  status_msg   TEXT,
  service_name TEXT NOT NULL DEFAULT '',
  start_ns     INTEGER NOT NULL,
  end_ns       INTEGER NOT NULL,
  duration_ns  INTEGER GENERATED ALWAYS AS (end_ns - start_ns) VIRTUAL,
  attrs        TEXT NOT NULL DEFAULT '{}', -- span attributes as JSON object
  events       TEXT,                       -- [{timeUnixNano, name, attributes}]
  links        TEXT,                       -- [{traceId, spanId, attributes}]
  resource     TEXT NOT NULL DEFAULT '{}', -- resource attributes as JSON
  scope_name   TEXT,
  scope_ver    TEXT,
  dropped      INTEGER NOT NULL DEFAULT 0, -- combined dropped counts
  received_ns  INTEGER NOT NULL            -- server receipt time
);
CREATE INDEX IF NOT EXISTS ix_spans_trace   ON spans(trace_id);
CREATE INDEX IF NOT EXISTS ix_spans_service ON spans(service_name, start_ns);
CREATE INDEX IF NOT EXISTS ix_spans_time    ON spans(start_ns);
"""

METRICS_DDL = """
CREATE TABLE IF NOT EXISTS metrics (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE,
  kind        TEXT NOT NULL,               -- sum | gauge
  unit        TEXT,
  description TEXT,
  monotonic   INTEGER                      -- sum only
);
CREATE TABLE IF NOT EXISTS attrsets (
  id    INTEGER PRIMARY KEY,
  hash  TEXT NOT NULL UNIQUE,              -- sha256 of canonical attrs JSON
  attrs TEXT NOT NULL                      -- {"http.route": "/checkout", ...}
);
CREATE TABLE IF NOT EXISTS samples (
  ts           INTEGER NOT NULL,           -- ms, datapoint time / 1e6
  metric_id    INTEGER NOT NULL,
  attrset_id   INTEGER NOT NULL,
  service_name TEXT NOT NULL DEFAULT '',
  value        REAL NOT NULL,
  PRIMARY KEY (ts, metric_id, attrset_id, service_name)
) STRICT, WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_samples_mat_ts     ON samples (metric_id, attrset_id, ts);
CREATE INDEX IF NOT EXISTS ix_samples_service_ts ON samples (service_name, ts);
"""

MIGRATIONS = [SPANS_DDL, METRICS_DDL]
VERSION = len(MIGRATIONS)


def connect(path):
    conn = sqlite3.connect(str(path))
    for pragma in PRAGMAS:
        conn.execute(pragma)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > VERSION:
        log.warning("otelite.sqlite is version %s, newer than this otelite understands (%s)", version, VERSION)
    else:
        for index, script in enumerate(MIGRATIONS[version:], start=version):
            conn.executescript(script)
            conn.execute(f"PRAGMA user_version = {index + 1}")
    return conn


SPANS_SQL = """
INSERT OR IGNORE INTO spans (
  trace_id, span_id, parent_id, name, kind, status_code, status_msg,
  service_name, start_ns, end_ns, attrs, events, links, resource,
  scope_name, scope_ver, dropped, received_ns
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def insert_spans(conn, rows):
    conn.executemany(SPANS_SQL, rows)


def metric_id(conn, name, kind, unit, description, monotonic):
    conn.execute(
        "INSERT OR IGNORE INTO metrics (name, kind, unit, description, monotonic) VALUES (?, ?, ?, ?, ?)",
        (name, kind, unit, description, monotonic),
    )
    row = conn.execute("SELECT id FROM metrics WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise RuntimeError("metric insert did not stick")
    return int(row[0])


def attrset_id(conn, attrs):
    canonical = json.dumps(attrs, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    conn.execute("INSERT OR IGNORE INTO attrsets (hash, attrs) VALUES (?, ?)", (digest, canonical))
    row = conn.execute("SELECT id FROM attrsets WHERE hash = ?", (digest,)).fetchone()
    if row is None:
        raise RuntimeError("attrset insert did not stick")
    return int(row[0])


def insert_metric(conn, metric):
    mid = metric_id(
        conn, metric["name"], metric["kind"], metric.get("unit"), metric.get("description"), metric.get("monotonic")
    )
    for ts, value, attrs in metric["points"]:
        conn.execute(
            "INSERT OR IGNORE INTO samples (ts, metric_id, attrset_id, service_name, value) VALUES (?, ?, ?, ?, ?)",
            (ts, mid, attrset_id(conn, attrs), metric["service_name"], float(value)),
        )
