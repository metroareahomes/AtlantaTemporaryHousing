import asyncio
import json

import httpx

from app.ha import HAClient


def client_recording(calls):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json=[])
    return HAClient("http://ha", "t", http=httpx.AsyncClient(base_url="http://ha",
                                                            transport=httpx.MockTransport(handler)))


def test_notify_omits_empty_notification_id():
    # HA rejects "notification_id": null with HTTP 400.
    calls = []
    asyncio.run(client_recording(calls).notify("T", "M"))
    assert calls == [("/api/services/persistent_notification/create", {"title": "T", "message": "M"})]


def test_climate_states_only_honeywell_entities():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/template":
            return httpx.Response(200, json=["climate.keep"])
        return httpx.Response(200, json=[
            {"entity_id": "climate.keep", "state": "cool", "attributes": {"friendly_name": "Keep",
                                                                         "current_temperature": 72}},
            {"entity_id": "climate.ghost", "state": "unavailable", "attributes": {"friendly_name": "Ghost"}},
            {"entity_id": "lock.maple", "state": "locked", "attributes": {}},
        ])

    ha = HAClient("http://ha", "t", http=httpx.AsyncClient(base_url="http://ha",
                                                          transport=httpx.MockTransport(handler)))
    states = asyncio.run(ha.climate_states())
    assert [s["entity_id"] for s in states] == ["climate.keep"]
    assert "/api/template" in calls


def test_notify_also_uses_notify_service():
    calls = []
    asyncio.run(client_recording(calls).notify("T", "M", service="notify.mobile_app_x", notification_id="k"))
    assert calls[0][1]["notification_id"] == "k"
    assert calls[1] == ("/api/services/notify/mobile_app_x", {"title": "T", "message": "M"})
