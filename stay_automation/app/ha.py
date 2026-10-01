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
