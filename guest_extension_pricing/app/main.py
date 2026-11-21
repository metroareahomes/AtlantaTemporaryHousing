"""Independent Home Assistant ingress panel and Hostaway webhook consumer."""
import asyncio
import hmac
import json
import logging
import os
import secrets
import sys
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.templating import Jinja2Templates

from .db import DB
from .extension_pricing import ExtensionPricing, OWNERS, SETTINGS_KEY, TZ, read_settings, validate_settings
from .hostaway import HostawayClient
from .report import report_rows

log = logging.getLogger(__name__)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


async def link_stay_webhook(client):
    info = await client.get("http://supervisor/addons/self/info")
    info.raise_for_status()
    slug = info.json()["data"]["slug"]
    if not slug.endswith("_guest_extension_pricing"):
        raise ValueError("Guest Extension Pricing app slug unavailable")

    url = "http://supervisor/core/api/config/automation/config/stay_automation_hostaway_webhook"
    response = await client.get(url)
    response.raise_for_status()
    automation = response.json()
    triggers = automation.get("triggers", automation.get("trigger", []))
    webhook_ids = [trigger["webhook_id"] for trigger in triggers
                   if trigger.get("trigger", trigger.get("platform")) == "webhook"]
    if len(webhook_ids) != 1:
        raise ValueError("Expected the existing Stay Automation webhook")
    actions_key = "actions" if "actions" in automation else "action"
    actions = automation[actions_key]
    if not isinstance(actions, list):
        raise ValueError("Expected Stay Automation action list")
    forward = {
        "action": "hassio.addon_stdin",
        "continue_on_error": True,
        "data": {"addon": slug, "input": "{{ trigger.json | tojson }}"},
    }
    if forward not in actions:
        response = await client.post(url, json={**automation, actions_key: [*actions, forward]})
        response.raise_for_status()
    return webhook_ids[0]


def create_app(options, db_path, supervisor_token, *, hostaway=None, stdin=False):
    db = DB(db_path)
    account = str(options.get("hostaway_account_id", ""))
    if hostaway is None:
        hostaway = HostawayClient(account, options.get("hostaway_api_key", ""))
    service = ExtensionPricing(SimpleNamespace(
        db=db, hostaway=hostaway, now=lambda: datetime.now(TZ),
        settings=SimpleNamespace(hostaway_account_id=account),
    ))
    csrf = secrets.token_urlsafe(32)

    async def tick_forever():
        while True:
            try:
                await service.tick()
            except Exception:
                log.warning("Extension pricing check failed; will retry.")
            await asyncio.sleep(60)

    @asynccontextmanager
    async def lifespan(_):
        if stdin:
            loop = asyncio.get_running_loop()

            def receive(line):
                try:
                    if service.receive(line) and len(line) < 1_000_000:
                        db.set_setting("last_message_event", datetime.now(TZ).strftime("%Y-%m-%d %I:%M %p"))
                except Exception:
                    log.warning("Could not record a Hostaway message event.")

            def read():
                for line in sys.stdin:
                    if len(line) < 1_000_000:
                        loop.call_soon_threadsafe(receive, line)

            threading.Thread(target=read, daemon=True).start()
        task = asyncio.create_task(tick_forever())
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await hostaway.close()
            db.conn.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def page(request, message="", status=200, owner=OWNERS[0]):
        rows, totals = report_rows(db, owner)
        return templates.TemplateResponse(
            request, "index.html", {
                "config": read_settings(db)[owner], "csrf": csrf, "message": message,
                "connected": db.get_setting("shared_webhook_connected"),
                "last_message": db.get_setting("last_message_event"),
                "report_rows": rows, "report_totals": totals, "owner": owner, "owners": OWNERS,
            }, status_code=status,
        )

    async def checked_form(request):
        form = await request.form()
        if not hmac.compare_digest(str(form.get("csrf", "")).encode(), csrf.encode()):
            raise HTTPException(403, "Reload the app and try again.")
        return form

    @app.get("/")
    async def index(request: Request, owner: str = OWNERS[0]):
        if owner not in OWNERS:
            raise HTTPException(404)
        return page(request, owner=owner)

    @app.post("/settings")
    async def settings(request: Request):
        form = await checked_form(request)
        owner = form.get("owner")
        if owner not in OWNERS:
            raise HTTPException(404)
        try:
            config = validate_settings(form)
            if config["enabled"] and not db.get_setting("shared_webhook_connected"):
                raise ValueError("Use the Stay Automation webhook before enabling pricing.")
        except ValueError as exc:
            return page(request, str(exc), 400, owner=owner)
        rules = read_settings(db)
        rules[owner] = config
        db.set_setting(SETTINGS_KEY, json.dumps({"owners": rules}))
        return page(request, "Settings saved.", owner=owner)

    @app.post("/connect")
    async def connect(request: Request):
        form = await checked_form(request)
        owner = form.get("owner")
        if owner not in OWNERS:
            raise HTTPException(404)
        if not account or not options.get("hostaway_api_key") or not supervisor_token:
            return page(request, "Enter the Hostaway credentials in the app configuration.", 400, owner=owner)
        try:
            async with httpx.AsyncClient(headers={"Authorization": f"Bearer {supervisor_token}"}) as client:
                shared_id = await link_stay_webhook(client)
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return page(request, "Could not link the Stay Automation webhook. Check its setup and try again.",
                        502, owner=owner)
        db.set_setting("shared_webhook_connected", "1")
        legacy_id = db.get_setting("webhook_id")
        if legacy_id and legacy_id != shared_id:
            try:
                await hostaway.disable_legacy_webhook(legacy_id)
            except Exception:
                log.warning("Unable to disable the former Guest Extension Pricing webhook.")
                return page(request, "Shared webhook linked. The former pricing webhook could not be disabled.",
                            owner=owner)
        return page(request, "Stay Automation webhook linked.", owner=owner)

    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    options = json.loads(Path("/data/options.json").read_text())
    uvicorn.run(create_app(options, "/data/extensions.db", os.environ.get("SUPERVISOR_TOKEN"), stdin=True),
                host="0.0.0.0", port=8099, access_log=False)
