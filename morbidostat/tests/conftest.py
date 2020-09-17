# -*- coding: utf-8 -*-
# conftest.py

import pytest
from morbidostat.utils import config


# Replace libraries by fake RPi ones
import sys
import fake_rpi

sys.modules["RPi"] = fake_rpi.RPi  # Fake RPi
sys.modules["RPi.GPIO"] = fake_rpi.RPi.GPIO  # Fake GPIO
sys.modules["smbus"] = fake_rpi.smbus  # Fake smbus (I2C)
config["network"]["leader_hostname"] = "localhost"


def pytest_runtest_setup(item):
    pass
