# Rust core — spec

**Status:** done (rules in Rust only)
**Date:** 2026-02-14
**Scope:** `native/` crate + `pgdump_convert/core.py` dispatch; CLI
surface unchanged.

## Context

The conversion pipeline is I/O-bound: streaming a multi-GB `dump.sql`
through the transform line-by-line in Python is the hot path. Decision:
the transform rules live **only in Rust** — no pure-Python copy to keep
in sync.

## Spec

- `native/` is a PyO3/maturin crate built as `pgdump_convert._rs`.
- It exposes `filter_dump(src, dst) -> int` (count of rewritten NOT
  NULL constraints) implementing: `\restrict`/`\unrestrict` and
  `SET transaction_timeout` drops, PG18 NOT NULL table-constraint
  rewrite, COPY passthrough.
- `core._filter_dump` calls the extension; a missing extension is a
  clear `ClickException` (reinstall with a Rust toolchain or use a
  prebuilt wheel).
- Rules are added in `native/src/lib.rs` with `cargo test` coverage;
  CI runs `cargo test` plus the Python suite on 3.10–3.14.

## Alternatives considered

- **Rewrite the whole CLI in Rust** — rejected: the CLI is thin and the
  Python interface/ecosystem is worth keeping.
- **Pure-Python fallback** — rejected: two implementations of the rules
  would drift; the extension is required instead.

## Tasks

- [x] Rust crate + PyO3 bindings for `filter_dump`
- [x] Python dispatch; missing extension errors clearly
- [x] Rust unit tests + CI (`cargo test`, pytest matrix)
- [ ] Benchmark the native transform on a realistic multi-GB dump and
      record numbers in README
- [ ] Consider moving zip staging / container orchestration into Rust
      later if profiling justifies it
