# -*- coding: utf-8 -*-
"""
Continuously monitor the bioreactor and take action. This is the core of the io algorithm
"""
import time, signal, sys, os, traceback

import threading
from enum import Enum
from typing import Iterator
import json

import click

from morbidostat.actions.add_media import add_media
from morbidostat.actions.remove_waste import remove_waste
from morbidostat.actions.add_alt_media import add_alt_media
from morbidostat.pubsub import publish, subscribe_and_callback
from morbidostat.utils import log_start, log_stop
from morbidostat.utils.timing import every
from morbidostat.utils.streaming_calculations import PID
from morbidostat.whoami import unit, experiment
from morbidostat.background_jobs.utils.alt_media_calculator import AltMediaCalculator
from morbidostat.background_jobs.utils import events
from morbidostat.background_jobs import BackgroundJob

VIAL_VOLUME = 14
JOB_NAME = os.path.splitext(os.path.basename((__file__)))[0]


class ControlAlgorithm(BackgroundJob):
    """
    This is the super class that algorithms inherit from. The `run` function will
    execute every N minutes (selected at the start of the program). This calls the `execute` function,
    which is what subclasses will define.
    """

    latest_growth_rate = None
    latest_od = None
    publish_out = ["volume", "target_od", "target_growth_rate", "sensor"]

    def __init__(self, unit=None, experiment=None, verbose=0, sensor="135/A", **kwargs):
        self.unit = unit
        self.verbose = verbose
        self.experiment = experiment
        self.sensor = sensor
        self.alt_media_calculator = AltMediaCalculator(unit=self.unit, experiment=self.experiment, verbose=self.verbose)

        super(ControlAlgorithm, self).__init__(job_name=JOB_NAME, verbose=verbose, unit=unit, experiment=experiment)
        self.start_passive_listeners()

    def run(self, counter=None):
        if self.active == 0:
            return events.NoEvent("Paused. Set `active` to 0 to start again.")

        if (self.latest_growth_rate is None) or (self.latest_od is None):
            time.sleep(10)  # wait some time for data to arrive, and try again.
            return self.run()

        event = self.execute(counter)
        publish(f"morbidostat/{self.unit}/{self.experiment}/log", f"[{JOB_NAME}]: triggered {event}.", verbose=self.verbose)
        return event

    def execute(self, counter) -> events.Event:
        raise NotImplementedError

    def execute_io_action(self, alt_media_ml=0, media_ml=0, waste_ml=0, log=True):
        assert (
            abs(alt_media_ml + media_ml - waste_ml) < 1e-5
        ), f"in order to keep same volume, IO should be equal. {alt_media_ml}, {media_ml}, {waste_ml}"

        if log:
            # TODO: this is not being stored or used.
            publish(
                f"morbidostat/{self.unit}/{self.experiment}/io_batched",
                json.dumps({"alt_media_ml": alt_media_ml, "media_ml": media_ml, "waste_ml": waste_ml}),
                verbose=self.verbose,
            )

        if waste_ml > 0.5:
            """
            this can be smarter to minimize noise.
            """
            self.execute_io_action(alt_media_ml=alt_media_ml / 2, media_ml=media_ml / 2, waste_ml=waste_ml / 2, log=False)
            self.execute_io_action(alt_media_ml=alt_media_ml / 2, media_ml=media_ml / 2, waste_ml=waste_ml / 2, log=False)
        else:
            if alt_media_ml > 0:
                add_alt_media(ml=alt_media_ml, verbose=self.verbose)
                time.sleep(2.5)  # allow time for the addition to mix
            if media_ml > 0:
                add_media(ml=media_ml, verbose=self.verbose)
                time.sleep(2.5)  # allow time for the addition to mix
            if waste_ml > 0:
                remove_waste(ml=waste_ml, verbose=self.verbose)
                # run remove_waste for an additional second to keep volume constant (determined by the length of the waste tube)
                remove_waste(duration=1, verbose=self.verbose)

    def set_growth_rate(self, message):
        self.previous_growth_rate = self.latest_growth_rate
        self.latest_growth_rate = float(message.payload)

    def set_OD(self, message):
        self.previous_od = self.latest_od
        self.latest_od = float(message.payload)

    def start_passive_listeners(self):
        subscribe_and_callback(self.set_OD, f"morbidostat/{self.unit}/{self.experiment}/od_filtered/{self.sensor}")
        subscribe_and_callback(self.set_growth_rate, f"morbidostat/{self.unit}/{self.experiment}/growth_rate")

        super(ControlAlgorithm, self).start_passive_listeners()


######################
# modes of operation
######################


class Silent(ControlAlgorithm):
    def execute(self, *args, **kwargs) -> events.Event:
        return events.NoEvent("Never execute IO events in Silent mode")


class Turbidostat(ControlAlgorithm):
    """
    turbidostat mode - try to keep cell density constant. The algorithm should run at
    near every second (limited by the OD reading rate)
    """

    def __init__(self, target_od=None, volume=None, **kwargs):
        self.target_od = target_od
        self.volume = volume
        super(Turbidostat, self).__init__(**kwargs)

    def execute(self, *args, **kwargs) -> events.Event:
        if self.latest_od >= self.target_od:
            self.execute_io_action(media_ml=self.volume, waste_ml=self.volume)
            return events.DilutionEvent(f"latest OD={self.latest_od:.2f}V >= target OD={self.target_od:.2f}V")
        else:
            return events.NoEvent(f"latest OD={self.latest_od:.2f}V < target OD={self.target_od:.2f}V")


class PIDTurbidostat(ControlAlgorithm):
    """
    turbidostat mode - try to keep cell density constant using a PID target at the OD.

    The PID tells use what fraction of volume we should limit. For example, of PID
    returns 0.03, then we should remove 97% of the volume. Choose volume to be about 0.5ml - 1.0ml.
    """

    def __init__(self, target_od=None, volume=None, verbose=0, **kwargs):
        self.target_od = target_od
        self.volume = volume
        self.min_od = 0.75 * target_od
        self.verbose = verbose
        self.pid = PID(0.07, 0.05, 0.2, setpoint=self.target_od, output_limits=(0, 1), sample_time=None, verbose=self.verbose)
        super(PIDTurbidostat, self).__init__(verbose=self.verbose, **kwargs)

    def execute(self, *args, **kwargs) -> events.Event:
        if self.latest_od <= self.min_od:
            return events.NoEvent(f"current OD, {self.latest_od:.2f}, less than OD to start diluting, {self.min_od:.2f}")
        else:
            output = self.pid.update(self.latest_od)

            volume_to_cycle = (1 - output) * self.volume

            if volume_to_cycle == 0:
                return events.NoEvent(f"PID output={output:.2f}, so no volume to cycle")
            else:
                self.execute_io_action(media_ml=volume_to_cycle, waste_ml=volume_to_cycle)
                return events.DilutionEvent(f"PID output={output:.2f}, volume to cycle={volume_to_cycle:.2f}mL")


class PIDMorbidostat(ControlAlgorithm):
    """
    As defined in Zhong 2020
    """

    def __init__(self, target_growth_rate=None, target_od=None, duration=None, volume=None, verbose=0, **kwargs):
        self.target_growth_rate = target_growth_rate
        self.target_od = target_od
        self.min_od = 0.75 * self.target_od
        self.max_od = 1.1 * self.target_od
        self.duration = duration
        self.verbose = verbose
        self.pid = PID(
            -2.00, -0.01, -0.05, setpoint=self.target_growth_rate, output_limits=(0, 1), sample_time=None, verbose=self.verbose
        )

        if volume is not None:
            publish(
                f"morbidostat/{self.unit}/{self.experiment}/log",
                f"[{JOB_NAME}]: Ignoring volume parameter; volume set by target growth rate and duration.",
                verbose=self.verbose,
            )

        self.volume = self.target_growth_rate * VIAL_VOLUME * (self.duration / 60)
        super(PIDMorbidostat, self).__init__(verbose=self.verbose, **kwargs)

    def execute(self, *args, **kwargs) -> events.Event:
        if self.latest_od <= self.min_od:
            return events.NoEvent(f"Latest OD less than OD to start diluting, {self.min_od:.2f}")
        else:
            fraction_of_alt_media_to_add = self.pid.update(
                self.latest_growth_rate, dt=self.duration
            )  # duration is measured in minutes, not seconds (as simple_pid would want)

            # dilute more if our OD keeps creeping up - we want to stay in the linear range.
            if self.latest_od > self.max_od:
                publish(
                    f"morbidostat/{self.unit}/{self.experiment}/log",
                    f"[{JOB_NAME}]: executing double dilution since we are above max OD, {self.max_od:.2f}.",
                    verbose=self.verbose,
                )
                volume = 2 * self.volume
            else:
                volume = self.volume

            alt_media_ml = fraction_of_alt_media_to_add * volume
            media_ml = (1 - fraction_of_alt_media_to_add) * volume

            self.execute_io_action(alt_media_ml=alt_media_ml, media_ml=media_ml, waste_ml=volume)
            event = events.AltMediaEvent(
                f"PID output={fraction_of_alt_media_to_add:.2f}, alt_media_ml={alt_media_ml:.2f}mL, media_ml={media_ml:.2f}mL"
            )
            event.media_ml = media_ml  # can be used for testing later
            event.alt_media_ml = alt_media_ml
            return event


class Morbidostat(ControlAlgorithm):
    """
    As defined in Toprak 2013.
    """

    def __init__(self, target_od=None, volume=None, **kwargs):
        self.target_od = target_od
        self.volume = volume
        super(Morbidostat, self).__init__(**kwargs)

    def execute(self, *args, **kwargs) -> events.Event:
        """
        morbidostat mode - keep cell density below and threshold using chemical means. The conc.
        of the chemical is diluted slowly over time, allowing the microbes to recover.
        """
        if self.previous_od is None:
            return events.NoEvent("Skip first event to wait for OD readings.")
        elif self.latest_od >= self.target_od and self.latest_od >= self.previous_od:
            # if we are above the threshold, and growth rate is greater than dilution rate
            # the second condition is an approximation of this.
            self.execute_io_action(alt_media_ml=self.volume, waste_ml=self.volume)
            return events.AltMediaEvent(
                f"Latest OD, {self.latest_od:.2f} >= Target OD, {self.target_od:.2f} and Latest OD, {self.latest_od:.2f} >= Previous OD, {self.previous_od:.2f}"
            )
        else:
            self.execute_io_action(media_ml=self.volume, waste_ml=self.volume)
            return events.DilutionEvent(
                f"Latest OD, {self.latest_od:.2f} < Target OD, {self.target_od:.2f} or Latest OD, {self.latest_od:.2f} < Previous OD, {self.previous_od:.2f}"
            )


@log_start(unit, experiment)
@log_stop(unit, experiment)
def io_controlling(mode=None, duration=None, verbose=0, sensor="135/A", skip_first_run=False, **kwargs) -> Iterator[events.Event]:

    algorithms = {
        "silent": Silent,
        "morbidostat": Morbidostat,
        "turbidostat": Turbidostat,
        "pid_turbidostat": PIDTurbidostat,
        "pid_morbidostat": PIDMorbidostat,
    }

    assert mode in algorithms.keys()

    publish(
        f"morbidostat/{unit}/{experiment}/log",
        f"[{JOB_NAME}]: starting {mode} with {duration}min intervals, metadata: {kwargs}",
        verbose=verbose,
    )

    if skip_first_run:
        publish(f"morbidostat/{unit}/{experiment}/log", f"[{JOB_NAME}]: skipping first run", verbose=verbose)
        time.sleep(duration * 60)

    kwargs["verbose"] = verbose
    kwargs["duration"] = duration
    kwargs["unit"] = unit
    kwargs["experiment"] = experiment
    kwargs["sensor"] = sensor

    algo = algorithms[mode](**kwargs)

    def _gen():
        try:
            yield from every(duration * 60, algo.run)
        except Exception as e:
            publish(f"morbidostat/{unit}/{experiment}/error_log", f"[{JOB_NAME}]: failed {str(e)}", verbose=verbose)
            raise e

    return _gen()


@click.command()
@click.option("--mode", default="silent", help="set the mode of the system: turbidostat, morbidostat, silent, etc.")
@click.option("--target-od", default=None, type=float)
@click.option("--target-growth-rate", default=None, type=float, help="used in PIDMorbidostat only")
@click.option("--duration", default=30, help="Time, in minutes, between every monitor check")
@click.option("--volume", default=None, help="the volume to exchange, mL", type=float)
@click.option("--sensor", default="135/A")
@click.option(
    "--skip-first-run",
    is_flag=True,
    help="Normally IO will run immediately. Set this flag to wait <duration>min before executing.",
)
@click.option("--verbose", "-v", count=True, help="print to std.out")
def click_io_controlling(mode, target_od, target_growth_rate, duration, volume, sensor, skip_first_run, verbose):
    controller = io_controlling(
        mode=mode,
        target_od=target_od,
        target_growth_rate=target_growth_rate,
        duration=duration,
        volume=volume,
        skip_first_run=skip_first_run,
        sensor=sensor,
        verbose=verbose,
    )
    while True:
        next(controller)


if __name__ == "__main__":
    click_io_controlling()
