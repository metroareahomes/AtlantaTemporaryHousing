"""Arrival checks follow the current reservation time, not fixed hours of the day."""
import asyncio
from datetime import timedelta

from app.db import DB
from app.locks import LockManager, add_time, desired_codes, next_check, DEFAULTS
from test_locks import lm, res, norm, at, tick_at, FakeLock, FakeHostaway, SETTINGS
from app.sync import Syncer


def run(coro):
    return asyncio.run(coro)


def test_morning_installations_retry_until_the_relative_final_check(lm):
    lm.s.upsert_reservation(res())
    lm.db.set_setting("final_check_minutes", "90")
    lm.db.set_setting("backup_codes_enabled", "1")
    lm.db.set_setting("guest_messages_enabled", "1")
    lm.ha.broken_add = {"HA-Ann Lee"}
    calls = []
    real_add = lm.ha.add_code
    async def counted(entity, name, code):
        if name == "HA-Ann Lee":
            calls.append(code)
        await real_add(entity, name, code)
    lm.ha.add_code = counted
    tick_at(lm, at("2030-01-10T08:00"))
    assert calls == ["4821"] and lm.hostaway.sent == []
    tick_at(lm, at("2030-01-10T08:16"))
    assert len(calls) == 2 and lm.hostaway.sent == []
    tick_at(lm, at("2030-01-10T14:29"))
    assert len(calls) == 3 and lm.hostaway.sent == []
    assert lm.db.one("SELECT next_check_at FROM locks")["next_check_at"] == "2030-01-10T19:30:00+00:00"
    tick_at(lm, at("2030-01-10T14:30"))
    assert len(calls) == 4 and len(lm.hostaway.sent) == 1
    lm.db.execute("UPDATE locks SET next_check_at=NULL")
    tick_at(lm, at("2030-01-10T14:50"))
    assert len(calls) == 4 and len(lm.hostaway.sent) == 1


def test_final_deadline_moves_with_checkin_not_fixed_afternoon_hour():
    cfg = dict(DEFAULTS, final_check_minutes=90, afternoon_check_hour=14)
    assert next_check([norm(check_in=18)], at("2030-01-10T14:00"), cfg) == at("2030-01-10T16:30")
    assert next_check([norm(check_in=9)], at("2030-01-10T07:00"), cfg) == at("2030-01-10T07:30")


def test_moving_arrival_moves_check_and_preserves_existing_access(lm):
    lm.s.upsert_reservation(res())
    lm.ha.codes["lock.a"]["HA-Ann Lee"] = "4821"  # written by the previous version
    lm.s.upsert_reservation(res(check_in=18))
    tick_at(lm, at("2030-01-10T12:00"))
    assert lm.ha.codes["lock.a"]["HA-Ann Lee"] == "4821"
    assert lm.db.one("SELECT next_check_at FROM locks")["next_check_at"] == "2030-01-10T22:00:00+00:00"
    assert lm.hostaway.sent == []


def test_change_inside_lead_window_wakes_running_scheduler(lm):
    lm.s.now = lambda: at("2030-01-10T14:00")
    lm.db.set_setting("add_hour", "23")
    lm.db.set_setting("early_lead_hours", "0")
    lm.s.on_locks = lm.wake_property
    lm.s.upsert_reservation(res(check_in=18))
    async def exercise():
        task = asyncio.create_task(lm.run())
        try:
            for _ in range(10):
                await asyncio.sleep(0)
            assert "HA-Ann Lee" not in lm.ha.codes["lock.a"]
            lm.s.upsert_reservation(res(check_in=14))
            for _ in range(30):
                await asyncio.sleep(0)
            assert lm.ha.codes["lock.a"]["HA-Ann Lee"] == "4821"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    run(exercise())


def test_checkin_moved_into_past_is_checked_even_more_than_six_hours_late(lm):
    lm.s.upsert_reservation(res(check_in=8))
    tick_at(lm, at("2030-01-10T18:00"))
    assert lm.ha.codes["lock.a"]["HA-Ann Lee"] == "4821"


def test_one_installation_attempt_and_one_backup_message(lm):
    lm.db.set_setting("backup_codes_enabled", "1")
    lm.db.set_setting("guest_messages_enabled", "1")
    lm.s.upsert_reservation(res())
    lm.ha.broken_add = {"HA-Ann Lee"}
    calls = []
    real_add = lm.ha.add_code
    async def counted(entity, name, code):
        if name == "HA-Ann Lee":
            calls.append(code)
        await real_add(entity, name, code)
    lm.ha.add_code = counted
    tick_at(lm, at("2030-01-10T15:00"))
    for clock in ("2030-01-10T15:20", "2030-01-11T06:01"):
        lm.db.execute("UPDATE locks SET next_check_at=NULL")
        tick_at(lm, at(clock))
    assert calls == ["4821"]
    assert len(lm.hostaway.sent) == 1


def test_successful_hostaway_code_does_not_get_an_extra_write_or_backup(lm):
    lm.s.upsert_reservation(res())
    lm.ha.codes["lock.a"]["Hostaway guest"] = "4821"
    tick_at(lm, at("2030-01-10T15:00"))
    assert lm.ha.codes["lock.a"] == {"Master": "9999", "Hostaway guest": "4821"}
    assert lm.hostaway.sent == []
    assert lm.db.one("SELECT COUNT(*) n FROM guest_access_checks")["n"] == 1


def test_timeout_that_installed_the_code_is_verified_before_backup(lm):
    lm.s.upsert_reservation(res())
    real_add = lm.ha.add_code
    async def timed_out(*args):
        await real_add(*args)
        raise TimeoutError()
    lm.ha.add_code = timed_out
    tick_at(lm, at("2030-01-10T15:00"))
    assert lm.ha.codes["lock.a"]["HA-Ann Lee"] == "4821"
    assert lm.hostaway.sent == []


def test_attempt_budget_survives_manager_restart(lm):
    lm.s.upsert_reservation(res())
    lm.ha.broken_add = {"HA-Ann Lee"}
    tick_at(lm, at("2030-01-10T15:00"))
    restarted = LockManager(lm.s)
    restarted.verify_delay = restarted.gap = 0
    lm.ha.broken_add = set()
    lm.db.execute("UPDATE locks SET next_check_at=NULL")
    tick_at(restarted, at("2030-01-10T15:30"))
    assert "HA-Ann Lee" not in lm.ha.codes["lock.a"]
    assert lm.db.one("SELECT COUNT(*) n FROM guest_code_attempts")["n"] == 1


def test_changed_guest_code_gets_one_new_attempt(lm):
    lm.s.upsert_reservation(res())
    lm.ha.broken_add = {"HA-Ann Lee"}
    tick_at(lm, at("2030-01-10T15:00"))
    lm.ha.broken_add = set()
    lm.s.upsert_reservation(res(code="6677"))
    tick_at(lm, at("2030-01-10T15:10"))
    assert lm.ha.codes["lock.a"]["HA-Ann Lee"] == "6677"
    assert lm.db.one("SELECT COUNT(*) n FROM guest_code_attempts")["n"] == 2


def test_cancelled_or_checked_out_booking_does_not_receive_backup(lm):
    lm.db.set_setting("backup_codes_enabled", "1")
    lm.db.set_setting("guest_messages_enabled", "1")
    lm.s.upsert_reservation(res(status="cancelled"))
    tick_at(lm, at("2030-01-10T16:00"))
    lm.s.upsert_reservation(res())
    tick_at(lm, at("2030-01-15T10:01"))
    assert lm.hostaway.sent == []


def test_cancellation_during_write_does_not_send_backup(lm):
    lm.s.upsert_reservation(res())
    lm.ha.broken_add = {"HA-Ann Lee"}
    lm.ha.on_add = lambda: lm.s.upsert_reservation(res(status="cancelled"))
    tick_at(lm, at("2030-01-10T15:00"))
    assert lm.hostaway.sent == []
    assert not lm.db.one("SELECT * FROM guest_notices")


def test_cancel_or_move_during_a_slow_read_uses_current_reservation(lm):
    lm.s.upsert_reservation(res())
    original = lm.ha.get_codes
    async def changed(entity):
        result = await original(entity)
        lm.s.upsert_reservation(res(arrival="2030-01-12"))
        return result
    lm.ha.get_codes = changed
    tick_at(lm, at("2030-01-10T15:00"))
    assert "HA-Ann Lee" not in lm.ha.codes["lock.a"]
    assert lm.hostaway.sent == []


def test_automation_switched_off_during_read_stops_guest_actions(lm):
    lm.s.upsert_reservation(res())
    original = lm.ha.get_codes
    async def disabled(entity):
        result = await original(entity)
        lm.db.execute("UPDATE properties SET lock_automation=0")
        return result
    lm.ha.get_codes = disabled
    tick_at(lm, at("2030-01-10T15:00"))
    assert lm.ha.codes["lock.a"] == {"Master": "9999"}
    assert lm.hostaway.sent == []


def test_setting_is_validated_before_any_settings_are_written(lm):
    from fastapi.testclient import TestClient
    from app.web import create_app
    client = TestClient(create_app(lm.s, lm))
    lm.db.set_setting("final_check_minutes", "60")
    lm.db.set_setting("add_hour", "8")
    for value in ("-1", "1441", "hello"):
        response = client.post("/settings-save", data={"final_check_minutes": value, "add_hour": "9"})
        assert response.status_code == 400
        assert lm.db.get_int("final_check_minutes", 0) == 60
        assert lm.db.get_int("add_hour", 0) == 8
