# Rust core with Python bindings — spec

**Status:** in progress
**Date:** 2026-02-14
**Scope:** `native/` crate + `pgdump_convert/core.py` dispatch; CLI
surface unchanged.

## Context

The conversion pipeline is I/O-bound: streaming a multi-GB `dump.sql`
through `_filter_dump` line-by-line in Python is the hot path. A native
core is faster and can later be embedded by other programs.

## Spec

- `native/` is a PyO3/maturin crate built as `pgdump_convert._rs`.
- It exposes `filter_dump(src, dst) -> int` (count of rewritten NOT
  NULL constraints) implementing the same transform as the Python
  pipeline: `\restrict`/`\unrestrict` and `SET transaction_timeout`
  drops, PG18 NOT NULL table-constraint rewrite, COPY passthrough.
- `core._filter_dump` prefers the native module and falls back to the
  pure-Python `LINE_RULES`/`BLOCK_RULES` pipeline when the extension is
  absent (e.g. source checkout without a Rust toolchain).
- CI builds the extension on Python 3.10–3.14 and runs the suite once
  more with `PYTHONPATH=.` (no extension) to cover the fallback.

## Alternatives considered

- **Rewrite the whole CLI in Rust** — rejected: the CLI is thin and the
  Python interface/ecosystem is worth keeping.
- **Require the extension (no fallback)** — rejected: keeps `pip
install` working without a Rust toolchain for users who don't need
  the speed.

## Tasks

- [x] Profile/justify: `_filter_dump` is the hot path on multi-GB dumps
- [x] Rust crate + PyO3 bindings for `filter_dump`
- [x] Python dispatch with pure-Python fallback
- [x] CI matrix builds native + fallback job
- [ ] Expose the rule registries through the bindings so new transforms
      are added once, not mirrored in Rust and Python
- [ ] Benchmarks in README (native vs Python on a realistic dump)
- [ ] Consider moving zip staging / container orchestration into Rust
      later if profiling justifies it
