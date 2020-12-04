# -*- coding: utf-8 -*-
"""
cmd line interface for running individual pioreactor units (including leader)

> pio run stirring
> pio run od_reading --od-angle-channel 135,0
> pio log
"""
import click
import importlib
from subprocess import call
from pioreactor.whoami import am_I_leader

WORKER_JOBS = [
    "stirring",
    "growth_rate_calculating",
    "od_reading",
    "io_controlling",
    "stirring",
    "add_alt_media",
    "add_media",
    "remove_waste",
    "od_normalization",
]

LEADER_JOBS = [
    "log_aggregating",
    "mqtt_to_db_streaming",
    "time_series_aggregating",
    "download_experiment_data",
]

if am_I_leader():
    valid_jobs = LEADER_JOBS
else:
    valid_jobs = WORKER_JOBS


@click.group()
def pio():
    pass


@pio.command(name="logs")
def logs():
    from sh import tail

    try:
        tail_sh = tail("-f", "/var/log/pioreactor.log", _iter=True)
        for line in tail_sh:
            print(line, end="")
    except KeyboardInterrupt:
        tail_sh.kill()


@pio.command(name="kill")
@click.argument("process")
def kill(process):
    # TODO this fails for python
    from sh import kill, pgrep

    assert process in (valid_jobs + ["python"]), "Must be python a valid Pioreactor job."

    try:
        # remove the _oldest_ one
        kill(int(pgrep("-f", "-o", process)))
    except Exception:
        pass


@pio.command(
    name="run", context_settings=dict(ignore_unknown_options=True, allow_extra_args=True)
)
@click.argument("job", type=click.Choice(valid_jobs, case_sensitive=True))
@click.option("--background", "-b", is_flag=True)
@click.pass_context
def run(ctx, job, background):

    extra_args = list(ctx.args)

    if am_I_leader():
        job = f"leader.{job}"

    if importlib.util.find_spec(f"pioreactor.background_jobs.{job}"):
        loc = f"pioreactor.background_jobs.{job}"
    elif importlib.util.find_spec(f"pioreactor.actions.{job}"):
        loc = f"pioreactor.actions.{job}"
    else:
        raise ValueError(f"Job {job} not found")

    command = ["python3", "-u", "-m", loc] + extra_args

    if background:
        command = (
            ["nohup"]
            + command
            + [
                "-v",
                " 2>&1",
                "| sudo tee -a",
                "/var/log/pioreactor.log",
                ">",
                "/dev/null",
                "&",
            ]
        )
        click.echo(click.style("Appending logs to /var/log/pioreactor.log", fg="green"))
        click.echo(
            click.style("Tip: Tail logs using ", fg="green")
            + click.style("pio logs", bold=True)
        )

    call(" ".join(command), shell=True)
    return


if __name__ == "__main__":
    pio()
