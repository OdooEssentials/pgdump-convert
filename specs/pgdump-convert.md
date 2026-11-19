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
               [--engine filter|docker] [--dry-run] [--allow-restore-errors]
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
- `--engine filter` (default): stream the dump through the transform
  rules and repack — seconds on multi-GB dumps, no docker needed, but
  not verified against a server. `pg_version` is stamped with `--to`.
- `--engine docker`: additionally restore into a throwaway
  `postgres:<to>` container and re-dump with that server's `pg_dump` —
  proves the output loads on the target and is emitted in its canonical
  dialect. Docker is an optional dependency used only here.
- `_filter_dump` applies registered rules so version quirks are easy to
  add: `LINE_RULES` (per-line drop/rewrite via `LineRule`) and
  `BLOCK_RULES` (multi-line statement transforms via `BlockRule`
  subclasses, e.g. `Pg18NotNull`). Rules rewrite/drop:
  `\restrict`/`\unrestrict`, `SET transaction_timeout`, PG18 NOT NULL
  table constraints → `ALTER TABLE ... SET NOT NULL`. COPY payloads
  pass through untouched.
- Under `--engine docker` the container is always removed
  (`try/finally`); `--dry-run` needs no docker or pg tools.
- Restore `ERROR:` lines in the psql log fail the conversion fast
  (partial restores must not be re-dumped silently);
  `--allow-restore-errors` opts out.

## Alternatives considered

- **Custom format (`-Fc`) intermediate** — rejected: `pg_restore`
  refuses dumps from newer `pg_dump` majors; plain SQL is the only
  format both directions tolerate.
- **Docker round-trip as the default** — rejected after benchmarking:
  the pipeline is I/O-bound (Rust transform was only ~1.3x faster), and
  the known PG-version quirks are few. Filter-only is the default;
  `--engine docker` remains as the verification step for when certainty
  matters (e.g. before uploading to odoo.sh).
- **Require docker-in-docker / remote docker** — out of scope; the
  tool uses the local docker daemon (`DOCKER_HOST` honored).

## Tasks

- [x] Spec
- [x] Port conversion pipeline from osh_pgconvert (incl. PG18 filter)
- [x] CLI with file/DB sources, filestore, dry-run
- [x] Tests (fake docker + fake pg tools)
- [ ] GitHub repo + CI
