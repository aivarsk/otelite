"""PromQL-ish queries over the metrics dictionary tables (SQLite).

Subset, look-and-feel only: instant vector selectors
`metric{label="v"}` with optional `offset 5m`, optional range `[5m]` on
the selector, `rate/increase(...[5m])` for monotonic counters, the
`avg/min/max/sum/count_over_time(...[range])` window aggregations,
`topk/bottomk(k, ...)` and `quantile(phi, ...)` over the instant vector,
comparison filters (`m > 5`, with optional `bool`), and
`sum/avg/min/max/count by|without (labels) (...)` aggregations. No
arithmetic between vectors (matching semantics is a project),
`histogram_quantile` (otelite drops histograms at ingest), no instant
vectors at a timestamp other than --to (default: now). Metric names may
contain dots (otelite metrics do, Prometheus proper forbids them).

AST: ("agg", op, without, labels, expr), ("rate", expr), ("sel", name,
matchers, range_ms_or_None). Matchers are (name, op, value).
"""

import json

from .. import db
from . import combinators as pc

DEFAULT_RANGE_MS = 5 * 60_000

DURATION_MS = {"ms": 1, "s": 1_000, "m": 60_000, "h": 3_600_000}


def _duration_ms(text):
    if text[-2:] == "ms":
        unit, number = "ms", text[:-2]
    else:
        unit, number = text[-1], text[:-1]
    return int(float(number) * DURATION_MS[unit])


_metric_name = pc.lexeme(pc.regex(r"[a-zA-Z_:][\w:.]*", "metric name"))
_matchers = pc.between(pc.token("{"), pc.token("}"), pc.sep_by(pc.label_matcher, pc.token(",")))
_range = pc.between(pc.token("["), pc.token("]"), pc.duration_literal).map(_duration_ms)
_offset = (pc.keyword("offset") >> pc.duration_literal).map(_duration_ms)
_number = pc.lexeme(pc.regex(r"-?\d+(?:\.\d+)?", "number")).map(float)


def _selector(text, pos):
    name, pos = _metric_name(text, pos)
    matchers, pos = pc.optional(_matchers, [])(text, pos)
    range_ms, pos = pc.optional(_range)(text, pos)
    offset_ms, pos = pc.optional(_offset, 0)(text, pos)
    return ("sel", name, matchers, range_ms, offset_ms), pos


_AGG_OPS = ["sum", "avg", "min", "max", "count"]

_expr = pc.forward()


_FUNC_NAMES = [
    "avg_over_time",
    "min_over_time",
    "max_over_time",
    "sum_over_time",
    "count_over_time",
    "increase",
    "rate",
    "topk",
    "bottomk",
    "quantile",
]
_PAIR_FUNCS = ("topk", "bottomk", "quantile")


def _function(text, pos):
    for name in _FUNC_NAMES:
        try:
            _kw, pos = pc.keyword(name)(text, pos)
            break
        except pc.ParseError:
            continue
    else:
        raise pc.ParseError(text, pos, "function")
    _lb, pos = pc.token("(")(text, pos)
    if name in _PAIR_FUNCS:
        arg, pos = _number(text, pos)
        _comma, pos = pc.token(",")(text, pos)
        inner, pos = _expr(text, pos)
        _rb, pos = pc.token(")")(text, pos)
        return (name, arg, inner), pos
    inner, pos = _expr(text, pos)
    _rb, pos = pc.token(")")(text, pos)
    return (name, inner), pos


def _aggregation(text, pos):
    for op in _AGG_OPS:
        try:
            _kw, pos = pc.keyword(op)(text, pos)
            break
        except pc.ParseError:
            continue
    else:
        raise pc.ParseError(text, pos, "aggregation")
    without = False
    labels = []
    for grouping, is_without in (("by", False), ("without", True)):
        try:
            _kw, pos2 = pc.keyword(grouping)(text, pos)
            _lb, pos2 = pc.token("(")(text, pos2)
            names, pos2 = _labels_parens(text, pos2)
            _rb, pos2 = pc.token(")")(text, pos2)
        except pc.ParseError:
            continue
        without = is_without
        labels = names
        pos = pos2
        break
    _lb, pos = pc.token("(")(text, pos)
    inner, pos = _expr(text, pos)
    _rb, pos = pc.token(")")(text, pos)
    return ("agg", op, without, labels, inner), pos


def _labels_parens(text, pos):
    return pc.sep_by(pc.lexeme(pc.identifier), pc.token(","))(text, pos)


def _expr_impl(text, pos):
    # ordered choice is cleaner than sniffing keywords: a plain selector is
    # what is left when functions and aggregations do not parse
    try:
        node, pos = _function(text, pos)
    except pc.ParseError:
        try:
            node, pos = _aggregation(text, pos)
        except pc.ParseError:
            node, pos = _selector(text, pos)
    # comparison filter tail: expr > 5, optionally "expr > bool 5"
    try:
        op, pos2 = pc.lexeme(pc.regex(r"==|!=|>=|<=|>|<", "comparison"))(text, pos)
        as_bool = False
        try:
            _kw, pos3 = pc.keyword("bool")(text, pos2)
            as_bool = True
            pos2 = pos3
        except pc.ParseError:
            pass
        number, pos2 = _number(text, pos2)
        return ("cmpfilter", op, number, as_bool, node), pos2
    except pc.ParseError:
        return node, pos


_expr.define(_expr_impl)
_query = pc.lexeme(_expr)
parse = pc.complete(_query)


def _fetch_samples(conn, name, matchers, from_ms, to_ms):
    rows = conn.execute(
        """
        SELECT m.name, s.service_name, a.attrs, s.ts, s.value
        FROM samples s
        JOIN metrics m ON m.id = s.metric_id
        JOIN attrsets a ON a.id = s.attrset_id
        WHERE m.name = ? AND s.ts >= ? AND s.ts <= ?
        ORDER BY s.ts
        """,
        (name, from_ms, to_ms),
    ).fetchall()
    out = []
    for row in rows:
        labels = {"service": row[1]}
        labels.update(json.loads(row[2] or "{}"))
        if any(not pc.match_value(str(labels.get(label, "")), op, pattern) for label, op, pattern in matchers):
            continue
        out.append({"metric": row[0], "labels": labels, "time_ms": row[3], "value": row[4]})
    return out


def _series_key(sample):
    return json.dumps([sample["metric"], sorted(sample["labels"].items())], separators=(",", ":"))


def _last_per_series(samples):
    latest = pc._dict()
    for sample in samples:
        key = _series_key(sample)
        if key not in latest or sample["time_ms"] > latest[key]["time_ms"]:
            latest[key] = sample
    return list(latest.values())


_OVER_TIME = {
    "avg_over_time": "avg",
    "min_over_time": "min",
    "max_over_time": "max",
    "sum_over_time": "sum",
    "count_over_time": "count",
}


def _group_samples(samples):
    series = pc._dict()
    for sample in samples:
        series.setdefault(_series_key(sample), []).append(sample)
    for points in series.values():
        points.sort(key=lambda p: p["time_ms"])
    return series


def _delta(inner, ctx, rate):
    out = []
    for points in _group_samples(_eval(inner, ctx)).values():
        if len(points) < 2:
            continue
        first, last = points[0], points[-1]
        delta = max(last["value"] - first["value"], 0.0)
        seconds = (last["time_ms"] - first["time_ms"]) / 1000
        value = delta / seconds if rate and seconds > 0 else delta
        out.append({**last, "value": value})
    return out


def _over_time(inner, ctx, op):
    out = []
    for points in _group_samples(_eval(inner, ctx)).values():
        values = [point["value"] for point in points]
        out.append({**points[-1], "value": float(pc.aggregate_value(op, values))})
    return out


def _quantile(phi, values):
    ordered = sorted(values)
    if not ordered:
        return None
    index = phi * (len(ordered) - 1)
    low = int(index)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (index - low)


def _compare(value, op, other):
    if op == "==":
        return value == other
    if op == "!=":
        return value != other
    if op == ">":
        return value > other
    if op == "<":
        return value < other
    if op == ">=":
        return value >= other
    return value <= other


def _group(vector, op, without, labels):
    def key_for(sample):
        if without:
            kept = {k: v for k, v in sample["labels"].items() if k not in labels}
        else:
            kept = {k: sample["labels"][k] for k in labels if k in sample["labels"]}
        return json.dumps(sorted(kept.items()), separators=(",", ":"))

    groups = pc._dict()
    for sample in vector:
        groups.setdefault(key_for(sample), []).append(sample["value"])
    out = []
    for key, values in sorted(groups.items()):
        labels_out = dict(json.loads(key))
        out.append({"metric": "", "labels": labels_out, "time_ms": None, "value": pc.aggregate_value(op, values)})
    return out


def _instant(node, ctx):
    vector = _eval(node, ctx)
    return _last_per_series(vector) if node[0] == "sel" else vector


def _eval(node, ctx):
    tag = node[0]
    if tag == "sel":
        _tag, name, matchers, range_ms, offset_ms = node
        end = ctx["to_ms"] - offset_ms
        start = end - range_ms if range_ms is not None else ctx["from_ms"]
        return _fetch_samples(ctx["conn"], name, matchers, start, end)
    if tag in ("rate", "increase"):
        return _delta(node[1], ctx, rate=(tag == "rate"))
    if tag in _OVER_TIME:
        return _over_time(node[1], ctx, _OVER_TIME[tag])
    if tag in ("topk", "bottomk"):
        vector = _instant(node[2], ctx)
        vector.sort(key=lambda row: row["value"], reverse=(tag == "topk"))
        return vector[: int(node[1])]
    if tag == "quantile":
        vector = _instant(node[2], ctx)
        return [
            {"metric": "", "labels": {}, "time_ms": None, "value": _quantile(node[1], [row["value"] for row in vector])}
        ]
    if tag == "cmpfilter":
        _tag, op, number, as_bool, inner = node
        vector = _instant(inner, ctx)
        if as_bool:
            return [{**row, "value": 1.0 if _compare(row["value"], op, number) else 0.0} for row in vector]
        return [row for row in vector if _compare(row["value"], op, number)]
    _tag, op, without, labels, inner = node
    return _group(_instant(inner, ctx), op, without, labels)


def execute(cfg, text, from_ms=None, to_ms=None, limit=1000):
    """Run a PromQL-ish query against cfg's samples tables. Returns a list
    of {"metric", "labels", "time_ms", "value"} rows."""
    from_ms, to_ms = pc.default_range(from_ms, to_ms, DEFAULT_RANGE_MS)
    conn = db.connect(cfg.db_path)
    try:
        ctx = {"conn": conn, "from_ms": from_ms, "to_ms": to_ms}
        return _eval(parse(text), ctx)[:limit]
    finally:
        conn.close()
