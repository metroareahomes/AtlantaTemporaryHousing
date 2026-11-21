"""Home Assistant REST client (Supervisor proxy inside the add-on, direct URL locally)."""
import json
import re
from collections.abc import Callable
from typing import Any

import httpx

# Schlage cloud calls are slow; a delete once took over two minutes and still succeeded.
SCHLAGE_TIMEOUT = httpx.Timeout(30, read=240)

WEBHOOK_AUTOMATION_ID = "stay_automation_hostaway_webhook"


class HAError(Exception):
    pass


class HAClient:
    def __init__(self, base_url: str, token: str, http: httpx.AsyncClient | None = None, *,
                 extra_climate_entities: Callable[[], list[str]] = lambda: []):
        self._extra_climate_entities = extra_climate_entities
        self._http = http or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def _post(self, path: str, payload: dict[str, Any], **kwargs: Any) -> Any:
        resp = await self._http.post(path, json=payload, **kwargs)
        if resp.status_code >= 400:
            raise HAError(f"POST {path}: HTTP {resp.status_code} {resp.text[:200]}")
        return resp.json() if resp.content else None

    async def ping(self) -> bool:
        resp = await self._http.get("/api/")
        return resp.status_code == 200

    async def schlage_locks(self) -> list[dict[str, Any]]:
        """Lock entities that belong to the Schlage integration, with name and state."""
        entity_ids = await self._post(
            "/api/template", {"template": "{{ integration_entities('schlage') | select('match', 'lock[.]') | list | tojson }}"}
        )
        wanted = set(entity_ids if isinstance(entity_ids, list) else [])
        resp = await self._http.get("/api/states")
        resp.raise_for_status()
        return [
            {
                "entity_id": s["entity_id"],
                "name": s["attributes"].get("friendly_name", s["entity_id"]),
                "state": s["state"],
            }
            for s in resp.json()
            if s["entity_id"] in wanted
        ]

    async def lock_state(self, entity_id: str) -> str | None:
        """Read one HA cached entity state; never operate or force-refresh the lock."""
        from urllib.parse import quote
        response = await self._http.get('/api/states/' + quote(entity_id, safe=''))
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise HAError('Could not read the lock state from Home Assistant')
        data = response.json()
        if data.get('entity_id') != entity_id or not isinstance(data.get('state'), str):
            raise HAError('Invalid lock state response from Home Assistant')
        return data['state']

    @staticmethod
    def _climate_from_state(s: dict[str, Any]) -> dict[str, Any]:
        a = s.get("attributes") or {}
        return {
            "entity_id": s["entity_id"],
            "name": a.get("friendly_name", s["entity_id"]),
            "state": s["state"],
            "current_temp": a.get("current_temperature"),
            "target_temp": a.get("temperature"),
            "target_low": a.get("target_temp_low"),
            "target_high": a.get("target_temp_high"),
            "hvac_action": a.get("hvac_action"),
            "fan_mode": a.get("fan_mode"),
            "min_temp": a.get("min_temp"),
            "max_temp": a.get("max_temp"),
        }

    async def lyric_entity_ids(self) -> set[str]:
        """Read Lyric entity membership from HA locally; do not refresh the cloud."""
        raw = await self._post("/api/template", {
            "template": "{{ integration_entities('lyric') | select('match', 'climate[.]') | list | tojson }}"})
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise HAError("Could not read Lyric entities") from exc
        if not isinstance(raw, list):
            raise HAError("Invalid Lyric entity discovery response")
        return {eid for eid in raw if isinstance(eid, str)}

    async def _climate_entity_ids(self, extras: set[str] | None = None) -> set[str]:
        """Native Resideo entities plus explicitly selected Lyric entities only."""
        extras = extras if extras is not None else set(self._extra_climate_entities())
        extras = {e for e in extras if re.fullmatch(r"climate\.[a-z0-9_]+", e)}
        template = "{% set ids = integration_entities('resideo') %}"
        if extras:
            template += ("{% set allowed = " + json.dumps(sorted(extras)) + " %}"
                         "{% set ids = ids + (integration_entities('lyric') | select('in', allowed) | list) %}")
        template += "{{ ids | select('match', 'climate[.]') | unique | list | tojson }}"
        raw = await self._post("/api/template", {"template": template})
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise HAError("Could not identify permitted thermostat entities") from exc
        if not isinstance(raw, list):
            raise HAError("Invalid thermostat discovery response")
        return {eid for eid in raw if isinstance(eid, str) and re.fullmatch(r"climate\.[a-z0-9_]+", eid)}

    async def climate_states(self) -> list[dict[str, Any]]:
        """Resideo climate entities with readings. Leftover climate.* from removed devices are ignored."""
        extras = set(self._extra_climate_entities())
        wanted = await self._climate_entity_ids(extras)
        resp = await self._http.get("/api/states")
        resp.raise_for_status()
        out = []
        for s in resp.json():
            eid = s.get("entity_id") or ""
            if not eid.startswith("climate."):
                continue
            if eid not in wanted:
                continue
            item = self._climate_from_state(s)
            item["manual_assignment_required"] = eid in extras
            out.append(item)
        return out

    async def climate_state(self, entity_id: str) -> dict[str, Any] | None:
        """One thermostat after a set, without listing every climate entity Home Assistant still has."""
        resp = await self._http.get(f"/api/states/{entity_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return self._climate_from_state(resp.json())

    async def set_temperature(self, entity_id: str, *, temperature: float | None = None,
                              low: float | None = None, high: float | None = None) -> None:
        payload: dict[str, Any] = {"entity_id": entity_id}
        if temperature is not None:
            payload["temperature"] = temperature
        if low is not None:
            payload["target_temp_low"] = low
        if high is not None:
            payload["target_temp_high"] = high
        await self._post("/api/services/climate/set_temperature", payload)

    async def set_hvac_mode(self, entity_id: str, mode: str) -> None:
        await self._post("/api/services/climate/set_hvac_mode", {"entity_id": entity_id, "hvac_mode": mode})

    async def lock_batteries(self) -> dict[str, int]:
        """Battery percent per Schlage lock, from the battery sensor on the same device. Empty if unavailable."""
        template = (
            "{% set ns = namespace(items=[]) %}"
            "{% for l in integration_entities('schlage') | select('match', 'lock[.]') %}"
            "{% for s in device_entities(device_id(l)) | select('match', 'sensor[.]') %}"
            "{% if state_attr(s, 'device_class') == 'battery' and states(s) | is_number %}"
            "{% set ns.items = ns.items + [[l, states(s) | float | round(0) | int]] %}"
            "{% endif %}{% endfor %}{% endfor %}{{ ns.items | tojson }}"
        )
        body = await self._post("/api/template", {"template": template})
        return {item[0]: int(item[1]) for item in body if isinstance(item, list) and len(item) == 2} \
            if isinstance(body, list) else {}

    async def get_codes(self, entity_id: str) -> dict[str, str]:
        """Access codes in the lock as {name: code}."""
        body = await self._post(
            "/api/services/schlage/get_codes?return_response",
            {"entity_id": entity_id},
            timeout=SCHLAGE_TIMEOUT,
        )
        codes = (body or {}).get("service_response", {}).get(entity_id, {})
        return {c["name"]: c["code"] for c in codes.values()}

    async def add_code(self, entity_id: str, name: str, code: str) -> None:
        await self._post(
            "/api/services/schlage/add_code",
            {"entity_id": entity_id, "name": name, "code": code, "notify_on_use": False},
            timeout=SCHLAGE_TIMEOUT,
        )

    async def delete_code(self, entity_id: str, name: str) -> None:
        await self._post(
            "/api/services/schlage/delete_code",
            {"entity_id": entity_id, "name": name},
            timeout=SCHLAGE_TIMEOUT,
        )

    async def notify_target(self, title: str, message: str, target: str) -> None:
        """One outbound service only; a persistent-notice failure must not replay a successful push."""
        if not re.fullmatch(r'notify\.[a-zA-Z0-9_]+', target):
            raise HAError('Invalid staff notification target')
        await self._post(f"/api/services/notify/{target.removeprefix('notify.')}",
                         {'title': title, 'message': message})

    async def notify(self, title: str, message: str, *, service: str = "", services: list[str] | None = None,
                     notification_id: str = "") -> None:
        """Persistent notification in HA, plus optional notify services (e.g. notify.mobile_app_x)."""
        payload = {"title": title, "message": message}
        if notification_id:
            payload["notification_id"] = notification_id
        await self._post("/api/services/persistent_notification/create", payload)
        targets = list(services or [])
        if service and service not in targets:
            targets.append(service)
        failed = []
        for target in targets:  # one broken recipient must not stop the others from being told
            if not target.startswith("notify."):
                continue
            try:
                await self._post(f"/api/services/notify/{target.removeprefix('notify.')}",
                                 {"title": title, "message": message})
            except Exception as exc:
                failed.append(f"{target}: {exc}")
        if failed:
            raise HAError("notify failed for " + "; ".join(failed))

    async def webhook_automation_exists(self) -> bool:
        resp = await self._http.get(f"/api/config/automation/config/{WEBHOOK_AUTOMATION_ID}")
        return resp.status_code == 200

    async def own_slug(self) -> str:
        """This add-on's real Supervisor slug. Installed from a repository it is '<hash>_stay_automation', not
        'local_...'; hassio.addon_stdin only works with the real one. Empty when it cannot be found."""
        resp = await self._http.get("http://supervisor/addons/self/info")
        if resp.status_code != 200:
            return ""
        return str(((resp.json() or {}).get("data") or {}).get("slug") or "")

    async def create_webhook_automation(self, webhook_id: str, addon_slug: str) -> None:
        """HA receives the Hostaway webhook publicly and hands the JSON to this add-on's stdin."""
        path = f"/api/config/automation/config/{WEBHOOK_AUTOMATION_ID}"
        existing = await self._http.get(path)
        extension_actions = []
        if existing.status_code == 200:
            current = existing.json()
            for action in current.get("actions", current.get("action", [])):
                data = action.get("data", {}) if isinstance(action, dict) else {}
                companion = str(data.get("addon", ""))
                if re.fullmatch(r"(?:[a-f0-9]{8}|local)_guest_extension_pricing", companion) and action == {
                    "action": "hassio.addon_stdin", "continue_on_error": True,
                    "data": {"addon": companion, "input": "{{ trigger.json | tojson }}"},
                }:
                    extension_actions.append(action)
        elif existing.status_code != 404:
            existing.raise_for_status()
        await self._post(
            path,
            {
                "id": WEBHOOK_AUTOMATION_ID,
                "alias": "Stay Automation - Hostaway webhook",
                "description": "Managed by the Stay Automation add-on.",
                "mode": "queued",
                "max": 100,
                "triggers": [{
                    "trigger": "webhook",
                    "webhook_id": webhook_id,
                    "allowed_methods": ["POST"],
                    "local_only": False,
                }],
                "actions": [{
                    "action": "hassio.addon_stdin",
                    "data": {"addon": addon_slug, "input": "{{ trigger.json | tojson }}"},
                }, *extension_actions],
            },
        )

    async def reauthorization_entries(self) -> list[dict[str, Any]]:
        """Read HA's actual reauth flows and entity registry, without reloading integrations."""
        import asyncio
        from websockets.asyncio.client import connect
        base = str(self._http.base_url).rstrip('/')
        suffix = '/websocket' if base.endswith('/core') else '/api/websocket'
        url = base.replace('https://', 'wss://', 1).replace('http://', 'ws://', 1) + suffix
        authorization = self._http.headers.get('Authorization', '')
        async with connect(url, additional_headers={'Authorization': authorization}, open_timeout=10, close_timeout=5) as ws:
            hello = json.loads(await asyncio.wait_for(ws.recv(), 15))
            if hello.get('type') == 'auth_required':
                await ws.send(json.dumps({'type': 'auth', 'access_token': authorization.removeprefix('Bearer ')}))
                hello = json.loads(await asyncio.wait_for(ws.recv(), 15))
            if hello.get('type') != 'auth_ok':
                raise HAError('Home Assistant denied the reauthorization monitor connection')
            results = []
            for request_id, command in enumerate(('config_entries/flow/progress', 'config/entity_registry/list', 'config_entries/get'), 1):
                await ws.send(json.dumps({'id': request_id, 'type': command}))
                reply = json.loads(await asyncio.wait_for(ws.recv(), 15))
                if reply.get('id') != request_id or not reply.get('success'):
                    raise HAError('Could not read integration reauthorization state')
                results.append(reply['result'])
        flows, entities, entries = results
        return [{'entry_id': f['context']['entry_id'], 'flow_id': f['flow_id'],
                 'domain': f['handler'],
                 'title': next((e['title'] for e in entries if e['entry_id'] == f['context']['entry_id']), f['handler']),
                 'entities': [e['entity_id'] for e in entities if e.get('config_entry_id') == f['context']['entry_id']]}
                for f in flows if f.get('context', {}).get('source') == 'reauth' and f['context'].get('entry_id')]
