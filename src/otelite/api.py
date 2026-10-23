import collections
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db

MAX_TRACES = 200
MAX_LOG_LINES = 1000
MAX_ERROR_RECORDS = 5000
MAX_SERIES_POINTS = 5000


def _dict():
    return dict()


def _connect(cfg):
    return db.connect(cfg.db_path)


def traces(cfg, from_ms, to_ms, service=None):
    sql = """
    SELECT trace_id,
           min(start_ns) AS start_ns,
           max(end_ns) - min(start_ns) AS duration_ns,
           count(*) AS spans,
           coalesce(sum(status_code = 2), 0) AS errors,
           (SELECT name FROM spans r WHERE r.trace_id = spans.trace_id ORDER BY (r.parent_id IS NULL) DESC, r.start_ns LIMIT 1) AS root,
           (SELECT service_name FROM spans r WHERE r.trace_id = spans.trace_id ORDER BY (r.parent_id IS NULL) DESC, r.start_ns LIMIT 1) AS service
    FROM spans
    WHERE start_ns >= ? AND start_ns <= ?
    """
    params = [from_ms * 1_000_000, to_ms * 1_000_000]
    if service:
        sql += " AND service_name = ?"
        params.append(service)
    sql += " GROUP BY trace_id ORDER BY start_ns DESC LIMIT ?"
    params.append(MAX_TRACES)
    conn = _connect(cfg)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return {
        "traces": [
            {
                "trace_id": row[0].hex(),
                "start_ms": row[1] // 1_000_000,
                "duration_ms": row[2] // 1_000_000,
                "spans": row[3],
                "errors": row[4],
                "root": row[5] or "",
                "service": row[6] or "",
            }
            for row in rows
        ]
    }


def trace(cfg, trace_id_hex):
    try:
        trace_id = bytes.fromhex(trace_id_hex)
    except ValueError:
        return None
    conn = _connect(cfg)
    rows = conn.execute(
        """
        SELECT hex(span_id), hex(parent_id), name, kind, status_code, status_msg,
               service_name, start_ns, end_ns, attrs, events
        FROM spans WHERE trace_id = ? ORDER BY start_ns
        """,
        (trace_id,),
    ).fetchall()
    conn.close()
    return {
        "spans": [
            {
                "span_id": row[0],
                "parent_id": row[1] or None,
                "name": row[2],
                "kind": row[3],
                "status_code": row[4],
                "status_msg": row[5],
                "service": row[6],
                "start_ms": row[7] // 1_000_000,
                "duration_ms": (row[8] - row[7]) // 1_000_000,
                "attrs": json.loads(row[9] or "{}"),
                "events": json.loads(row[10] or "[]"),
            }
            for row in rows
        ]
    }


def metrics(cfg):
    conn = _connect(cfg)
    rows = conn.execute(
        """
        SELECT m.id, m.name, m.kind, coalesce(m.unit, ''), coalesce(m.description, ''),
               m.monotonic, count(s.ts)
        FROM metrics m LEFT JOIN samples s ON s.metric_id = m.id
        GROUP BY m.id ORDER BY m.name
        """
    ).fetchall()
    conn.close()
    groups = collections.defaultdict(list)
    for row in rows:
        key = (row[3], row[2])
        groups[key].append({"id": row[0], "name": row[1], "description": row[4], "samples": row[6]})
    return {
        "groups": [
            {
                "unit": unit or "unitless",
                "kind": kind,
                "metrics": metrics_in_group,
            }
            for (unit, kind), metrics_in_group in sorted(groups.items())
        ]
    }


def series(cfg, metric_ids, from_ms, to_ms):
    if not metric_ids:
        return {"series": []}
    placeholders = ",".join("?" for _ in metric_ids)
    sql = f"""
    SELECT s.metric_id, m.name, s.service_name, s.ts, s.value
    FROM samples s JOIN metrics m ON m.id = s.metric_id
    WHERE s.metric_id IN ({placeholders}) AND s.ts >= ? AND s.ts <= ?
    ORDER BY s.ts
    LIMIT {MAX_SERIES_POINTS}
    """
    conn = _connect(cfg)
    rows = conn.execute(sql, [*metric_ids, from_ms, to_ms]).fetchall()
    conn.close()
    grouped = collections.defaultdict(list)
    for metric_id, name, service, ts, value in rows:
        key = (metric_id, name, service)
        grouped[key].append((ts, value))
    return {
        "series": [
            {
                "metric_id": key[0],
                "name": key[1],
                "service_name": key[2],
                "points": [[ts, value] for ts, value in points],
            }
            for key, points in grouped.items()
        ]
    }


def _scan_day_records(records_dir, from_ms, to_ms, service=None, q=None):
    needle = q.lower() if q else None
    start_day = datetime.fromtimestamp(from_ms / 1000, timezone.utc).date()
    end_day = datetime.fromtimestamp(to_ms / 1000, timezone.utc).date()
    day = start_day
    while day <= end_day:
        day_dir = Path(records_dir) / day.isoformat()
        if day_dir.is_dir():
            if service:
                files = [day_dir / f"{service}.jsonl"]
            else:
                files = sorted(day_dir.glob("*.jsonl"))
            for path in files:
                if not path.exists():
                    continue
                with open(path, encoding="utf-8") as handle:
                    for line in handle:
                        if needle and needle not in line.lower():
                            continue
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        ts_ms = record.get("time_unix_nano", 0) // 1_000_000
                        if ts_ms < from_ms or ts_ms > to_ms:
                            continue
                        yield ts_ms, record
        day += timedelta(days=1)


def logs(cfg, from_ms, to_ms, service=None, q=None):
    # global timeline: interleave services by record time, cap keeps the earliest lines
    matches = sorted(_scan_day_records(cfg.logs_dir, from_ms, to_ms, service, q), key=lambda item: item[0])
    return {
        "logs": [
            {
                "time_ms": ts_ms,
                "severity": record.get("severity_text", ""),
                "service": record.get("service_name", ""),
                "body": record.get("body", ""),
                "trace_id": record.get("trace_id", ""),
                "span_id": record.get("span_id", ""),
            }
            for ts_ms, record in matches[:MAX_LOG_LINES]
        ]
    }


def logs_series(cfg, from_ms, to_ms, service=None, q=None, buckets=120):
    span_ms = max(to_ms - from_ms, 1)
    bucket_ms = max(-(-span_ms // buckets), 1)
    count = max(-(-span_ms // bucket_ms), 1)
    severities = {}
    for ts_ms, record in _scan_day_records(cfg.logs_dir, from_ms, to_ms, service, q):
        severity = record.get("severity_text", "") or "UNSPECIFIED"
        if severity not in severities:
            severities[severity] = [0] * count
        series = severities[severity]
        index = (ts_ms - from_ms) // bucket_ms
        series[min(max(index, 0), count - 1)] += 1
    return {
        "from_ms": from_ms,
        "to_ms": to_ms,
        "bucket_ms": bucket_ms,
        "severities": severities,
    }


def errors(cfg, from_ms, to_ms, service=None, q=None):
    # Sentry errors grouped the way Sentry does: one row per group (client
    # fingerprint or exception type plus stacktrace), the latest occurrence
    # riding along for the drill-down, sorted by hit count
    matches = sorted(_scan_day_records(cfg.errors_dir, from_ms, to_ms, service, q), key=lambda item: item[0])
    groups = _dict()
    for ts_ms, record in matches[:MAX_ERROR_RECORDS]:
        key = record.get("group") or record.get("event_id") or str(ts_ms)
        group = groups.get(key)
        if group is None:
            group = groups[key] = {
                "group": key,
                "kind": record.get("kind", ""),
                "value": record.get("value", ""),
                "message": record.get("message", ""),
                "level": record.get("level", ""),
                "count": 0,
                "first_ms": ts_ms,
                "last_ms": ts_ms,
                "services": set(),
                "releases": set(),
                "trace_id": record.get("trace_id", ""),
                "event": record,
            }
        group["count"] += 1
        group["first_ms"] = min(group["first_ms"], ts_ms)
        group["last_ms"] = max(group["last_ms"], ts_ms)
        group["services"].add(record.get("service_name", ""))
        group["releases"].add(record.get("release", ""))
        if record.get("trace_id"):
            group["trace_id"] = record["trace_id"]
        if ts_ms >= group["last_ms"]:
            group["event"] = record
    ordered = sorted(groups.values(), key=lambda g: (-g["count"], g["kind"], g["message"]))
    return {
        "errors": [
            {
                "group": g["group"],
                "kind": g["kind"],
                "value": g["value"],
                "message": g["message"],
                "level": g["level"],
                "count": g["count"],
                "first_ms": g["first_ms"],
                "last_ms": g["last_ms"],
                "services": sorted(s for s in g["services"] if s),
                "releases": sorted(s for s in g["releases"] if s),
                "trace_id": g["trace_id"],
                "event": g["event"].get("event", {}),
            }
            for g in ordered
        ]
    }


def _count_day_records(records_dir, from_ms, to_ms):
    counts = _dict()
    start_day = datetime.fromtimestamp(from_ms / 1000, timezone.utc).date()
    end_day = datetime.fromtimestamp(to_ms / 1000, timezone.utc).date()
    day = start_day
    while day <= end_day:
        day_dir = Path(records_dir) / day.isoformat()
        if day_dir.is_dir():
            for path in day_dir.glob("*.jsonl"):
                service = path.stem
                with open(path, encoding="utf-8") as handle:
                    for line in handle:
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        ts_ms = record.get("time_unix_nano", 0) // 1_000_000
                        if from_ms <= ts_ms <= to_ms:
                            counts[service] = counts.get(service, 0) + 1
        day += timedelta(days=1)
    return counts


def errors_series(cfg, from_ms, to_ms, service=None, q=None, buckets=120):
    span_ms = max(to_ms - from_ms, 1)
    bucket_ms = max(-(-span_ms // buckets), 1)
    count = max(-(-span_ms // bucket_ms), 1)
    kinds = _dict()
    for ts_ms, record in _scan_day_records(cfg.errors_dir, from_ms, to_ms, service, q):
        kind = record.get("kind") or "unknown"
        if kind not in kinds:
            kinds[kind] = [0] * count
        series = kinds[kind]
        index = (ts_ms - from_ms) // bucket_ms
        series[min(max(index, 0), count - 1)] += 1
    return {
        "from_ms": from_ms,
        "to_ms": to_ms,
        "bucket_ms": bucket_ms,
        "kinds": kinds,
    }


def stats(cfg, from_ms, to_ms):
    conn = _connect(cfg)
    span_rows = conn.execute(
        """
        SELECT service_name, count(*), coalesce(sum(status_code = 2), 0)
        FROM spans WHERE start_ns >= ? AND start_ns <= ?
        GROUP BY service_name
        """,
        (from_ms * 1_000_000, to_ms * 1_000_000),
    ).fetchall()
    sample_rows = conn.execute(
        "SELECT service_name, count(*) FROM samples WHERE ts >= ? AND ts <= ? GROUP BY service_name",
        (from_ms, to_ms),
    ).fetchall()
    conn.close()
    services = {}
    for name, count, errors in span_rows:
        services[name] = {"spans": count, "errors": errors}
    for name, count in sample_rows:
        services.setdefault(name, {})["samples"] = count
    for name, count in _count_day_records(cfg.logs_dir, from_ms, to_ms).items():
        services.setdefault(name, {})["logs"] = count
    for name, count in _count_day_records(cfg.errors_dir, from_ms, to_ms).items():
        services.setdefault(name, {})["sentry"] = count
    return {
        "services": [
            {
                "service": name or "unknown",
                "spans": values.get("spans", 0),
                "errors": values.get("errors", 0),
                "samples": values.get("samples", 0),
                "logs": values.get("logs", 0),
                "sentry": values.get("sentry", 0),
            }
            for name, values in sorted(services.items())
        ]
    }
