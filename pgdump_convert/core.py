"""Convert a PostgreSQL dump to a different server major version."""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import click

from .rules import BLOCK_RULES, LINE_RULES

COPY_RE = re.compile(rb"^COPY\s")


def _info(msg):
    click.echo(msg, err=True)


def _warning(msg):
    click.secho(f"warning: {msg}", fg="yellow", err=True)


def _success(msg):
    # stderr: stdout may carry the binary zip.
    click.secho(msg, fg="green", err=True)


def _run(args, *, capture=True, **kwargs):
    """Run ``docker <args>`` and return the CompletedProcess."""
    if capture:
        kwargs["capture_output"] = True
    return subprocess.run(["docker", *args], check=False, **kwargs)


def _docker_exec(container, args, **kwargs):
    return _run(["exec", container, *args], **kwargs)


def _filter_dump(src, dst):
    """Copy *src* to *dst* applying the registered transform rules.

    Streams line by line so multi-GB dumps don't exhaust memory. COPY
    payloads pass through untouched; line rules (``LINE_RULES``) and
    block rules (``BLOCK_RULES``) do the rewriting.
    """
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


def _restore(container, db_name, dump_file, log_path, allow_errors=False):
    cp = _run(["cp", str(dump_file), f"{container}:/tmp/dump.sql"])
    if cp.returncode != 0:
        raise click.ClickException(
            f"docker cp failed: {cp.stderr.decode(errors='replace')[:300]}"
        )
    with open(log_path, "wb") as log:
        result = _docker_exec(
            container,
            ["psql", "-U", "postgres", "-d", db_name, "-f", "/tmp/dump.sql"],
            capture=False,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    lines = log_path.read_text(errors="replace").splitlines()
    errors = [line for line in lines if "ERROR:" in line]
    _info(f"psql reported {len(errors)} ERROR line(s)")
    for line in errors[-5:]:
        _warning(f"  {line}")
    if result.returncode != 0:
        # Fatal psql errors (connection loss, missing file) exit nonzero
        # and are not covered by --allow-restore-errors.
        raise click.ClickException(
            f"psql restore failed with exit code {result.returncode}:\n"
            + "\n".join(lines[-5:])
        )
    if errors and not allow_errors:
        raise click.ClickException(
            f"restore reported {len(errors)} ERROR line(s); the dump may "
            "be incomplete. Re-run with --allow-restore-errors to continue "
            "anyway."
        )


def _docker_load(
    dump_file, work, image, db_name, container, allow_errors=False, keep=False
):
    """Restore *dump_file* into a throwaway *image* container.

    With ``keep=False`` this is a test: the container is removed
    afterwards. With ``keep=True`` it is left running for inspection.
    """
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
        _restore(container, db_name, dump_file, work / "psql.log", allow_errors)
    finally:
        if not keep:
            _run(["rm", "-f", container])
    if keep:
        _info(f"container '{container}' left running for inspection")
        _info(f"  connect: docker exec -it {container} psql -U postgres -d {db_name}")
        _info(f"  remove:  docker rm -f {container}")


def _check_sql(path, desc):
    """Reject non-SQL input: zip archives and custom-format dumps."""
    with open(path, "rb") as f:
        magic = f.read(5)
    if magic[:2] == b"PK":
        raise click.ClickException(
            f"{desc} is a zip archive; pgdump-convert expects plain SQL "
            "(pg_dump -Fp)"
        )
    if magic == b"PGDMP":
        raise click.ClickException(
            f"{desc} is a custom-format pg_dump; pgdump-convert needs "
            "plain SQL (pg_dump -Fp)"
        )
    if path.stat().st_size == 0:
        raise click.ClickException(f"{desc} is empty")


def _emit_sql(src, output):
    """Write the filtered SQL to *output* (``"-"`` = stdout)."""
    if str(output) == "-":
        with open(src, "rb") as f:
            shutil.copyfileobj(f, sys.stdout.buffer)
        _success("wrote sql to stdout")
    else:
        os.replace(src, output)
        _success(f"wrote {output}")


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


def convert(
    sql_path,
    output,
    image="postgres:16",
    db_name="odoo",
    docker=None,
    dry_run=False,
    allow_restore_errors=False,
):
    """Convert *sql_path* — a plain SQL dump — for another PG version.

    ``sql_path == "-"`` reads from stdin; ``output == "-"`` writes to
    stdout. The registered transform rules rewrite the SQL; ``docker``
    optionally loads the result into a throwaway container running
    *image*'s PostgreSQL (``"test"`` verifies and removes it, ``"import"``
    keeps it running for inspection).
    """
    stdin_mode = str(sql_path) == "-"
    stdout_mode = str(output) == "-"
    src_desc = "stdin" if stdin_mode else str(sql_path)
    dst_desc = "stdout" if stdout_mode else str(output)
    if dry_run:
        _info(f"Would filter {src_desc} and write {dst_desc}")
        if docker:
            _info(f"Would load into a throwaway {image} container ({docker})")
        return
    if docker and shutil.which("docker") is None:
        raise click.ClickException(f"docker not found on PATH (--docker-{docker})")
    # Work next to a real output dir (or cwd for stdout): /tmp is often a
    # small tmpfs and dumps are huge.
    anchor = Path.cwd() if stdout_mode else Path(output).parent
    if not stdout_mode:
        anchor.mkdir(parents=True, exist_ok=True)
    container = f"pgdump-convert-{os.getpid()}"
    with tempfile.TemporaryDirectory(dir=anchor) as tmp:
        work = Path(tmp)
        if stdin_mode:
            _info("reading sql from stdin")
            src = work / "input.sql"
            with open(src, "wb") as f:
                shutil.copyfileobj(sys.stdin.buffer, f)
        else:
            src = Path(sql_path)
        _check_sql(src, src_desc)
        filtered = work / "filtered.sql"
        _filter_dump(src, filtered)
        if docker:
            _docker_load(
                filtered,
                work,
                image,
                db_name,
                container,
                allow_restore_errors,
                keep=docker == "import",
            )
        _emit_sql(filtered, output)


def convert_db(
    src_db,
    output,
    image="postgres:16",
    db_name="odoo",
    docker=None,
    dry_run=False,
    allow_restore_errors=False,
):
    """Dump database *src_db* via local ``pg_dump`` and convert it.

    Connection settings come from the environment (libpq). ``docker``
    optionally loads the result into a throwaway ``postgres`` container
    (``"test"`` verifies and removes it, ``"import"`` keeps it running).
    """
    stdout_mode = str(output) == "-"
    if dry_run:
        dst = "stdout" if stdout_mode else str(output)
        _info(f"Would pg_dump '{src_db}' and filter to {dst}")
        if docker:
            _info(f"Would load into a throwaway {image} container ({docker})")
        return
    if docker and shutil.which("docker") is None:
        raise click.ClickException(f"docker not found on PATH (--docker-{docker})")
    if shutil.which("pg_dump") is None:
        raise click.ClickException("pg_dump not found on PATH")
    if shutil.which("psql") is None:
        raise click.ClickException("psql not found on PATH")
    if not _db_exists(src_db):
        raise click.ClickException(f"database '{src_db}' does not exist")
    anchor = Path.cwd() if stdout_mode else Path(output).parent
    if not stdout_mode:
        anchor.mkdir(parents=True, exist_ok=True)
    container = f"pgdump-convert-{os.getpid()}"
    with tempfile.TemporaryDirectory(dir=anchor) as tmp:
        work = Path(tmp)
        _info(f"dumping '{src_db}'")
        _dump_db(src_db, work / "dump.sql")
        filtered = work / "filtered.sql"
        _filter_dump(work / "dump.sql", filtered)
        if docker:
            _docker_load(
                filtered,
                work,
                image,
                db_name,
                container,
                allow_restore_errors,
                keep=docker == "import",
            )
        _emit_sql(filtered, output)


__all__ = ["convert", "convert_db"]
