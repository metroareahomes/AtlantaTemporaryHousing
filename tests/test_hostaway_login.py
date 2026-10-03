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


def hook_client(existing, post_status=200):
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        if req.url.path.endswith("/accessTokens"):
            return httpx.Response(200, json={"access_token": "t"})
        if req.method == "GET":
            return httpx.Response(200, json={"status": "success", "result": existing})
        if req.method == "PUT":
            return httpx.Response(200, json={"status": "success", "result": {"id": 5, "isEnabled": 1}})
        if post_status != 200:
            return httpx.Response(post_status, text='{"message":"url is not reachable"}')
        return httpx.Response(200, json={"status": "success", "result": {"id": 9, "isEnabled": 1}})

    c = HostawayClient("1", "k", http=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://x"))
    return c, calls


URL = "https://ha.example.com/api/webhook/secret"


def test_registering_twice_is_not_an_error():
    c, calls = hook_client([{"id": 5, "url": URL, "isEnabled": 1}])
    assert asyncio.run(c.register_webhook(URL))["id"] == 5
    assert ("POST", "/webhooks/unifiedWebhooks") not in calls  # nothing new created


def test_a_disabled_hook_with_our_url_is_switched_back_on():
    c, calls = hook_client([{"id": 5, "url": URL, "isEnabled": 0}])
    assert asyncio.run(c.register_webhook(URL))["isEnabled"] == 1
    assert ("PUT", "/webhooks/unifiedWebhooks/5") in calls


def test_hostaways_reason_for_a_refusal_is_shown():
    c, _ = hook_client([], post_status=400)
    with pytest.raises(HostawayError, match="HTTP 400 - .*url is not reachable"):
        asyncio.run(c.register_webhook(URL))


def test_refusal_while_another_webhook_exists_says_so_without_leaking_its_secret():
    c, _ = hook_client([{"id": 1, "url": "https://other.example.org/hook/abc123secret", "isEnabled": 1}],
                       post_status=400)
    with pytest.raises(HostawayError) as err:
        asyncio.run(c.register_webhook(URL))
    assert "already has 1 unified webhook" in str(err.value) and "other.example.org" in str(err.value)
    assert "abc123secret" not in str(err.value)
