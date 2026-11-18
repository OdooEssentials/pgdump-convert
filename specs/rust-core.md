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

- [ ] Profile `_filter_dump` on a realistic multi-GB dump
- [ ] Rust crate + PyO3 bindings for the text transform
- [ ] Swap `core._filter_dump` for the binding behind the same signature
- [ ] Extend bindings to zip staging if worthwhile
- [ ] Benchmarks in README

---

_Note: the parent feature spec lives in `dreispt/osh-contrib` at
`specs/pgconvert-db-export.md`; this repo mirrors that conversion
logic standalone._
