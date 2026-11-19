# pgdump-convert — spec

**Status:** in progress
**Date:** 2026-02-14
**Scope:** this repo; standalone extraction of the conversion pipeline
from `dreispt/osh-contrib`'s `osh_pgconvert` plugin.

## Context

`osh db dump-convert` converts PostgreSQL dumps between major versions
(e.g. PG18 → PG16 for odoo.sh). That logic is useful outside osh
projects too, so it lives here as a standalone CLI.

**Decision (2026-02):** the tool is SQL→SQL only — no Odoo zip format,
no manifest, no filestore. Input must be plain SQL (`pg_dump -Fp`);
output is the filtered SQL. Zip/custom-format inputs are rejected.

## Spec

```
pgdump-convert [INPUT] [OUTPUT] [--to 16] [--image IMAGE]
               [--container-db NAME] [--from-db NAME]
               [--docker-test|--docker-import] [--dry-run]
               [--allow-restore-errors]
```

- `INPUT` defaults to **stdin** (`-` explicit); must be plain SQL.
  Bare invocation on a TTY errors instead of blocking.
- `OUTPUT` defaults to **stdout** (`-` explicit); all log messages go
  to stderr.
- `--from-db NAME`: dump via local `pg_dump` (libpq env applies). First
  positional becomes the OUTPUT path (defaults to stdout).
- Default pipeline: stream the dump through the transform rules and
  write the result — seconds on multi-GB dumps, no docker needed.
- `--docker-test`: post-conversion check — restore the converted SQL
  into a throwaway `postgres:<to>` container to prove it loads, then
  remove the container (`try/finally`).
- `--docker-import`: same restore but the container is left running
  for inspection. Mutually exclusive with `--docker-test`.
- Restore `ERROR:` lines in the psql log fail the conversion fast;
  `--allow-restore-errors` opts out. Nonzero psql exit codes always
  fail (fatal errors are not SQL errors).
- `_filter_dump` applies registered rules so version quirks are easy
  to add: `LINE_RULES` (per-line drop/rewrite via `LineRule`) and
  `BLOCK_RULES` (mostly declarative `StatementRule` specs — start /
  element / emit template; `BlockRule` subclasses remain for complex
  cases). Rules rewrite/drop:
  `\restrict`/`\unrestrict`, `SET transaction_timeout`, PG18 NOT NULL
  table constraints → `ALTER TABLE ... SET NOT NULL`. COPY payloads
  pass through untouched.
- `--dry-run` needs no docker or pg tools.

## Alternatives considered

- **Odoo zip in/out** — dropped: the zip/manifest/filestore layer is
  Odoo-specific; plain SQL is the lower-level primitive other tools
  (incl. the osh plugin) can wrap.
- **Docker round-trip as the default** — rejected after benchmarking:
  the pipeline is I/O-bound and the known PG-version quirks are few.
  Conversion is filter-only; `--docker-test`/`--docker-import` remain
  as optional helpers.
- **Rust core with PyO3 bindings** — **abandoned**: implemented and
  benchmarked on a real 2.7 GB PG18 Odoo dump, where the
  Rust transform was only ~1.3x faster than pure Python (2.9 s vs
  3.7 s, byte-identical output). The pipeline moves ~5.4 GB of I/O,
  so the transform is not the bottleneck — no relevant speedup over
  pure Python. Revisit only if profiling ever shows the transform
  itself dominating.
- **Custom format (`-Fc`) input** — rejected: `pg_restore` refuses
  dumps from newer `pg_dump` majors; plain SQL is the only tolerated
  direction.
- **Re-dump canonicalization** — the docker helpers verify the dump
  loads but the output stays the filtered input, not a re-dump from
  the target server.

## Tasks

- [x] Spec
- [x] Port conversion pipeline from osh_pgconvert (incl. PG18 filter)
- [x] CLI: file/stdin sources, docker helpers, dry-run
- [x] Tests (fake docker + fake pg tools)
- [x] GitHub repo + CI
- [x] Simplify to SQL→SQL (drop zip/manifest/filestore)
