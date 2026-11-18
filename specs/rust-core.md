# Rust core with Python bindings — spec

**Status:** future
**Date:** 2026-02-14
**Scope:** `pgdump_convert/core.py` internals; CLI surface unchanged.

## Context

The conversion pipeline is I/O-bound today: streaming a multi-GB
`dump.sql` through `_filter_dump` line-by-line in Python is the hot
path, plus zip repacking. A native core would be faster and would let
the tool be embedded by other programs.

## Spec (when picked up)

- Implement the dump transformation (`_filter_dump`: restrict-strip,
  `transaction_timeout` drop, PG18 NOT NULL rewrite) and the
  restore/re-dump orchestration in Rust.
- Expose it to Python via PyO3 bindings; the `pgdump-convert` CLI keeps
  working unchanged on top of the bindings.
- Docker orchestration may stay in Python initially — the Rust core
  covers the streaming text transform first.
- Acceptance: identical output zips for the existing test corpus; the
  Python test suite keeps passing against the bindings.

## Alternatives considered

- **Keep pure Python** — acceptable now; revisit if profiling shows the
  filter is a bottleneck on real dumps.
- **Rewrite the whole CLI in Rust** — rejected for now: the CLI is thin
  and Python bindings preserve the existing interface and ecosystem.

## Tasks

- [x] Profile `_filter_dump` on a realistic multi-GB dump — done on the
      2.7 GB `vinha-odoo` PG18 dump: Rust 2.9 s vs Python 3.7 s (**1.3×**,
      byte-identical output). The pipeline is I/O-bound (~5.4 GB
      read+write), so the transform is not the bottleneck.
- [x] ~~Rust crate + PyO3 bindings~~ — implemented then **reverted**:
      the speedup did not justify a required Rust toolchain. Python
      `LINE_RULES`/`BLOCK_RULES` remain the single implementation.
- [ ] Revisit only if profiling shows the transform (not docker/psql
      I/O) dominates on real workloads, or if the tool is embedded by
      other programs

---

_Note: the parent feature spec lives in `dreispt/osh-contrib` at
`specs/pgconvert-db-export.md`; this repo mirrors that conversion
logic standalone._
