# pgdump-convert — spec

**Status:** in progress
**Date:** 2026-02-14
**Scope:** this repo; standalone extraction of the conversion pipeline
from `dreispt/osh-contrib`'s `osh_pgconvert` plugin.

## Context

`osh db dump-convert` converts PostgreSQL dumps between major versions
(e.g. PG18 → PG16 for odoo.sh) by restoring `dump.sql` into a
throwaway `postgres:<to>` docker container and re-dumping. That logic
is useful outside osh projects too, so it lives here as a standalone
CLI.

## Spec

```
pgdump-convert [INPUT] [OUTPUT] [--to 16] [--image IMAGE]
               [--container-db NAME] [--from-db NAME] [--filestore DIR]
               [--docker-test|--docker-import] [--dry-run]
               [--allow-restore-errors]
```

- `INPUT` defaults to **stdin** (`-` explicit); Odoo zip backup or
  plain `pg_dump -Fp` `.sql` file. `pg_dump -Fc` custom format → clear
  error. Bare invocation on a TTY errors instead of blocking.
- `OUTPUT` defaults to **stdout** (`-` explicit); the zip is streamed
  binary-safe and all log messages go to stderr.
- `--from-db NAME`: dump via local `pg_dump` (libpq env applies);
  synthesizes `manifest.json` with installed modules from
  `ir_module_module`. First positional becomes the OUTPUT path
  (defaults to stdout).
- `--filestore DIR`: bundle a directory as `filestore/` in the zip
  (works with file and DB sources).
- Output is always an Odoo-format zip: plain-SQL `dump.sql`, optional
  `filestore/`, `manifest.json` with `pg_version` set to the target
  version.
- Default: stream the dump through the transform rules and repack —
  seconds on multi-GB dumps, no docker needed, covers only the known
  version quirks. `pg_version` is stamped with `--to`.
- `--docker-test`: post-conversion check — restore the produced
  `dump.sql` into a throwaway `postgres:<to>` container to prove it
  loads, then remove the container. The zip keeps the filtered dump
  (not a re-dump); on success `pg_version` is the real server version.
- `--docker-import`: same restore but the container is left running
  for inspection. Mutually exclusive with `--docker-test`. Docker is
  an optional dependency used only by these two flags.
- `_filter_dump` applies registered rules so version quirks are easy to
  add: `LINE_RULES` (per-line drop/rewrite via `LineRule`) and
  `BLOCK_RULES` (multi-line statement transforms via `BlockRule`
  subclasses, e.g. `Pg18NotNull`). Rules rewrite/drop:
  `\restrict`/`\unrestrict`, `SET transaction_timeout`, PG18 NOT NULL
  table constraints → `ALTER TABLE ... SET NOT NULL`. COPY payloads
  pass through untouched.
- Under `--docker-test` the container is always removed
  (`try/finally`); `--docker-import` deliberately keeps it. `--dry-run`
  needs no docker or pg tools.
- Restore `ERROR:` lines in the psql log fail the conversion fast
  (partial restores must not be re-dumped silently);
  `--allow-restore-errors` opts out.

## Alternatives considered

- **Custom format (`-Fc`) intermediate** — rejected: `pg_restore`
  refuses dumps from newer `pg_dump` majors; plain SQL is the only
  format both directions tolerate.
- **Docker round-trip as the default** — rejected after benchmarking:
  the pipeline is I/O-bound (Rust transform was only ~1.3x faster), and
  the known PG-version quirks are few. Conversion is filter-only by
  default; `--docker-test`/`--docker-import` remain as optional docker
  helpers for when certainty or inspection matters (e.g. before
  uploading to odoo.sh).
- **Require docker-in-docker / remote docker** — out of scope; the
  tool uses the local docker daemon (`DOCKER_HOST` honored).

## Tasks

- [x] Spec
- [x] Port conversion pipeline from osh_pgconvert (incl. PG18 filter)
- [x] CLI with file/DB sources, filestore, dry-run
- [x] Tests (fake docker + fake pg tools)
- [ ] GitHub repo + CI
