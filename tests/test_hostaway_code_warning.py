"""Missing Hostaway-code warnings must obey the same grace period as missing lock codes."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.web import dashboard_rows, missing_code_warning_due
from test_sync import res, run, syncer

TZ = ZoneInfo("America/New_York")


def at(s):
    return datetime.fromisoformat(s).replace(tzinfo=TZ)


def prepare_no_code(syncer, check_in_hour=16, add_hour=8):
    reservation = res(code=None)
    reservation["checkInTime"] = check_in_hour
    syncer.hostaway.reservations_data = {1: reservation}
    run(syncer.import_listings())
    run(syncer.discover_locks())
    run(syncer.sync_reservations())
    syncer.db.set_setting("add_hour", str(add_hour))
    return syncer


def no_code_warning(syncer, stamp):
    syncer.now = lambda: at(stamp)
    home = next(row for row in dashboard_rows(syncer) if row["name"] == "Maple 124b")
    return [p["text"] for p in home["problems"] if p["text"] == "No door code in Hostaway."]


@pytest.mark.parametrize("stamp,expected", [
    ("2030-01-09T12:00", False),  # within 36 hours, but before installation
    ("2030-01-10T07:59", False),
    ("2030-01-10T08:00", False),  # installation time is not immediately actionable
    ("2030-01-10T08:14", False),
    ("2030-01-10T08:15", True),
    ("2030-01-10T09:00", True),
])
def test_no_hostaway_code_waits_for_installation_plus_fifteen_minutes(syncer, stamp, expected):
    prepare_no_code(syncer)
    assert bool(no_code_warning(syncer, stamp)) is expected


@pytest.mark.parametrize("add_hour,checkin,just_before,due", [
    (10, 16, "2030-01-10T10:14", "2030-01-10T10:15"),
    (8, 9, "2030-01-10T06:14", "2030-01-10T06:15"),
])
def test_warning_uses_existing_configured_or_early_install_schedule(
    syncer, add_hour, checkin, just_before, due
):
    prepare_no_code(syncer, check_in_hour=checkin, add_hour=add_hour)
    assert not no_code_warning(syncer, just_before)
    assert no_code_warning(syncer, due)


def test_no_warning_when_hostaway_provides_the_code(syncer):
    prepare_no_code(syncer)
    syncer.hostaway.reservations_data[1] = res(code="4821")
    run(syncer.sync_reservations())
    assert not no_code_warning(syncer, "2030-01-10T08:30")


def test_grace_gate_never_changes_code_installation_or_reservation_data(syncer):
    prepare_no_code(syncer)
    before = syncer.db.query("SELECT * FROM reservations")
    lock_before = syncer.db.query("SELECT * FROM locks")
    no_code_warning(syncer, "2030-01-10T07:45")
    no_code_warning(syncer, "2030-01-10T08:30")
    assert syncer.db.query("SELECT * FROM reservations") == before
    assert syncer.db.query("SELECT * FROM locks") == lock_before
