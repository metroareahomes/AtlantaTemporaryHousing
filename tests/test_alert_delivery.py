"""Regression tests: failed staff notifications must remain eligible for retry."""
import pytest

from app.db import DB
from app.locks import LockManager


class FakeHA:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    async def notify(self, title, message, *, services, notification_id):
        self.calls.append((title, message, services, notification_id))
        if self.fail:
            raise RuntimeError("mobile notification unavailable")


class FakeSyncer:
    def __init__(self, db, ha):
        self.db = db
        self.ha = ha
        self.hostaway = None


@pytest.mark.asyncio
async def test_failed_notification_does_not_suppress_retry():
    db = DB(":memory:")
    ha = FakeHA(fail=True)
    manager = LockManager(FakeSyncer(db, ha))
    await manager.alert("Guest access problem", "Check lock", key="lockfail:1:2030-01-10")
    assert db.one("SELECT * FROM alerts_sent WHERE key = ?", ("lockfail:1:2030-01-10",)) is None
    assert len(ha.calls) == 1
    assert db.one("SELECT * FROM events WHERE kind = 'alert.failed'") is not None

    ha.fail = False
    await manager.alert("Guest access problem", "Check lock", key="lockfail:1:2030-01-10")
    assert len(ha.calls) == 2
    assert db.one("SELECT * FROM alerts_sent WHERE key = ?", ("lockfail:1:2030-01-10",)) is not None

    await manager.alert("Guest access problem", "Check lock", key="lockfail:1:2030-01-10")
    assert len(ha.calls) == 2


@pytest.mark.asyncio
async def test_successful_notification_is_deduplicated():
    db = DB(":memory:")
    ha = FakeHA()
    manager = LockManager(FakeSyncer(db, ha))
    await manager.alert("Thermostat offline", "Check thermostat", key="therm:1")
    await manager.alert("Thermostat offline", "Check thermostat", key="therm:1")
    assert len(ha.calls) == 1


@pytest.mark.asyncio
async def test_unkeyed_failed_notification_does_not_crash():
    db = DB(":memory:")
    ha = FakeHA(fail=True)
    manager = LockManager(FakeSyncer(db, ha))
    await manager.alert("Test", "Message")
    assert len(ha.calls) == 1
    assert db.one("SELECT * FROM events WHERE kind = 'alert.failed'") is not None
