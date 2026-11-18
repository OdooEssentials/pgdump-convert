# pgdump-convert

Convert a PostgreSQL dump to a different server major version.

```
pgdump-convert [INPUT] [OUTPUT] [--to 16] [--image IMAGE]
               [--container-db NAME] [--from-db NAME] [--filestore DIR]
               [--dry-run]
```

The dump is restored into a throwaway `postgres:<version>` docker
container and re-dumped from that server, so the output is guaranteed to
be compatible with the target version — e.g. turning a PostgreSQL 18
dump into one a PostgreSQL 16 server (odoo.sh) can restore.

## Sources

- **File** — an Odoo-format zip backup (`dump.sql` + `filestore/` +
  `manifest.json`) or a plain `pg_dump -Fp` `.sql` file. Custom-format
  (`pg_dump -Fc`) files are rejected.
- **Database** — `--from-db NAME` dumps via the local `pg_dump`; libpq
  environment variables (`PGHOST`, `PGUSER`, `PGPASSWORD`, `~/.pgpass`,
  …) apply as usual. A manifest is synthesized with the installed
  module list from `ir_module_module`.

With `--from-db` the first positional argument is the output path.

## Output

Always an Odoo-format zip: `dump.sql` stays plain SQL (the format Odoo's
zip restore feeds to `psql`), `filestore/` is included when present in
the input zip or via `--filestore DIR`, and `manifest.json`'s
`pg_version` is updated to the target server version.

The dump filter also rewrites constructs newer PostgreSQL clients emit
that older servers reject:

- `\restrict`/`\unrestrict` meta-commands (psql <17.6);
- `SET transaction_timeout` (GUC added in PG17);
- PG18 table-level `[CONSTRAINT name] NOT NULL col` elements, rewritten
  as `ALTER TABLE ... ALTER COLUMN ... SET NOT NULL`.

Requires docker. The container is always removed afterwards.

The dump transform runs in a Rust extension (`pgdump_convert._rs`,
PyO3/maturin) when built; otherwise the identical pure-Python pipeline
is used, so the CLI works from a plain `pip install` too.

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

Note: the Rust extension (`native/`) mirrors the built-in rules for
speed. When you add a rule here, port it to `native/src/lib.rs` too —
or wait for the bindings work in `specs/rust-core.md`.

## Install

```
pip install pgdump-convert   # or: pipx install pgdump-convert
```

## Development

```
# Rust toolchain required for the native extension (optional: the
# pure-Python fallback is used when it is absent)
pip install maturin
pip install -e '.[test]'
python -m pytest
```

The Rust core lives in `native/` and is built by maturin as
`pgdump_convert._rs`. CI builds it on every Python version and also
runs the suite once without it to cover the fallback.

## Roadmap

- Expose the rule registries (`LINE_RULES`/`BLOCK_RULES`) through the
  Rust bindings so new transforms can be added in one place. See
  `specs/rust-core.md`.
