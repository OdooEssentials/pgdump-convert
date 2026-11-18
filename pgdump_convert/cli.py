"""``pgdump-convert`` command line interface."""

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
    dry_run,
    allow_restore_errors,
):
    """Convert a PostgreSQL dump to a different server major version.

    Restores the dump into a throwaway ``postgres:<version>`` docker
    container and re-dumps from that server, so the output is guaranteed
    compatible with the target version. Produces an Odoo-format zip
    backup (``dump.sql`` + ``filestore/`` + ``manifest.json``).

    The source can be a backup zip, a plain ``pg_dump -Fp`` .sql file,
    or a database: pass ``--from-db NAME``.

    \b
      pgdump-convert backup.zip
      pgdump-convert dump.sql backup-pg16.zip --to 16
      pgdump-convert --from-db minha-vinha --filestore /path/to/filestore
    """
    image = image or f"postgres:{to}"
    if from_db:
        # No input is read, so the first positional is the OUTPUT.
        if output is not None:
            raise click.UsageError("with --from-db only one output path is allowed")
        out = Path(input) if input else Path.cwd() / f"{from_db}-pg{to}.zip"
        core.convert_db(
            from_db,
            out,
            image=image,
            db_name=db_name,
            filestore_dir=filestore_dir,
            dry_run=dry_run,
            allow_restore_errors=allow_restore_errors,
        )
        return
    if input is None:
        raise click.ClickException(
            "Nothing to convert: pass a backup file or --from-db NAME."
        )
    src = Path(input)
    if not src.is_file():
        raise click.ClickException(f"{src} is not a file")
    out = Path(output) if output else src.with_name(f"{src.stem}-pg{to}.zip")
    core.convert(
        src,
        out,
        image=image,
        db_name=db_name,
        filestore_dir=filestore_dir,
        dry_run=dry_run,
        allow_restore_errors=allow_restore_errors,
    )


if __name__ == "__main__":
    main()
