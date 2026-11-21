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
from urllib.parse import urlsplit

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.templating import Jinja2Templates

from .db import DB
from .extension_pricing import ExtensionPricing, SETTINGS_KEY, TZ, read_settings, validate_settings
from .hostaway import HostawayClient
from .report import report_rows

log = logging.getLogger(__name__)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


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

    def page(request, message="", status=200):
        rows, totals = report_rows(db)
        return templates.TemplateResponse(
            request, "index.html", {
                "config": read_settings(db), "csrf": csrf, "message": message,
                "connected": db.get_setting("connected"),
                "last_message": db.get_setting("last_message_event"),
                "report_rows": rows, "report_totals": totals,
            }, status_code=status,
        )

    async def checked_form(request):
        form = await request.form()
        if not hmac.compare_digest(str(form.get("csrf", "")).encode(), csrf.encode()):
            raise HTTPException(403, "Reload the app and try again.")
        return form

    @app.get("/")
    async def index(request: Request):
        return page(request)

    @app.post("/settings")
    async def settings(request: Request):
        form = await checked_form(request)
        try:
            config = validate_settings(form)
            if config["enabled"] and not db.get_setting("connected"):
                raise ValueError("Connect Hostaway before enabling the automation.")
        except ValueError as exc:
            return page(request, str(exc), 400)
        db.set_setting(SETTINGS_KEY, json.dumps(config))
        return page(request, "Settings saved.")

    @app.post("/connect")
    async def connect(request: Request):
        await checked_form(request)
        public_url = options.get("home_assistant_public_url", "").strip().rstrip("/")
        parsed = urlsplit(public_url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or
                parsed.path or parsed.query or parsed.fragment or not account or not supervisor_token):
            return page(request, "Enter the Hostaway credentials and the public HTTPS Home Assistant address in Configuration.", 400)
        webhook = db.get_setting("webhook_id")
        if not webhook:
            webhook = secrets.token_urlsafe(32)
            db.set_setting("webhook_id", webhook)
        try:
            async with httpx.AsyncClient(headers={"Authorization": f"Bearer {supervisor_token}"}) as client:
                info = await client.get("http://supervisor/addons/self/info")
                info.raise_for_status()
                slug = info.json()["data"]["slug"]
                automation = await client.post(
                    "http://supervisor/core/api/config/automation/config/guest_extension_pricing_webhook",
                    json={
                        "id": "guest_extension_pricing_webhook",
                        "alias": "Guest Extension Pricing Hostaway webhook",
                        "mode": "queued", "max": 100,
                        "triggers": [{"trigger": "webhook", "webhook_id": webhook,
                                      "allowed_methods": ["POST"], "local_only": False}],
                        "actions": [{"action": "hassio.addon_stdin",
                                     "data": {"addon": slug, "input": "{{ trigger.json | tojson }}"}}],
                    },
                )
                automation.raise_for_status()
            await hostaway.register_webhook(f"{public_url}/api/webhook/{webhook}")
        except Exception:
            return page(request, "Could not connect Hostaway. Check the credentials, public address, and whether Hostaway allows another webhook.", 502)
        db.set_setting("connected", "1")
        return page(request, "Hostaway webhook registered.")

    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    options = json.loads(Path("/data/options.json").read_text())
    uvicorn.run(create_app(options, "/data/extensions.db", os.environ.get("SUPERVISOR_TOKEN"), stdin=True),
                host="0.0.0.0", port=8099, access_log=False)
