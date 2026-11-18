"""Tests for the ``pgdump-convert`` CLI and conversion pipeline."""

import json
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
        # Streaming call (psql restore log or pg_dump output).
        if "pg_dump" in args:
            kwargs["stdout"].write(
                b"-- fake pg_dump 16 output\n"
                b"\\restrict FAKEKEY123\n"
                b"CREATE TABLE t(id int);\n"
                b"\\unrestrict FAKEKEY123\n"
            )
        return _cp(args)
    if "pg_isready" in args:
        return _cp(args)
    if any("SHOW" in a for a in args):
        return _cp(args, stdout=b"16.14 (Debian 16.14-1.pgdg13+1)\n")
    return _cp(args)


def _make_backup(path):
    """Write a minimal Odoo-format zip backup at *path*."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "dump.sql",
            "-- dump header\n"
            "\\restrict ABC123\n"
            "CREATE TABLE res_partner(id int);\n"
            "\\unrestrict ABC123\n"
            "-- PostgreSQL database dump complete\n",
        )
        zf.writestr(
            "manifest.json",
            json.dumps(
                {
                    "odoo_dump": "1",
                    "db_name": "vinha",
                    "pg_version": "18.6",
                    "version": "19.0",
                }
            ),
        )
        zf.writestr("filestore/ab/cd", "attachment-bytes")


def _fake_docker_and_path(monkeypatch):
    monkeypatch.setattr(core, "_run", _fake_docker)
    monkeypatch.setattr(core.shutil, "which", lambda name: "/usr/bin/" + name)


def test_zip_backup_repackages(tmp_path, monkeypatch):
    """A PG18 Odoo backup zip converts for a PG16 target.

    The output zip keeps the filestore and manifest, its dump.sql is the
    re-dumped output without \\restrict lines, and the manifest's
    pg_version reflects the target server.
    """
    src = tmp_path / "backup.zip"
    out = tmp_path / "backup-pg16.zip"
    _make_backup(src)
    _fake_docker_and_path(monkeypatch)

    result = CliRunner().invoke(main, [str(src), str(out), "--to", "16"])

    assert result.exit_code == 0, result.output
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        assert "dump.sql" in names
        assert "manifest.json" in names
        assert "filestore/ab/cd" in names
        assert zf.read("filestore/ab/cd") == b"attachment-bytes"
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["pg_version"] == "16.14"
        assert manifest["version"] == "19.0"
        dump = zf.read("dump.sql")
    assert b"CREATE TABLE t(id int);" in dump
    assert b"\\restrict" not in dump
    assert b"\\unrestrict" not in dump


def test_plain_sql_file_accepted(tmp_path, monkeypatch):
    """A plain pg_dump -Fp .sql file converts like a zip's dump.sql.

    The output zip carries the re-dumped SQL and no manifest/filestore,
    since a bare SQL file has neither.
    """
    src = tmp_path / "dump.sql"
    src.write_text(
        "-- plain pg_dump\n"
        "\\restrict KEY1\n"
        "CREATE TABLE res_users(id int);\n"
        "\\unrestrict KEY1\n"
    )
    out = tmp_path / "dump-pg16.zip"
    _fake_docker_and_path(monkeypatch)

    result = CliRunner().invoke(main, [str(src), str(out), "--to", "16"])

    assert result.exit_code == 0, result.output
    with zipfile.ZipFile(out) as zf:
        assert "dump.sql" in zf.namelist()
        assert "manifest.json" not in zf.namelist()
        dump = zf.read("dump.sql")
    assert b"CREATE TABLE t(id int);" in dump
    assert b"\\restrict" not in dump


def test_custom_format_dump_rejected(tmp_path):
    """A pg_dump -Fc file fails with a clear error, not a psql failure."""
    src = tmp_path / "dump.pgdump"
    src.write_bytes(b"PGDMP" + b"\x00" * 100)

    result = CliRunner().invoke(main, [str(src), "--dry-run"])

    assert result.exit_code != 0
    assert "custom" in result.output.lower() or "plain" in result.output.lower()


def test_filestore_dir_bundled(tmp_path, monkeypatch):
    """--filestore DIR adds the directory to the output zip as filestore/."""
    src = tmp_path / "dump.sql"
    src.write_text("CREATE TABLE t(id int);\n")
    fs = tmp_path / "fs"
    (fs / "ab").mkdir(parents=True)
    (fs / "ab" / "cd").write_bytes(b"attachment-bytes")
    out = tmp_path / "out.zip"
    _fake_docker_and_path(monkeypatch)

    result = CliRunner().invoke(main, [str(src), str(out), "--filestore", str(fs)])

    assert result.exit_code == 0, result.output
    with zipfile.ZipFile(out) as zf:
        assert zf.read("filestore/ab/cd") == b"attachment-bytes"


def test_dry_run_needs_no_docker(tmp_path, monkeypatch):
    """--dry-run reports the plan without requiring docker."""
    src = tmp_path / "backup.zip"
    _make_backup(src)
    monkeypatch.setattr(core.shutil, "which", lambda name: None)

    result = CliRunner().invoke(main, [str(src), "--to", "16", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "postgres:16" in result.output


def test_no_input_errors(tmp_path, monkeypatch):
    """No INPUT and no --from-db is an error."""
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, [])

    assert result.exit_code != 0
    assert "Nothing to convert" in result.output


# --- database source --------------------------------------------------------


def _fake_db_backend(monkeypatch, modules=None):
    """Stub the local pg tools; fake docker still runs."""
    _fake_docker_and_path(monkeypatch)
    monkeypatch.setattr(core, "_db_exists", lambda db: True)

    def fake_dump(src_db, out_path):
        Path(out_path).write_bytes(
            b"-- pg_dump of project db\nCREATE TABLE ir_module_module(id int);\n"
        )

    monkeypatch.setattr(core, "_dump_db", fake_dump)
    rows = modules if modules is not None else ["base|19.0.1.0"]
    monkeypatch.setattr(
        core,
        "_pg_tool",
        lambda tool, args: (
            (0, "\n".join(rows).encode() + b"\n", b"")
            if "ir_module_module" in " ".join(args)
            else (0, b"", b"")
        ),
    )


def test_from_db_exports_database(tmp_path, monkeypatch):
    """--from-db dumps via local pg_dump and synthesizes a manifest with
    the installed module list."""
    _fake_db_backend(monkeypatch, modules=["base|19.0.1.0", "sale|1.0"])
    out = tmp_path / "export.zip"

    result = CliRunner().invoke(main, ["--from-db", "vinha", str(out), "--to", "16"])

    assert result.exit_code == 0, result.output
    with zipfile.ZipFile(out) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["odoo_dump"] == "1"
        assert manifest["db_name"] == "vinha"
        assert manifest["modules"] == {"base": "19.0.1.0", "sale": "1.0"}
        assert manifest["pg_version"] == "16.14"
        assert b"CREATE TABLE t(id int);" in zf.read("dump.sql")


def test_from_db_missing_database_errors(tmp_path, monkeypatch):
    """--from-db on a nonexistent database fails before docker runs."""
    _fake_docker_and_path(monkeypatch)
    monkeypatch.setattr(core, "_db_exists", lambda db: False)

    result = CliRunner().invoke(main, ["--from-db", "ghost", str(tmp_path / "o.zip")])

    assert result.exit_code != 0
    assert "ghost" in result.output


def test_from_db_rejects_two_output_paths(tmp_path):
    """With --from-db there is no input, so only one positional is allowed."""
    result = CliRunner().invoke(
        main, ["--from-db", "vinha", "a.zip", "b.zip", "--dry-run"]
    )

    assert result.exit_code != 0
    assert "output" in result.output.lower()


def test_from_db_dry_run_needs_no_tools(tmp_path, monkeypatch):
    """--from-db --dry-run prints the plan without docker or pg_dump."""
    monkeypatch.setattr(core.shutil, "which", lambda name: None)

    result = CliRunner().invoke(main, ["--from-db", "vinha", "--to", "16", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "vinha" in result.output
    assert "postgres:16" in result.output


def test_from_db_filestore_dir(tmp_path, monkeypatch):
    """--from-db --filestore DIR bundles the directory into the zip."""
    _fake_db_backend(monkeypatch)
    fs = tmp_path / "fs"
    (fs / "ab").mkdir(parents=True)
    (fs / "ab" / "cd").write_bytes(b"attachment-bytes")
    out = tmp_path / "export.zip"

    result = CliRunner().invoke(
        main,
        ["--from-db", "vinha", str(out), "--to", "16", "--filestore", str(fs)],
    )

    assert result.exit_code == 0, result.output
    with zipfile.ZipFile(out) as zf:
        assert zf.read("filestore/ab/cd") == b"attachment-bytes"


# --- PG18 syntax downgrade ---------------------------------------------------


def test_pg18_not_null_constraints_rewritten(tmp_path, monkeypatch):
    """A PG18 dump using named NOT NULL table constraints converts to PG16.

    PG18 dumps ``CONSTRAINT name NOT NULL col`` / bare ``NOT NULL col``
    as table elements; PG<=17 rejects that syntax, so the converter moves
    the constraint into an ALTER TABLE after the CREATE TABLE.
    """
    src = tmp_path / "backup.zip"
    out = tmp_path / "backup-pg16.zip"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr(
            "dump.sql",
            "CREATE TABLE public.ir_actions (\n"
            "    id integer NOT NULL,\n"
            "    name jsonb NOT NULL\n"
            ");\n"
            "CREATE TABLE public.ir_act_server (\n"
            "    CONSTRAINT ir_actions_name_not_null NOT NULL name,\n"
            "    code text\n"
            ")\n"
            "INHERITS (public.ir_actions);\n"
            "CREATE TABLE public.ir_act_window (\n"
            "    NOT NULL name,\n"
            "    view_id integer\n"
            ")\n"
            "INHERITS (public.ir_actions);\n"
            "COPY public.ir_act_server (id, name, code) FROM stdin;\n"
            "1\t{}\t-x-\n"
            "\\.\n",
        )
    staged = {}

    def fake(args, **kwargs):
        if args[0] == "cp":
            staged["dump"] = Path(args[1]).read_bytes()
        return _fake_docker(args, **kwargs)

    monkeypatch.setattr(core, "_run", fake)
    monkeypatch.setattr(core.shutil, "which", lambda name: "/usr/bin/" + name)

    result = CliRunner().invoke(main, [str(src), str(out), "--to", "16"])

    assert result.exit_code == 0, result.output
    dump_in = staged["dump"]
    assert b"CONSTRAINT ir_actions_name_not_null NOT NULL name" not in dump_in
    assert (
        b"ALTER TABLE ONLY public.ir_act_server ALTER COLUMN name SET NOT NULL;"
        in dump_in
    )
    assert (
        b"ALTER TABLE ONLY public.ir_act_window ALTER COLUMN name SET NOT NULL;"
        in dump_in
    )
    # COPY payload must pass through untouched
    assert b"1\t{}\t-x-" in dump_in
