"""Sentry error ingestion: store and envelope endpoints normalized to JSONL.

Point Sentry SDKs at otelite by rewriting the DSN host (and path, when the
SDK has one); any project id and any or no sentry key is accepted, nothing
is verified. A dev box has no secrets worth checking and CI runners rotate
keys daily.

File format, one flattened JSON record per line in
errors/YYYY-MM-DD/<project_id>-<sentry_key>.jsonl (this module is the spec):

- the whole Sentry event rides untouched under "event", so one JSONL line
  is the complete replayable payload; every other top-level key is a
  flattened copy for grep and grouping.
- service_name is "<project_id>-<sentry_key>": the pair identifies who
  sent the error the way service.name does for OTLP, and it is the file
  name, so `grep <trace-id> errors/` is the trace join.
- group is sha1 of the client fingerprint when one is present, else sha1
  of the exception type plus its stacktrace frames (filename, function,
  line). The page groups by it; kind is the exception type.
- trace_id / span_id come from contexts.trace when the SDK is bridged to
  OTEL, which is the join to the trace waterfall.
- timestamps are event time, falling back to server receipt time when the
  client sent none. A crash may leave a partial last line; readers skip
  unparseable tails, same as logs.

Envelope bodies are newline-separated JSON headers and payloads; only
"event" items are stored. Attachments, transactions, profiles, replays,
check-ins, and client reports are skipped on purpose: errors are the
signal, the rest is Sentry product surface.
"""

import hashlib
import json
import time
from datetime import datetime, timezone

GROUP_HEX = 16


def parse_auth(header):
    # X-Sentry-Auth: Sentry sentry_version=7, sentry_client=x/1.0, sentry_key=abc
    # the "Sentry " scheme prefix glues to the first parameter name, so drop
    # it before splitting; real SDKs put sentry_key first
    if not header:
        return ""
    _, sep, rest = header.partition(" ")
    body = rest if sep else header
    for part in body.split(","):
        name, eq, value = part.strip().partition("=")
        if eq and name.strip() == "sentry_key":
            return value.strip().strip('"')
    return ""


def _key_from_dsn(dsn):
    # dsn shape: scheme://key@host/path/project; the key is a credential
    # in name only, we want it as a label
    rest = dsn.split("//", 1)[-1]
    return rest.split("@", 1)[0] if "@" in rest else ""


def records_from_store(project, body, auth_key, received_ns):
    event = json.loads(body)
    if not isinstance(event, dict):
        raise ValueError("event must be a JSON object")
    return [_record(project, auth_key, event, received_ns)]


def records_from_envelope(project, body, auth_key, received_ns):
    # envelope: one JSON header line, then item header / payload pairs.
    # item headers may carry "length" when the payload is binary or holds
    # newlines (attachments); event payloads are read line-wise.
    header_raw, pos = _line(body, 0)
    if not header_raw.strip():
        raise ValueError("empty envelope")
    try:
        header = json.loads(header_raw)
    except json.JSONDecodeError:
        raise ValueError("bad envelope header") from None
    if not auth_key:
        auth_key = _key_from_dsn(header.get("dsn", ""))
    records = []
    while pos < len(body):
        item_raw, pos = _line(body, pos)
        if not item_raw.strip():
            break
        try:
            item = json.loads(item_raw)
        except json.JSONDecodeError:
            raise ValueError("bad envelope item header") from None
        length = item.get("length")
        if isinstance(length, int) and length >= 0:
            payload = body[pos : pos + length]
            pos += length
            if body[pos : pos + 1] == b"\n":
                pos += 1
        else:
            payload, pos = _line(body, pos)
        if item.get("type") != "event":
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            raise ValueError("bad envelope event payload") from None
        if isinstance(event, dict):
            records.append(_record(project, auth_key, event, received_ns))
    return records


def _line(body, pos):
    end = body.find(b"\n", pos)
    if end == -1:
        return body[pos:].rstrip(b"\r"), len(body)
    return body[pos:end].rstrip(b"\r"), end + 1


def _timestamp_ns(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if value > 1e17:  # already nanoseconds
            return int(value)
        if value > 1e11:  # milliseconds
            return int(value * 1_000_000)
        return int(value * 1_000_000_000)  # seconds
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1_000_000_000)
    return None


def _message_text(message, fallback):
    if isinstance(message, str):
        return message
    if isinstance(message, dict):
        return message.get("formatted") or message.get("message") or fallback
    return fallback


def _group(fingerprint, kind, frames, message):
    if fingerprint:
        raw = json.dumps(fingerprint, separators=(",", ":"))
    elif kind or frames:
        raw = (
            kind
            + "\n"
            + "\n".join(
                f"{frame.get('filename') or frame.get('module') or ''}|{frame.get('function') or ''}|{frame.get('lineno') or 0}"
                for frame in frames
            )
        )
    else:
        raw = "message\n" + message
    return hashlib.sha1(raw.encode()).hexdigest()[:GROUP_HEX]


def _record(project, key, event, received_ns):
    ts_ns = _timestamp_ns(event.get("timestamp")) or received_ns
    exceptions = (event.get("exception") or {}).get("values") or []
    first = exceptions[0] if exceptions else {}
    frames = (first.get("stacktrace") or {}).get("frames") or []
    kind = first.get("type", "")
    value = first.get("value", "")
    message = _message_text(event.get("message"), value)
    fingerprint = event.get("fingerprint") or []
    contexts = event.get("contexts") or {}
    trace = contexts.get("trace") or {}
    return {
        "time_unix_nano": ts_ns,
        "day": time.strftime("%Y-%m-%d", time.gmtime(ts_ns // 1_000_000_000)),
        "service_name": f"{project}-{key or 'nokey'}",
        "project_id": project,
        "sentry_key": key,
        "event_id": str(event.get("event_id") or ""),
        "level": event.get("level", ""),
        "kind": kind,
        "value": value,
        "message": message,
        "release": event.get("release") or "",
        "environment": event.get("environment") or "",
        "fingerprint": fingerprint,
        "group": _group(fingerprint, kind, frames, message),
        "trace_id": str(trace.get("trace_id") or "").lower(),
        "span_id": str(trace.get("span_id") or "").lower(),
        "event": event,
    }
