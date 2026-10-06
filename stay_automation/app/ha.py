"""Home Assistant REST client (Supervisor proxy inside the add-on, direct URL locally)."""
from typing import Any

import httpx

# Schlage cloud calls are slow; a delete once took over two minutes and still succeeded.
SCHLAGE_TIMEOUT = httpx.Timeout(30, read=240)

WEBHOOK_AUTOMATION_ID = "stay_automation_hostaway_webhook"


class HAError(Exception):
    pass


class HAClient:
    def __init__(self, base_url: str, token: str, http: httpx.AsyncClient | None = None):
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

    async def climate_states(self) -> list[dict[str, Any]]:
        """Every climate.* entity with its readings. state is the HVAC mode, or 'unavailable' when offline."""
        resp = await self._http.get("/api/states")
        resp.raise_for_status()
        out = []
        for s in resp.json():
            if not s["entity_id"].startswith("climate."):
                continue
            a = s.get("attributes") or {}
            out.append({
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
            })
        return out

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

    async def update_entity(self, entity_id: str) -> None:
        """Ask Home Assistant to refresh one entity from Honeywell. Used instead of Lyric's background poll."""
        await self._post("/api/services/homeassistant/update_entity", {"entity_id": entity_id})

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
        await self._post(
            f"/api/config/automation/config/{WEBHOOK_AUTOMATION_ID}",
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
                }],
            },
        )
