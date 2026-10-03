import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import DB
from app.sync import Syncer, webhook_reservation_id
from app.web import create_app

SETTINGS = Settings(
    hostaway_account_id="12345", hostaway_api_key="x", ha_url="http://ha", ha_token="t",
    db_path=":memory:", is_addon=False,
)


def res(id=1, listing=100, arrival="2030-01-10", departure="2030-01-15", code="4821", status="new"):
    return {"id": id, "listingMapId": listing, "status": status, "guestName": "Ann Lee",
            "arrivalDate": arrival, "departureDate": departure, "checkInTime": 16,
            "checkOutTime": 10, "doorCode": code}


class FakeHostaway:
    def __init__(self):
        self.listings_data = [
            {"id": 100, "internalListingName": "Maple 124b"},
            {"id": 200, "internalListingName": "CC6"},
        ]
        self.reservations_data = {1: res()}

    async def listings(self):
        return self.listings_data

    async def reservations(self, departing_from):
        return list(self.reservations_data.values())

    async def reservation(self, rid):
        return self.reservations_data[rid]


class FakeHA:
    def __init__(self):
        self.locks = [
            {"entity_id": "lock.124_maple", "name": "124 Maple", "state": "locked"},
            {"entity_id": "lock.cc6_schlage", "name": "Cc6 Schlage", "state": "locked"},
            {"entity_id": "lock.6805_hill", "name": "6805 Hill", "state": "unavailable"},
        ]
        self.codes = {"lock.124_maple": {"Master": "9999", "Ann": "4821"}}

    async def schlage_locks(self):
        return self.locks

    async def get_codes(self, entity_id):
        return self.codes.get(entity_id, {})

    async def ping(self):
        return True

    async def webhook_automation_exists(self):
        return False


@pytest.fixture
def syncer():
    return Syncer(DB(":memory:"), SETTINGS, FakeHostaway(), FakeHA())


def run(coro):
    return asyncio.run(coro)


def test_import_discover_and_match(syncer):
    assert run(syncer.import_listings()) == 2
    assert run(syncer.import_listings()) == 0
    assert run(syncer.discover_locks()) == 2
    locks = {l["entity_id"]: l for l in syncer.db.query("SELECT * FROM locks")}
    assert locks["lock.124_maple"]["match_source"] == "auto"
    assert locks["lock.6805_hill"]["property_id"] is None


def test_manual_assignment_survives_rediscovery(syncer):
    run(syncer.import_listings())
    run(syncer.discover_locks())
    lock = syncer.db.one("SELECT * FROM locks WHERE entity_id = 'lock.124_maple'")
    other = syncer.db.one("SELECT id FROM properties WHERE hostaway_listing_id = 200")["id"]
    syncer.assign_lock(lock["id"], other)
    run(syncer.discover_locks())
    again = syncer.db.one("SELECT * FROM locks WHERE id = ?", (lock["id"],))
    assert again["property_id"] == other and again["match_source"] == "manual"


def test_webhook_automation_is_pointed_at_the_real_addon_slug(syncer):
    class Ha(FakeHA):
        made = []

        async def own_slug(self):
            return "a1b2c3d4_stay_automation"

        async def create_webhook_automation(self, webhook_id, slug):
            self.made.append((webhook_id, slug))

    syncer.ha = Ha()
    assert run(syncer.fix_addon_slug()) is False  # not running as an add-on (development mode)

    object.__setattr__(syncer.settings, "is_addon", True)
    try:
        syncer.db.set_setting("webhook_id", "abc")
        syncer.db.set_setting("addon_slug", "local_stay_automation")
        assert run(syncer.fix_addon_slug()) is True
        assert Ha.made == [("abc", "a1b2c3d4_stay_automation")]
        assert syncer.db.get_setting("addon_slug") == "a1b2c3d4_stay_automation"
        assert run(syncer.fix_addon_slug()) is False  # already right, nothing rewritten
    finally:
        object.__setattr__(syncer.settings, "is_addon", False)


def test_unusable_webhook_is_logged_without_guest_data(syncer):
    body = {"event": "something.odd", "guestName": "Secret Person"}
    assert run(syncer.handle_webhook(json.dumps(body))) == []
    note = syncer.db.one("SELECT message FROM events WHERE kind = 'webhook.ignored'")["message"]
    assert "event=something.odd" in note and "guestName" in note and "Secret Person" not in note
    assert syncer.db.get_setting("last_webhook_at")  # it did arrive


def test_guest_message_webhooks_are_dropped_quietly(syncer):
    body = {"object": "conversationMessage", "event": "message.received", "accountId": 12345, "data": {"id": 9}}
    assert run(syncer.handle_webhook(json.dumps(body))) == []
    assert syncer.db.one("SELECT 1 FROM events WHERE kind LIKE 'webhook.%'") is None
    assert not syncer.db.get_setting("last_webhook_at")  # the top bar only moves for reservation news


def test_sync_and_webhook_record_changes(syncer):
    run(syncer.import_listings())
    assert run(syncer.sync_reservations()) == 1
    assert run(syncer.sync_reservations()) == 0

    syncer.hostaway.reservations_data[1] = res(departure="2030-01-20")
    payload = {"object": "reservation", "event": "reservation.updated", "accountId": 12345,
               "data": {"id": 1, "listingMapId": 100, "departureDate": "forged"}}
    assert run(syncer.handle_webhook(json.dumps(payload))) == ["extended"]
    stored = syncer.db.one("SELECT * FROM reservations WHERE id = 1")
    assert stored["departure_date"] == "2030-01-20"  # from the API, not the payload
    kinds = [e["kind"] for e in syncer.db.query("SELECT kind FROM events ORDER BY id")]
    assert "reservation.created" in kinds and "reservation.extended" in kinds


def test_webhook_reservation_id():
    body = {"object": "reservation", "accountId": 12345, "data": {"id": 55}}
    assert webhook_reservation_id(body, "12345") == 55
    assert webhook_reservation_id(json.dumps(json.dumps(body)), "12345") == 55  # double-encoded stdin
    assert webhook_reservation_id({**body, "accountId": 1}, "12345") is None
    assert webhook_reservation_id({"object": "conversationMessage", "data": {"id": 5}}, "12345") is None
    assert webhook_reservation_id("not json", "12345") is None


def test_lock_code_check_stores_hashes_only(syncer):
    run(syncer.import_listings())
    run(syncer.discover_locks())
    run(syncer.sync_reservations())
    lock = syncer.db.one("SELECT * FROM locks WHERE entity_id = 'lock.124_maple'")
    run(syncer.read_lock_codes(lock))
    stored = syncer.db.one("SELECT * FROM locks WHERE id = ?", (lock["id"],))
    assert "4821" not in stored["code_hashes"]
    assert json.loads(stored["code_names"]) == ["Ann", "Master"]
    assert syncer.code_hash("4821") in json.loads(stored["code_hashes"])


def test_dashboard_pages_render(syncer):
    run(syncer.import_listings())
    run(syncer.discover_locks())
    run(syncer.sync_reservations())
    client = TestClient(create_app(syncer))
    page = client.get("/")
    assert page.status_code == 200
    assert "Maple 124b" in page.text and "Not checked yet" in page.text
    assert 'href="static/style.css"' in page.text  # relative, for HA ingress
    for path in ("/properties", "/events", "/setup"):
        assert client.get(path).status_code == 200, path

    lock = syncer.db.one("SELECT * FROM locks WHERE entity_id = 'lock.124_maple'")
    resp = client.post("/lock-refresh", data={"property_id": lock["property_id"]}, follow_redirects=False)
    assert resp.status_code == 303
    assert "In lock" in client.get("/").text
