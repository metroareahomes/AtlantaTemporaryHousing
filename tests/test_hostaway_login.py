import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import DB
from app.hostaway import HostawayClient, HostawayError
from app.sync import Syncer
from app.web import create_app


def client(account="1", key="k", status=401):
    transport = httpx.MockTransport(lambda req: httpx.Response(status, json={}))
    return HostawayClient(account, key, http=httpx.AsyncClient(transport=transport, base_url="https://x"))


def test_empty_credentials_say_so_without_calling_hostaway():
    with pytest.raises(HostawayError, match="empty"):
        asyncio.run(client(account="", key="").listings())


def test_rejected_credentials_explain_where_to_fix_them():
    with pytest.raises(HostawayError, match="Configuration tab"):
        asyncio.run(client(status=401).listings())


def test_sync_button_shows_a_readable_page_not_a_500_trace():
    settings = Settings(hostaway_account_id="1", hostaway_api_key="x", ha_url="http://ha", ha_token="t",
                        db_path=":memory:", is_addon=False)

    class HA:
        async def schlage_locks(self):
            return []

    db = DB(":memory:")
    s = Syncer(db, settings, client(), HA())
    r = TestClient(create_app(s), follow_redirects=False).post("/sync")
    assert r.status_code == 502 and "Configuration tab" in r.text
    assert db.one("SELECT * FROM events WHERE kind = 'hostaway.error'") is not None
