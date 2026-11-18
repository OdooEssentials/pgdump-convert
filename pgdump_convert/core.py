"""Convert a PostgreSQL dump to a different server major version."""

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

import click

try:
    from pgdump_convert import _rs as _native
except ImportError:  # pragma: no cover - native ext is optional
    _native = None

COPY_RE = re.compile(rb"^COPY\s")


# --- dump transform rules ----------------------------------------------------
#
# ``_filter_dump`` streams the dump through two kinds of rules so new
# PostgreSQL-version quirks are a few lines to add:
#
# - LINE_RULES: per-line filters. Add ``LineRule(name, pattern,
#   replacement)``; ``replacement=None`` drops the line, otherwise the
#   match is expanded via ``re.Match.expand`` (``\\g<name>`` groups work).
# - BLOCK_RULES: multi-line statement transforms. Subclass ``BlockRule``,
#   implement ``starts``/``feed``/``emit``, and register the class in
#   ``BLOCK_RULES``.


@dataclass(frozen=True)
class LineRule:
    """Drop or rewrite lines matching ``pattern``.

    ``replacement=None`` drops the line; otherwise the match is expanded
    with ``re.Match.expand`` so ``\\g<name>``/``\\1`` groups are allowed.
    """

    name: str
    pattern: re.Pattern
    replacement: bytes | None = None

    def apply(self, line):
        m = self.pattern.match(line)
        if m is None:
            return line
        return None if self.replacement is None else m.expand(self.replacement)


RESTRICT_RE = re.compile(rb"^\\(?:un)?restrict [A-Za-z0-9]+\s*$")
SET_TIMEOUT_RE = re.compile(rb"^SET\s+transaction_timeout\s*=")

LINE_RULES = [
    # psql <17.6 cannot parse these meta-commands emitted by pg_dump 17.6+.
    LineRule("restrict-unrestrict", RESTRICT_RE),
    # transaction_timeout was added in PG17; older servers reject the SET.
    LineRule("transaction-timeout", SET_TIMEOUT_RE),
]


class BlockRule:
    """Buffer one multi-line SQL statement and emit its replacement.

    ``starts(line)`` returns True on the statement's first line and
    initializes state; ``feed(line)`` buffers each line (including the
    first) and returns True once the statement is complete; ``emit()``
    returns the replacement bytes. ``dropped`` counts rewritten elements
    for the end-of-run warning.
    """

    dropped = 0

    def starts(self, line):
        raise NotImplementedError

    def feed(self, line):
        raise NotImplementedError

    def emit(self):
        raise NotImplementedError


# PG18 emits NOT NULL as named table constraints; PG<=17 rejects them.
PG18_NOT_NULL_RE = re.compile(
    rb'^\s*(?:CONSTRAINT\s+\w+\s+)?NOT\s+NULL\s+("[^"]+"|\w+),?\s*$'
)
CREATE_TABLE_RE = re.compile(
    rb"^CREATE\s+(?:UNLOGGED\s+|TEMP(?:ORARY)?\s+)?TABLE\s+(?:IF NOT EXISTS\s+)?"
    rb'((?:"[^"]+"|[\w]+)(?:\.(?:"[^"]+"|[\w]+))?)\s*\('
)


class Pg18NotNull(BlockRule):
    """Rewrite PG18 ``[CONSTRAINT name] NOT NULL col`` table elements.

    They are moved into ``ALTER TABLE ONLY <table> ALTER COLUMN <col>
    SET NOT NULL;`` statements after the CREATE TABLE.
    """

    def __init__(self):
        self._buf = []
        self._table = None
        self._cols = []
        self.dropped = 0

    def starts(self, line):
        m = CREATE_TABLE_RE.match(line)
        if m is None:
            return False
        self._table = m.group(1)
        self._buf = []
        self._cols = []
        return True

    def feed(self, line):
        self._buf.append(line)
        return line.rstrip().endswith(b";")

    def emit(self):
        out = bytearray()
        for buf_line in self._buf:
            m = PG18_NOT_NULL_RE.match(buf_line)
            if m:
                self._cols.append(m.group(1))
                self.dropped += 1
                continue
            out += buf_line
        for col in self._cols:
            out += (
                b"ALTER TABLE ONLY "
                + self._table
                + b" ALTER COLUMN "
                + col
                + b" SET NOT NULL;\n"
            )
        self._buf = []
        self._cols = []
        return bytes(out)


BLOCK_RULES = [Pg18NotNull]


def _info(msg):
    click.echo(msg, err=True)


def _warning(msg):
    click.secho(f"warning: {msg}", fg="yellow", err=True)


def _success(msg):
    click.secho(msg, fg="green")


def _run(args, *, capture=True, **kwargs):
    """Run ``docker <args>`` and return the CompletedProcess."""
    if capture:
        kwargs["capture_output"] = True
    return subprocess.run(["docker", *args], check=False, **kwargs)


def _docker_exec(container, args, **kwargs):
    return _run(["exec", container, *args], **kwargs)


def _extract(zip_path, work):
    """Extract dump.sql, manifest.json and filestore/ from the backup zip."""
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            if (
                name == "dump.sql"
                or name == "manifest.json"
                or name.startswith("filestore/")
            ):
                zf.extract(name, work)
    if not (work / "dump.sql").exists():
        raise click.ClickException(f"{zip_path} does not contain dump.sql")


def _import_sql(path, work):
    """Stage a plain-SQL dump file as ``work/dump.sql``.

    Custom-format dumps (``PGDMP`` magic) are rejected: they cannot be
    fed to ``psql`` and newer ``pg_restore`` refuses older-format files
    anyway, so they are outside what this command can convert.
    """
    with open(path, "rb") as f:
        magic = f.read(5)
    if magic == b"PGDMP":
        raise click.ClickException(
            f"{path} is a custom-format pg_dump; dump-convert needs "
            "plain SQL (pg_dump -Fp) or an Odoo zip backup"
        )
    shutil.copyfile(path, work / "dump.sql")


def _filter_dump(src, dst):
    """Copy *src* to *dst* applying the registered transform rules.

    Uses the Rust extension (``pgdump_convert._rs``) when available and
    falls back to the pure-Python pipeline otherwise. Both stream line by
    line so multi-GB dumps don't exhaust memory; COPY payloads pass
    through untouched.
    """
    if _native is not None:
        dropped = _native.filter_dump(str(src), str(dst))
        if dropped:
            _warning(
                f"rewrote {dropped} table-level NOT NULL constraint(s) "
                "as ALTER TABLE ... SET NOT NULL"
            )
        return
    _filter_dump_python(src, dst)


def _filter_dump_python(src, dst):
    """Pure-Python fallback for ``_filter_dump`` (see ``LINE_RULES`` and
    ``BLOCK_RULES`` above)."""
    in_copy = False
    block = None
    blocks = [cls() for cls in BLOCK_RULES]
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        for line in fin:
            if in_copy:
                fout.write(line)
                if line.rstrip(b"\r\n") == b"\\.":
                    in_copy = False
                continue
            if block is not None:
                if block.feed(line):
                    fout.write(block.emit())
                    block = None
                continue
            for rule in LINE_RULES:
                line = rule.apply(line)
                if line is None:
                    break
            if line is None:
                continue
            if COPY_RE.match(line):
                in_copy = True
            for rule in blocks:
                if rule.starts(line):
                    block = rule
                    if rule.feed(line):
                        fout.write(rule.emit())
                        block = None
                    break
            else:
                fout.write(line)
    dropped = sum(r.dropped for r in blocks)
    if dropped:
        _warning(
            f"rewrote {dropped} table-level NOT NULL constraint(s) "
            "as ALTER TABLE ... SET NOT NULL"
        )


def _wait_ready(container):
    for _ in range(60):
        result = _docker_exec(container, ["pg_isready", "-U", "postgres"])
        if result.returncode == 0:
            return
        time.sleep(1)
    raise click.ClickException(f"container {container} did not become ready")


def _restore(container, db_name, dump_file, log_path):
    _run(["cp", str(dump_file), f"{container}:/tmp/dump.sql"])
    with open(log_path, "wb") as log:
        _docker_exec(
            container,
            ["psql", "-U", "postgres", "-d", db_name, "-f", "/tmp/dump.sql"],
            capture=False,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    errors = [
        line
        for line in log_path.read_text(errors="replace").splitlines()
        if "ERROR:" in line
    ]
    _info(f"psql reported {len(errors)} ERROR line(s)")
    for line in errors[-5:]:
        _warning(f"  {line}")


def _redump(container, db_name, raw_path, out_path):
    """pg_dump from the container; return server_version.

    Raw output goes to *raw_path*, then is streamed through
    ``_filter_dump`` into *out_path*.
    """
    with open(raw_path, "wb") as f:
        result = _docker_exec(
            container,
            ["pg_dump", "-Fp", "--no-owner", "-U", "postgres", db_name],
            capture=False,
            stdout=f,
            stderr=subprocess.PIPE,
        )
    if result.returncode != 0:
        raise click.ClickException(
            f"pg_dump failed: {result.stderr.decode(errors='replace')[:500]}"
        )
    # The container's pg_dump may itself emit \restrict (>=16.15); strip it.
    _filter_dump(raw_path, out_path)
    raw_path.unlink()
    result = _docker_exec(
        container,
        ["psql", "-U", "postgres", "-tA", "-c", "SHOW server_version"],
    )
    return result.stdout.decode().strip().split()[0]


def _write_zip(work, output, pg_version):
    """Repack dump.sql + filestore/ + manifest.json into the output zip."""
    manifest_path = work / "manifest.json"
    manifest = {}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
    if manifest:
        manifest["pg_version"] = pg_version
        manifest_path.write_text(json.dumps(manifest, indent=4))
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        if manifest:
            zf.write(manifest_path, "manifest.json")
        filestore = work / "filestore"
        if filestore.exists():
            for path in sorted(filestore.rglob("*")):
                if path.is_file():
                    zf.write(
                        path,
                        "filestore/" + path.relative_to(filestore).as_posix(),
                    )
        zf.write(work / "dump-new.sql", "dump.sql")
    _success(f"wrote {output}")


def _convert_work(work, output, image, db_name, container):
    """Run the throwaway-container conversion on ``work/dump.sql``.

    ``work`` may also hold ``manifest.json`` and ``filestore/``; they are
    preserved into the output zip by ``_write_zip``.
    """
    dump_in = work / "dump-in.sql"
    _filter_dump(work / "dump.sql", dump_in)
    (work / "dump.sql").unlink()

    _info(f"starting throwaway {image} container")
    result = _run(
        [
            "run",
            "-d",
            "--name",
            container,
            "-e",
            "POSTGRES_HOST_AUTH_METHOD=trust",
            image,
        ]
    )
    if result.returncode != 0:
        raise click.ClickException(
            f"docker run failed: {result.stderr.decode(errors='replace')[:300]}"
        )
    try:
        _wait_ready(container)
        _docker_exec(container, ["createdb", "-U", "postgres", db_name])
        _info("restoring dump")
        _restore(container, db_name, dump_in, work / "psql.log")
        _info(f"re-dumping with pg_dump from {image}")
        server_version = _redump(
            container, db_name, work / "dump-raw.sql", work / "dump-new.sql"
        )
    finally:
        _run(["rm", "-f", container])
    _info(f"server_version: {server_version}")
    _write_zip(work, output, server_version)


def convert(
    zip_path,
    output,
    image="postgres:16",
    db_name="odoo",
    filestore_dir=None,
    dry_run=False,
):
    """Convert *zip_path* (Odoo zip backup or plain SQL dump).

    The input is staged as ``dump.sql`` — the plain-SQL format Odoo zip
    backups use — then restored into a throwaway container running
    *image*'s PostgreSQL and re-dumped from that server. *filestore_dir*,
    when given, is bundled into the output zip as ``filestore/``.
    """
    zip_path = Path(zip_path)
    output = Path(output)
    is_zip = zipfile.is_zipfile(zip_path)
    if not is_zip:
        with open(zip_path, "rb") as f:
            is_custom = f.read(5) == b"PGDMP"
    else:
        is_custom = False
    if is_custom:
        raise click.ClickException(
            f"{zip_path} is a custom-format pg_dump; dump-convert needs "
            "plain SQL (pg_dump -Fp) or an Odoo zip backup"
        )
    if dry_run:
        _info(
            f"Would restore {zip_path} into a throwaway {image} container "
            f"and write {output}"
        )
        if filestore_dir:
            _info(f"Would include filestore from {filestore_dir}")
        return
    if shutil.which("docker") is None:
        raise click.ClickException("docker not found on PATH")
    output.parent.mkdir(parents=True, exist_ok=True)
    container = f"pgdump-convert-{os.getpid()}"
    # Work next to the output: /tmp is often a small tmpfs and dumps are huge.
    with tempfile.TemporaryDirectory(dir=output.parent) as tmp:
        work = Path(tmp)
        if is_zip:
            _extract(zip_path, work)
        else:
            _import_sql(zip_path, work)
        if filestore_dir:
            stage_filestore(filestore_dir, work)
        _convert_work(work, output, image, db_name, container)


def _pg_tool(tool, args):
    """Run a local PostgreSQL client tool; return (rc, stdout, stderr)."""
    try:
        result = subprocess.run([tool, *args], capture_output=True, check=False)
    except FileNotFoundError:
        return None, b"", f"{tool} not found on PATH".encode()
    return result.returncode, result.stdout, result.stderr


def _db_exists(db_name):
    """Return True if the local PostgreSQL database *db_name* exists."""
    rc, _, _ = _pg_tool("psql", ["-d", db_name, "-tA", "-c", "SELECT 1"])
    return rc == 0


def _dump_db(src_db, out_path):
    """pg_dump *src_db* as plain SQL into *out_path* via the local tools.

    Connection settings come from the environment (``PGHOST``, ``PGUSER``,
    ``PGPASSWORD``, ``~/.pgpass``, ...), like any libpq client.
    """
    with open(out_path, "wb") as f:
        try:
            result = subprocess.run(
                ["pg_dump", "-Fp", "--no-owner", "-d", src_db],
                stdout=f,
                stderr=subprocess.PIPE,
                check=False,
            )
        except FileNotFoundError:
            raise click.ClickException(
                "Could not locate `pg_dump`. Is PostgreSQL installed?"
            )
    if result.returncode != 0:
        raise click.ClickException(
            f"pg_dump failed: {result.stderr.decode(errors='replace')[:500]}"
        )


def _write_source_manifest(src_db, work):
    """Synthesize an Odoo-style manifest.json for a live database export.

    ``modules`` comes from ``ir_module_module``; version fields that only
    the Odoo server knows are left out rather than invented.
    """
    manifest = {"odoo_dump": "1", "db_name": src_db, "modules": {}}
    rc, stdout, _ = _pg_tool(
        "psql",
        [
            "-d",
            src_db,
            "-At",
            "-c",
            "SELECT name, latest_version FROM ir_module_module "
            "WHERE state = 'installed'",
        ],
    )
    if rc == 0:
        modules = {}
        for line in stdout.decode(errors="replace").splitlines():
            parts = line.split("|")
            if len(parts) >= 2:
                modules[parts[0]] = parts[1]
        manifest["modules"] = modules
    (work / "manifest.json").write_text(json.dumps(manifest, indent=4))


def convert_db(
    src_db,
    output,
    image="postgres:16",
    db_name="odoo",
    filestore_dir=None,
    dry_run=False,
):
    """Export database *src_db* and convert it.

    The dump is produced by the local ``pg_dump`` (libpq env applies) and
    goes through the same throwaway-container conversion as file input.
    """
    output = Path(output)
    if dry_run:
        _info(
            f"Would pg_dump '{src_db}' and write {output} via a throwaway "
            f"{image} container"
        )
        if filestore_dir:
            _info(f"Would include filestore from {filestore_dir}")
        return
    if shutil.which("docker") is None:
        raise click.ClickException("docker not found on PATH")
    if shutil.which("pg_dump") is None:
        raise click.ClickException("pg_dump not found on PATH")
    if not _db_exists(src_db):
        raise click.ClickException(f"database '{src_db}' does not exist")
    output.parent.mkdir(parents=True, exist_ok=True)
    container = f"pgdump-convert-{os.getpid()}"
    with tempfile.TemporaryDirectory(dir=output.parent) as tmp:
        work = Path(tmp)
        _info(f"dumping '{src_db}'")
        _dump_db(src_db, work / "dump.sql")
        if filestore_dir:
            stage_filestore(filestore_dir, work)
        _write_source_manifest(src_db, work)
        _convert_work(work, output, image, db_name, container)


def stage_filestore(filestore_dir, work):
    """Copy *filestore_dir* into ``work/filestore``."""
    src = Path(filestore_dir)
    if not src.is_dir():
        raise click.ClickException(f"{filestore_dir} is not a directory")
    shutil.copytree(src, work / "filestore")


__all__ = ["convert", "convert_db", "stage_filestore"]
