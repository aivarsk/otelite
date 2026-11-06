"""TraceQL-ish queries over the spans table (SQLite).

Subset, look-and-feel only: `{ ... }` span selectors with && and ||,
attribute comparisons (span attrs, resource attrs, name, kind, status,
duration), structural composition of two selectors (`>>` descendant,
`<<` ancestor, `~` sibling), and `| count()` plus `| avg/min/max/sum(...)`
pipes with optional `by(...)`. Deliberately absent: spanset arithmetic
beyond one structural step at a time, `{a} && {b}` spanset intersection,
`select()` and coalesce.

ASTs: ("select", conditions, pipes) with condition nodes ("or"/"and",
a, b) and ("cmp", op, lhs, rhs); operands ("attr", scope, name) where
scope is "span" or "resource", or ("lit", value). Pipes are
("pipe", agg, attr_or_None, group_attrs).
"""

import json
import re

from .. import db
from . import combinators as pc

MAX_SPANS = 10_000

DURATION_NS = {"ns": 1, "us": 1_000, "ms": 1_000_000, "s": 1_000_000_000, "m": 60_000_000_000, "h": 3_600_000_000_000}

SPECIAL_ATTRS = {"name", "kind", "status", "status_code", "duration"}


def _duration_ns(text):
    if text[-2:] in ("ns", "us", "ms"):
        unit, number = text[-2:], text[:-2]
    else:
        unit, number = text[-1], text[:-1]
    return int(float(number) * DURATION_NS[unit])


_string = pc.quoted_string.map(lambda value: ("lit", value))
_duration = pc.duration_literal.map(lambda text: ("lit", _duration_ns(text)))
_number = pc.lexeme(pc.regex(r"-?\d+(?:\.\d+)?", "number")).map(lambda s: ("lit", float(s) if "." in s else int(s)))
_status_lit = (pc.keyword("error") | pc.keyword("ok") | pc.keyword("unset")).map(
    lambda s: ("lit", {"error": 2, "ok": 1, "unset": 0}[s])
)
_attr = pc.lexeme(pc.regex(r"(?:resource\.)?(?:\.)?[A-Za-z_][\w.]*", "attribute")).map(
    lambda s: (
        "attr",
        "resource" if s.startswith("resource.") else "span",
        s.removeprefix("resource.").removeprefix("."),
    )
)
_operand = _string | _duration | _number | _status_lit | _attr
_cmp_op = pc.lexeme(pc.regex(r"=~|!=|>=|<=|=|>|<", "comparison"))


def _or_expr(text, pos):
    left, pos = _and_expr(text, pos)
    while True:
        try:
            _kw, pos = pc.token("||")(text, pos)
            right, pos = _and_expr(text, pos)
            left = ("or", left, right)
        except pc.ParseError:
            return left, pos


def _and_expr(text, pos):
    left, pos = _atom(text, pos)
    while True:
        try:
            _kw, pos = pc.token("&&")(text, pos)
            right, pos = _atom(text, pos)
            left = ("and", left, right)
        except pc.ParseError:
            return left, pos


def _atom(text, pos):
    try:
        return pc.between(pc.token("{"), pc.token("}"), pc.optional(_or_expr, ("all",)))(text, pos)
    except pc.ParseError:
        pass
    lhs, pos = _operand(text, pos)
    op, pos = _cmp_op(text, pos)
    rhs, pos = _operand(text, pos)
    return ("cmp", op, lhs, rhs), pos


_AGGS = ["count", "avg", "min", "max", "sum"]


def _group_list(text, pos):
    return pc.sep_by(pc.lexeme(pc.regex(r"[A-Za-z_][\w.]*", "attribute name")), pc.token(","))(text, pos)


def _aggregate(text, pos):
    for name in _AGGS:
        try:
            _kw, pos = pc.keyword(name)(text, pos)
            break
        except pc.ParseError:
            continue
    else:
        raise pc.ParseError(text, pos, "aggregate")
    attr = None
    # optional argument list: count() or sum(duration)
    try:
        _lb, pos = pc.token("(")(text, pos)
        inner_attr, pos = pc.optional(_attr)(text, pos)
        _rb, pos = pc.token(")")(text, pos)
        attr = inner_attr[2] if inner_attr else None
    except pc.ParseError:
        pass
    # optional grouping: ... by (a, b)
    try:
        _kw, pos = pc.keyword("by")(text, pos)
        _lb, pos = pc.token("(")(text, pos)
        groups, pos = _group_list(text, pos)
        _rb, pos = pc.token(")")(text, pos)
    except pc.ParseError:
        groups = []
    return ("pipe", name, attr, groups), pos


_STRUCT_OPS = (">>", "<<", "~")
MAX_STRUCT_TRACES = 500


def _spanquery(text, pos):
    left, pos = _or_expr(text, pos)
    while True:
        for op in _STRUCT_OPS:
            try:
                _tok, pos2 = pc.lexeme(pc.lit(op))(text, pos)
                right, pos2 = _or_expr(text, pos2)
                left = ("struct", op, left, right)
                pos = pos2
                break
            except pc.ParseError:
                continue
        else:
            return left, pos


def _query_fn(text, pos):
    spanexpr, pos = pc.lexeme(pc.Parser(_spanquery))(text, pos)
    pipes, pos = pc.many(pc.token("|") >> _aggregate)(text, pos)
    return ("select", spanexpr, pipes), pos


_query = pc.Parser(_query_fn)
parse = pc.complete(_query)


def _sql_string(value):
    return "'" + str(value).replace("'", "''") + "'"


def _operand_sql(node, params):
    if node[0] == "lit":
        params.append(node[1])
        return "?"
    _tag, scope, name = node
    if name == "name":
        return "name"
    if name in ("status", "status_code"):
        return "status_code"
    if name == "kind":
        return "kind"
    if name == "duration":
        return "duration_ns"
    column = "resource" if scope == "resource" else "attrs"
    return f"json_extract({column}, {_sql_string('$.' + chr(34) + name + chr(34))})"


def _where(node, params):
    tag = node[0]
    if tag == "all":
        return "1=1"
    if tag in ("and", "or"):
        joiner = " AND " if tag == "and" else " OR "
        return f"({_where(node[1], params)}){joiner}({_where(node[2], params)})"
    _tag, op, lhs, rhs = node
    left = _operand_sql(lhs, params)
    right = _operand_sql(rhs, params)
    if op == "=~":
        return f"({left} REGEXP {right})"
    return f"({left} {op} {right})"


def _fetch_rows(conn, conditions, limit, from_ms=None, to_ms=None):
    params = pc._list()
    where = _where(conditions, params)
    if from_ms is not None and to_ms is not None:
        where += " AND start_ns >= ? AND start_ns <= ?"
        params.extend([from_ms * 1_000_000, to_ms * 1_000_000])
    sql = f"""
    SELECT hex(trace_id), hex(span_id), hex(parent_id), name, kind, status_code, status_msg,
           service_name, start_ns, end_ns, attrs, events, resource, scope_name
    FROM spans WHERE {where} ORDER BY start_ns LIMIT ?
    """
    return conn.execute(sql, params + [limit]).fetchall()


def _row_dict(raw):
    return {
        "trace_id": raw[0],
        "span_id": raw[1],
        "parent_id": raw[2],
        "name": raw[3],
        "kind": raw[4],
        "status_code": raw[5],
        "status_msg": raw[6],
        "service_name": raw[7],
        "start_ms": raw[8] // 1_000_000,
        "duration_ms": (raw[9] - raw[8]) // 1_000_000,
        "attrs": json.loads(raw[10] or "{}"),
        "events": json.loads(raw[11] or "[]"),
        "resource": json.loads(raw[12] or "{}"),
        "scope_name": raw[13],
    }


def _attr_value(row, name):
    if name.startswith("resource."):
        return row["resource"].get(name[len("resource.") :])
    if name.startswith("span."):
        return row["attrs"].get(name[len("span.") :])
    if name == "name":
        return row["name"]
    if name in ("duration", "duration_ns"):
        return row["duration_ms"] * 1_000_000
    if name in ("status", "status_code"):
        return row["status_code"]
    if name == "kind":
        return row["kind"]
    if name == "service_name":
        return row["service_name"]
    if name in row["attrs"]:
        return row["attrs"][name]
    return row["resource"].get(name)


def _group(rows, group_attrs, measure):
    if not group_attrs:
        return [measure(rows)]
    buckets = pc._dict()
    for row in rows:
        key = tuple(str(_attr_value(row, name)) for name in group_attrs)
        buckets.setdefault(key, []).append(row)
    out = []
    for key, vals in sorted(buckets.items()):
        out.append({**dict(zip(group_attrs, key)), **measure(vals)})
    return out


def _apply_pipes(rows, pipes):
    for _tag, agg, attr, groups in pipes:
        if agg == "count":
            rows = _group(rows, groups, lambda vals: {"count": len(vals)})
            continue

        def measure(vals):
            numbers = [
                value
                for value in (_attr_value(v, attr) for v in vals)
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            ]
            return {agg: pc.aggregate_value(agg, numbers) if numbers else None}

        rows = _group(rows, groups, measure)
    return rows


def _run_struct(conn, node):
    # structural composition: fetch both sides, then walk whole traces so
    # parent chains exist even when a side's conditions did not match them
    _tag, op, left_conds, right_conds = node
    left = [_row_dict(raw) for raw in _fetch_rows(conn, left_conds, MAX_SPANS)]
    right = [_row_dict(raw) for raw in _fetch_rows(conn, right_conds, MAX_SPANS)]
    trace_ids = list({row["trace_id"] for row in left + right})[:MAX_STRUCT_TRACES]
    all_rows = [_row_dict(raw) for raw in _fetch_trace_spans(conn, trace_ids)]
    by_trace = pc._dict()
    for row in all_rows:
        by_trace.setdefault(row["trace_id"], pc._dict())[row["span_id"]] = row
    left_keys = {(row["trace_id"], row["span_id"]) for row in left}
    right_keys = {(row["trace_id"], row["span_id"]) for row in right}

    def ancestors(row):
        chain = pc._list()
        trace = by_trace.get(row["trace_id"], {})
        current = row
        for _ in range(64):
            parent_id = current["parent_id"]
            if not parent_id or parent_id not in trace:
                return chain
            current = trace[parent_id]
            chain.append(current)

    def descendants(row):
        children = pc._dict()
        for item in all_rows:
            if item["parent_id"]:
                children.setdefault((item["trace_id"], item["parent_id"]), []).append(item)
        found = []
        stack = list(children.get((row["trace_id"], row["span_id"]), []))
        while stack:
            current = stack.pop()
            found.append(current)
            stack.extend(children.get((current["trace_id"], current["span_id"]), []))
        return found

    if op == ">>":
        return [row for row in right if any((a["trace_id"], a["span_id"]) in left_keys for a in ancestors(row))]
    if op == "<<":
        return [row for row in left if any((d["trace_id"], d["span_id"]) in right_keys for d in descendants(row))]
    # "~" siblings: spans from either side that share a parent with a
    # matched span from the other side
    keys = left_keys | right_keys
    out = []
    for row in left + right:
        trace = by_trace.get(row["trace_id"], {})
        for sibling_id, sibling in trace.items():
            if (
                sibling_id != row["span_id"]
                and sibling["parent_id"] == row["parent_id"]
                and (row["trace_id"], sibling_id) in keys
            ):
                out.append(row)
                break
    return out


def _fetch_trace_spans(conn, trace_ids):
    if not trace_ids:
        return []
    marks = ",".join("?" for _ in trace_ids)
    sql = f"""
    SELECT hex(trace_id), hex(span_id), hex(parent_id), name, kind, status_code, status_msg,
           service_name, start_ns, end_ns, attrs, events, resource, scope_name
    FROM spans WHERE trace_id IN ({marks}) ORDER BY start_ns
    """
    return conn.execute(sql, [bytes.fromhex(trace_id) for trace_id in trace_ids]).fetchall()


def _regexp(pattern, value):
    return value is not None and re.search(pattern, str(value)) is not None


def execute(cfg, text, from_ms=None, to_ms=None, limit=1000):
    """Run a TraceQL-ish query against cfg's spans table.

    Returns a list of row dicts (spans) or aggregate rows.
    """
    _select, spanexpr, pipes = parse(text)
    conn = db.connect(cfg.db_path)
    try:
        conn.create_function("regexp", 2, _regexp)
        if spanexpr[0] == "struct":
            rows = _run_struct(conn, spanexpr)
            if from_ms is not None:
                rows = [row for row in rows if row["start_ms"] >= from_ms]
            if to_ms is not None:
                rows = [row for row in rows if row["start_ms"] <= to_ms]
        else:
            rows = [_row_dict(raw) for raw in _fetch_rows(conn, spanexpr, MAX_SPANS, from_ms, to_ms)]
    finally:
        conn.close()
    if pipes:
        rows = _apply_pipes(rows, pipes)
    return rows[:limit]
