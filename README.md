# pgdump-convert

You have a PostgreSQL dump produced by a newer server (say PostgreSQL 18) and you need it on an older one (odoo.sh runs 16). Fed to the old
server as-is, it fails: newer `psql` meta-commands, GUCs, and syntax are
rejected. `pgdump-convert` rewrites the SQL for the target version.

```
cat dump.sql | pgdump-convert --to 16 > dump-pg16.sql
```

## Usage

```
pgdump-convert [INPUT] [OUTPUT] [--to 16] [--image IMAGE]
               [--container-db NAME] [--from-db NAME]
               [--docker-test|--docker-import] [--dry-run]
               [--allow-restore-errors]
```

`INPUT` defaults to **stdin** and `OUTPUT` to **stdout** — the tool is
pipe-friendly (`-` selects either stream explicitly). Input must be
plain SQL (`pg_dump -Fp`); zip archives and custom-format (`pg_dump
-Fc`) dumps are rejected. All progress messages go to stderr.

### Sources

- **File/stdin** — a plain `pg_dump -Fp` `.sql` file or SQL piped on
  stdin.
- **Database** — `--from-db NAME` dumps via the local `pg_dump`; libpq
  environment variables (`PGHOST`, `PGUSER`, `PGPASSWORD`,
  `~/.pgpass`, …) apply as usual.

With `--from-db` the first positional argument is the output path
(default: stdout).

### What it rewrites

The transform rules cover constructs newer PostgreSQL clients emit
that older servers reject:

- `\restrict`/`\unrestrict` meta-commands (psql <17.6);
- `SET transaction_timeout` (GUC added in PG17);
- PG18 table-level `[CONSTRAINT name] NOT NULL col` elements, rewritten
  as `ALTER TABLE ... ALTER COLUMN ... SET NOT NULL`.

COPY payloads are never touched, and multi-GB dumps stream line by
line.

### Docker helpers

By default the tool only rewrites the SQL — fast (seconds on multi-GB
dumps) and needs no docker, but covers only the known version quirks
above. Two optional flags load the converted SQL into a
`postgres:<to>` container:

- **`--docker-test`** — restores the dump to prove it loads on the
  target version, then removes the container. Restore `ERROR:` lines
  fail the conversion — the dump may be incomplete; pass
  `--allow-restore-errors` to accept them anyway.
- **`--docker-import`** — same restore, but the container is left
  running for inspection (`docker exec -it <name> psql -U postgres -d
odoo`; remove with `docker rm -f <name>`). Mutually exclusive with
  `--docker-test`.

### Adding a transform rule

Version-compatibility quirks live in `pgdump_convert/rules.py` as two
registries, both applied while streaming the dump (COPY payloads are
never touched). Prefer the declarative forms — a new quirk is usually a
few lines of regex/template, no new code:

- **`LINE_RULES`** — per-line drop/rewrite. Append
  `LineRule("name", PATTERN, replacement)`; `replacement=None` drops the
  line, otherwise the match is expanded with `re.Match.expand`
  (`\g<name>` groups work).
- **`BLOCK_RULES`** — multi-line statement transforms. The common case
  is declarative: `StatementRule(name, start=START_RE, element=ELEM_RE,
emit=TEMPLATE)` buffers one statement (first line matching `start`,
  last line matching `end`, default `;`), drops lines matching
  `element`, and expands `emit` once per dropped line. Named groups
  from both `start` and `element` are available as `\g<name>` in the
  template — the `pg18-not-null` rule is the example:

  ```python
  StatementRule(
      "pg18-not-null",
      start=CREATE_TABLE_RE,     # captures ?P<table>
      element=PG18_NOT_NULL_RE,  # captures ?P<col>
      emit=b"ALTER TABLE ONLY \\g<table> ALTER COLUMN \\g<col>"
           b" SET NOT NULL;\n",
  )
  ```

  When a transform can't be expressed that way — stateful rewrites,
  reordering, conditional logic — subclass `BlockRule`
  (`starts`/`feed`/`emit`) and register the class in `BLOCK_RULES`.
  Entries are factories (a class or a callable spec), instantiated
  fresh on every run.

## Install

```
pip install pgdump-convert   # or: pipx install pgdump-convert
```

Docker is an **optional** dependency: only needed for `--docker-test`
and `--docker-import`. The default conversion needs nothing beyond
Python.

## Development

```
python -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/python -m pytest
```

Tests use fake `docker`/pg-tool binaries; the docker helpers have also
been exercised against real PG18→PG16 dumps.

Spec and decisions: `specs/pgdump-convert.md`.
