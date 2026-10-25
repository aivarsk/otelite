import asyncio
import gzip
import json
import pkgutil
import re
import time
import urllib.parse

from . import api, normalize, sentryio

MAX_HEADER_BYTES = 64 * 1024

REASONS = {
    200: "OK",
    400: "Bad Request",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    411: "Length Required",
    413: "Payload Too Large",
    429: "Too Many Requests",
    500: "Internal Server Error",
    501: "Not Implemented",
}

SIGNALS = {
    "/v1/traces": ("spans", lambda payload, received_ns: normalize.spans_from(payload, received_ns)),
    "/v1/metrics": ("metrics", lambda payload, received_ns: normalize.metrics_from(payload)),
    "/v1/logs": ("logs", lambda payload, received_ns: normalize.log_records_from(payload)),
}

# Sentry SDKs POST to <dsn-path>/api/<project_id>/store/ or .../envelope/;
# the dsn path prefix is optional and the project id is ours to ignore.
SENTRY_PATH = re.compile(r"^(.*/)?api/([^/]+)/(store|envelope)/?$")

JSON_TYPE = "application/json"
HTML_TYPE = "text/html; charset=utf-8"


class HttpError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


async def _read_request(reader, writer, cfg):
    line = await asyncio.wait_for(reader.readline(), timeout=30)
    if not line:
        return None
    try:
        method, target, _version = line.decode("latin-1").strip().split()
    except ValueError:
        raise HttpError(400, "bad request line")
    headers = {}
    total = 0
    while True:
        header_line = await asyncio.wait_for(reader.readline(), timeout=30)
        if header_line in (b"\r\n", b"\n", b""):
            break
        key, sep, value = header_line.decode("latin-1").partition(":")
        if not sep:
            raise HttpError(400, "bad header line")
        headers[key.strip().lower()] = value.strip()
        total += len(header_line)
        if total > MAX_HEADER_BYTES:
            raise HttpError(400, "headers too large")
    if headers.get("expect", "").lower() == "100-continue":
        writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
        await writer.drain()
    if "chunked" in headers.get("transfer-encoding", "").lower():
        raise HttpError(501, "chunked transfer-encoding is not supported")
    raw_length = headers.get("content-length")
    if raw_length is None:
        if method in ("GET", "HEAD", "OPTIONS"):
            length = 0
        else:
            raise HttpError(411, "content-length is required")
    else:
        try:
            length = int(raw_length)
        except ValueError:
            raise HttpError(400, "bad content-length")
    if length > cfg.max_body_bytes:
        raise HttpError(413, "body larger than OTELITE_MAX_BODY_BYTES")
    try:
        body = await asyncio.wait_for(reader.readexactly(length), timeout=30)
    except asyncio.IncompleteReadError:
        raise HttpError(400, "incomplete body")
    if headers.get("content-encoding", "").lower() == "gzip":
        try:
            body = gzip.decompress(body)
        except OSError:
            raise HttpError(400, "bad gzip body")
    return method, target, headers, body


async def _write_response(writer, code, payload, content_type, close, extra_headers=None):
    headers = {
        "Content-Type": content_type,
        "Content-Length": str(len(payload)),
        "Connection": "close" if close else "keep-alive",
    }
    if extra_headers:
        headers.update(extra_headers)
    head = f"HTTP/1.1 {code} {REASONS[code]}\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n"
    writer.write(head.encode() + payload)
    await writer.drain()


def _json(code, obj, extra=None):
    return code, json.dumps(obj, separators=(",", ":")).encode(), JSON_TYPE, extra or {}


def _query(target):
    parsed = urllib.parse.urlsplit(target)
    params = urllib.parse.parse_qs(parsed.query)
    return parsed.path, {key: values[0] for key, values in params.items()}


def _range(params):
    now_ms = int(time.time() * 1000)
    try:
        to_ms = int(params.get("to", now_ms))
        from_ms = int(params.get("from", to_ms - 3600_000))
    except ValueError:
        raise HttpError(400, "from/to must be epoch milliseconds")
    if from_ms < 0 or to_ms < from_ms:
        raise HttpError(400, "bad from/to range")
    return from_ms, to_ms


def asset(name):
    # bundled files read through pkgutil so the zipapp serves them too
    if name not in _ASSETS:
        data = pkgutil.get_data(__package__ or "otelite", name)
        if data is None:
            raise HttpError(500, f"missing bundled asset: {name}")
        _ASSETS[name] = data
    return _ASSETS[name]


def _asset_cache():
    return {}


_ASSETS = _asset_cache()


def make_routes(cfg, writer):
    def route(method, target, body, headers):
        path, params = _query(target)
        if method == "GET":
            return _get(path, params)
        if path == "/healthz":
            return _json(200, {"status": "ok"})
        if cfg.archive and method == "POST":
            return _json(403, {"message": "read-only archive mode, ingestion is disabled"})
        signal = SIGNALS.get(path)
        if signal is not None:
            if method != "POST":
                return _json(405, {"message": "POST only"})
            try:
                payload = json.loads(body)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return _json(400, {"message": "invalid json"})
            if not isinstance(payload, dict):
                return _json(400, {"message": "payload must be a JSON object"})
            kind, parse = signal
            rows = parse(payload, time.time_ns())
            if writer.submit((kind, rows)):
                return _json(200, {})
            return _json(429, {"message": "queue full, retry later"}, {"Retry-After": "1"})
        sentry = SENTRY_PATH.match(path)
        if sentry is not None:
            if method != "POST":
                return _json(405, {"message": "POST only"})
            return _sentry(sentry.group(2), sentry.group(3), body, headers)
        return _json(404, {"message": f"no route for {method} {path}"})

    def _sentry(project, endpoint, body, headers):
        key = sentryio.parse_auth(headers.get("x-sentry-auth", ""))
        try:
            if endpoint == "store":
                records = sentryio.records_from_store(project, body, key, time.time_ns())
            else:
                records = sentryio.records_from_envelope(project, body, key, time.time_ns())
        except (ValueError, UnicodeDecodeError):
            return _json(400, {"message": "invalid sentry payload"})
        if writer.submit(("errors", records)):
            event_id = records[0]["event_id"] if records else ""
            return _json(200, {"id": event_id})
        return _json(429, {"message": "queue full, retry later"}, {"Retry-After": "1"})

    def _get(path, params):
        if path == "/":
            return 200, asset("static/page.html"), HTML_TYPE, {"Cache-Control": "no-cache"}
        if path == "/healthz":
            return _json(200, {"status": "ok"})
        if path == "/static/uplot.js":
            return (
                200,
                asset("static/uplot.iife.min.js"),
                "text/javascript; charset=utf-8",
                {"Cache-Control": "no-cache"},
            )
        if path == "/static/uplot.css":
            return 200, asset("static/uplot.min.css"), "text/css; charset=utf-8", {"Cache-Control": "no-cache"}
        from_ms, to_ms = _range(params)
        service = params.get("service") or None
        if path == "/strace/traces":
            return _json(200, api.traces(cfg, from_ms, to_ms, service))
        if path == "/strace/trace":
            trace_id = params.get("id", "")
            result = api.trace(cfg, trace_id)
            if result is None:
                return _json(404, {"message": "trace not found"})
            return _json(200, result)
        if path == "/awk/metrics":
            return _json(200, api.metrics(cfg))
        if path == "/awk/series":
            try:
                ids = [int(part) for part in params.get("id", "").split(",") if part.strip()]
            except ValueError:
                raise HttpError(400, "id must be a comma-separated list of metric ids")
            return _json(200, api.series(cfg, ids, from_ms, to_ms))
        if path == "/grep/logs":
            return _json(200, api.logs(cfg, from_ms, to_ms, service, params.get("q") or None))
        if path == "/grep/logs/series":
            return _json(200, api.logs_series(cfg, from_ms, to_ms, service, params.get("q") or None))
        if path == "/grep/errors":
            return _json(200, api.errors(cfg, from_ms, to_ms, service, params.get("q") or None))
        if path == "/grep/errors/series":
            return _json(200, api.errors_series(cfg, from_ms, to_ms, service, params.get("q") or None))
        if path == "/perl/stats":
            return _json(200, api.stats(cfg, from_ms, to_ms))
        if path in SIGNALS or SENTRY_PATH.match(path):
            return _json(405, {"message": "POST only"})
        return _json(404, {"message": f"no route for GET {path}"})

    return route


async def handle_conn(reader, writer, cfg, route):
    try:
        while True:
            request = await _read_request(reader, writer, cfg)
            if request is None:
                break
            method, target, headers, body = request
            code, payload, content_type, extra = route(method, target, body, headers)
            client_close = headers.get("connection", "").lower() == "close"
            await _write_response(writer, code, payload, content_type, client_close, extra)
            if client_close:
                break
    except HttpError as exc:
        close = exc.code in (400, 411, 413, 500, 501)
        try:
            await _write_response(writer, exc.code, json.dumps({"message": exc.message}).encode(), JSON_TYPE, close)
        except (ConnectionResetError, BrokenPipeError):
            pass
    except (ConnectionResetError, BrokenPipeError, asyncio.TimeoutError, asyncio.LimitOverrunError):
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionResetError, BrokenPipeError):
            pass


async def serve(cfg, writer):
    route = make_routes(cfg, writer)

    async def handler(reader, writer_s):
        await handle_conn(reader, writer_s, cfg, route)

    server = await asyncio.start_server(handler, "0.0.0.0", cfg.port)
    sockets = ", ".join(str(sock.getsockname()) for sock in server.sockets)
    print(f"otelite listening on {sockets}, data in {cfg.root}")
    async with server:
        await server.serve_forever()
