# -*- coding: utf-8 -*-
"""
command line for running the same command on all workers,

> pios run od_reading
> pios run stirring
> pios sync
> pios kill <substring>
"""
from concurrent.futures import ThreadPoolExecutor
import logging

import click

from pioreactor.whoami import (
    am_I_leader,
    UNIVERSAL_IDENTIFIER,
    get_latest_experiment_name,
)
from pioreactor.config import get_active_workers_in_inventory, get_leader_hostname


ALL_WORKER_JOBS = [
    "stirring",
    "growth_rate_calculating",
    "dosing_control",
    "led_control",
    "stirring",
    "od_reading",
    "add_alt_media",
    "add_media",
    "remove_waste",
    "od_normalization",
    "monitor",
    "led_intensity",
]

logger = logging.getLogger("leader CLI")


def universal_identifier_to_all_units(units):
    if units == (UNIVERSAL_IDENTIFIER,):
        units = get_active_workers_in_inventory()
    return units


def sync_config_files(ssh_client, unit):
    """
    this function occurs in a thread
    """
    ftp_client = ssh_client.open_sftp()

    # move the global config.ini
    # there was a bug where if the leader == unit, the config.ini would get wiped
    if get_leader_hostname() != unit:
        ftp_client.put(
            "/home/pi/.pioreactor/config.ini", "/home/pi/.pioreactor/config.ini"
        )

    # move the local config.ini
    try:
        ftp_client.put(
            f"/home/pi/.pioreactor/config_{unit}.ini",
            "/home/pi/.pioreactor/unit_config.ini",
        )
    except Exception as e:
        print(f"Did you forget to create a config_{unit}.ini to ship to {unit}?")
        raise e

    ftp_client.close()
    return


@click.group()
def pios():
    """
    Command each of the worker Pioreactors with the `pios` command.
    See full documentation here: https://github.com/Pioreactor/pioreactor/wiki/Command-line-interface#leader-commands
    Report errors or feedback here: https://github.com/Pioreactor/pioreactor/issues
    """
    import sys

    if not am_I_leader():
        print("workers cannot run `pios` commands. Try `pio` instead.")
        sys.exit(0)

    if len(get_active_workers_in_inventory()) == 0:
        print("No active workers. See `inventory` section in config.ini.")
        sys.exit(0)


@pios.command("update", short_help="update PioreactorApp on workers")
@click.option(
    "--units",
    multiple=True,
    default=(UNIVERSAL_IDENTIFIER,),
    type=click.STRING,
    help="specify a Pioreactor name, default is all active units",
)
def update(units):
    """
    Pulls and installs the latest code from Github to the workers.
    """
    import paramiko

    command = "pio update --app"

    def _thread_function(unit):
        print(f"Executing on {unit}...")
        try:

            client = paramiko.SSHClient()
            client.load_system_host_keys()
            client.connect(unit, username="pi")

            (stdin, stdout, stderr) = client.exec_command(command)
            for line in stderr.readlines():
                pass

            client.close()

        except Exception as e:
            print(f"unit={unit}")
            logger.debug(e, exc_info=True)

    units = universal_identifier_to_all_units(units)
    with ThreadPoolExecutor(max_workers=len(units)) as executor:
        executor.map(_thread_function, units)


@pios.command(name="sync-configs", short_help="sync config")
@click.option(
    "--units",
    multiple=True,
    default=(UNIVERSAL_IDENTIFIER,),
    type=click.STRING,
    help="specify a hostname, default is all active units",
)
def sync_configs(units):
    """
    Deploys the global config.ini and worker specific config.inis to the workers.
    """
    import paramiko

    def _thread_function(unit):
        print(f"Executing on {unit}...")
        try:

            client = paramiko.SSHClient()
            client.load_system_host_keys()
            client.connect(unit, username="pi")

            sync_config_files(client, unit)

            client.close()
        except Exception as e:
            print(f"unit={unit}")
            logger.debug(e, exc_info=True)
            logger.error(f"Unable to connect to unit {unit}.")

    units = universal_identifier_to_all_units(units)
    with ThreadPoolExecutor(max_workers=len(units)) as executor:
        executor.map(_thread_function, units)


@pios.command("kill", short_help="kill a job(s) on workers")
@click.argument("job", nargs=-1)
@click.option(
    "--units",
    multiple=True,
    default=(UNIVERSAL_IDENTIFIER,),
    type=click.STRING,
    help="specify a hostname, default is all active units",
)
@click.option("-y", is_flag=True, help="skip asking for confirmation")
def kill(job, units, y):
    """
    Send a SIGTERM signal to JOB. JOB can be any Pioreactor job name, like "stirring".
    Example:

    > pios kill stirring

    multiple jobs accepted:

    > pios kill stirring dosing_control


    """
    from sh import ssh

    if not y:
        confirm = input(f"Confirm killing `{job}` on {units}? Y/n: ").strip()
        if confirm != "Y":
            return

    command = f"pio kill {' '.join(job)}"

    def _thread_function(unit):

        print(f"Executing {command} on {unit}...")
        ssh(unit, command)

    units = universal_identifier_to_all_units(units)
    with ThreadPoolExecutor(max_workers=len(units)) as executor:
        executor.map(_thread_function, units)


@pios.command(
    name="run",
    context_settings=dict(ignore_unknown_options=True, allow_extra_args=True),
    short_help="run a job on workers",
)
@click.argument("job", type=click.Choice(ALL_WORKER_JOBS, case_sensitive=True))
@click.option(
    "--units",
    multiple=True,
    default=(UNIVERSAL_IDENTIFIER,),
    type=click.STRING,
    help="specify a hostname, default is all active units",
)
@click.option("-y", is_flag=True, help="Skip asking for confirmation.")
@click.pass_context
def run(ctx, job, units, y):
    """
    Run a job on all, or specific, workers. Ex:

    > pios run stirring

    Will start stirring on all workers, after asking for confirmation.
    Each job has their own unique options:

    > pios run stirring --duty-cycle 10
    > pios run od_reading --od-angle-channel 135,0

    To specify specific units, use the `--units` keyword multiple times, ex:

    > pios run stirring --units pioreactor2 --units pioreactor3

    """
    from sh import ssh

    extra_args = list(ctx.args)

    if "unit" in extra_args:
        print("Did you mean to use 'units' instead of 'unit'? Exiting.")
        return

    core_command = " ".join(["pio", "run", job, *extra_args])
    command = " ".join(["nohup", core_command, ">/dev/null", "2>&1", "&"])

    if not y:
        confirm = input(f"Confirm running `{core_command}` on {units}? Y/n: ").strip()
        if confirm != "Y":
            return

    def _thread_function(unit):
        print(f"Executing {core_command} on {unit}.")
        ssh(unit, command)

    units = universal_identifier_to_all_units(units)
    with ThreadPoolExecutor(max_workers=len(units)) as executor:
        executor.map(_thread_function, units)

    return


@pios.command(
    name="update-settings",
    context_settings=dict(ignore_unknown_options=True, allow_extra_args=True),
    short_help="update settings on a job on workers",
)
@click.argument("job", type=click.Choice(ALL_WORKER_JOBS, case_sensitive=True))
@click.option(
    "--units",
    multiple=True,
    default=(UNIVERSAL_IDENTIFIER,),
    type=click.STRING,
    help="specify a hostname, default is all active units",
)
@click.pass_context
def update_settings(ctx, job, units):

    exp = get_latest_experiment_name()
    extra_args = {ctx.args[i][2:]: ctx.args[i + 1] for i in range(0, len(ctx.args), 2)}

    if "unit" in extra_args:
        print("Did you mean to use 'units' instead of 'unit'? Exiting.")
        return

    from pioreactor.pubsub import publish

    def _thread_function(unit):
        for (setting, value) in extra_args.items():
            publish(f"pioreactor/{unit}/{exp}/{job}/{setting}/set", value)

    units = universal_identifier_to_all_units(units)
    with ThreadPoolExecutor(max_workers=len(units)) as executor:
        executor.map(_thread_function, units)

    return


if __name__ == "__main__":
    pios()
