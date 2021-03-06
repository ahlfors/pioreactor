# -*- coding: utf-8 -*-
import signal
import faulthandler

import os
import sys
import threading
import atexit
from collections import namedtuple
import logging
from pioreactor.utils import pio_jobs_running
from pioreactor.pubsub import QOS, create_client
from pioreactor.whoami import UNIVERSAL_IDENTIFIER

faulthandler.enable()


def split_topic_for_setting(topic):
    SetAttrSplitTopic = namedtuple(
        "SetAttrSplitTopic", ["unit", "experiment", "job_name", "attr"]
    )
    v = topic.split("/")
    assert len(v) == 6, "something is wrong"
    return SetAttrSplitTopic(v[1], v[2], v[3], v[4])


class BackgroundJob:

    """
    This class handles the fanning out of class attributes, and the setting of those attributes. Use
    `pioreactor/<unit>/<experiment>/<job_name>/<attr>/set` to set an attribute.


    So this class controls most of the Homie convention that we follow:

    1. The device lifecycle: init -> ready -> disconnect (or lost).
        1. The job starts in `init`, where we publish `editable_settings` is a list of variables that will be sent
            to the broker on initialization and retained.
        2. The job moves to `ready`.
        3. We catch key interrupts and kill signals from the underlying machine, and set the state to
           `disconnected`.
        4. If the job exits otherwise (kill -9 or power loss), the state is `lost`, and a last-will saying so is broadcast.
    2. Attributes are broadcast under $properties, and each has $settable set to True. This isn't used at the moment.

    """

    # Homie lifecycle (normally per device (i.e. an rpi) but we are using it for "nodes", in Homie parlance)
    INIT = "init"
    READY = "ready"
    DISCONNECTED = "disconnected"
    SLEEPING = "sleeping"
    LOST = "lost"
    LIFECYCLE_STATES = {INIT, READY, DISCONNECTED, SLEEPING, LOST}

    # initial state is disconnected
    state = DISCONNECTED
    editable_settings = []

    def __init__(self, job_name: str, experiment=None, unit=None) -> None:

        self.job_name = job_name
        self.experiment = experiment
        self.unit = unit
        self.editable_settings = self.editable_settings + ["state"]
        self.logger = logging.getLogger(self.job_name)
        self.pub_client = self.create_pub_client()
        self.sub_client = self.create_sub_client()
        self.pubsub_clients = [self.pub_client, self.sub_client]

        self.check_for_duplicate_process()
        self.set_state(self.INIT)
        self.set_state(self.READY)
        self.set_up_disconnect_protocol()

    def create_pub_client(self):
        last_will = {
            "topic": f"pioreactor/{self.unit}/{self.experiment}/{self.job_name}/$state",
            "payload": self.LOST,
            "qos": QOS.EXACTLY_ONCE,
            "retain": True,
        }

        client = create_client(
            last_will=last_will,
            client_id=f"{self.unit}-pub-{self.job_name}-{id(self)}",
            keepalive=10,
        )

        return client

    def create_sub_client(self):
        client = create_client(client_id=f"{self.unit}-sub-{self.job_name}-{id(self)}")

        # when we reconnect to the broker, we want to republish our state
        # to overwrite potential last-will losts...
        # also reconnect to our old topics.
        def reconnect_protocol(client, userdata, flags, rc, properties=None):

            self.publish_attr("state")
            self.start_general_passive_listeners()
            self.start_passive_listeners()

        # the client connects async, but we want it to be connected before adding
        # our reconnect callback
        while not client.is_connected():
            pass

        client.on_connect = reconnect_protocol
        return client

    def publish(self, *args, **kwargs):
        self.pub_client.publish(*args, **kwargs)

    def publish_attr(self, attr: str) -> None:
        if attr == "state":
            attr_name = "$state"
        else:
            attr_name = attr

        self.publish(
            f"pioreactor/{self.unit}/{self.experiment}/{self.job_name}/{attr_name}",
            getattr(self, attr),
            retain=True,
            qos=QOS.EXACTLY_ONCE,
        )

    def subscribe_and_callback(self, callback, subscriptions, allow_retained=True, qos=0):
        """

        Parameters
        -------------
        callback: callable
            Callbacks only accept a single parameter, message.
        subscriptions: str, list of str
        allow_retained: bool
            if True, all messages are allowed, including messages that the broker has retained. Note
            that client can fire a msg with retain=True, but because the broker is serving it to a
            subscriber "fresh", it will have retain=False on the client side. More here:
            https://github.com/eclipse/paho.mqtt.python/blob/master/src/paho/mqtt/client.py#L364
        """

        def check_for_duplicate_subs(subs):

            from itertools import combinations
            from paho.mqtt.client import topic_matches_sub

            for pair in combinations(subs, 2):
                if topic_matches_sub(*subs):
                    self.logger.debug(
                        f"found equivalent pair of subs with same callback - this could cause duplication of callbacks: {pair}"
                    )
                    raise ValueError(
                        f"found equivalent pair of subs with same callback - this could cause duplication of callbacks: {pair}"
                    )

        def wrap_callback(actual_callback):
            def _callback(client, userdata, message):
                if not allow_retained and message.retain:
                    return
                try:
                    return actual_callback(message)
                except Exception as e:
                    self.logger.error(e, exc_info=True)
                    raise e

            return _callback

        assert callable(
            callback
        ), "callback should be callable - do you need to change the order of arguments?"

        subscriptions = (
            [subscriptions] if isinstance(subscriptions, str) else subscriptions
        )

        for sub in subscriptions:
            self.sub_client.message_callback_add(sub, wrap_callback(callback))
            self.sub_client.subscribe(sub, qos=qos)
        return

    def set_up_disconnect_protocol(self):
        # here, we set up how jobs should disconnect and exit.
        def disconnect_gracefully(*args):
            if self.state == self.DISCONNECTED:
                return
            self.set_state("disconnected")

        def exit_python(*args):
            self.logger.debug("Calling sys.exit(0)")
            sys.exit(0)

        # signals only work in main thread - and if we set state via MQTT,
        # this would run in a thread - so just skip.
        if threading.current_thread() is threading.main_thread():
            atexit.register(disconnect_gracefully)
            signal.signal(
                signal.SIGTERM, disconnect_gracefully
            )  # terminate command, ex: pkill
            signal.signal(signal.SIGINT, disconnect_gracefully)  # keyboard interrupt
            signal.signal(
                signal.SIGUSR1, exit_python
            )  # user defined signal, we use to exit

    def init(self):
        self.state = self.INIT
        self.logger.debug(self.INIT)

        if threading.current_thread() is not threading.main_thread():
            # if we re-init (via MQTT, close previous threads), but don't do this in main thread
            for client in self.pubsub_clients:
                client.loop_stop()  # pretty sure this doesn't close the thread if called in a thread: https://github.com/eclipse/paho.mqtt.python/blob/master/src/paho/mqtt/client.py#L1835
                client.disconnect()

            self.pub_client = self.create_pub_client()
            self.sub_client = self.create_sub_client()
            self.pubsub_clients = [self.pub_client, self.sub_client]

        self.declare_settable_properties_to_broker()
        self.start_general_passive_listeners()

    def ready(self):
        self.state = self.READY
        self.logger.info(self.READY)

    def sleeping(self):
        self.state = self.SLEEPING
        self.logger.debug(self.SLEEPING)

    def on_disconnect(self):
        # specific things to do when a job disconnects / exits
        pass

    def disconnected(self):
        # call job specific on_disconnect to clean up subjobs, etc.
        # however, if it fails, nothing below executes, so we don't get a clean
        # disconnect, etc.
        try:
            self.on_disconnect()
        except Exception as e:
            self.logger.error(e, exc_info=True)

        # set state to disconnect
        self.state = self.DISCONNECTED
        self.logger.info(self.DISCONNECTED)

        # disconnect from the passive subscription threads
        # this HAS to happen last, because this contains our publishing client
        for client in self.pubsub_clients:
            client.loop_stop()  # pretty sure this doesn't close the thread if if in a thread: https://github.com/eclipse/paho.mqtt.python/blob/master/src/paho/mqtt/client.py#L1835
            client.disconnect()

        # exit from python using a signal - this works in threads (sometimes `disconnected` is called in a thread)
        # this time.sleep is for race conflicts - without it was causing the MQTT client to disconnect too late and a last-will was sent.
        # previously had 0.25, needed to bump it.
        os.kill(os.getpid(), signal.SIGUSR1)

    def declare_settable_properties_to_broker(self):
        # this follows some of the Homie convention: https://homieiot.github.io/specification/
        self.publish(
            f"pioreactor/{self.unit}/{self.experiment}/{self.job_name}/$properties",
            ",".join(self.editable_settings),
            qos=QOS.AT_LEAST_ONCE,
        )

        for setting in self.editable_settings:
            self.publish(
                f"pioreactor/{self.unit}/{self.experiment}/{self.job_name}/{setting}/$settable",
                True,
                qos=QOS.AT_LEAST_ONCE,
            )

    def set_state(self, new_state):
        assert new_state in self.LIFECYCLE_STATES, f"saw {new_state}: not a valid state"
        getattr(self, new_state)()

    def set_attr_from_message(self, message):

        new_value = message.payload.decode()
        info_from_topic = split_topic_for_setting(message.topic)
        attr = info_from_topic.attr.lstrip("$")

        if attr not in self.editable_settings:
            return

        assert hasattr(self, attr), f"{self.job_name} has no attr {attr}."
        previous_value = getattr(self, attr)

        # a subclass may want to define a `set_<attr>` method that will be used instead
        # for example, see Stirring, and `set_state` here
        if hasattr(self, "set_%s" % attr):
            getattr(self, "set_%s" % attr)(new_value)

        else:
            try:
                # make sure to cast the input to the same value
                setattr(self, attr, type(previous_value)(new_value))
            except TypeError:
                setattr(self, attr, new_value)

        self.logger.info(
            f"Updated {attr} from {previous_value} to {getattr(self, attr)}."
        )

    def start_passive_listeners(self):
        # overwrite this to in subclasses to subscribe to topics in MQTT
        # using this handles reconnects correctly.
        pass

    def start_general_passive_listeners(self) -> None:
        # listen to changes in editable properties
        # everyone listens to $BROADCAST
        self.subscribe_and_callback(
            self.set_attr_from_message,
            [
                f"pioreactor/{self.unit}/{self.experiment}/{self.job_name}/+/set",
                f"pioreactor/{UNIVERSAL_IDENTIFIER}/{self.experiment}/{self.job_name}/+/set",
            ],
        )

    def check_for_duplicate_process(self):
        if (
            sum([p == self.job_name for p in pio_jobs_running()]) > 1
        ):  # this process counts as one - see if there is another.
            self.logger.warn(f"{self.job_name} is already running. Aborting.")
            raise ValueError(f"{self.job_name} is already running. Aborting.")

    def __setattr__(self, name: str, value) -> None:
        super(BackgroundJob, self).__setattr__(name, value)
        if (name in self.editable_settings) and hasattr(self, name):
            self.publish_attr(name)
