"""Dump transform rules for PostgreSQL version conversion.

``_filter_dump`` in ``core.py`` streams the dump through two kinds of
rules so new PostgreSQL-version quirks are a few lines to add:

- ``LINE_RULES``: per-line filters. Add ``LineRule(name, pattern,
  replacement)``; ``replacement=None`` drops the line, otherwise the
  match is expanded via ``re.Match.expand`` (``\\g<name>`` groups work).
- ``BLOCK_RULES``: multi-line statement transforms. The common case is
  declarative — ``StatementRule(name, start, element, emit)`` buffers
  one statement, drops lines matching ``element``, and expands ``emit``
  once per dropped line. Anything more complex subclasses ``BlockRule``
  (``starts``/``feed``/``emit``) and is registered in ``BLOCK_RULES``.
  Entries are factories: classes or callable specs, called per run.
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class LineRule:
    """Drop or rewrite lines matching ``pattern``.

    ``replacement=None`` drops the line; otherwise the match is expanded
    with ``re.Match.expand`` so ``\\g<name>``/``\\1`` groups are allowed.
    """

    name: str
    pattern: re.Pattern
    replacement: bytes | None = None

    def apply(self, line):
        m = self.pattern.match(line)
        if m is None:
            return line
        return None if self.replacement is None else m.expand(self.replacement)


RESTRICT_RE = re.compile(rb"^\\(?:un)?restrict [A-Za-z0-9]+\s*$")
SET_TIMEOUT_RE = re.compile(rb"^SET\s+transaction_timeout\s*=")

LINE_RULES = [
    # psql <17.6 cannot parse these meta-commands emitted by pg_dump 17.6+.
    LineRule("restrict-unrestrict", RESTRICT_RE),
    # transaction_timeout was added in PG17; older servers reject the SET.
    LineRule("transaction-timeout", SET_TIMEOUT_RE),
]


class BlockRule:
    """Buffer one multi-line SQL statement and emit its replacement.

    ``starts(line)`` returns True on the statement's first line and
    initializes state; ``feed(line)`` buffers each line (including the
    first) and returns True once the statement is complete; ``emit()``
    returns the replacement bytes. ``dropped`` counts rewritten elements
    for the end-of-run warning.
    """

    dropped = 0

    def starts(self, line):
        raise NotImplementedError

    def feed(self, line):
        raise NotImplementedError

    def emit(self):
        raise NotImplementedError


# --- declarative statement rules --------------------------------------------

END_OF_STATEMENT_RE = re.compile(rb";\s*$")
_GROUP_RE = re.compile(rb"\\g<(\w+)>")


def _expand(template, groups):
    """Expand ``\\g<name>`` placeholders in *template* from *groups*."""
    return _GROUP_RE.sub(lambda m: groups[m.group(1).decode()], template)


class StatementRule:
    """Declarative transform for one buffered SQL statement.

    ``start`` matches the statement's first line; its named groups
    (e.g. ``table``) stay visible for ``emit`` expansion. ``element``
    is matched against every buffered line — matching lines are dropped
    and each match expands ``emit`` once (named groups from both
    ``start`` and the element match). ``end`` marks the statement's
    last line (default: a line ending in ``;``).

    Calling the instance returns a fresh ``BlockRule`` worker, so
    ``BLOCK_RULES`` can hold specs directly.
    """

    def __init__(self, name, start, element, emit=None, end=END_OF_STATEMENT_RE):
        self.name = name
        self.start = start
        self.element = element
        self.emit = emit
        self.end = end

    def __call__(self):
        return _StatementBlock(self)


class _StatementBlock(BlockRule):
    def __init__(self, rule):
        self._rule = rule
        self._start_groups = {}
        self._buf = []
        self.dropped = 0

    def starts(self, line):
        m = self._rule.start.match(line)
        if m is None:
            return False
        self._start_groups = m.groupdict()
        self._buf = []
        self.dropped = 0
        return True

    def feed(self, line):
        self._buf.append(line)
        return self._rule.end.search(line) is not None

    def emit(self):
        out = bytearray()
        hits = []
        for buf_line in self._buf:
            m = self._rule.element.match(buf_line)
            if m:
                hits.append(m)
                self.dropped += 1
                continue
            out += buf_line
        if self._rule.emit is not None:
            for m in hits:
                groups = {**self._start_groups, **m.groupdict()}
                out += _expand(self._rule.emit, groups)
        self._buf = []
        return bytes(out)


# PG18 emits NOT NULL as named table constraints; PG<=17 rejects them.
# They are moved into ``ALTER TABLE ONLY <table> ALTER COLUMN <col>
# SET NOT NULL;`` statements after the CREATE TABLE.
PG18_NOT_NULL_RE = re.compile(
    rb'^\s*(?:CONSTRAINT\s+\w+\s+)?NOT\s+NULL\s+(?P<col>"[^"]+"|\w+),?\s*$'
)
CREATE_TABLE_RE = re.compile(
    rb"^CREATE\s+(?:UNLOGGED\s+|TEMP(?:ORARY)?\s+)?TABLE\s+(?:IF NOT EXISTS\s+)?"
    rb'(?P<table>(?:"[^"]+"|[\w]+)(?:\.(?:"[^"]+"|[\w]+))?)\s*\('
)

BLOCK_RULES = [
    StatementRule(
        "pg18-not-null",
        start=CREATE_TABLE_RE,
        element=PG18_NOT_NULL_RE,
        emit=(b"ALTER TABLE ONLY \\g<table> ALTER COLUMN \\g<col>" b" SET NOT NULL;\n"),
    ),
]
