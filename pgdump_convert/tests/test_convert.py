"""Tests for the ``pgdump-convert`` CLI and SQL transform pipeline."""

import subprocess
import zipfile
from pathlib import Path

from click.testing import CliRunner

import pgdump_convert.core as core
from pgdump_convert.cli import main


def _cp(args, rc=0, stdout=b"", stderr=b""):
    return subprocess.CompletedProcess(args, rc, stdout=stdout, stderr=stderr)


def _fake_docker(args, **kwargs):
    """Stand-in for ``docker``: accepts everything, returns canned output."""
    if kwargs.get("stdout") is not None and hasattr(kwargs["stdout"], "write"):
        # Streaming call (psql restore log).
        return _cp(args)
    if "pg_isready" in args:
        return _cp(args)
    return _cp(args)


def _make_sql(path, body=None):
    """Write a minimal plain-SQL dump at *path*."""
    Path(path).write_bytes(
        body
        or (
            b"-- dump header\n"
            b"\\restrict ABC123\n"
            b"CREATE TABLE res_partner(id int);\n"
            b"\\unrestrict ABC123\n"
            b"-- PostgreSQL database dump complete\n"
        )
    )


def _fake_backend(monkeypatch):
    monkeypatch.setattr(core, "_run", _fake_docker)
    monkeypatch.setattr(core.shutil, "which", lambda name: "/usr/bin/" + name)


# --- file and stream conversion ---------------------------------------------


def test_sql_file_to_file(tmp_path, monkeypatch):
    """A plain SQL dump is filtered for the target version.

    \\restrict meta-commands are stripped; no docker is needed.
    """
    src = tmp_path / "dump.sql"
    out = tmp_path / "dump-pg16.sql"
    _make_sql(src)
    monkeypatch.setattr(core.shutil, "which", lambda name: None)

    result = CliRunner().invoke(main, [str(src), str(out), "--to", "16"])

    assert result.exit_code == 0, result.output
    dump = out.read_bytes()
    assert b"CREATE TABLE res_partner" in dump
    assert b"\\restrict" not in dump
    assert b"\\unrestrict" not in dump


def test_stdin_to_stdout_roundtrip(tmp_path, monkeypatch):
    """With no positional args the SQL is read from stdin and written
    to stdout."""
    src = tmp_path / "dump.sql"
    _make_sql(src)
    monkeypatch.setattr(core.shutil, "which", lambda name: None)

    result = CliRunner().invoke(main, ["--to", "16"], input=src.read_bytes())

    assert result.exit_code == 0, result.output
    dump = result.stdout_bytes
    assert b"CREATE TABLE res_partner" in dump
    assert b"\\restrict" not in dump


def test_explicit_dash_stdin_stdout(tmp_path, monkeypatch):
    """'-' selects stdin/stdout explicitly, same as omitting them."""
    src = tmp_path / "dump.sql"
    _make_sql(src)
    monkeypatch.setattr(core.shutil, "which", lambda name: None)

    result = CliRunner().invoke(main, ["-", "-", "--to", "16"], input=src.read_bytes())

    assert result.exit_code == 0, result.output
    assert b"CREATE TABLE res_partner" in result.stdout_bytes


def test_empty_stdin_errors(tmp_path, monkeypatch):
    """An empty stdin stream fails instead of producing empty output."""
    monkeypatch.setattr(core.shutil, "which", lambda name: None)

    result = CliRunner().invoke(main, ["--to", "16"], input=b"")

    assert result.exit_code != 0
    assert "empty" in result.output


def test_zip_input_rejected(tmp_path):
    """Zip archives are not SQL — rejected with a clear error."""
    src = tmp_path / "backup.zip"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("dump.sql", "CREATE TABLE t(id int);\n")

    result = CliRunner().invoke(main, [str(src), "--to", "16"])

    assert result.exit_code != 0
    assert "zip" in result.output.lower() or "sql" in result.output.lower()


def test_custom_format_dump_rejected(tmp_path):
    """A pg_dump -Fc file fails with a clear error, not a psql failure."""
    src = tmp_path / "dump.pgdump"
    src.write_bytes(b"PGDMP" + b"\x00" * 100)

    result = CliRunner().invoke(main, [str(src)])

    assert result.exit_code != 0
    assert "custom" in result.output.lower() or "plain" in result.output.lower()


def test_dry_run_needs_no_tools(tmp_path, monkeypatch):
    """--dry-run reports the plan without docker or pg tools."""
    src = tmp_path / "dump.sql"
    _make_sql(src)
    monkeypatch.setattr(core.shutil, "which", lambda name: None)

    result = CliRunner().invoke(main, [str(src), "--to", "16", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "filter" in result.output


# --- docker helpers ----------------------------------------------------------


def _fake_docker_restore_errors(args, **kwargs):
    """Fake docker whose psql restore log contains SQL ERROR lines."""
    if kwargs.get("stdout") is not None and hasattr(kwargs["stdout"], "write"):
        if "psql" in args and "-f" in args:
            kwargs["stdout"].write(b'ERROR:  relation "missing_table" does not exist\n')
        return _cp(args)
    return _fake_docker(args, **kwargs)


def test_docker_test_restores_and_cleans_up(tmp_path, monkeypatch):
    """--docker-test loads the filtered SQL into postgres:<to>, checks
    it restores, and removes the container."""
    src = tmp_path / "dump.sql"
    out = tmp_path / "dump-pg16.sql"
    _make_sql(src)
    calls = []

    def recording_fake(args, **kwargs):
        calls.append(args)
        return _fake_docker(args, **kwargs)

    monkeypatch.setattr(core, "_run", recording_fake)
    monkeypatch.setattr(core.shutil, "which", lambda name: "/usr/bin/" + name)

    result = CliRunner().invoke(
        main, [str(src), str(out), "--to", "16", "--docker-test"]
    )

    assert result.exit_code == 0, result.output
    assert out.exists()
    assert any(c[:1] == ["run"] for c in calls)
    assert any(c[:2] == ["rm", "-f"] for c in calls)


def test_docker_import_keeps_container(tmp_path, monkeypatch):
    """--docker-import restores the dump and leaves the container
    running for inspection."""
    src = tmp_path / "dump.sql"
    out = tmp_path / "dump-pg16.sql"
    _make_sql(src)
    calls = []

    def recording_fake(args, **kwargs):
        calls.append(args)
        return _fake_docker(args, **kwargs)

    monkeypatch.setattr(core, "_run", recording_fake)
    monkeypatch.setattr(core.shutil, "which", lambda name: "/usr/bin/" + name)

    result = CliRunner().invoke(
        main, [str(src), str(out), "--to", "16", "--docker-import"]
    )

    assert result.exit_code == 0, result.output
    assert out.exists()
    assert "left running" in result.output
    assert not any(c[:2] == ["rm", "-f"] for c in calls)


def test_docker_test_and_import_are_exclusive(tmp_path):
    """--docker-test and --docker-import cannot be combined."""
    src = tmp_path / "dump.sql"
    _make_sql(src)

    result = CliRunner().invoke(
        main,
        [str(src), str(tmp_path / "o.sql"), "--docker-test", "--docker-import"],
    )

    assert result.exit_code != 0


def test_restore_errors_fail_fast(tmp_path, monkeypatch):
    """SQL errors during the container restore abort before output."""
    src = tmp_path / "dump.sql"
    out = tmp_path / "dump-pg16.sql"
    _make_sql(src)
    monkeypatch.setattr(core, "_run", _fake_docker_restore_errors)
    monkeypatch.setattr(core.shutil, "which", lambda name: "/usr/bin/" + name)

    result = CliRunner().invoke(
        main, [str(src), str(out), "--to", "16", "--docker-test"]
    )

    assert result.exit_code != 0
    assert "ERROR" in result.output
    assert not out.exists()


def test_allow_restore_errors_continues(tmp_path, monkeypatch):
    """--allow-restore-errors keeps going despite restore ERROR lines."""
    src = tmp_path / "dump.sql"
    out = tmp_path / "dump-pg16.sql"
    _make_sql(src)
    monkeypatch.setattr(core, "_run", _fake_docker_restore_errors)
    monkeypatch.setattr(core.shutil, "which", lambda name: "/usr/bin/" + name)

    result = CliRunner().invoke(
        main,
        [str(src), str(out), "--to", "16", "--docker-test", "--allow-restore-errors"],
    )

    assert result.exit_code == 0, result.output
    assert out.exists()


def _fake_docker_fatal_restore(args, **kwargs):
    """Fake docker whose psql restore dies with a nonzero exit code.

    Fatal psql errors print lowercase ``psql: error:`` — they never
    produce ``ERROR:`` lines, so only the exit code can catch them.
    """
    if kwargs.get("stdout") is not None and hasattr(kwargs["stdout"], "write"):
        if "psql" in args and "-f" in args:
            kwargs["stdout"].write(
                b"psql: error: connection to server on socket failed\n"
            )
            return _cp(args, rc=2)
        return _cp(args)
    return _fake_docker(args, **kwargs)


def test_fatal_restore_error_fails_even_with_flag(tmp_path, monkeypatch):
    """A nonzero psql exit (dead container, lost connection) aborts the
    conversion — --allow-restore-errors only covers per-statement
    ERROR lines, not a failed restore."""
    src = tmp_path / "dump.sql"
    out = tmp_path / "dump-pg16.sql"
    _make_sql(src)
    monkeypatch.setattr(core, "_run", _fake_docker_fatal_restore)
    monkeypatch.setattr(core.shutil, "which", lambda name: "/usr/bin/" + name)

    result = CliRunner().invoke(
        main,
        [str(src), str(out), "--to", "16", "--docker-test", "--allow-restore-errors"],
    )

    assert result.exit_code != 0
    assert "exit code" in result.output
    assert not out.exists()


# --- database source ---------------------------------------------------------


def _fake_db_backend(monkeypatch):
    """Stub the local pg tools; fake docker still runs."""
    _fake_backend(monkeypatch)
    monkeypatch.setattr(core, "_db_exists", lambda db: True)

    def fake_dump(src_db, out_path):
        Path(out_path).write_bytes(
            b"-- pg_dump of database\n"
            b"\\restrict KEY1\n"
            b"CREATE TABLE ir_module_module(id int);\n"
            b"\\unrestrict KEY1\n"
        )

    monkeypatch.setattr(core, "_dump_db", fake_dump)


def test_from_db_exports_database(tmp_path, monkeypatch):
    """--from-db dumps via local pg_dump and filters the result."""
    _fake_db_backend(monkeypatch)
    out = tmp_path / "export.sql"

    result = CliRunner().invoke(main, ["--from-db", "vinha", str(out), "--to", "16"])

    assert result.exit_code == 0, result.output
    dump = out.read_bytes()
    assert b"CREATE TABLE ir_module_module" in dump
    assert b"\\restrict" not in dump


def test_from_db_missing_database_errors(tmp_path, monkeypatch):
    """--from-db on a nonexistent database fails before conversion."""
    _fake_backend(monkeypatch)
    monkeypatch.setattr(core, "_db_exists", lambda db: False)

    result = CliRunner().invoke(main, ["--from-db", "ghost", str(tmp_path / "o.sql")])

    assert result.exit_code != 0
    assert "ghost" in result.output


def test_from_db_rejects_two_output_paths(tmp_path):
    """With --from-db there is no input, so only one positional is allowed."""
    result = CliRunner().invoke(
        main, ["--from-db", "vinha", "a.sql", "b.sql", "--dry-run"]
    )

    assert result.exit_code != 0
    assert "output" in result.output.lower()


def test_from_db_dry_run_needs_no_tools(tmp_path, monkeypatch):
    """--from-db --dry-run prints the plan without docker or pg_dump."""
    monkeypatch.setattr(core.shutil, "which", lambda name: None)

    result = CliRunner().invoke(main, ["--from-db", "vinha", "--to", "16", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "vinha" in result.output
    assert "filter" in result.output


# --- PG18 syntax downgrade ----------------------------------------------------


def test_pg18_not_null_constraints_rewritten(tmp_path, monkeypatch):
    """A PG18 dump using named NOT NULL table constraints is rewritten
    for PG<=17: they become ALTER TABLE ... SET NOT NULL statements.

    COPY payloads must pass through untouched.
    """
    src = tmp_path / "dump.sql"
    out = tmp_path / "dump-pg16.sql"
    src.write_bytes(
        b"CREATE TABLE public.ir_actions (\n"
        b"    id integer NOT NULL,\n"
        b"    name jsonb NOT NULL\n"
        b");\n"
        b"CREATE TABLE public.ir_act_server (\n"
        b"    CONSTRAINT ir_actions_name_not_null NOT NULL name,\n"
        b"    code text\n"
        b")\n"
        b"INHERITS (public.ir_actions);\n"
        b"CREATE TABLE public.ir_act_window (\n"
        b"    NOT NULL name,\n"
        b"    view_id integer\n"
        b")\n"
        b"INHERITS (public.ir_actions);\n"
        b"COPY public.ir_act_server (id, name, code) FROM stdin;\n"
        b"1\t{}\t-x-\n"
        b"\\.\n"
    )
    monkeypatch.setattr(core.shutil, "which", lambda name: None)

    result = CliRunner().invoke(main, [str(src), str(out), "--to", "16"])

    assert result.exit_code == 0, result.output
    dump = out.read_bytes()
    assert b"CONSTRAINT ir_actions_name_not_null NOT NULL name" not in dump
    assert (
        b"ALTER TABLE ONLY public.ir_act_server ALTER COLUMN name SET NOT NULL;" in dump
    )
    assert (
        b"ALTER TABLE ONLY public.ir_act_window ALTER COLUMN name SET NOT NULL;" in dump
    )
    # COPY payload must pass through untouched
    assert b"1\t{}\t-x-" in dump
