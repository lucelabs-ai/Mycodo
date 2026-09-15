# coding=utf-8
"""Tests the DeviceMeasurements -> Conversion relationship.

Regression test for the API 500 on GET /api/inputs/<unique_id>:
DeviceMeasurements had a conversion_id column but no 'conversion'
relationship, so the endpoint's join raised AttributeError.
"""
import pytest

from mycodo.databases.models import Conversion
from mycodo.databases.models import DeviceMeasurements


def test_device_measurements_has_conversion_relationship():
    """The model exposes a 'conversion' attribute to join on."""
    assert hasattr(DeviceMeasurements, 'conversion')


def test_conversion_join_does_not_raise(db):
    """The join used by the inputs API builds without AttributeError."""
    query = DeviceMeasurements.query.join(
        DeviceMeasurements.conversion, isouter=True)
    assert query is not None


def test_conversion_is_none_when_unset(db):
    """conversion_id defaults to '' so conversion resolves to None."""
    measurement = DeviceMeasurements()
    assert measurement.conversion is None
