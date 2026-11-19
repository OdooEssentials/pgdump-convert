"""Dump transform rules for PostgreSQL version conversion.

``_filter_dump`` in ``core.py`` streams the dump through two kinds of
rules so new PostgreSQL-version quirks are a few lines to add:

- ``LINE_RULES``: per-line filters. Add ``LineRule(name, pattern,
  replacement)``; ``replacement=None`` drops the line, otherwise the
  match is expanded via ``re.Match.expand`` (``\\g<name>`` groups work).
- ``BLOCK_RULES``: multi-line statement transforms. Subclass
  ``BlockRule``, implement ``starts``/``feed``/``emit``, and register
  the class in ``BLOCK_RULES``.
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


# PG18 emits NOT NULL as named table constraints; PG<=17 rejects them.
PG18_NOT_NULL_RE = re.compile(
    rb'^\s*(?:CONSTRAINT\s+\w+\s+)?NOT\s+NULL\s+("[^"]+"|\w+),?\s*$'
)
CREATE_TABLE_RE = re.compile(
    rb"^CREATE\s+(?:UNLOGGED\s+|TEMP(?:ORARY)?\s+)?TABLE\s+(?:IF NOT EXISTS\s+)?"
    rb'((?:"[^"]+"|[\w]+)(?:\.(?:"[^"]+"|[\w]+))?)\s*\('
)


class Pg18NotNull(BlockRule):
    """Rewrite PG18 ``[CONSTRAINT name] NOT NULL col`` table elements.

    They are moved into ``ALTER TABLE ONLY <table> ALTER COLUMN <col>
    SET NOT NULL;`` statements after the CREATE TABLE.
    """

    def __init__(self):
        self._buf = []
        self._table = None
        self._cols = []
        self.dropped = 0

    def starts(self, line):
        m = CREATE_TABLE_RE.match(line)
        if m is None:
            return False
        self._table = m.group(1)
        self._buf = []
        self._cols = []
        return True

    def feed(self, line):
        self._buf.append(line)
        return line.rstrip().endswith(b";")

    def emit(self):
        out = bytearray()
        for buf_line in self._buf:
            m = PG18_NOT_NULL_RE.match(buf_line)
            if m:
                self._cols.append(m.group(1))
                self.dropped += 1
                continue
            out += buf_line
        for col in self._cols:
            out += (
                b"ALTER TABLE ONLY "
                + self._table
                + b" ALTER COLUMN "
                + col
                + b" SET NOT NULL;\n"
            )
        self._buf = []
        self._cols = []
        return bytes(out)


BLOCK_RULES = [Pg18NotNull]
