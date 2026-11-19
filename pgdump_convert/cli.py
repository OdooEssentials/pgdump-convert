"""``pgdump-convert`` command line interface."""

import sys
from pathlib import Path

import click

from . import core


@click.command()
@click.argument("input", required=False, type=click.Path(dir_okay=False))
@click.argument("output", required=False, type=click.Path(dir_okay=False))
@click.option(
    "--to",
    default="16",
    metavar="VERSION",
    help="Target PostgreSQL major version (default: 16).",
)
@click.option(
    "--image",
    default=None,
    metavar="IMAGE",
    help="Full container image to use instead of postgres:<to>.",
)
@click.option(
    "--container-db",
    "db_name",
    default="odoo",
    help="Database name inside the throwaway container (default: odoo).",
)
@click.option(
    "--from-db",
    "from_db",
    default=None,
    metavar="NAME",
    help="Dump database NAME via the local pg_dump (libpq env) "
    "instead of reading INPUT.",
)
@click.option(
    "--filestore",
    "filestore_dir",
    default=None,
    metavar="DIR",
    type=click.Path(file_okay=False),
    help="Directory to bundle into the zip as filestore/.",
)
@click.option(
    "--docker-test",
    "docker_test",
    is_flag=True,
    help="Test the converted dump by restoring it into a throwaway "
    "postgres:<to> container, then remove the container (requires "
    "docker).",
)
@click.option(
    "--docker-import",
    "docker_import",
    is_flag=True,
    help="Import the converted dump into a postgres:<to> container and "
    "leave it running for inspection (requires docker).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Report what would run without doing it.",
)
@click.option(
    "--allow-restore-errors",
    is_flag=True,
    help="Continue even if the in-container restore reports SQL errors "
    "(default: fail fast, since the dump may be incomplete).",
)
@click.version_option()
def main(
    input,
    output,
    to,
    image,
    db_name,
    from_db,
    filestore_dir,
    docker_test,
    docker_import,
    dry_run,
    allow_restore_errors,
):
    """Convert a PostgreSQL dump to a different server major version.

    Rewrites the dump's SQL for the target version and produces an
    Odoo-format zip backup (``dump.sql`` + ``filestore/`` +
    ``manifest.json``). ``--docker-test``/``--docker-import``
    additionally load the result into a ``postgres:<version>``
    container — the former removes it after checking, the latter
    leaves it running for inspection.

    INPUT defaults to stdin and OUTPUT to stdout (``-`` for either).
    The source can be a backup zip, a plain ``pg_dump -Fp`` .sql file,
    or a database: pass ``--from-db NAME``.

    \b
      pgdump-convert backup.zip > backup-pg16.zip
      cat dump.sql | pgdump-convert --to 16 > backup-pg16.zip
      pgdump-convert --docker-test --to 16 backup.zip > backup-pg16.zip
      pgdump-convert --from-db minha-vinha --filestore /path/to/filestore
    """
    image = image or f"postgres:{to}"
    if docker_test and docker_import:
        raise click.UsageError(
            "--docker-test and --docker-import are mutually exclusive"
        )
    docker = "test" if docker_test else "import" if docker_import else None
    if from_db:
        # No input is read, so the first positional is the OUTPUT.
        if output is not None:
            raise click.UsageError("with --from-db only one output path is allowed")
        out = input if input else "-"
        core.convert_db(
            from_db,
            out,
            image=image,
            db_name=db_name,
            filestore_dir=filestore_dir,
            docker=docker,
            target_version=to,
            dry_run=dry_run,
            allow_restore_errors=allow_restore_errors,
        )
        return
    src = input if input is not None else "-"
    if src != "-":
        if not Path(src).is_file():
            raise click.ClickException(f"{src} is not a file")
    elif input is None and sys.stdin.isatty():
        raise click.ClickException(
            "Nothing to convert: pass a backup file, --from-db NAME, "
            "or pipe a dump on stdin."
        )
    out = output if output else "-"
    core.convert(
        src,
        out,
        image=image,
        db_name=db_name,
        filestore_dir=filestore_dir,
        docker=docker,
        target_version=to,
        dry_run=dry_run,
        allow_restore_errors=allow_restore_errors,
    )


if __name__ == "__main__":
    main()
