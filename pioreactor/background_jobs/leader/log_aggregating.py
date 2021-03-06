# -*- coding: utf-8 -*-
"""
This job runs on the leader, and is a replacement for the NodeRed aggregation job.
"""
import signal
import time
import os
import traceback
import click
import json

from pioreactor.pubsub import QOS
from pioreactor.background_jobs.base import BackgroundJob
from pioreactor.whoami import get_unit_name, UNIVERSAL_EXPERIMENT
from pioreactor.config import config

JOB_NAME = os.path.splitext(os.path.basename((__file__)))[0]


def current_time():
    return time.time_ns() // 1_000_000


class LogAggregation(BackgroundJob):

    editable_settings = ["log_display_count"]

    def __init__(
        self,
        topics,
        output,
        log_display_count=int(config["ui.overview.settings"]["log_display_count"]),
        **kwargs,
    ):
        super(LogAggregation, self).__init__(job_name=JOB_NAME, **kwargs)
        self.topics = topics
        self.output = output
        self.aggregated_log_table = self.read()
        self.log_display_count = log_display_count
        self.start_passive_listeners()

    def on_message(self, message):
        try:
            unit = message.topic.split("/")[1]
            payload = message.payload.decode()
            self.aggregated_log_table.insert(
                0,
                {
                    "timestamp": current_time(),
                    "message": payload,
                    "unit": unit,
                    "is_error": "error" in payload.lower(),
                    "is_warning": "warning" in payload.lower(),
                },
            )
            self.aggregated_log_table = self.aggregated_log_table[
                : self.log_display_count
            ]

            self.write()
        except Exception as e:
            traceback.print_exc()
            raise e
        return

    def clear(self, message):
        payload = message.payload
        if not payload:
            self.aggregated_log_table = []
            self.write()
        else:
            self.logger.warning("Only empty messages allowed to empty the log table.")

    def read(self):
        try:
            with open(self.output, "r") as f:
                return json.load(f)
        except Exception:
            return []

    def write(self):
        with open(self.output, "w") as f:
            json.dump(self.aggregated_log_table, f)

    def start_passive_listeners(self):
        self.subscribe_and_callback(self.on_message, self.topics)
        self.subscribe_and_callback(
            self.clear,
            f"pioreactor/{self.unit}/+/{self.job_name}/aggregated_log_table/set",
            qos=QOS.EXACTLY_ONCE,
        )


@click.command(name="log_aggregating")
@click.option(
    "--output",
    "-o",
    default="/home/pi/pioreactorui/backend/build/data/all_pioreactor.log.json",
    help="the output file",
)
def click_log_aggregating(output):
    """
    (leader only) Aggregate logs for the UI
    """
    logs = LogAggregation(  # noqa: F841
        ["pioreactor/+/+/app_logs_for_ui"],
        output,
        experiment=UNIVERSAL_EXPERIMENT,
        unit=get_unit_name(),
    )

    while True:
        signal.pause()
