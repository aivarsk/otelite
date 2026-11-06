"""Tiny parser combinator library plus shared bits for the ql grammars.

A Parser wraps a function (text, pos) -> (value, new_pos). Failure raises
ParseError; ordered choice tries the right branch only when the left one
failed, and reports whichever branch got furthest. This is all backtracking
and small inputs, so that is enough; no caching, no grammar preprocessing.

ASTs are plain tuples: (tag, ...) so the grammars stay readable and the
interpreters can switch on tag without visitor machinery.

Below the Parser class lives shared machinery the three grammars would
otherwise each redefine: reserved-word matching, quoted strings, label
matchers, duration literals, and the two runtime helpers (matcher and
aggregate) used by more than one interpreter.
"""

import re


class ParseError(Exception):
    def __init__(self, text, pos, expected):
        self.pos = pos
        self.expected = expected
        line = text.count("\n", 0, pos) + 1
        col = pos - text.rfind("\n", 0, pos)
        snippet = text[pos : pos + 24].split("\n", 1)[0]
        super().__init__(f"line {line} column {col}: expected {expected}, got {snippet!r}")


class Parser:
    def __init__(self, fn):
        self.fn = fn

    def __call__(self, text, pos):
        return self.fn(text, pos)

    def map(self, f):
        def parse(text, pos):
            value, pos = self(text, pos)
            return f(value), pos

        return Parser(parse)

    def then(self, other):
        # sequence, keep the right value
        def parse(text, pos):
            _value, pos = self(text, pos)
            return other(text, pos)

        return Parser(parse)

    def skip(self, other):
        # sequence, keep the left value
        def parse(text, pos):
            value, pos = self(text, pos)
            _other, pos = other(text, pos)
            return value, pos

        return Parser(parse)

    def or_(self, other):
        def parse(text, pos):
            try:
                return self(text, pos)
            except ParseError as first:
                try:
                    return other(text, pos)
                except ParseError as second:
                    raise second if second.pos >= first.pos else first

        return Parser(parse)

    def __or__(self, other):
        return self.or_(other)

    def __rshift__(self, other):
        return self.then(other)

    def __lshift__(self, other):
        return self.skip(other)


def satisfy(pred, expected):
    def parse(text, pos):
        if pos < len(text) and pred(text[pos]):
            return text[pos], pos + 1
        raise ParseError(text, pos, expected)

    return Parser(parse)


def lit(word):
    def parse(text, pos):
        if text.startswith(word, pos):
            return word, pos + len(word)
        raise ParseError(text, pos, repr(word))

    return Parser(parse)


def regex(pattern, expected=None):
    import re

    compiled = re.compile(pattern)

    def parse(text, pos):
        match = compiled.match(text, pos)
        if match:
            return match.group(0), match.end()
        raise ParseError(text, pos, expected or pattern)

    return Parser(parse)


def _undefined(text, pos):
    raise ParseError(text, pos, "recursive parser used before define()")


class Forward(Parser):
    # placeholder for recursive grammars; define() installs the real parser
    def __init__(self):
        super().__init__(self._parse)
        self.target = Parser(_undefined)

    def _parse(self, text, pos):
        return self.target(text, pos)

    def define(self, parser):
        self.target = parser


def forward():
    return Forward()


def optional(parser, default=None):
    def parse(text, pos):
        try:
            return parser(text, pos)
        except ParseError:
            return default, pos

    return Parser(parse)


def many(parser):
    def parse(text, pos):
        out = []
        while True:
            try:
                value, pos = parser(text, pos)
                out.append(value)
            except ParseError:
                return out, pos

    return Parser(parse)


def many1(parser):
    def parse(text, pos):
        first, pos = parser(text, pos)
        rest, pos = many(parser)(text, pos)
        return [first] + rest, pos

    return Parser(parse)


def sep_by(parser, sep):
    def parse(text, pos):
        out = []
        try:
            value, pos = parser(text, pos)
            out.append(value)
        except ParseError:
            return out, pos
        while True:
            try:
                _s, pos = sep(text, pos)
                value, pos = parser(text, pos)
                out.append(value)
            except ParseError:
                return out, pos

    return Parser(parse)


def between(open_parser, close_parser, parser):
    return open_parser >> parser << close_parser


def lexeme(parser):
    # trailing whitespace skipper; grammars stay whitespace-blind
    return parser << regex(r"\s*", "whitespace")


def token(word):
    return lexeme(lit(word))


def run(parser, text):
    value, pos = parser(text, 0)
    if text[pos:].strip():
        raise ParseError(text, pos, "end of input")
    return value


# --- shared grammar pieces -----------------------------------------------


def _list():
    return []


def _dict():
    return dict()


def keyword(word):
    # reserved word: must not be followed by an identifier char, dot, or
    # colon (colon matters to promql-style metric names)
    return lexeme(regex(rf"{re.escape(word)}(?![\w.:])", repr(word)))


def _unquote(raw):
    # string literals here are lenient about unknown escapes (\d stays \d);
    # PromQL/LogQL proper require valid Go escapes, noted in the compat notes
    body = raw[1:-1]
    out = []
    i = 0
    while i < len(body):
        char = body[i]
        if char == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            out.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(nxt, "\\" + nxt))
            i += 2
        else:
            out.append(char)
            i += 1
    return "".join(out)


quoted_string = lexeme(regex(r'"(?:[^"\\]|\\.)*"', "string")).map(_unquote)
identifier = lexeme(regex(r"[A-Za-z_][\w.]*", "name"))
matcher_op = lexeme(regex(r"=~|!=|!~|=", "matcher operator"))
duration_literal = lexeme(regex(r"\d+(?:\.\d+)?(?:ns|us|ms|s|m|h)", "duration"))


def _matcher_fn(text, pos):
    name, pos = identifier(text, pos)
    op, pos = matcher_op(text, pos)
    value, pos = quoted_string(text, pos)
    return (name, op, value), pos


label_matcher = Parser(_matcher_fn)


# --- shared runtime pieces -------------------------------------------------

_OPS = {"=", "!=", "=~", "!~"}


def match_value(value, op, pattern):
    if op == "=":
        return value == pattern
    if op == "!=":
        return value != pattern
    if op == "=~":
        return re.search(pattern, value or "") is not None
    if op == "!~":
        return re.search(pattern, value or "") is None
    raise ValueError(op)


def aggregate_value(op, values):
    if op == "avg":
        return sum(values) / len(values)
    if op == "min":
        return min(values)
    if op == "max":
        return max(values)
    if op == "count":
        return len(values)
    return sum(values)


def complete(parser):
    # wrap a whole-input parser with the trailing-garbage check every
    # grammar's parse() used to carry by hand
    def parse(text):
        parsed, pos = parser(text, 0)
        if text[pos:].strip():
            raise ParseError(text, pos, "end of input")
        return parsed

    return parse


def default_range(from_ms, to_ms, default_span_ms):
    # range defaulting shared by the file- and table-walking interpreters
    import datetime as _dt

    if to_ms is None:
        to_ms = int(_dt.datetime.now(_dt.timezone.utc).timestamp() * 1000)
    if from_ms is None:
        from_ms = to_ms - default_span_ms if default_span_ms else 0
    return from_ms, to_ms
