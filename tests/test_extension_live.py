from datetime import date
from types import SimpleNamespace

from app.extension_live import calendar_plan, update_holds
from test_sync import run, syncer


def test_uncertain_dates_never_authorize_release():
    checkout = date(2026, 11, 27)
    row = {
        "id": 17, "departure_date": checkout.isoformat(), "booked_turnover": [],
        "extension_status": "considering", "hold_end": date(2026, 11, 29),
        "projected_turnover": [date(2026, 11, 29)],
    }
    desired, releasable = calendar_plan([row], checkout)
    assert set(desired) == {checkout, date(2026, 11, 28), date(2026, 11, 29)}
    assert releasable == set()


def test_kurt_hold_then_release_only_owned_extension_night(syncer):
    syncer.db.execute(
        "INSERT INTO properties(hostaway_listing_id,name,owner_scope) VALUES(101,'Kurt home','Kurt')"
    )
    syncer.db.set_setting("extension_live_calendar", "1")
    today = date(2026, 11, 27)
    existing = "2026-11-28"
    days = {
        today.isoformat(): {"status": "available", "isAvailable": 1, "note": ""},
        existing: {"status": "blocked", "isAvailable": 0, "note": "Host hold"},
    }
    writes = []

    async def read(listing, first, last):
        return [days[first].copy()]

    async def write(listing, intervals):
        changes, = intervals
        night = changes["startDate"]
        writes.append((night, changes["isAvailable"]))
        days[night].update(isAvailable=changes["isAvailable"], note=changes["note"],
                           status="available" if changes["isAvailable"] else "blocked")

    manager = SimpleNamespace(db=syncer.db, hostaway=SimpleNamespace(
        pricing_calendar=read, apply_calendar_intervals=write
    ))
    row = {"id": 17, "departure_date": today.isoformat(), "booked_turnover": [],
           "extension_status": "considering", "hold_end": date(2026, 11, 29)}
    run(update_holds(manager, 101, [row], today, False))
    run(update_holds(manager, 101, [row], today, False))
    row.update(hold_end=None, extension_status="unclear")
    run(update_holds(manager, 101, [row], today, False))
    row["extension_status"] = "declined"
    run(update_holds(manager, 101, [row], today, False))
    assert writes == [(today.isoformat(), 0), (today.isoformat(), 1)]
    assert days[existing]["note"] == "Host hold"
    assert not syncer.db.query("SELECT 1 FROM extension_live_holds")
