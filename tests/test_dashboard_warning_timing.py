"""Regression tests for the Needs Attention guest-code warning timing."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.web import missing_code_warning_due

TZ = ZoneInfo("America/New_York")


def at(value):
    return datetime.fromisoformat(value).replace(tzinfo=TZ)


def reservation(checkin):
    return {"check_in_at": at(checkin).isoformat()}


@pytest.mark.parametrize("clock,expected", [
    ("2030-01-09T18:00", False),  # day before arrival
    ("2030-01-10T14:59", False),  # not yet scheduled
    ("2030-01-10T15:00", False),  # code just became due
    ("2030-01-10T15:14", False),  # grace period
    ("2030-01-10T15:15", True),   # warning becomes actionable
    ("2030-01-10T16:00", True),
])
def test_normal_checkin(clock, expected):
    assert missing_code_warning_due(reservation("2030-01-10T16:00"), at(clock), 8, 3, 60) is expected


@pytest.mark.parametrize("clock,expected", [
    ("2030-01-10T07:59", False),
    ("2030-01-10T08:14", False),
    ("2030-01-10T08:15", True),
])
def test_early_checkin(clock, expected):
    # 9 AM check-in means code is scheduled at 8 AM, one hour before arrival.
    assert missing_code_warning_due(reservation("2030-01-10T16:00"), at(clock), 8, 3, 60) is expected


def test_custom_schedule_and_final_check_limit():
    r = reservation("2030-01-10T15:00")
    assert not missing_code_warning_due(r, at("2030-01-10T14:14"), 15, 0, 60)
    assert missing_code_warning_due(r, at("2030-01-10T14:15"), 15, 0, 60)


def test_no_upcoming_reservation():
    assert not missing_code_warning_due(None, at("2030-01-10T15:30"), 8, 3, 60)
