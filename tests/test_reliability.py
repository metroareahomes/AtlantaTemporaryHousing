"""Failure/recovery coverage without touching production services."""
import asyncio
import json

import httpx
import pytest

from app.db import DB
from app.ha import HAClient
from app.hostaway import HostawayClient, HostawayError, HostawayMessageNotSent
from app.locks import LockManager
from test_alert_delivery import FakeSyncer
from test_locks import lm, at, res, norm
from test_sync import syncer


def run(coro):
    return asyncio.run(coro)


class DeliveryHA:
    def __init__(self):
        self.calls = []
        self.bad = {"notify.bad"}

    async def notify(self, title, message, *, services, notification_id):
        self.calls.append("dashboard")

    async def notify_mobile(self, title, message, target, key):
        self.calls.append(target)
        if target in self.bad:
            raise RuntimeError("offline")


def due(db, table):
    db.execute(f"UPDATE {table} SET next_attempt='2000-01-01T00:00:00+00:00'")


def test_partial_delivery_retries_only_failed_recipient_after_restart(tmp_path):
    path = str(tmp_path / "state.sqlite")
    db = DB(path)
    ha = DeliveryHA()
    db.set_setting("alert_service", "notify.good")
    db.execute("INSERT INTO alert_recipients(name,target) VALUES('Bad','notify.bad')")
    manager = LockManager(FakeSyncer(db, ha))
    run(manager.alert("Problem", "Check", key="problem"))
    assert ha.calls == ["dashboard", "notify.good", "notify.bad"]
    assert not db.one("SELECT * FROM alerts_sent")
    run(manager.retry_alerts())
    assert len(ha.calls) == 3  # backoff
    restarted = LockManager(FakeSyncer(DB(path), ha))
    due(db, "alert_deliveries")
    ha.bad.clear()
    run(restarted.retry_alerts())
    assert ha.calls == ["dashboard", "notify.good", "notify.bad", "notify.bad"]
    assert db.one("SELECT * FROM alerts_sent WHERE key='problem'")
    run(restarted.alert("Problem", "Check", key="problem"))
    assert len(ha.calls) == 4


def test_concurrent_alert_calls_are_serialized():
    db, ha = DB(":memory:"), DeliveryHA()
    manager = LockManager(FakeSyncer(db, ha))
    async def both():
        await asyncio.gather(*(manager.alert("T", "M", key="same") for _ in range(5)))
    run(both())
    assert ha.calls == ["dashboard"]


def test_persistent_failure_does_not_block_mobile_delivery():
    ha = DeliveryHA()
    async def broken(*args, **kwargs):
        raise RuntimeError("persistent service unavailable")
    ha.notify = broken
    db = DB(":memory:")
    db.set_setting("alert_service", "notify.good")
    run(LockManager(FakeSyncer(db, ha)).alert("T", "M", key="k"))
    assert ha.calls == ["notify.good"]
    assert db.one("SELECT delivered FROM alert_deliveries WHERE target='notify.good'")["delivered"] == 1


def hostaway(handler):
    c = HostawayClient("1", "k", load_token=lambda: "t",
                       http=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://x"))
    return c


def test_guest_post_server_error_is_not_replayed():
    calls = []
    def handler(req):
        calls.append(req.method)
        if req.method == "GET":
            return httpx.Response(200, json={"status": "success", "result": [{"id": 5, "reservationId": 1}]})
        return httpx.Response(500)
    with pytest.raises(HostawayError, match="uncertain"):
        run(hostaway(handler).send_guest_message(1, "backup"))
    assert calls == ["GET", "POST"]


def test_guest_post_timeout_is_not_replayed():
    calls = []
    def handler(req):
        calls.append(req.method)
        if req.method == "GET":
            return httpx.Response(200, json={"status": "success", "result": [{"id": 5, "reservationId": 1}]})
        raise httpx.ReadTimeout("lost response")
    with pytest.raises(httpx.ReadTimeout):
        run(hostaway(handler).send_guest_message(1, "backup"))
    assert calls == ["GET", "POST"]


def test_pre_send_failure_is_identified_as_safe_to_retry():
    def handler(req):
        return httpx.Response(200, json={"status": "success", "result": []})
    with pytest.raises(HostawayMessageNotSent):
        run(hostaway(handler).send_guest_message(1, "backup"))


def test_hostaway_get_transport_failure_recovers(monkeypatch):
    calls = []
    async def no_sleep(_):
        pass
    monkeypatch.setattr("app.hostaway.asyncio.sleep", no_sleep)
    def handler(req):
        calls.append(req.method)
        if len(calls) == 1:
            raise httpx.ConnectError("offline")
        return httpx.Response(200, json={"status": "success", "result": []})
    assert run(hostaway(handler).listings()) == []
    assert calls == ["GET", "GET"]


def test_webhook_failure_survives_and_retries(syncer):
    original = syncer.hostaway.reservation
    async def broken(_):
        raise RuntimeError("Hostaway unavailable")
    syncer.hostaway.reservation = broken
    payload = {"object": "reservation", "data": {"id": 1}}
    with pytest.raises(RuntimeError):
        run(syncer.handle_webhook(payload))
    assert syncer.db.one("SELECT * FROM webhook_pending WHERE reservation_id=1")
    syncer.hostaway.reservation = original
    due(syncer.db, "webhook_pending")
    run(syncer.retry_webhooks())
    assert syncer.db.one("SELECT * FROM reservations WHERE id=1")
    assert not syncer.db.one("SELECT * FROM webhook_pending")


def test_failed_full_sync_keeps_cached_reservations_and_success_time(syncer):
    run(syncer.sync_reservations())
    before = syncer.db.query("SELECT * FROM reservations")
    stamp = syncer.db.get_setting("last_sync_at")
    async def broken(_):
        raise RuntimeError("offline")
    syncer.hostaway.reservations = broken
    with pytest.raises(RuntimeError):
        run(syncer.sync_reservations())
    assert syncer.db.query("SELECT * FROM reservations") == before
    assert syncer.db.get_setting("last_sync_at") == stamp


def test_thermostat_wake_failure_does_not_drop_other_homes(syncer):
    calls = []
    syncer._thermostat_wake.update({1, 2})
    async def wake(pid):
        calls.append(pid)
        if pid == 1:
            raise RuntimeError("offline")
    syncer.on_thermostats = wake
    run(syncer.wake_thermostats())
    assert set(calls) == {1, 2}
    assert syncer._thermostat_wake == {1}


def setup_fallback(lm):
    lm.s.now = lambda: at("2030-01-10T15:15")
    lm.s.upsert_reservation(res())
    lm.db.set_setting("backup_codes_enabled", "1")
    lm.db.set_setting("guest_messages_enabled", "1")
    lm.db.execute("UPDATE properties SET backup_code='5555'")
    return lm.db.one("SELECT * FROM locks")


def test_backup_message_retries_known_pre_send_failure(lm):
    lock = setup_fallback(lm)
    original = lm.hostaway.send_guest_message
    async def broken(*args):
        raise HostawayMessageNotSent("no conversation yet")
    lm.hostaway.send_guest_message = broken
    run(lm.fallback(norm(), lock, "test"))
    assert lm.db.one("SELECT next_attempt FROM guest_notices")["next_attempt"]
    lm.hostaway.send_guest_message = original
    lm.s.now = lambda: at("2030-01-10T15:21")
    run(lm.retry_guest_messages())
    assert len(lm.hostaway.sent) == 1
    assert lm.db.one("SELECT delivered FROM guest_notices")["delivered"] == 1
    run(lm.retry_guest_messages())
    assert len(lm.hostaway.sent) == 1


def test_ambiguous_backup_message_is_not_resent_and_code_is_reserved(lm):
    lock = setup_fallback(lm)
    calls = []
    async def uncertain(*args):
        calls.append(args)
        raise httpx.ReadTimeout("response lost")
    lm.hostaway.send_guest_message = uncertain
    run(lm.fallback(norm(), lock, "test"))
    run(lm.fallback(norm(), lock, "test"))
    run(lm.retry_guest_messages())
    assert len(calls) == 1
    assert lm.db.one("SELECT backup_used_by FROM properties")["backup_used_by"] == 1
    assert lm.db.one("SELECT next_attempt FROM guest_notices")["next_attempt"] is None


def test_backup_retry_stops_after_checkout(lm):
    lock = setup_fallback(lm)
    async def broken(*args):
        raise HostawayMessageNotSent()
    lm.hostaway.send_guest_message = broken
    run(lm.fallback(norm(), lock, "test"))
    lm.s.now = lambda: at("2030-01-16T12:00")
    run(lm.retry_guest_messages())
    assert lm.db.one("SELECT next_attempt FROM guest_notices")["next_attempt"] is None


def test_mobile_notification_uses_stable_replacement_tag():
    payloads = []
    def handler(req):
        payloads.append(json.loads(req.content))
        return httpx.Response(200, json=[])
    ha = HAClient("http://ha", "t", http=httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ha"))
    run(ha.notify_mobile("T", "M", "notify.phone", "unique"))
    assert payloads == [{"title": "T", "message": "M", "data": {"tag": "unique"}}]
