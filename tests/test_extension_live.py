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


def test_live_inquiry_is_scoped_and_sends_once(syncer, monkeypatch):
    from app import extension_inquiry
    syncer.db.execute(
        "INSERT INTO properties(hostaway_listing_id,name,owner_scope) VALUES(101,'Kurt home','Kurt')"
    )
    syncer.db.set_setting("extension_live_properties", "[101]")
    syncer.settings = SimpleNamespace(extension_ai_api_key="test", hostaway_account_id="22")
    sent = []

    async def prepare(*args):
        return {"id": 17, "departure_date": "2026-12-01"}, 31, 32, "Could you confirm your checkout date?"

    async def send(thread, body):
        sent.append((thread, body))

    monkeypatch.setattr(extension_inquiry, "prepare_inquiry", prepare)
    monkeypatch.delenv("ALL_PROXY", raising=False)
    monkeypatch.delenv("all_proxy", raising=False)
    syncer.hostaway = SimpleNamespace(send_extension_message=send)
    raw = {"id": 23, "accountId": 22, "listingMapId": 101, "status": "inquiry"}
    extension_inquiry.queue_inquiry(syncer, dict(raw, accountId=999), 23)
    assert not syncer.db.query("SELECT * FROM extension_live_inquiries")
    extension_inquiry.queue_inquiry(syncer, raw, 23)
    run(extension_inquiry.run_inquiries(syncer))
    extension_inquiry.queue_inquiry(syncer, raw, 23)
    run(extension_inquiry.run_inquiries(syncer))
    assert sent == [(31, "Could you confirm your checkout date?"), (32, extension_inquiry.WAITING_FOR_GUEST)]


def test_verified_turnover_survives_failed_extension_review():
    checkout = date(2026, 12, 1)
    row = {"id": 17, "departure_date": checkout.isoformat(),
           "booked_turnover": [checkout], "error": "Could not evaluate this reservation.",
           "extension_status": "declined", "hold_end": date(2026, 12, 3)}
    unverified = {"id": 18, "error": "Reservation could not be verified"}
    desired, releasable = calendar_plan([row, unverified], checkout)
    assert desired == {checkout: (17, "Turnover hold")}
    assert not releasable


def test_manual_calendar_update_respects_settings(syncer, monkeypatch):
    from fastapi.testclient import TestClient

    from app import extension_live
    from app.web import create_app

    scans = []

    async def scan(syncer, state, *, turnover_only):
        scans.append(turnover_only)
        state["rows"] = []

    monkeypatch.setattr(extension_live, "scan_kurt", scan)
    client = TestClient(create_app(syncer))
    assert client.post("/extension-calendar-update", follow_redirects=False).status_code == 303
    assert not scans
    syncer.db.set_setting("extension_live_calendar", "1")
    response = client.post("/extension-calendar-update")
    assert response.status_code == 200
    assert "Calendar review complete." in response.text
    assert len(scans) == 1
