import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from guest_extension_pricing.app.db import DB
from guest_extension_pricing.app.extension_pricing import DEFAULTS, ExtensionPricing, SETTINGS_KEY, TZ, extension_intent, validate_settings
from test_sync import run


def test_extension_language():
    assert extension_intent("Could we stay two more nights?")
    assert extension_intent("We might need to extend our reservation.")
    assert extension_intent("Is the house still available through Tuesday?")
    assert not extension_intent("We won't need to extend our reservation.")
    assert not extension_intent("Could we extend checkout until 11?")
    assert not extension_intent("Thanks! <blockquote>Could we stay another night?</blockquote>")


def test_extension_rates_survive_sync_and_stop_when_unblocked():
    start = date(2030, 6, 12)
    dates = [(start + timedelta(days=i)).isoformat() for i in range(5)]
    calendar = {
        night: dict(date=night, price=200, status="blocked" if i != 2 else "available")
        for i, night in enumerate(dates)
    }
    writes = []

    class Hostaway:
        async def reservation(self, reservation_id):
            assert reservation_id == 9
            return dict(status="new", listingMapId=100, departureDate=dates[0])

        async def pricing_calendar(self, listing, first, last):
            assert listing == 100
            return [dict(day) for night, day in calendar.items() if first <= night <= last]

        async def apply_calendar_intervals(self, listing, intervals):
            assert listing == 100
            for row in intervals:
                assert set(row) == {"startDate", "endDate", "price"}
                night = row["startDate"]
                assert row["endDate"] == night and calendar[night]["status"] == "blocked"
                calendar[night]["price"] = row["price"]
                writes.append((night, row["price"]))

    db = DB(":memory:")
    db.set_setting(SETTINGS_KEY, json.dumps({"enabled": True, "percent": "-10"}))
    now = datetime(2030, 6, 10, 17, tzinfo=TZ)
    manager = ExtensionPricing(SimpleNamespace(
        db=db, hostaway=Hostaway(), now=lambda: now, settings=SimpleNamespace(hostaway_account_id="123")
    ))
    payload = dict(object="conversationMessage", event="message.received", accountId=123,
                   data=dict(id=5, reservationId=9, isIncoming=1, body="Can we add another night?"))
    assert manager.receive(payload)
    assert manager.receive(json.dumps(json.dumps(payload)))
    run(manager.tick())
    assert writes == [(dates[0], 180), (dates[1], 180)]

    now = datetime(2030, 6, 11, 0, 45, tzinfo=TZ)
    run(manager.tick())
    assert db.query("SELECT snapshot_rate FROM extension_rates WHERE night=?", (dates[0],)) == [
        {"snapshot_rate": "180.00"}
    ]

    calendar[dates[1]]["status"] = "available"
    now = now.replace(hour=1)
    run(manager.tick())
    assert calendar[dates[1]]["price"] == 180
    calendar[dates[0]]["price"] = 225
    calendar[dates[1]]["price"] = 230
    now = now.replace(hour=2)
    run(manager.tick())
    assert len(writes) == 2
    now = now.replace(hour=4)
    run(manager.tick())
    run(manager.tick())
    assert writes == [(dates[0], 180), (dates[1], 180), (dates[0], 180)]
    assert calendar[dates[1]]["price"] == 230
    assert calendar[dates[3]]["price"] == 200


@pytest.mark.parametrize("nights, listing_tags, selected, eligible", [
    (6, ["Mike"], ["Mike"], False),
    (7, ["Mike"], ["Mike"], True),
    (14, ["Kurt"], ["Mike", "Kurt"], True),
    (15, ["Kurt"], ["Kurt"], False),
    (10, ["Kurt"], ["Mike"], False),
    (10, [], ["Mike", "Kurt"], False),
    (10, [], [], True),
    (10, ["Mike", "Kurt"], ["Kurt"], True),
])
def test_extension_filters_booked_nights_and_listing_tags(nights, listing_tags, selected, eligible):
    today = date(2030, 6, 10)
    checkout = today + timedelta(days=2)
    calls = []

    class Hostaway:
        async def reservation(self, _):
            return {
                "status": "modified",
                "listingMapId": 100,
                "arrivalDate": (checkout - timedelta(days=nights)).isoformat(),
                "departureDate": checkout.isoformat(),
                "listingTags": ["Mike"],
            }

        async def listing_details(self, _):
            return {"listingTags": listing_tags}

        async def pricing_calendar(self, _, start, end):
            calls.append((start, end))
            return [{"date": start, "status": "available"}]

    db = DB(":memory:")
    manager = ExtensionPricing(SimpleNamespace(db=db, hostaway=Hostaway()))
    config = dict(DEFAULTS, percent="10", min_nights=7, max_nights=14, tags=selected)
    run(manager.start_extension(9, today, config))
    assert bool(calls) == eligible


def test_extension_filter_settings_allow_blank_bounds_and_reject_reversed_range():
    form = {"extension_percent": "10", "extension_save": "00:45", "extension_restore": "04:00",
            "extension_min_nights": "", "extension_max_nights": "14", "extension_tag_mike": "on"}
    settings = validate_settings(form)
    assert (settings["min_nights"], settings["max_nights"], settings["tags"]) == (None, 14, ["Mike"])
    with pytest.raises(ValueError, match="Minimum stay length"):
        validate_settings(dict(form, extension_min_nights="15"))


def test_standalone_ingress_settings_persist(tmp_path):
    import re
    from fastapi.testclient import TestClient
    from guest_extension_pricing.app.main import create_app

    path = tmp_path / "extension.db"
    form = {"extension_percent": "15", "extension_save": "00:45", "extension_restore": "04:00",
            "extension_min_nights": "7", "extension_max_nights": "14", "extension_tag_kurt": "on"}
    class Hostaway:
        async def close(self):
            pass

    with TestClient(create_app({}, path, None, hostaway=Hostaway())) as client:
        initial = client.get("/")
        assert initial.status_code == 200
        form["csrf"] = re.search(r'name="csrf" value="([^"]+)"', initial.text)[1]
        assert client.post("/settings", data=dict(form, csrf="invalid")).status_code == 403
        assert client.post("/settings", data=dict(form, extension_enabled="on")).status_code == 400
        assert client.post("/settings", data=form).status_code == 200
    with TestClient(create_app({}, path, None, hostaway=Hostaway())) as client:
        response = client.get("/")
        assert 'name="extension_min_nights" value="7"' in response.text
        assert 'name="extension_tag_kurt" checked' in response.text


@pytest.mark.parametrize("reservation_id, external_guest, hour, minute, restore, credited", [
    (9, None, 1, 59, False, True),
    (9, None, 2, 0, False, False),
    (9, None, 3, 59, False, False),
    (9, None, 4, 1, True, True),
    (10, "requester", 1, 59, False, True),
    (10, "another-guest", 1, 59, False, False),
    (10, None, 1, 59, False, False),
])
def test_only_requesting_guest_bookings_in_price_window_get_credit(
    reservation_id, external_guest, hour, minute, restore, credited,
):
    from guest_extension_pricing.app.report import guest_key, reconcile_bookings, report_rows

    db = DB(":memory:")
    db.set_setting(SETTINGS_KEY, json.dumps({"enabled": True, "percent": "10"}))
    now = datetime(2030, 6, 10, 17, tzinfo=TZ)
    manager = ExtensionPricing(SimpleNamespace(
        db=db, now=lambda: now, settings=SimpleNamespace(hostaway_account_id="123"),
    ))
    db.execute(
        "INSERT INTO extension_rates(listing_id,night,rate,base_rate,reservation_id,guest_key) "
        "VALUES (100,'2030-06-12','110.00','100.00',9,?)",
        (guest_key({"channelId": 20, "guestExternalAccountId": "requester"}),),
    )
    config = dict(DEFAULTS, enabled=True)
    row, = db.query("SELECT * FROM extension_rates")
    blocked_day = {"status": "blocked", "price": 110}
    run(manager.apply_rate(row, blocked_day, "2030-06-10", "17:00", config))
    now = datetime(2030, 6, 11, 4 if restore else hour, 0 if restore else minute, tzinfo=TZ)
    if restore:
        row, = db.query("SELECT * FROM extension_rates")
        run(manager.apply_rate(row, blocked_day, "2030-06-11", "04:00", config))
        now = now.replace(minute=minute)
    row, = db.query("SELECT * FROM extension_rates")
    run(manager.apply_rate(row, {"status": "available", "price": 110},
                           "2030-06-11", now.strftime("%H:%M"), config))
    payload = {"object": "reservation", "event": "reservation.updated", "data": {
        "id": reservation_id, "accountId": 123, "listingMapId": 100, "status": "modified",
        "arrivalDate": "2030-06-12", "departureDate": "2030-06-14", "currency": "USD",
        "channelId": 20, "guestExternalAccountId": external_guest,
    }}
    manager.receive(payload)
    reconcile_bookings(db, now)
    manager.receive(payload)
    reconcile_bookings(db, now)
    rows, totals = report_rows(db)
    assert [row["rent_increase"] for row in rows] == (["10.00"] if credited else [])
    assert [str(total["rent_increase"]) for _, total in totals] == (["10.00"] if credited else [])
    assert db.query("SELECT tracking FROM extension_rates") == [{"tracking": 0}]


def test_previously_adjusted_night_cannot_be_adjusted_again_after_unblock():
    db = DB(":memory:")
    db.execute(
        "INSERT INTO extension_rates(listing_id,night,rate,applied,tracking) VALUES (100,'2030-06-11','110',1,0)"
    )

    class Hostaway:
        async def reservation(self, _):
            return {"status": "new", "listingMapId": 100, "departureDate": "2030-06-11"}

        async def pricing_calendar(self, *args):
            return [
                {"date": "2030-06-11", "price": 110, "status": "blocked"},
                {"date": "2030-06-12", "price": 110, "status": "available"},
            ]

    manager = ExtensionPricing(SimpleNamespace(db=db, hostaway=Hostaway()))
    run(manager.start_extension(42, date(2030, 6, 10), dict(DEFAULTS, percent="10")))
    assert db.query("SELECT rate,tracking FROM extension_rates") == [{"rate": "110", "tracking": 0}]
