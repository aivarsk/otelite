import json
import time

MAX_ATTRS = 256
MAX_EVENTS = 128
MAX_LINKS = 128

_STATUS_CODES = {
    "STATUS_CODE_UNSET": 0,
    "STATUS_CODE_OK": 1,
    "STATUS_CODE_ERROR": 2,
}


def _attr_value(value):
    if "stringValue" in value:
        return value["stringValue"]
    if "boolValue" in value:
        return bool(value["boolValue"])
    if "intValue" in value:
        return int(value["intValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "arrayValue" in value:
        return [_attr_value(v) for v in value["arrayValue"].get("values", [])]
    if "kvlistValue" in value:
        return _attrs_to_object(value["kvlistValue"].get("values", []))
    if "bytesValue" in value:
        return value["bytesValue"]
    return None


def _attrs_to_object(pairs, cap=MAX_ATTRS):
    obj = {}
    for pair in (pairs or [])[:cap]:
        key = pair.get("key")
        if key is not None:
            obj[key] = _attr_value(pair.get("value", {}))
    return obj


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _status_code(status):
    code = status.get("code", 0)
    if isinstance(code, str):
        return _STATUS_CODES.get(code, 0)
    return _int(code)


def _hex_bytes(value, length):
    if not value:
        return None
    try:
        raw = bytes.fromhex(value)
    except ValueError:
        return None
    return raw if len(raw) == length else None


def _walk(payload, plural):
    # resource<plural> messages: resource attributes, service name, scope
    # groups; instrumentationLibrary* is the pre-1.16 OTLP name
    for message in payload.get("resource" + plural, []):
        resource = _attrs_to_object(message.get("resource", {}).get("attributes", []))
        service_name = str(resource.get("service.name", ""))
        groups = message.get("scope" + plural) or message.get("instrumentationLibrary" + plural) or []
        yield resource, service_name, groups


def spans_from(payload, received_ns):
    rows = []
    for resource, service_name, groups in _walk(payload, "Spans"):
        for group in groups:
            scope = group.get("scope") or group.get("instrumentationLibrary") or {}
            scope_name = scope.get("name")
            scope_ver = scope.get("version")
            for span in group.get("spans", []):
                row = _span_row(span, resource, service_name, scope_name, scope_ver, received_ns)
                if row is not None:
                    rows.append(row)
    return rows


def _span_row(span, resource, service_name, scope_name, scope_ver, received_ns):
    trace_id = _hex_bytes(span.get("traceId"), 16)
    span_id = _hex_bytes(span.get("spanId"), 8)
    if trace_id is None or span_id is None:
        return None
    parent_id = _hex_bytes(span.get("parentSpanId"), 8)
    start_ns = _int(span.get("startTimeUnixNano"))
    end_ns = _int(span.get("endTimeUnixNano"))
    dropped = (
        _int(span.get("droppedAttributesCount"))
        + _int(span.get("droppedEventsCount"))
        + _int(span.get("droppedLinksCount"))
    )
    events = [
        {
            "timeUnixNano": _int(event.get("timeUnixNano")),
            "name": event.get("name", ""),
            "attributes": _attrs_to_object(event.get("attributes", [])),
        }
        for event in span.get("events", [])[:MAX_EVENTS]
    ]
    links = [
        {
            "traceId": link.get("traceId", ""),
            "spanId": link.get("spanId", ""),
            "attributes": _attrs_to_object(link.get("attributes", [])),
        }
        for link in span.get("links", [])[:MAX_LINKS]
    ]
    return (
        trace_id,
        span_id,
        parent_id,
        span.get("name", ""),
        _int(span.get("kind")),
        _status_code(span.get("status", {})),
        span.get("status", {}).get("message"),
        service_name,
        start_ns,
        end_ns,
        json.dumps(_attrs_to_object(span.get("attributes", [])), separators=(",", ":")),
        json.dumps(events, separators=(",", ":")) if events else None,
        json.dumps(links, separators=(",", ":")) if links else None,
        json.dumps(resource, separators=(",", ":")),
        scope_name,
        scope_ver,
        dropped,
        received_ns,
    )


def metrics_from(payload):
    metrics = []
    for _resource, service_name, groups in _walk(payload, "Metrics"):
        for group in groups:
            for metric in group.get("metrics", []):
                parsed = _metric(metric, service_name)
                if parsed is not None:
                    metrics.append(parsed)
    return metrics


def _metric(metric, service_name):
    name = metric.get("name")
    if not name:
        return None
    data = metric.get("data", {})
    if "sum" in data:
        kind = "sum"
        body = data["sum"]
        monotonic = 1 if body.get("isMonotonic") else 0
    elif "gauge" in data:
        kind = "gauge"
        body = data["gauge"]
        monotonic = None
    else:
        return None  # histograms and the rest are deferred
    points = []
    for point in body.get("dataPoints", []):
        ts = _int(point.get("timeUnixNano"))
        value = point.get("asInt", point.get("asDouble"))
        if not ts or value is None:
            continue
        points.append((ts // 1_000_000, value, _attrs_to_object(point.get("attributes", []))))
    return {
        "name": name,
        "description": metric.get("description"),
        "unit": metric.get("unit"),
        "kind": kind,
        "monotonic": monotonic,
        "service_name": service_name,
        "points": points,
    }


def log_records_from(payload):
    records = []
    for resource, service_name, groups in _walk(payload, "Logs"):
        for group in groups:
            scope = group.get("scope") or group.get("instrumentationLibrary") or {}
            for record in group.get("logRecords", []):
                parsed = _log_record(record, resource, service_name, scope.get("name"))
                if parsed is not None:
                    records.append(parsed)
    return records


def _log_record(record, resource, service_name, scope_name):
    ns = _int(record.get("timeUnixNano") or record.get("observedTimeUnixNano"))
    if not ns:
        return None
    body = _attr_value(record["body"]) if "body" in record else None
    if not isinstance(body, str):
        body = json.dumps(body, separators=(",", ":"))
    return {
        "time_unix_nano": ns,
        "severity_number": _int(record.get("severityNumber")),
        "severity_text": record.get("severityText", ""),
        "body": body,
        "attributes": _attrs_to_object(record.get("attributes", [])),
        "trace_id": (record.get("traceId") or "").lower(),
        "span_id": (record.get("spanId") or "").lower(),
        "service_name": service_name,
        "resource": resource,
        "scope": scope_name,
        "day": time.strftime("%Y-%m-%d", time.gmtime(ns // 1_000_000_000)),
    }
