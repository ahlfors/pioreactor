# -*- coding: utf-8 -*-
"""
Continuously monitor the bioreactor and take action. This is the core of the io algorithm
"""
import time
import signal
import sys
import os

import threading
from enum import Enum
from typing import Iterator
import json

import click
from simple_pid import PID as simple_PID
from paho.mqtt import subscribe as mqtt_subscribe


from morbidostat.actions.add_media import add_media
from morbidostat.actions.remove_waste import remove_waste
from morbidostat.actions.add_alt_media import add_alt_media
from morbidostat.utils.timing_and_threading import every
from morbidostat.utils.pubsub import publish, subscribe
from morbidostat.utils import get_unit_from_hostname, get_latest_experiment_name, leader_hostname
from morbidostat.background_jobs import events

VIAL_VOLUME = 14


class PID:
    # TODO: move to streaming calculations module

    def __init__(self, *args, unit=None, experiment=None, verbose=False, **kwargs):
        self.pid = simple_PID(*args, **kwargs)
        self.unit = unit
        self.experiment = experiment
        self.verbose = verbose

    def update(self, input_, dt=None):
        output = self.pid(input_, dt)
        self.publish_pid_stats()
        return output

    def publish_pid_stats(self):
        to_send = {
            "setpoint": self.pid.setpoint,
            "output_limits_lb": self.pid.output_limits[0],
            "output_limits_ub": self.pid.output_limits[1],
            "Kd": self.pid.Kd,
            "Ki": self.pid.Ki,
            "Kp": self.pid.Kp,
            "integral": self.pid._integral,
            "proportional": self.pid._proportional,
            "derivative": self.pid._derivative,
            "latest_input": self.pid._last_input,
            "latest_output": self.pid._last_output,
        }
        publish(f"morbidostat/{self.unit}/{self.experiment}/pid_log", json.dumps(to_send), verbose=self.verbose)


class ControlAlgorithm:
    """
    This is the super class that algorithms inherit from. The `run` function will
    execute every N minutes (selected at the start of the program). This calls the `execute` function,
    which is what subclasses will define.
    """

    latest_growth_rate = None
    latest_od = None

    def __init__(self, unit=None, experiment=None, verbose=False, **kwargs):
        self.unit = unit
        self.verbose = verbose
        self.experiment = experiment
        self.start_passive_listeners()

    def run(self, counter=None):
        self.set_OD_measurements()
        event = self.execute(counter)
        publish(f"morbidostat/{self.unit}/{self.experiment}/log", f"[io_controlling]: triggered {event}.", verbose=self.verbose)
        return event

    def set_OD_measurements(self):
        self.previous_rate, self.previous_od = self.latest_growth_rate, self.latest_od
        self.latest_growth_rate = float(subscribe(f"morbidostat/{self.unit}/{self.experiment}/growth_rate").payload)
        # TODO: this below line will break when I use 135A and 135B
        self.latest_od = float(subscribe(f"morbidostat/{self.unit}/{self.experiment}/od_filtered/135").payload)
        return

    def execute(self, counter) -> events.Event:
        """
        Should return a member of the Event class (defined above)
        """
        raise NotImplementedError

    def execute_io_action(self, alt_media_ml=0, media_ml=0, waste_ml=0):
        assert (
            abs(alt_media_ml + media_ml - waste_ml) < 1e-5
        ), f"in order to keep same volume, IO should be equal. {alt_media_ml}, {media_ml}, {waste_ml}"

        if waste_ml > 0.5:
            """
            this can be smarter to minimize noise.
            """
            self.execute_io_action(alt_media_ml=alt_media_ml / 2, media_ml=media_ml / 2, waste_ml=waste_ml / 2)
            self.execute_io_action(alt_media_ml=alt_media_ml / 2, media_ml=media_ml / 2, waste_ml=waste_ml / 2)
        else:
            if alt_media_ml > 0:
                add_alt_media(alt_media_ml, verbose=self.verbose)
            if media_ml > 0:
                add_media(media_ml, verbose=self.verbose)
            if waste_ml > 0:
                remove_waste(waste_ml, verbose=self.verbose)

    def set_attr(self, message):
        try:
            payload = json.loads(message.payload)
            for k, v in payload.items():
                setattr(self, k, v)
                publish(f"morbidostat/{self.unit}/{self.experiment}/log", f"Updated {k} to {v}.")
        except:
            traceback.print_exc()

    def start_passive_listeners(self):
        job_name = os.path.splitext(os.path.basename((__file__)))[0]
        topic = f"morbidostat/{self.unit}/{self.experiment}/{job_name}/+"

        def job_callback(client, userdata, message):
            try:
                topic = message.topic
                function_to_run = topic.split("/")[-1]
                getattr(self, function_to_run)(message)
            except:
                traceback.print_exc()
                publish(f"morbidostat/{self.unit}/{self.experiment}/log", f"No function {function_to_run} found.")

        passive_listener = threading.Thread(
            target=mqtt_subscribe.callback, args=(job_callback, topic), kwargs={"hostname": leader_hostname}, daemon=True
        )
        passive_listener.start()


######################
# modes of operation
######################


class Silent(ControlAlgorithm):
    def execute(self, *args, **kwargs) -> events.Event:
        return events.NoEvent("never execute IO events in Silent mode")


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

    The PID tells use what fraction of max_volume we should limit. For example, of PID
    returns 0.03, then we should remove 97% of the max volume. Choose max volume to be about 0.5ml.
    """

    def __init__(self, target_od=None, volume=None, **kwargs):
        super(PIDTurbidostat, self).__init__(**kwargs)
        self.target_od = target_od
        self.max_volume = volume
        self.od_to_start_diluting = 0.75 * target_od
        self.pid = PID(
            0.07,
            0.05,
            0.2,
            setpoint=self.target_od,
            output_limits=(0, 1),
            sample_time=None,
            unit=self.unit,
            experiment=self.experiment,
            verbose=self.verbose,
        )

    def execute(self, *args, **kwargs) -> events.Event:
        if self.latest_od <= self.od_to_start_diluting:
            return events.NoEvent(f"OD less than OD to start diluting, {self.od_to_start_diluting:.2f}")
        else:
            output = self.pid.update(self.latest_od)

            volume_to_cycle = (1 - output) * self.max_volume

            if volume_to_cycle == 0:
                return events.NoEvent(f"PID output={output:.2f}, so no volume to cycle")
            else:
                self.execute_io_action(media_ml=volume_to_cycle, waste_ml=volume_to_cycle)
                return events.DilutionEvent(f"PID output={output:.2f}, volume to cycle={volume_to_cycle:.2f}mL")


class PIDMorbidostat(ControlAlgorithm):
    """
    As defined in Zhong 2020
    """

    def __init__(self, target_growth_rate=None, target_od=None, duration=None, volume=None, **kwargs):
        super(PIDMorbidostat, self).__init__(**kwargs)
        self.target_growth_rate = target_growth_rate
        self.od_to_start_diluting = 0.75 * target_od
        self.max_od = 1.20 * target_od
        self.duration = duration
        self.pid = PID(
            -8.00,
            -0.01,
            0.0,
            setpoint=self.target_growth_rate,
            output_limits=(0, 1),
            sample_time=None,
            unit=self.unit,
            experiment=self.experiment,
            verbose=self.verbose,
        )

        if volume is not None:
            publish(
                f"morbidostat/{self.unit}/{self.experiment}/log",
                f"[io_controlling]: Ignoring volume parameter; volume set by target growth rate and duration.",
                verbose=self.verbose,
            )

        self.volume = self.target_growth_rate * VIAL_VOLUME * (self.duration / 60)

    def execute(self, *args, **kwargs) -> events.Event:
        if self.latest_od <= self.od_to_start_diluting:
            return events.NoEvent(f"Latest OD less than OD to start diluting, {self.od_to_start_diluting:.2f}")
        else:
            fraction_of_alt_media_to_add = self.pid.update(
                self.latest_growth_rate, dt=self.duration
            )  # duration is measured in minutes, not seconds (as simple_pid would want)

            # dilute more if our OD keeps creeping up - we want to stay in the linear range.
            if self.latest_od > self.max_od:
                publish(
                    f"morbidostat/{self.unit}/{self.experiment}/log",
                    f"[io_controlling]: executing double dilution since we are above max OD, {self.max_od:.2f}.",
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
            return events.NoEvent("Skip first event to set parameters")
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


def io_controlling(mode=None, duration=None, verbose=False, skip_first_run=False, **kwargs) -> Iterator[events.Event]:
    unit = get_unit_from_hostname()
    experiment = get_latest_experiment_name()

    def terminate(*args):
        publish(f"morbidostat/{unit}/{experiment}/log", f"[io_controlling]: terminated.", verbose=verbose)
        sys.exit()

    signal.signal(signal.SIGTERM, terminate)

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
        f"[io_controlling]: starting {mode} with {duration}min intervals, metadata: {kwargs}",
        verbose=verbose,
    )

    if skip_first_run:
        publish(f"morbidostat/{unit}/{experiment}/log", f"[io_controlling]: skipping first run", verbose=verbose)
        time.sleep(duration * 60)

    kwargs["verbose"] = verbose
    kwargs["duration"] = duration
    kwargs["unit"] = unit
    kwargs["experiment"] = experiment
    ##############################
    # main loop
    ##############################
    try:
        yield from every(duration * 60, algorithms[mode](**kwargs).run)
    except Exception as e:
        publish(f"morbidostat/{unit}/{experiment}/error_log", f"[io_controlling]: failed {str(e)}", verbose=verbose)
        publish(f"morbidostat/{unit}/{experiment}/log", f"[io_controlling]: failed {str(e)}", verbose=verbose)
        raise e


@click.command()
@click.option("--mode", default="silent", help="set the mode of the system: turbidostat, morbidostat, silent, etc.")
@click.option("--target_od", default=None, type=float)
@click.option("--target_growth_rate", default=None, type=float)
@click.option("--duration", default=30, help="Time, in minutes, between every monitor check")
@click.option("--volume", default=None, help="the volume to exchange, mL")
@click.option(
    "--skip_first_run",
    is_flag=True,
    help="Normally IO will run immediately. Set this flag to wait <duration>min before executing.",
)
@click.option("--verbose", is_flag=True)
def click_io_controlling(mode, target_od, target_growth_rate, duration, volume, skip_first_run, verbose):
    controller = io_controlling(
        mode=mode,
        target_od=target_od,
        target_growth_rate=target_growth_rate,
        duration=duration,
        volume=volume,
        skip_first_run=skip_first_run,
        verbose=verbose,
    )
    while True:
        next(controller)


if __name__ == "__main__":
    click_io_controlling()
