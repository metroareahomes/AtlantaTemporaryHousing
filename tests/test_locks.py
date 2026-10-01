import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.config import Settings
from app.db import DB
from app.locks import (BACKUP_NAME, DEFAULTS, LockManager, code_name, desired_codes, next_check, plan)
from app.reservations import normalize
from app.sync import Syncer

TZ = ZoneInfo("America/New_York")
SETTINGS = Settings(hostaway_account_id="1", hostaway_api_key="x", ha_url="http://ha", ha_token="t",
                    db_path=":memory:", is_addon=False)


def at(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=TZ)


def res(id=1, arrival="2030-01-10", departure="2030-01-15", code="4821", check_in=16, status="new"):
    return {"id": id, "listingMapId": 100, "status": status, "guestName": "Ann Lee",
            "arrivalDate": arrival, "departureDate": departure, "checkInTime": check_in,
            "checkOutTime": 10, "doorCode": code}


def norm(**kw):
    return normalize(res(**kw), TZ, 15, 10)


# ---- pure planning ---------------------------------------------------------

def test_desired_codes_window():
    r = [norm()]
    assert desired_codes(r, at("2030-01-10T07:59"), 8, 3) == {}
    assert desired_codes(r, at("2030-01-10T08:00"), 8, 3) == {"HA-Ann Lee": "4821"}
    assert desired_codes(r, at("2030-01-15T09:59"), 8, 3) == {"HA-Ann Lee": "4821"}
    assert desired_codes(r, at("2030-01-15T10:00"), 8, 3) == {}
    early = [norm(check_in=9)]  # 9 AM check-in: code goes in at 6 AM
    assert desired_codes(early, at("2030-01-10T06:00"), 8, 3) == {"HA-Ann Lee": "4821"}
    assert desired_codes([norm(status="cancelled")], at("2030-01-12T12:00"), 8, 3) == {}
    assert desired_codes([norm(code=None)], at("2030-01-12T12:00"), 8, 3) == {}
    assert desired_codes([], at("2030-01-12T12:00"), 8, 3, backup_code="5555") == {BACKUP_NAME: "5555"}
    assert desired_codes([], at("2030-01-12T12:00"), 8, 3, staff={"Cleaner": "2468"}) == {"Cleaner": "2468"}


def test_code_name_uses_guest_and_avoids_collisions():
    a = norm(id=1)
    b = normalize({**res(id=2), "guestName": "Ann Lee"}, TZ, 15, 10)
    assert code_name(a) == "HA-Ann Lee"
    assert code_name(b, {"HA-Ann Lee"}) == "HA-Ann Lee 2"
    assert code_name(normalize({**res(id=9), "guestName": None}, TZ, 15, 10)) == "HA-9"


def test_plan():
    p = plan({"HA-Ann Lee": "4821"}, {"Master": "9999"})
    assert p.add == {"HA-Ann Lee": "4821"} and p.delete == []
    p = plan({}, {"Master": "9999", "HA-7": "1111"})
    assert p.delete == ["HA-7"] and not p.add  # never touches codes we did not write
    p = plan({"HA-Ann Lee": "4821"}, {"HA-Ann Lee": "1111"})
    assert p.delete == ["HA-Ann Lee"] and p.add == {"HA-Ann Lee": "4821"}
    p = plan({"HA-Ann Lee": "4821"}, {"Ann Hostaway": "4821"})
    assert p.ok and p.elsewhere == ["HA-Ann Lee"]  # Hostaway already wrote it
    p = plan({"HA-Bob": "4821"}, {"HA-Ann Lee": "4821"})  # value only held by a code we are removing
    assert p.delete == ["HA-Ann Lee"] and p.add == {"HA-Bob": "4821"}
    assert plan({"HA-Ann Lee": "4821"}, {"HA-Ann Lee": "4821", "Master": "9"}).ok
    p = plan({}, {"Master": "9999", "Cleaner": "2468"}, managed={"Cleaner"})
    assert p.delete == ["Cleaner"]  # deactivated staff code is ours to remove


def test_next_check_picks_the_nearest_event():
    r = [norm()]
    cfg = dict(DEFAULTS)
    assert next_check(r, at("2030-01-09T12:00"), cfg) == at("2030-01-10T06:00")  # daily check
    assert next_check(r, at("2030-01-10T07:00"), cfg) == at("2030-01-10T08:00")  # add
    assert next_check(r, at("2030-01-10T08:05"), cfg) == at("2030-01-10T14:00")  # afternoon
    assert next_check(r, at("2030-01-10T14:05"), cfg) == at("2030-01-10T15:00")  # final check
    assert next_check(r, at("2030-01-15T07:00"), cfg) == at("2030-01-15T10:00")  # checkout


# ---- reconcile against a fake lock ------------------------------------------

class FakeLock:
    """Behaves like the Schlage integration: optional silent failures and timeouts that still work."""

    def __init__(self):
        self.codes = {"lock.a": {"Master": "9999"}}
        self.broken_add = set()
        self.delete_times_out = False
        self.read_fails = False
        self.on_add = None
        self.notes = []

    async def get_codes(self, entity_id):
        if self.read_fails:
            raise TimeoutError("schlage timeout")
        return dict(self.codes[entity_id])

    async def add_code(self, entity_id, name, code):
        if self.on_add:
            self.on_add()
        if name not in self.broken_add:
            self.codes[entity_id][name] = code

    async def delete_code(self, entity_id, name):
        self.codes[entity_id].pop(name, None)
        if self.delete_times_out:
            raise TimeoutError()

    async def notify(self, title, message, service="", services=None, notification_id=""):
        self.notes.append((title, message))
        for target in list(services or []) + ([service] if service else []):
            self.notes.append((f"via:{target}", message))


class FakeHostaway:
    def __init__(self):
        self.sent = []

    async def send_guest_message(self, reservation_id, body):
        self.sent.append((reservation_id, body))


@pytest.fixture
def lm():
    db = DB(":memory:")
    s = Syncer(db, SETTINGS, FakeHostaway(), FakeLock())
    pid = db.execute("INSERT INTO properties(hostaway_listing_id, hostaway_name, name, lock_automation) "
                     "VALUES(100, 'Maple 1', 'Maple 1', 1)")
    db.execute("INSERT INTO locks(entity_id, name, state, property_id, match_source) "
               "VALUES('lock.a', 'Maple front', 'locked', ?, 'manual')", (pid,))
    m = LockManager(s)
    m.verify_delay = m.gap = 0
    return m


def run(coro):
    return asyncio.run(coro)


def tick_at(m, when):
    m.s.now = lambda: when
    return run(m.tick())


def lock_alerts(m):
    return [t for t, _ in m.ha.notes if t.startswith("Lock problem")]


def lock_row(m):
    return m.db.one("SELECT * FROM locks")


def test_adds_verifies_and_removes_at_checkout(lm):
    lm.s.upsert_reservation(res())
    assert tick_at(lm, at("2030-01-10T08:01")) == 1
    assert lm.ha.codes["lock.a"] == {"Master": "9999", "HA-Ann Lee": "4821"}
    assert lock_row(lm)["fail_count"] == 0
    assert tick_at(lm, at("2030-01-10T08:30")) == 0  # nothing due until the afternoon check

    lm.ha.delete_times_out = True  # the real lock did this: timed out, still deleted
    assert tick_at(lm, at("2030-01-15T10:01")) == 1
    assert lm.ha.codes["lock.a"] == {"Master": "9999"}
    assert lock_row(lm)["fail_count"] == 0
    kinds = [e["kind"] for e in lm.db.query("SELECT kind FROM events")]
    assert "code.added" in kinds and "code.removed" in kinds


def test_extension_keeps_the_code_and_moves_removal(lm):
    lm.s.upsert_reservation(res())
    tick_at(lm, at("2030-01-10T08:01"))
    lm.s.upsert_reservation(res(departure="2030-01-20"))  # webhook: extended
    assert lock_row(lm)["next_check_at"] is None
    tick_at(lm, at("2030-01-15T10:01"))
    assert "HA-Ann Lee" in lm.ha.codes["lock.a"]


def test_cancellation_removes_code_right_away(lm):
    lm.s.upsert_reservation(res())
    tick_at(lm, at("2030-01-10T08:01"))
    lm.s.upsert_reservation(res(status="cancelled"))
    tick_at(lm, at("2030-01-10T08:10"))
    assert "HA-Ann Lee" not in lm.ha.codes["lock.a"]


def test_failed_add_alerts_once_and_retries(lm):
    lm.s.upsert_reservation(res())
    lm.ha.broken_add = {"HA-Ann Lee"}
    tick_at(lm, at("2030-01-10T08:01"))
    row = lock_row(lm)
    assert row["fail_count"] == 1 and "missing HA-Ann Lee" in row["last_error"]
    assert row["next_check_at"].startswith("2030-01-10T13:16")  # 15 minutes later, in UTC
    assert len(lock_alerts(lm)) == 1

    tick_at(lm, at("2030-01-10T08:17"))
    assert lock_row(lm)["fail_count"] == 2 and len(lock_alerts(lm)) == 1  # no repeat alert

    lm.ha.broken_add = set()
    tick_at(lm, at("2030-01-10T08:33"))
    assert lock_row(lm)["fail_count"] == 0 and "HA-Ann Lee" in lm.ha.codes["lock.a"]


def test_final_check_without_backup_alerts_staff_only(lm):
    lm.s.upsert_reservation(res())
    lm.ha.broken_add = {"HA-Ann Lee"}
    tick_at(lm, at("2030-01-10T08:01"))
    tick_at(lm, at("2030-01-10T15:00"))
    notice = lm.db.one("SELECT * FROM guest_notices")
    assert notice["delivered"] == 0 and lm.hostaway.sent == []
    assert any("NOT confirmed" in title for title, _ in lm.ha.notes)


def test_final_check_sends_backup_code_and_rotates_it_after_checkout(lm):
    lm.db.set_setting("backup_codes_enabled", "1")
    lm.db.set_setting("guest_messages_enabled", "1")
    lm.s.upsert_reservation(res())
    lm.ha.broken_add = {"HA-Ann Lee"}
    tick_at(lm, at("2030-01-10T08:01"))
    backup = lm.db.one("SELECT backup_code FROM properties")["backup_code"]
    assert lm.ha.codes["lock.a"][BACKUP_NAME] == backup

    tick_at(lm, at("2030-01-10T15:00"))
    tick_at(lm, at("2030-01-10T15:20"))
    assert len(lm.hostaway.sent) == 1  # once per reservation
    rid, body = lm.hostaway.sent[0]
    assert rid == 1 and backup in body and "Ann" in body

    tick_at(lm, at("2030-01-15T10:01"))
    new = lm.db.one("SELECT backup_code, backup_used_by FROM properties")
    assert new["backup_code"] != backup and new["backup_used_by"] is None
    tick_at(lm, at("2030-01-15T10:02"))
    assert lm.ha.codes["lock.a"][BACKUP_NAME] == new["backup_code"]


def test_unreadable_lock_at_final_check_triggers_fallback(lm):
    lm.s.upsert_reservation(res())
    lm.ha.read_fails = True
    tick_at(lm, at("2030-01-10T15:00"))
    assert lock_row(lm)["fail_count"] == 1
    assert lm.db.one("SELECT * FROM guest_notices") is not None


def test_homes_without_automation_are_left_alone(lm):
    lm.db.execute("UPDATE properties SET lock_automation = 0")
    lm.s.upsert_reservation(res())
    assert tick_at(lm, at("2030-01-10T08:01")) == 0
    assert "HA-Ann Lee" not in lm.ha.codes["lock.a"]


def test_change_during_reconcile_is_not_lost(lm):
    # A cancellation arrives by webhook while the (slow) add call is in flight.
    lm.s.upsert_reservation(res())
    lm.ha.on_add = lambda: lm.s.upsert_reservation(res(status="cancelled"))
    tick_at(lm, at("2030-01-10T08:01"))
    assert lock_row(lm)["next_check_at"] is None  # the reset survived the end of the reconcile
    lm.ha.on_add = None
    tick_at(lm, at("2030-01-10T08:06"))
    assert "HA-Ann Lee" not in lm.ha.codes["lock.a"]


def test_moved_reservation_follows_the_guest(lm):
    pid = lm.db.execute("INSERT INTO properties(hostaway_listing_id, hostaway_name, name, lock_automation) "
                        "VALUES(200, 'Oak 2', 'Oak 2', 1)")
    lm.db.execute("INSERT INTO locks(entity_id, name, state, property_id, match_source) "
                  "VALUES('lock.b', 'Oak front', 'locked', ?, 'manual')", (pid,))
    lm.ha.codes["lock.b"] = {}
    lm.s.upsert_reservation(res())
    tick_at(lm, at("2030-01-10T08:01"))
    assert "HA-Ann Lee" in lm.ha.codes["lock.a"]

    moved = res()
    moved["listingMapId"] = 200
    assert lm.s.upsert_reservation(moved) == ["moved"]
    assert tick_at(lm, at("2030-01-10T08:10")) == 2  # both homes looked at again
    assert "HA-Ann Lee" not in lm.ha.codes["lock.a"] and lm.ha.codes["lock.b"] == {"HA-Ann Lee": "4821"}


def test_switched_off_home_keeps_current_guest_code_then_cleans_up(lm):
    lm.db.set_setting("backup_codes_enabled", "1")
    lm.s.upsert_reservation(res())
    lm.s.upsert_reservation(res(id=2, arrival="2030-01-16", departure="2030-01-20", code="7777"))
    tick_at(lm, at("2030-01-10T08:01"))
    assert set(lm.ha.codes["lock.a"]) == {"Master", "HA-Ann Lee", BACKUP_NAME}

    lm.db.execute("UPDATE properties SET lock_automation = 0")
    lm.db.execute("UPDATE locks SET next_check_at = NULL")  # what saving the Properties page does
    tick_at(lm, at("2030-01-10T09:00"))
    assert set(lm.ha.codes["lock.a"]) == {"Master", "HA-Ann Lee"}  # guest keeps their code, backup goes
    tick_at(lm, at("2030-01-15T10:01"))
    assert lm.ha.codes["lock.a"] == {"Master": "9999"}  # removed at checkout
    tick_at(lm, at("2030-01-16T08:01"))
    assert lm.ha.codes["lock.a"] == {"Master": "9999"}  # nothing new is added
    assert not [t for t, _ in lm.ha.notes if "NOT confirmed" in t]  # and no guest fallback


def test_backup_code_never_equals_a_code_already_in_the_lock(lm, monkeypatch):
    lm.db.set_setting("backup_codes_enabled", "1")
    picks = iter([8999, 1233])  # randbelow(9000) + 1000: first "9999" (= Master), then "2233"
    monkeypatch.setattr("app.locks.secrets.randbelow", lambda n: next(picks))
    tick_at(lm, at("2030-01-10T08:01"))  # lock never read before: Master's value slips through once
    assert lm.db.one("SELECT backup_code FROM properties")["backup_code"] is None  # ...and is caught
    tick_at(lm, at("2030-01-10T08:06"))
    assert lm.db.one("SELECT backup_code FROM properties")["backup_code"] == "2233"
    assert lm.ha.codes["lock.a"] == {"Master": "9999", BACKUP_NAME: "2233"}


def test_code_hostaway_already_wrote_is_left_alone_and_logged_once(lm):
    lm.s.upsert_reservation(res())
    lm.ha.codes["lock.a"]["Ann Lee"] = "4821"  # Hostaway put it in
    tick_at(lm, at("2030-01-10T08:01"))
    tick_at(lm, at("2030-01-10T14:01"))
    assert lm.ha.codes["lock.a"] == {"Master": "9999", "Ann Lee": "4821"}
    kinds = [e["kind"] for e in lm.db.query("SELECT kind FROM events")]
    assert kinds.count("code.present") == 1 and "code.added" not in kinds
    tick_at(lm, at("2030-01-10T15:00"))  # final check: code counts as confirmed
    assert lm.db.one("SELECT * FROM guest_notices") is None


def test_alerts_when_hostaway_leaves_a_departed_guest_code(lm):
    lm.s.upsert_reservation(res())
    lm.ha.codes["lock.a"]["Ann Lee"] = "4821"
    tick_at(lm, at("2030-01-10T08:01"))

    def stale_alerts():
        return [m for t, m in lm.ha.notes if t.startswith("Old guest code")]

    tick_at(lm, at("2030-01-15T10:01"))  # checkout: Hostaway still has time to remove it
    assert stale_alerts() == []
    tick_at(lm, at("2030-01-15T12:01"))
    tick_at(lm, at("2030-01-15T12:30"))
    assert len(stale_alerts()) == 1 and '"Ann Lee"' in stale_alerts()[0]
    assert lm.ha.codes["lock.a"]["Ann Lee"] == "4821"  # reported, never deleted by us


def test_no_stale_alert_when_hostaway_removed_it(lm):
    lm.s.upsert_reservation(res())
    lm.ha.codes["lock.a"]["Ann Lee"] = "4821"
    tick_at(lm, at("2030-01-10T08:01"))
    del lm.ha.codes["lock.a"]["Ann Lee"]
    tick_at(lm, at("2030-01-15T12:01"))
    assert not [t for t, _ in lm.ha.notes if t.startswith("Old guest code")]


def test_daily_report_once_a_day(lm):
    lm.s.upsert_reservation(res())
    tick_at(lm, at("2030-01-10T06:30"))
    assert not any(t.startswith("Arrivals") for t, _ in lm.ha.notes)
    tick_at(lm, at("2030-01-10T07:05"))
    tick_at(lm, at("2030-01-10T07:10"))
    reports = [m for t, m in lm.ha.notes if t.startswith("Arrivals")]
    assert len(reports) == 1 and "Maple 1" in reports[0] and "1 arrivals today" in reports[0]


def test_staff_codes_added_to_automated_locks_and_removed_when_inactive(lm):
    lm.db.execute("INSERT INTO staff(name, code, active) VALUES('Cleaner', '2468', 1)")
    lm.s.upsert_reservation(res())
    tick_at(lm, at("2030-01-10T08:01"))
    assert lm.ha.codes["lock.a"]["Cleaner"] == "2468"
    assert lm.ha.codes["lock.a"]["HA-Ann Lee"] == "4821"

    lm.db.execute("UPDATE staff SET active = 0 WHERE name = 'Cleaner'")
    lm.db.execute("UPDATE locks SET next_check_at = NULL")
    tick_at(lm, at("2030-01-10T08:20"))
    assert "Cleaner" not in lm.ha.codes["lock.a"]
    assert "HA-Ann Lee" in lm.ha.codes["lock.a"]


def test_alert_recipients_are_notified(lm):
    lm.db.execute(
        "INSERT INTO alert_recipients(name, target, active) VALUES('Kurt phone', 'notify.mobile_app_kurt', 1)"
    )
    lm.s.upsert_reservation(res())
    lm.ha.broken_add = {"HA-Ann Lee"}
    tick_at(lm, at("2030-01-10T08:01"))
    assert any(t == "via:notify.mobile_app_kurt" for t, _ in lm.ha.notes)


def test_staff_codes_survive_when_home_automation_is_switched_off(lm):
    lm.db.execute("INSERT INTO staff(name, code, active) VALUES('Cleaner', '2468', 1)")
    lm.s.upsert_reservation(res())
    tick_at(lm, at("2030-01-10T08:01"))
    lm.db.execute("UPDATE properties SET lock_automation = 0")
    lm.db.execute("UPDATE locks SET next_check_at = NULL")
    tick_at(lm, at("2030-01-10T09:00"))
    assert lm.ha.codes["lock.a"]["Cleaner"] == "2468"  # cleaners keep access
    assert "HA-Ann Lee" in lm.ha.codes["lock.a"]


def test_staff_save_rejects_bad_input_without_dropping_anyone():
    from fastapi.testclient import TestClient
    from app.web import create_app

    db = DB(":memory:")
    s = Syncer(db, SETTINGS, FakeHostaway(), FakeLock())
    sid = db.execute("INSERT INTO staff(name, code, active) VALUES('Cleaner', '2468', 1)")
    client = TestClient(create_app(s), follow_redirects=False)

    r = client.post("/staff-save", data={f"name_{sid}": "Cleaner", f"code_{sid}": "24x8", f"active_{sid}": "on"})
    assert r.status_code == 400
    assert db.one("SELECT active, code FROM staff") == {"active": 1, "code": "2468"}  # untouched

    r = client.post("/staff-save", data={f"name_{sid}": "HA-Bob", f"code_{sid}": "2468", f"active_{sid}": "on"})
    assert r.status_code == 400

    r = client.post("/staff-save", data={f"name_{sid}": "Cleaner", f"code_{sid}": "1357", f"active_{sid}": "on",
                                         "name_new": "Handyman", "code_new": "9753", "active_new": "on"})
    assert r.status_code == 303
    assert {x["name"]: x["code"] for x in db.query("SELECT * FROM staff")} == {"Cleaner": "1357", "Handyman": "9753"}


def test_legacy_id_named_slot_is_replaced_by_guest_name(lm):
    lm.ha.codes["lock.a"]["HA-1"] = "4821"  # written by the previous version
    lm.s.upsert_reservation(res())
    tick_at(lm, at("2030-01-10T08:01"))
    assert lm.ha.codes["lock.a"] == {"Master": "9999", "HA-Ann Lee": "4821"}


def test_staff_rename_removes_old_name_from_locks_then_drops_row(lm):
    from fastapi.testclient import TestClient
    from app.web import create_app

    sid = lm.db.execute("INSERT INTO staff(name, code, active) VALUES('José', '2468', 1)")
    lm.s.upsert_reservation(res())
    tick_at(lm, at("2030-01-10T08:01"))
    assert lm.ha.codes["lock.a"]["José"] == "2468"

    client = TestClient(create_app(lm.s), follow_redirects=False)
    assert client.post("/staff-save", data={f"name_{sid}": "Jose R", f"code_{sid}": "2468",
                                            f"active_{sid}": "on"}).status_code == 303
    rows = {r["name"]: r["active"] for r in lm.db.query("SELECT * FROM staff")}
    assert rows == {"Jose R": 1, "José": 0}  # old name kept (inactive) so the lock can drop it
    tick_at(lm, at("2030-01-10T08:20"))
    assert "José" not in lm.ha.codes["lock.a"] and lm.ha.codes["lock.a"]["Jose R"] == "2468"

    client.post("/staff-save", data={f"name_{sid}": "Jose R", f"code_{sid}": "2468", f"active_{sid}": "on"})
    assert [r["name"] for r in lm.db.query("SELECT name FROM staff")] == ["Jose R"]  # tombstone dropped


def test_one_broken_recipient_does_not_block_the_others():
    import httpx
    from app.ha import HAClient, HAError

    calls = []

    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(500, text="x") if "bad" in req.url.path else httpx.Response(200, json=[])

    ha = HAClient("http://ha", "t", http=httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                                           base_url="http://ha"))
    with pytest.raises(HAError):
        asyncio.run(ha.notify("t", "m", services=["notify.bad", "notify.good"]))
    assert "/api/services/notify/good" in calls


def test_same_name_guests_keep_stable_slots_and_nobody_loses_a_code_mid_stay(lm):
    lm.s.upsert_reservation(res(id=1, arrival="2030-01-10", departure="2030-01-12", code="1111"))
    lm.s.upsert_reservation(res(id=2, arrival="2030-01-11", departure="2030-01-14", code="2222"))
    tick_at(lm, at("2030-01-11T08:01"))
    assert lm.ha.codes["lock.a"] == {"Master": "9999", "HA-Ann Lee": "1111", "HA-Ann Lee 2": "2222"}

    removed = []
    real_delete = lm.ha.delete_code

    async def spy(entity_id, name):
        removed.append(name)
        await real_delete(entity_id, name)

    lm.ha.delete_code = spy
    tick_at(lm, at("2030-01-12T10:01"))  # guest 1 leaves; guest 2 is still in the house
    assert removed == ["HA-Ann Lee"]  # guest 2's slot was never touched
    assert lm.ha.codes["lock.a"] == {"Master": "9999", "HA-Ann Lee 2": "2222"}


def test_recipients_must_be_notify_services_and_bad_input_saves_nothing():
    from fastapi.testclient import TestClient
    from app.web import create_app

    db = DB(":memory:")
    s = Syncer(db, SETTINGS, FakeHostaway(), FakeLock())
    rid = db.execute("INSERT INTO alert_recipients(name, target, active) VALUES('Kurt', 'notify.kurt', 1)")
    client = TestClient(create_app(s), follow_redirects=False)

    r = client.post("/recipients-save", data={f"rname_{rid}": "Kurt", f"rtarget_{rid}": "notify.new",
                                              "rname_new": "Mike", "rtarget_new": "mike@example.com"})
    assert r.status_code == 400  # an email address would be silently skipped when alerting
    assert db.one("SELECT target FROM alert_recipients")["target"] == "notify.kurt"  # nothing half-saved

    r = client.post("/recipients-save", data={f"rname_{rid}": "Kurt", f"rtarget_{rid}": "notify.kurt",
                                              "ractive_" + str(rid): "on",
                                              "rname_new": "Mike", "rtarget_new": "notify.mike"})
    assert r.status_code == 303 and db.one("SELECT COUNT(*) n FROM alert_recipients")["n"] == 2


def test_staff_duplicate_codes_and_names_are_refused():
    from fastapi.testclient import TestClient
    from app.web import create_app

    db = DB(":memory:")
    s = Syncer(db, SETTINGS, FakeHostaway(), FakeLock())
    a = db.execute("INSERT INTO staff(name, code, active) VALUES('Cleaner', '2468', 1)")
    db.execute("INSERT INTO staff(name, code, active) VALUES('Old', '1111', 0)")
    client = TestClient(create_app(s), follow_redirects=False)

    same_code = client.post("/staff-save", data={f"name_{a}": "Cleaner", f"code_{a}": "2468", f"active_{a}": "on",
                                                 "name_new": "Handyman", "code_new": "2468", "active_new": "on"})
    assert same_code.status_code == 400 and "more than one" in same_code.text
    same_name = client.post("/staff-save", data={f"name_{a}": "Cleaner", f"code_{a}": "2468", f"active_{a}": "on",
                                                 "name_new": "Old", "code_new": "9999", "active_new": "on"})
    assert same_name.status_code == 400 and "already in the list" in same_name.text
    assert db.one("SELECT COUNT(*) n FROM staff")["n"] == 2
