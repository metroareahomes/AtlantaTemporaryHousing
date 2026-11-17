"""Only opted-in Lyric entities enter the app; discovery reads HA's cache only."""
import asyncio
import json
import re

import httpx
import pytest
from jinja2 import Environment
from fastapi.testclient import TestClient

from app.ha import HAClient, HAError
from app.web import create_app
from test_sync import syncer
from test_climate import cm, at, tick

CREEK = "climate.livingroom_thermostat"


def state(eid):
    return {"entity_id": eid, "state": "cool",
            "attributes": {"friendly_name": eid, "temperature": 72, "current_temperature": 74}}


def cached_client(selected, *, resideo=None, lyric=None):
    calls = []
    resideo = ["climate.resideo_main"] if resideo is None else resideo
    lyric = [CREEK, "climate.other_lyric", "climate.disabled_lyric"] if lyric is None else lyric
    env = Environment()
    env.tests["match"] = lambda value, pattern: bool(re.match(pattern, value))
    def handler(req):
        calls.append((req.method, req.url.path))
        if req.url.path == "/api/template":
            template = json.loads(req.content)["template"]
            result = env.from_string(template).render(
                integration_entities=lambda name: resideo if name == "resideo" else lyric)
            return httpx.Response(200, json=json.loads(result))
        assert req.url.path == "/api/states"  # no update_entity service or direct cloud calls
        return httpx.Response(200, json=[state("climate.resideo_main"), state(CREEK),
                                        state("climate.other_lyric"), state("climate.unrelated")])
    ha = HAClient("http://ha", "t", extra_climate_entities=lambda: selected,
                  http=httpx.AsyncClient(base_url="http://ha", transport=httpx.MockTransport(handler)))
    return ha, calls


def test_selected_creekside_only_and_other_integrations_excluded():
    ha, calls = cached_client([CREEK])
    result = asyncio.run(ha.climate_states())
    assert [s["entity_id"] for s in result] == ["climate.resideo_main", CREEK]
    assert result[0]["manual_assignment_required"] is False
    assert result[1]["manual_assignment_required"] is True
    assert calls == [("POST", "/api/template"), ("GET", "/api/states")]


def test_default_selection_does_not_include_lyric():
    ha, _ = cached_client([])
    assert [s["entity_id"] for s in asyncio.run(ha.climate_states())] == ["climate.resideo_main"]


def test_allowlist_must_also_belong_to_lyric():
    ha, _ = cached_client(["climate.unrelated"])
    assert [s["entity_id"] for s in asyncio.run(ha.climate_states())] == ["climate.resideo_main"]


def test_disabled_lyric_has_no_live_entity_to_import():
    ha, _ = cached_client(["climate.disabled_lyric"])
    assert [s["entity_id"] for s in asyncio.run(ha.climate_states())] == ["climate.resideo_main"]


def test_empty_resideo_discovery_never_falls_back_to_every_thermostat():
    ha, _ = cached_client([], resideo=[])
    assert asyncio.run(ha.climate_states()) == []


def test_creekside_can_be_selected_when_no_resideo_entities_are_present():
    ha, _ = cached_client([CREEK], resideo=[])
    assert [s["entity_id"] for s in asyncio.run(ha.climate_states())] == [CREEK]


def test_malformed_template_response_stops_discovery():
    def handler(req):
        return httpx.Response(200, json={"unexpected": True})
    ha = HAClient("http://ha", "t", http=httpx.AsyncClient(
        base_url="http://ha", transport=httpx.MockTransport(handler)))
    with pytest.raises(HAError):
        asyncio.run(ha.climate_states())


def test_selected_thermostat_requires_manual_assignment_before_control(cm):
    cm.ha.climate[CREEK] = {**cm.ha.climate["climate.maple"], "entity_id": CREEK,
                           "name": "Maple 1", "manual_assignment_required": True}
    tick(cm, at("2030-06-10T12:00"))
    creek = cm.db.one("SELECT * FROM thermostats WHERE entity_id=?", (CREEK,))
    assert creek["property_id"] is None
    assert creek["match_source"] is None
    assert all(call[0] != CREEK for call in cm.ha.sets)
    home = cm.db.one("SELECT id FROM properties")["id"]
    cm.assign(creek["id"], home)
    tick(cm, at("2030-06-10T12:05"))
    assert any(call[0] == CREEK for call in cm.ha.sets)


def test_saving_selection_is_normalized_but_does_not_assign_or_call_ha(syncer):
    client = TestClient(create_app(syncer), follow_redirects=False)
    response = client.post("/additional-thermostats-save",
                           data={"lyric_climate_entities": f"{CREEK}, {CREEK}"})
    assert response.status_code == 303
    assert syncer.db.get_setting("lyric_climate_entities") == CREEK
    assert syncer.db.one("SELECT * FROM thermostats") is None


@pytest.mark.parametrize("bad", ["notify.phone", "climate.bad-name", "climate.x {{bad}}",
                                  "climate.x\n<script>", " ".join(f"climate.x{i}" for i in range(11))])
def test_bad_selection_does_not_save_partial_changes(syncer, bad):
    syncer.db.set_setting("lyric_climate_entities", CREEK)
    response = TestClient(create_app(syncer)).post(
        "/additional-thermostats-save", data={"lyric_climate_entities": bad})
    assert response.status_code == 400
    assert syncer.db.get_setting("lyric_climate_entities") == CREEK


def test_removing_selected_lyric_device_removes_its_control_row_only(syncer):
    syncer.db.set_setting("lyric_climate_entities", CREEK)
    syncer.db.execute("INSERT INTO thermostats(entity_id,name) VALUES(?,?)", (CREEK, "Creekside"))
    syncer.db.execute("INSERT INTO thermostats(entity_id,name) VALUES('climate.keep','Other home')")
    response = TestClient(create_app(syncer), follow_redirects=False).post(
        "/additional-thermostats-save", data={"lyric_climate_entities": ""})
    assert response.status_code == 303
    assert syncer.db.one("SELECT * FROM thermostats WHERE entity_id=?", (CREEK,)) is None
    assert syncer.db.one("SELECT * FROM thermostats WHERE entity_id='climate.keep'")
