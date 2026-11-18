//! Native dump-filtering core for pgdump-convert.
//!
//! Mirrors `pgdump_convert.core`'s transform rules: per-line drop/rewrite
//! rules plus a multi-line CREATE TABLE rewrite, with COPY payloads
//! passed through untouched. Exposed to Python via PyO3.

use pyo3::exceptions::PyIOError;
use pyo3::prelude::*;
use regex::bytes::Regex;
use std::fs::File;
use std::io::{BufRead, BufReader, BufWriter, Write};
use std::path::Path;
use std::sync::LazyLock;

static RESTRICT_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^\\(?:un)?restrict [A-Za-z0-9]+\s*$").unwrap());
static SET_TIMEOUT_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^SET\s+transaction_timeout\s*=").unwrap());
static COPY_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^COPY\s").unwrap());
static CREATE_TABLE_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r#"^CREATE\s+(?:UNLOGGED\s+|TEMP(?:ORARY)?\s+)?TABLE\s+(?:IF NOT EXISTS\s+)?((?:"[^"]+"|[\w]+)(?:\.(?:"[^"]+"|[\w]+))?)\s*\("#,
    )
    .unwrap()
});
static PG18_NOT_NULL_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r#"^\s*(?:CONSTRAINT\s+\w+\s+)?NOT\s+NULL\s+("[^"]+"|\w+),?\s*$"#)
        .unwrap()
});

/// Buffered CREATE TABLE statement awaiting its closing `;`.
struct Block {
    table: Vec<u8>,
    lines: Vec<Vec<u8>>,
}

impl Block {
    fn new(table: &[u8]) -> Self {
        Block {
            table: table.to_vec(),
            lines: Vec::new(),
        }
    }

    /// Statement complete when a buffered line ends with `;`.
    fn feed(&mut self, line: &[u8]) -> bool {
        self.lines.push(line.to_vec());
        line.trim_ascii().ends_with(b";")
    }

    /// Emit the CREATE TABLE minus PG18 NOT NULL elements, followed by
    /// ALTER TABLE ... SET NOT NULL for each removed column.
    fn emit(&self) -> (Vec<u8>, u64) {
        let mut out = Vec::new();
        let mut cols: Vec<&[u8]> = Vec::new();
        for line in &self.lines {
            if let Some(caps) = PG18_NOT_NULL_RE.captures(line) {
                cols.push(caps.get(1).unwrap().as_bytes());
            } else {
                out.extend_from_slice(line);
            }
        }
        for col in &cols {
            out.extend_from_slice(b"ALTER TABLE ONLY ");
            out.extend_from_slice(&self.table);
            out.extend_from_slice(b" ALTER COLUMN ");
            out.extend_from_slice(col);
            out.extend_from_slice(b" SET NOT NULL;\n");
        }
        (out, cols.len() as u64)
    }
}

fn line_is_copy_end(line: &[u8]) -> bool {
    let t = line.strip_suffix(b"\n").unwrap_or(line);
    let t = t.strip_suffix(b"\r").unwrap_or(t);
    t == b"\\."
}

/// Stream *src* to *dst* applying the transform rules.
///
/// Returns the number of PG18 NOT NULL table constraints rewritten.
fn filter_dump_inner(src: &Path, dst: &Path) -> std::io::Result<u64> {
    let mut fin = BufReader::new(File::open(src)?);
    let mut fout = BufWriter::new(File::create(dst)?);
    let mut in_copy = false;
    let mut block: Option<Block> = None;
    let mut dropped = 0u64;
    let mut buf = Vec::new();
    loop {
        buf.clear();
        if fin.read_until(b'\n', &mut buf)? == 0 {
            break;
        }
        let line = &buf[..];
        if in_copy {
            fout.write_all(line)?;
            if line_is_copy_end(line) {
                in_copy = false;
            }
            continue;
        }
        if let Some(b) = &mut block {
            if b.feed(line) {
                let (out, n) = b.emit();
                fout.write_all(&out)?;
                dropped += n;
                block = None;
            }
            continue;
        }
        if RESTRICT_RE.is_match(line) || SET_TIMEOUT_RE.is_match(line) {
            continue;
        }
        if COPY_RE.is_match(line) {
            in_copy = true;
        }
        if let Some(caps) = CREATE_TABLE_RE.captures(line) {
            let table = caps.get(1).unwrap().as_bytes().to_vec();
            let mut b = Block::new(&table);
            if b.feed(line) {
                let (out, n) = b.emit();
                fout.write_all(&out)?;
                dropped += n;
            } else {
                block = Some(b);
            }
            continue;
        }
        fout.write_all(line)?;
    }
    fout.flush()?;
    Ok(dropped)
}

#[pyfunction]
fn filter_dump(src: &str, dst: &str) -> PyResult<u64> {
    filter_dump_inner(Path::new(src), Path::new(dst))
        .map_err(|e| PyIOError::new_err(e.to_string()))
}

#[pymodule]
fn _rs(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(filter_dump, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn run_filter(input: &[u8]) -> (Vec<u8>, u64) {
        static NEXT: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
        let id = NEXT.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let dir = std::env::temp_dir().join(format!(
            "pgdump-convert-test-{}-{}",
            std::process::id(),
            id
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let src = dir.join("in.sql");
        let dst = dir.join("out.sql");
        let mut f = File::create(&src).unwrap();
        f.write_all(input).unwrap();
        drop(f);
        let dropped = filter_dump_inner(&src, &dst).unwrap();
        let out = std::fs::read(&dst).unwrap();
        std::fs::remove_dir_all(&dir).ok();
        (out, dropped)
    }

    #[test]
    fn drops_restrict_and_timeout() {
        let (out, dropped) = run_filter(
            b"\\restrict ABC123\nSET transaction_timeout = 0;\nSELECT 1;\n\\unrestrict ABC123\n",
        );
        assert_eq!(out, b"SELECT 1;\n");
        assert_eq!(dropped, 0);
    }

    #[test]
    fn rewrites_pg18_not_null() {
        let (out, dropped) = run_filter(
            b"CREATE TABLE public.t (\n    id integer NOT NULL,\n    CONSTRAINT t_name_nn NOT NULL name,\n    val text\n)\nINHERITS (public.base);\n",
        );
        assert_eq!(dropped, 1);
        let text = String::from_utf8(out).unwrap();
        assert!(!text.contains("CONSTRAINT t_name_nn"));
        assert!(text.contains("id integer NOT NULL"));
        assert!(text.contains(
            "ALTER TABLE ONLY public.t ALTER COLUMN name SET NOT NULL;"
        ));
    }

    #[test]
    fn copy_payload_untouched() {
        let (out, _) = run_filter(
            b"COPY public.t (id) FROM stdin;\n1\nNOT NULL fake\n\\.\nSELECT 1;\n",
        );
        assert!(out.windows(8).any(|w| w == b"NOT NULL"));
        assert!(out.ends_with(b"SELECT 1;\n"));
    }

    #[test]
    fn single_line_create_table() {
        let (out, dropped) = run_filter(b"CREATE TABLE public.u (id int);\n");
        assert_eq!(dropped, 0);
        assert_eq!(out, b"CREATE TABLE public.u (id int);\n");
    }
}
