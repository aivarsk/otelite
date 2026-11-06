"""LogQL-ish queries over the JSONL log files.

Subset, look-and-feel only: stream selector `{service="checkout",
severity=~"E.*"}`, line filters `|=`, `!=`, `|~`, `!~` (matched against
the log body, not the JSON envelope, so timestamp digits never match),
and a pipeline with extractors `| json`, `| logfmt`, `| regexp "..."`
(named groups, non-matching lines dropped) plus field filters
`| field="value"` over the extracted record (or the parsed body when it
is JSON). Range and label semantics are otelite's, not Loki's: labels
are service_name / severity_text / trace_id plus every attribute, and
the "streams" are the day files. Deliberately absent: range aggregations
and metric pipelines over logs, `| line_format`/`| label_format`
templates, set operations between selectors.

AST: ("select", matchers, stages). Matchers are (name, op, value); stages
are ("json",), ("line", op, pattern), or ("field", name, op, value).
"""

import json
import re

from ..api import _iter_day_records
from . import combinators as pc


def _pipeline_stage(text, pos):
    # line filters are two-character operators glued to the selector:
    # {..} |= "x", {..} != "x", {..} |~ "re", {..} !~ "re"
    for op in ("|=", "|~", "!=", "!~"):
        try:
            _op, pos = pc.lexeme(pc.lit(op))(text, pos)
            value, pos = pc.quoted_string(text, pos)
            return ("line", op, value), pos
        except pc.ParseError:
            continue
    # "| json", "| logfmt", "| regexp", or a field filter "| name=value"
    _bar, pos = pc.token("|")(text, pos)
    for name in ("json", "logfmt"):
        try:
            _kw, pos = pc.keyword(name)(text, pos)
            return (name,), pos
        except pc.ParseError:
            pass
    try:
        _kw, pos = pc.keyword("regexp")(text, pos)
        pattern, pos = pc.quoted_string(text, pos)
        return ("regexp", pattern), pos
    except pc.ParseError:
        pass
    name, pos = pc.identifier(text, pos)
    op, pos = pc.matcher_op(text, pos)
    value, pos = pc.quoted_string(text, pos)
    return ("field", name, op, value), pos


def _query_fn(text, pos):
    selector = pc.between(pc.token("{"), pc.token("}"), pc.sep_by(pc.label_matcher, pc.token(",")))
    matchers, pos = pc.lexeme(selector)(text, pos)
    stages, pos = pc.many(_pipeline_stage)(text, pos)
    return ("select", matchers, stages), pos


_query = pc.Parser(_query_fn)
parse = pc.complete(_query)


def _label_value(record, name):
    if name == "service":
        return record.get("service_name", "")
    if name == "severity":
        return record.get("severity_text", "")
    if name == "trace_id":
        return record.get("trace_id", "")
    attrs = record.get("attributes") or {}
    if name in attrs:
        return attrs[name]
    return record.get(name, "")


def _extract_body_fields(body):
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _match_number(candidate, op, raw):
    value = float(raw)
    if op == "=":
        return candidate == value
    if op == "!=":
        return candidate != value
    if op == "=~":
        return re.search(raw, str(candidate)) is not None
    if op == "!~":
        return re.search(raw, str(candidate)) is None
    return False


def _extract_logfmt(body):
    fields = pc._dict()
    for match in re.finditer(r'([A-Za-z_][\w.-]*)=(?:"((?:[^"\\]|\\.)*)"|(\S+))', body):
        value = match.group(2) if match.group(2) is not None else match.group(3)
        fields[match.group(1)] = value
    return fields


def _extract_regexp(body, pattern):
    match = re.search(pattern, body)
    return dict(match.groupdict()) if match else None


def _run_stage(stage, line, record, fields):
    tag = stage[0]
    if tag == "json":
        fields.update(_extract_body_fields(record.get("body", "")))
        return True
    if tag == "logfmt":
        fields.update(_extract_logfmt(record.get("body", "")))
        return True
    if tag == "regexp":
        extracted = _extract_regexp(record.get("body", ""), stage[1])
        if extracted is None:
            return False
        fields.update(extracted)
        return True
    if tag == "line":
        _stage_tag, op, pattern = stage
        if op == "|=":
            return pattern in line
        if op == "!=":
            return pattern not in line
        if op == "|~":
            return re.search(pattern, line) is not None
        if op == "!~":
            return re.search(pattern, line) is None
        return True
    _stage_tag, name, op, value = stage
    candidate = fields.get(name, record.get(name, ""))
    if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
        try:
            return _match_number(candidate, op, value)
        except ValueError:
            return False
    return pc.match_value(str(candidate), op, str(value))


def execute(cfg, text, from_ms=None, to_ms=None, limit=1000):
    """Run a LogQL-ish query over cfg's log files. Returns a list of log
    row dicts (time_ms, severity, service, body, trace_id)."""
    from_ms, to_ms = pc.default_range(from_ms, to_ms, None)
    _select, matchers, stages = parse(text)
    out = []
    for ts_ms, _service, record in _iter_day_records(cfg.logs_dir, from_ms, to_ms):
        if any(not pc.match_value(str(_label_value(record, name)), op, pattern) for name, op, pattern in matchers):
            continue
        # the "line" for line filters is the log body, not the JSON envelope;
        # matching timestamps inside the record would surprise everyone
        line = str(record.get("body", ""))
        fields = pc._dict()
        if all(_run_stage(stage, line, record, fields) for stage in stages):
            out.append(
                {
                    "time_ms": ts_ms,
                    "severity": record.get("severity_text", ""),
                    "service": record.get("service_name", ""),
                    "body": record.get("body", ""),
                    "trace_id": record.get("trace_id", ""),
                }
            )
            if len(out) >= limit:
                break
    return out
