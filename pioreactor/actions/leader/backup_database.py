# -*- coding: utf-8 -*-

import logging
import click
from pioreactor.config import config, get_active_workers_in_inventory
from pioreactor.whoami import get_unit_name

logger = logging.getLogger("backup_database")


def backup_database(output):
    """
    This action will create a backup of the SQLite3 database into specified output. It then
    will try to scp the backup to any available worker Pioreactors as a futher backup.

    A cronjob is set up as well to run this action every 12 hours.

    """
    import sqlite3
    from sh import scp, ErrorReturnCode

    def progress(status, remaining, total):
        logger.debug(f"Copied {total-remaining} of {total} pages.")

    logger.debug(f"Starting backup of database to {output}")

    con = sqlite3.connect(config.get("storage", "database"))
    bck = sqlite3.connect(output)

    with bck:
        con.backup(bck, pages=-1, progress=progress)

    bck.close()
    con.close()
    logger.debug(
        f"Completed backup of database to {output}. Attempting distributed backup..."
    )

    n_backups = 2
    backups_complete = 0
    available_workers = get_active_workers_in_inventory()

    while (backups_complete < n_backups) and (len(available_workers) > 0):
        backup_unit = available_workers.pop()
        if backup_unit == get_unit_name():
            continue

        try:
            scp(output, f"{backup_unit}:{output}")
        except ErrorReturnCode:
            logger.debug(
                f"Unable to backup database to {backup_unit}. Is it online?",
                exc_info=True,
            )
            logger.warning(f"Unable to backup database to {backup_unit}.")
        else:
            logger.debug(f"Backed up database to {backup_unit}:{output}.")
            backups_complete += 1

    return


@click.command(name="backup_database")
@click.option("--output", default="/home/pi/.pioreactor/pioreactor.sqlite.backup")
def click_backup_database(output):
    """
    (leader only) Backup db to workers.
    """
    return backup_database(output)
