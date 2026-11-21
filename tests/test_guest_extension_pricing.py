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
