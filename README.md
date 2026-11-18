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
built by maturin from `native/`) — it is required, not optional.

### Adding a transform rule

Version-compatibility quirks live in `native/src/lib.rs` — the single
source of truth, implemented as streaming line rules (drop/rewrite
regexes) and a `CREATE TABLE` block rewriter for multi-line statements.
COPY payloads are never touched. Add a rule there and cover it with a
`cargo test` case.

## Install

```
pip install pgdump-convert   # or: pipx install pgdump-convert
```

## Development

A Rust toolchain is required to build from source (prebuilt wheels will
not need one):

```
pip install maturin
pip install -e '.[test]'
python -m pytest
cargo test --manifest-path native/Cargo.toml
```

The Rust core lives in `native/` and is built by maturin as
`pgdump_convert._rs`; it is the only implementation of the dump
transform rules. CI runs `cargo test` plus the Python suite on
Python 3.10–3.14.

## Roadmap

- Benchmark the native transform against realistic multi-GB dumps and
  record numbers in this README. See `specs/rust-core.md`.
