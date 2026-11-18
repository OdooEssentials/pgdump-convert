"""Convert a PostgreSQL dump to a different server major version."""

import json
import os
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

import click

try:
    from pgdump_convert import _rs as _native
except ImportError:
    _native = None


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
    """Stream *src* to *dst* applying the Rust transform rules.

    The rules (``\\restrict`` drops, ``SET transaction_timeout`` drop,
    PG18 NOT NULL rewrite) live in ``native/src/lib.rs`` — the single
    source of truth — and run in ``pgdump_convert._rs``. COPY payloads
    pass through untouched.
    """
    if _native is None:
        raise click.ClickException(
            "pgdump-convert's Rust extension (pgdump_convert._rs) is "
            "missing; reinstall with a Rust toolchain or use a prebuilt wheel"
        )
    dropped = _native.filter_dump(str(src), str(dst))
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
