# pgdump-convert

You have a PostgreSQL dump — or an Odoo zip backup — produced by a
newer server (say PostgreSQL 18) and you need it on an older one (odoo.sh
runs 16). Fed to the old server as-is, it fails: newer `psql`
meta-commands, GUCs, and syntax are rejected. `pgdump-convert` rewrites
the dump for the target version and repacks it as an Odoo-compatible zip.

```
cat backup.sql | pgdump-convert --to 16 > backup-pg16.zip
```

## Usage

```
pgdump-convert [INPUT] [OUTPUT] [--to 16] [--image IMAGE]
               [--container-db NAME] [--from-db NAME] [--filestore DIR]
               [--engine filter|docker] [--dry-run]
               [--allow-restore-errors]
```

`INPUT` defaults to **stdin** and `OUTPUT` to **stdout** — the tool is
pipe-friendly (`-` selects either stream explicitly). All progress
messages go to stderr, so stdout carries only the zip.

### Sources

- **File/stdin** — an Odoo-format zip backup (`dump.sql` + `filestore/`
  - `manifest.json`), a plain `pg_dump -Fp` `.sql` file, or either
    piped on stdin. Custom-format (`pg_dump -Fc`) input is rejected.
- **Database** — `--from-db NAME` dumps via the local `pg_dump`; libpq
  environment variables (`PGHOST`, `PGUSER`, `PGPASSWORD`, `~/.pgpass`,
  …) apply as usual. A manifest is synthesized with the installed
  module list from `ir_module_module`.

With `--from-db` the first positional argument is the output path
(default: stdout).

### Output

Always an Odoo-format zip: `dump.sql` stays plain SQL (the format
Odoo's zip restore feeds to `psql`), `filestore/` is included when
present in the input zip or via `--filestore DIR`, and `manifest.json`'s
`pg_version` is updated to the target version.

### Engines

- **`filter`** (default) — streams the dump through the registered
  transform rules and repacks. Fast (seconds on multi-GB dumps) and
  needs no docker, but the result is _not verified_ against a server:
  it covers the known version quirks below, nothing else.
- **`docker`** — additionally restores the filtered dump into a
  throwaway `postgres:<to>` container and re-dumps with that server's
  `pg_dump`. Slower (minutes on multi-GB dumps) but proves the output
  actually loads on the target version and is emitted in its canonical
  dialect. Requires docker; the container is always removed afterwards.
  Restore `ERROR:` lines fail the conversion — the dump may be
  incomplete; pass `--allow-restore-errors` to re-dump anyway.

The transform rules rewrite constructs newer PostgreSQL clients emit
that older servers reject:

- `\restrict`/`\unrestrict` meta-commands (psql <17.6);
- `SET transaction_timeout` (GUC added in PG17);
- PG18 table-level `[CONSTRAINT name] NOT NULL col` elements, rewritten
  as `ALTER TABLE ... ALTER COLUMN ... SET NOT NULL`.

### Adding a transform rule

Version-compatibility quirks live in `pgdump_convert/core.py` as two
registries, both applied while streaming the dump (COPY payloads are
never touched):

- **`LINE_RULES`** — per-line drop/rewrite. Append
  `LineRule("name", PATTERN, replacement)`; `replacement=None` drops the
  line, otherwise the match is expanded with `re.Match.expand`
  (`\g<name>` groups work).
- **`BLOCK_RULES`** — multi-line statement transforms. Subclass
  `BlockRule` (`starts`/`feed`/`emit`) and register the class, e.g. the
  `Pg18NotNull` rule that rewrites CREATE TABLE bodies.

## Install

```
pip install pgdump-convert   # or: pipx install pgdump-convert
```

Docker is an **optional** dependency: only needed for
`--engine docker` (the verification round-trip). The default `filter`
engine needs nothing beyond Python.

## Development

```
python -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/python -m pytest
```

For engine tests, fake `docker`/pg-tool binaries are used; the docker
path has also been exercised against real PG18→PG16 dumps.

Spec and decisions: `specs/pgdump-convert.md`.
Spec for the (deferred) Rust-core idea: `specs/rust-core.md`.
