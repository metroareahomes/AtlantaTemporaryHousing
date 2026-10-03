"""Dashboard served through HA Ingress. All links are relative so they work under the ingress path."""
import json
import secrets
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .hostaway import HostawayError
from .locks import DEFAULT_GUEST_MESSAGE, DEFAULTS, LockManager
from .reservations import code_status, property_state, within
from .sync import Syncer

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")

STATUS_LABELS = {
    "occupied": "Occupied",
    "vacant": "Vacant",
    "arriving_today": "Arriving today",
    "departing_today": "Departing today",
    "turnover_today": "Turnover today",
}
CODE_LABELS = {
    "in_lock": "In lock",
    "missing": "NOT in lock",
    "unknown": "Not checked yet",
    "no_code": "No code from Hostaway",
    "no_lock": "No lock",
    "none": "",
}


def _fmt(iso: str | None, tz) -> str:
    if not iso:
        return ""
    return datetime.fromisoformat(iso).astimezone(tz).strftime("%a %b %d, %I:%M %p").replace(" 0", " ")


def dashboard_rows(syncer: Syncer, query: str = "") -> list[dict[str, Any]]:
    now = syncer.now()
    tz = syncer.tz
    locks_by_property: dict[int, list[dict]] = {}
    for lock in syncer.db.query("SELECT * FROM locks WHERE property_id IS NOT NULL ORDER BY name"):
        locks_by_property.setdefault(lock["property_id"], []).append(lock)

    rows = []
    for prop in syncer.db.query("SELECT * FROM properties WHERE active = 1 ORDER BY name"):
        if query and query.lower() not in prop["name"].lower():
            continue
        state = property_state(syncer.reservations_for(prop["id"]), now)
        locks = locks_by_property.get(prop["id"], [])
        per_lock = [
            code_status(
                state.next, None if lock["code_hashes"] is None else set(json.loads(lock["code_hashes"])),
                True, syncer.code_hash,
            )
            for lock in locks
        ] or [code_status(state.next, None, False, syncer.code_hash)]
        # The worst lock decides: one lock without the code means the guest may be stuck at that door.
        order = ["missing", "unknown", "no_code", "in_lock", "no_lock", "none"]
        code = min(per_lock, key=order.index)

        problems = []
        soon = within(state.next, now, 36)
        if code == "missing" and soon:
            problems.append("Guest code not in lock")
        if code == "no_code" and soon:
            problems.append("No door code in Hostaway")
        for lock in locks:
            if lock["state"] == "unavailable":
                problems.append(f"{lock['name']} offline")
            elif lock["codes_error"]:
                problems.append(f"{lock['name']}: read failed")
            if prop["lock_automation"] and lock["fail_count"]:
                problems.append(f"{lock['name']}: {lock['last_error']} (tries: {lock['fail_count']})")

        rows.append({
            "id": prop["id"],
            "name": prop["name"],
            "status": state.status,
            "status_label": STATUS_LABELS[state.status],
            "current_guest": state.current["guest_name"] if state.current else "",
            "current_out": _fmt(state.current["check_out_at"], tz) if state.current else "",
            "next_guest": state.next["guest_name"] if state.next else "",
            "next_in": _fmt(state.next["check_in_at"], tz) if state.next else "",
            "next_in_sort": state.next["check_in_at"] if state.next else "9999",
            "door_code": (state.next or {}).get("door_code") or "",
            "code": code,
            "code_label": CODE_LABELS[code],
            "locks": locks,
            "lock_read": _fmt(max((l["codes_read_at"] for l in locks if l["codes_read_at"]), default=None), tz),
            "problems": problems,
            "lock_automation": prop["lock_automation"],
        })
    rows.sort(key=lambda r: (not r["problems"], r["next_in_sort"]))
    return rows


SETTING_LABELS = {
    "add_hour": "Add guest code at (hour, 0-23, arrival day)",
    "early_lead_hours": "For early check-ins, add this many hours before",
    "afternoon_check_hour": "Afternoon re-check (hour)",
    "final_check_minutes": "Final check, minutes before check-in",
    "retry_minutes": "Retry failed locks every (minutes)",
    "daily_check_hour": "Daily lock check (hour)",
    "report_hour": "Daily arrivals report (hour)",
    "stale_check_minutes": "Alert if Hostaway has not removed a guest code this many minutes after checkout",
}


def create_app(syncer: Syncer, locks: LockManager | None = None, lifespan=None) -> FastAPI:
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    db = syncer.db

    @app.exception_handler(HostawayError)
    async def hostaway_failed(request: Request, exc: HostawayError):
        """Show what is wrong (usually the credentials) instead of a 500 stack trace."""
        db.log("hostaway.error", str(exc), level="error")
        return templates.TemplateResponse(
            request, "error.html",
            {"message": str(exc), "last_sync": "", "last_webhook": ""}, status_code=502)

    @app.exception_handler(StarletteHTTPException)
    async def bad_input(request: Request, exc: StarletteHTTPException):
        """A rejected form entry (wrong code length, duplicate name...) gets a readable page, not bare JSON.
        Webhooks and other machine callers keep the normal response."""
        if exc.status_code != 400 or "webhook" in request.url.path:
            return await http_exception_handler(request, exc)
        return templates.TemplateResponse(
            request, "error.html",
            {"title": "That was not saved", "message": exc.detail, "last_sync": "", "last_webhook": ""},
            status_code=400)

    def page(request: Request, name: str, **ctx: Any) -> HTMLResponse:
        return templates.TemplateResponse(request, name, {
            "last_sync": _fmt(db.get_setting("last_sync_at"), syncer.tz),
            "last_webhook": _fmt(db.get_setting("last_webhook_at"), syncer.tz),
            **ctx,
        })

    @app.get("/", response_class=HTMLResponse)
    async def status(request: Request, q: str = ""):
        rows = dashboard_rows(syncer, q)
        counts = {k: sum(1 for r in rows if r["status"] == k) for k in STATUS_LABELS}
        return page(request, "status.html", rows=rows, q=q, counts=counts,
                    problems=sum(1 for r in rows if r["problems"]))

    @app.get("/properties", response_class=HTMLResponse)
    async def properties(request: Request):
        props = db.query("SELECT * FROM properties ORDER BY name")
        locks = db.query("SELECT * FROM locks ORDER BY name")
        by_property: dict[int, list[dict]] = {}
        for lock in locks:
            if lock["property_id"]:
                by_property.setdefault(lock["property_id"], []).append(lock)
        for prop in props:
            prop["lock_list"] = by_property.get(prop["id"], [])
        return page(request, "properties.html", properties=props, locks=locks)

    @app.post("/properties-save")
    async def properties_save(request: Request):
        """Apply only what was changed on this page (each field is posted next to the value it was shown with),
        so saving a stale tab, or a page that loaded before someone else's change, cannot flip other homes."""
        form = await request.form()
        props = db.query("SELECT * FROM properties")
        taken = {p["backup_code"] for p in props if p["backup_code"]}
        updates: list[tuple[str, tuple]] = []
        toggles: list[tuple[str, str, int]] = []  # (event kind, message, property id)
        touched: set[int] = set()

        # Work out and validate everything first, so a bad entry saves nothing.
        for p in props:
            pid = p["id"]
            if f"was_auto_{pid}" not in form:
                continue  # this row was not on the page that was submitted
            sets: list[str] = []
            args: list[Any] = []
            name = str(form.get(f"name_{pid}", "")).strip()
            if name and name != p["name"]:
                sets.append("name = ?")
                args.append(name)
            for field, column, label in (("auto", "lock_automation", "Lock automation"), ("active", "active", "Active")):
                now_on = 1 if form.get(f"{field}_{pid}") else 0
                was_on = 1 if form.get(f"was_{field}_{pid}") == "1" else 0
                if now_on != was_on and now_on != p[column]:
                    sets.append(f"{column} = ?")
                    args.append(now_on)
                    toggles.append((f"{field}.{'on' if now_on else 'off'}",
                                    f"{label} turned {'ON' if now_on else 'OFF'} for {p['name']} (Properties page)",
                                    pid))
                    touched.add(pid)
            backup = str(form.get(f"backup_{pid}", "")).strip()
            if backup and backup != str(form.get(f"was_backup_{pid}", "")).strip() and backup != (p["backup_code"] or ""):
                if not backup.isdigit() or len(backup) != 4:
                    raise HTTPException(400, f"{p['name']}: the backup code must be exactly 4 digits")
                if backup in taken:
                    raise HTTPException(400, f"{p['name']}: backup code {backup} is already used by another home")
                in_lock: set[str] = set()
                for lock in db.query("SELECT code_hashes FROM locks WHERE property_id = ? AND code_hashes IS NOT NULL",
                                     (pid,)):
                    in_lock |= set(json.loads(lock["code_hashes"]))
                if syncer.code_hash(backup) in in_lock:
                    raise HTTPException(400, f"{p['name']}: backup code {backup} already opens this lock under "
                                             "another name (Master, a cleaner, a guest). Pick a different one")
                sets += ["backup_code = ?", "backup_used_by = NULL"]
                args.append(backup)
                taken.add(backup)
                toggles.append(("backup.edited", f"Backup code changed for {p['name']} (Properties page)", pid))
                touched.add(pid)
            if sets:
                updates.append((f"UPDATE properties SET {', '.join(sets)} WHERE id = ?", (*args, pid)))

        for sql, args in updates:
            db.execute(sql, args)
        for kind, message, pid in toggles:
            db.log(kind, message, level="warning" if kind == "auto.off" else "info", property_id=pid)

        for lock in db.query("SELECT id, property_id FROM locks"):
            if f"was_lock_{lock['id']}" not in form:
                continue
            value, was = str(form.get(f"lock_{lock['id']}", "")), str(form.get(f"was_lock_{lock['id']}", ""))
            if value != was:
                new_pid = int(value) if value.isdigit() else None
                if new_pid != lock["property_id"]:
                    syncer.assign_lock(lock["id"], new_pid)
                    touched |= {x for x in (new_pid, lock["property_id"]) if x}

        # Only the homes that changed get their locks looked at again (each look is a slow Schlage call).
        if touched:
            db.execute(f"UPDATE locks SET next_check_at = NULL WHERE property_id IN ({', '.join('?' for _ in touched)})",
                       tuple(touched))
        db.log("properties.saved", "Property settings saved")
        return RedirectResponse("properties", status_code=303)

    @app.get("/staff", response_class=HTMLResponse)
    async def staff(request: Request):
        return page(
            request, "staff.html",
            staff=db.query("SELECT * FROM staff ORDER BY name"),
            recipients=db.query("SELECT * FROM alert_recipients ORDER BY name"),
        )

    @app.post("/staff-save")
    async def staff_save(request: Request):
        form = await request.form()

        def check(name: str, code: str) -> None:
            if name.startswith("HA-"):
                raise HTTPException(400, "Staff names cannot start with HA- (reserved for guest/backup codes)")
            if not code.isdigit() or len(code) != 4:
                raise HTTPException(400, f"{name}: code must be exactly 4 digits")

        # Validate everything first so a typo can never half-save (or silently drop someone's lock code).
        updates, seen, active_codes = [], set(), set()
        for row in db.query("SELECT id, name, code FROM staff"):
            sid = row["id"]
            name = str(form.get(f"name_{sid}", "")).strip()
            code = str(form.get(f"code_{sid}", "")).strip()
            if not name:  # cleared name = remove this person: deactivate so the locks drop the code
                updates.append(("UPDATE staff SET active = 0 WHERE id = ?", (sid,)))
                continue
            check(name, code)
            if name in seen:
                raise HTTPException(400, f"{name} is in the list twice")
            seen.add(name)
            active = 1 if form.get(f"active_{sid}") else 0
            if active:
                if code in active_codes:  # Schlage refuses two identical codes; one would silently never be added
                    raise HTTPException(400, f"Code {code} is used by more than one active person")
                active_codes.add(code)
            updates.append(("UPDATE staff SET name = ?, code = ?, active = ? WHERE id = ?",
                            (name, code, active, sid)))
            if name != row["name"]:
                # Renamed: the old name is still on the locks. Keep it as an inactive row (added after the
                # rename frees the name) so the locks remove it; it disappears from this page once no lock
                # holds it any more.
                updates.append(("INSERT OR IGNORE INTO staff(name, code, active) VALUES(?, ?, 0)",
                                (row["name"], row["code"])))
        new_name = str(form.get("name_new", "")).strip()
        new_code = str(form.get("code_new", "")).strip()
        if new_name or new_code:
            if not new_name:
                raise HTTPException(400, "New staff needs a name")
            check(new_name, new_code)
            if new_name in seen or db.one("SELECT 1 FROM staff WHERE name = ?", (new_name,)):
                raise HTTPException(400, f"{new_name} is already in the list (tick Active on that row to bring "
                                         "them back)")
            new_active = 1 if form.get("active_new") else 0
            if new_active and new_code in active_codes:
                raise HTTPException(400, f"Code {new_code} is used by more than one active person")
            updates.append(("INSERT INTO staff(name, code, active) VALUES(?, ?, ?)",
                            (new_name, new_code, new_active)))
        try:
            for sql, args in updates:
                db.execute(sql, args)
        except sqlite3.IntegrityError:  # e.g. two people swapped names in one save
            raise HTTPException(400, "Two people ended up with the same name; change one name at a time")
        # Drop deactivated/blank rows that no longer appear in any lock snapshot
        # (Parse the stored JSON rather than LIKE-matching it: accents are stored escaped and % _ are wildcards.)
        on_locks: set[str] = set()
        for lock in db.query("SELECT code_names FROM locks WHERE code_names IS NOT NULL"):
            try:
                on_locks.update(json.loads(lock["code_names"]))
            except (TypeError, ValueError):
                pass
        for row in db.query("SELECT id, name FROM staff WHERE active = 0"):
            if row["name"] not in on_locks:
                db.execute("DELETE FROM staff WHERE id = ?", (row["id"],))
        db.execute("UPDATE locks SET next_check_at = NULL WHERE property_id IN "
                   "(SELECT id FROM properties WHERE lock_automation = 1)")
        db.log("staff.saved", "Staff codes saved")
        return RedirectResponse("staff", status_code=303)

    @app.post("/recipients-save")
    async def recipients_save(request: Request):
        form = await request.form()

        def check(name: str, target: str) -> None:
            # Anything else would be skipped silently when alerting, so refuse it up front.
            if not target.startswith("notify.") or len(target) <= len("notify."):
                raise HTTPException(400, f"{name}: the target must be a Home Assistant notify service, "
                                         "like notify.mobile_app_kurts_iphone")

        # Validate everything first so a typo cannot half-save.
        updates = []
        for row in db.query("SELECT id FROM alert_recipients"):
            rid = row["id"]
            name = str(form.get(f"rname_{rid}", "")).strip()
            target = str(form.get(f"rtarget_{rid}", "")).strip()
            if not name or not target:
                updates.append(("DELETE FROM alert_recipients WHERE id = ?", (rid,)))
                continue
            check(name, target)
            updates.append(("UPDATE alert_recipients SET name = ?, target = ?, active = ? WHERE id = ?",
                            (name, target, 1 if form.get(f"ractive_{rid}") else 0, rid)))
        new_name = str(form.get("rname_new", "")).strip()
        new_target = str(form.get("rtarget_new", "")).strip()
        if new_name or new_target:
            if not new_name or not new_target:
                raise HTTPException(400, "New recipient needs a name and a notify target")
            check(new_name, new_target)
            updates.append(("INSERT INTO alert_recipients(name, target, active) VALUES(?, ?, ?)",
                            (new_name, new_target, 1 if form.get("ractive_new") else 0)))
        for sql, args in updates:
            db.execute(sql, args)
        db.log("recipients.saved", "Alert recipients saved")
        return RedirectResponse("staff", status_code=303)

    @app.get("/events", response_class=HTMLResponse)
    async def events(request: Request):
        rows = db.query(
            "SELECT e.*, p.name AS property FROM events e LEFT JOIN properties p ON p.id = e.property_id "
            "ORDER BY e.id DESC LIMIT 300"
        )
        for r in rows:
            r["at_local"] = _fmt(r["at"], syncer.tz)
        return page(request, "events.html", events=rows)

    @app.post("/sync")
    async def sync_now():
        await syncer.import_listings()
        await syncer.discover_locks()
        changed = await syncer.sync_reservations()
        db.log("sync.manual", f"Manual sync: {changed} reservations changed")
        return RedirectResponse(".", status_code=303)

    @app.post("/lock-refresh")
    async def lock_refresh(property_id: int = Form(...)):
        for lock in db.query("SELECT * FROM locks WHERE property_id = ?", (property_id,)):
            await syncer.read_lock_codes(lock)
        return RedirectResponse(".", status_code=303)

    @app.get("/setup", response_class=HTMLResponse)
    async def setup(request: Request):
        webhook_id = db.get_setting("webhook_id")
        public_url = db.get_setting("public_url", "")
        try:
            ha_ok = await syncer.ha.ping()
            automation = await syncer.ha.webhook_automation_exists()
        except Exception:
            ha_ok, automation = False, False
        return page(
            request, "setup.html",
            ha_ok=ha_ok, automation=automation, public_url=public_url,
            addon_slug=db.get_setting("addon_slug", "local_stay_automation"),
            webhook_url=f"{public_url}/api/webhook/{webhook_id[:6]}…" if webhook_id else "",
            registered=db.get_setting("hostaway_webhook_registered_at"),
            is_addon=syncer.settings.is_addon,
            numbers=[(k, SETTING_LABELS[k], db.get_int(k, v)) for k, v in DEFAULTS.items()],
            alert_service=db.get_setting("alert_service", "") or "",
            backup_codes_enabled=db.get_bool("backup_codes_enabled"),
            guest_messages_enabled=db.get_bool("guest_messages_enabled"),
            guest_message=db.get_setting("guest_message") or DEFAULT_GUEST_MESSAGE,
            automated=db.one("SELECT COUNT(*) AS n FROM properties WHERE lock_automation = 1")["n"],
        )

    @app.post("/settings-save")
    async def settings_save(request: Request):
        form = await request.form()
        for key, default in DEFAULTS.items():
            value = str(form.get(key, "")).strip()
            if value.isdigit():
                db.set_setting(key, value)
        db.set_setting("alert_service", str(form.get("alert_service", "")).strip())
        db.set_setting("backup_codes_enabled", "1" if form.get("backup_codes_enabled") else "0")
        db.set_setting("guest_messages_enabled", "1" if form.get("guest_messages_enabled") else "0")
        message = str(form.get("guest_message", "")).strip()
        try:
            message.format(guest="", property="", code="")
        except (KeyError, IndexError, ValueError):
            raise HTTPException(400, "Guest message may only use {guest}, {property} and {code}")
        db.set_setting("guest_message", message)
        db.execute("UPDATE locks SET next_check_at = NULL")
        db.log("settings.saved", "Lock automation settings saved")
        return RedirectResponse("setup", status_code=303)

    @app.post("/test-alert")
    async def test_alert():
        if locks is not None:
            await locks.alert("Stay Automation test", "Alerts are working.")
        return RedirectResponse("setup", status_code=303)

    @app.post("/setup-webhook")
    async def setup_webhook(public_url: str = Form(...), addon_slug: str = Form(...),
                            register_hostaway: str = Form("")):
        public_url = public_url.strip().rstrip("/")
        if not public_url.startswith("https://"):
            raise HTTPException(400, "Public URL must start with https://")
        db.set_setting("public_url", public_url)
        db.set_setting("addon_slug", addon_slug.strip())
        webhook_id = db.get_setting("webhook_id") or secrets.token_urlsafe(32)
        db.set_setting("webhook_id", webhook_id)
        await syncer.ha.create_webhook_automation(webhook_id, addon_slug.strip())
        db.log("setup.webhook", "Home Assistant webhook automation saved")
        if register_hostaway:
            await syncer.hostaway.register_webhook(f"{public_url}/api/webhook/{webhook_id}")
            db.set_setting("hostaway_webhook_registered_at", datetime.now(syncer.tz).isoformat())
            db.log("setup.webhook", "Webhook registered with Hostaway")
        return RedirectResponse("setup", status_code=303)

    @app.post("/webhook/{secret}")
    async def webhook_direct(secret: str, request: Request):
        """Direct webhook for local development; on HA the webhook arrives through stdin."""
        expected = db.get_setting("webhook_id")
        if not expected or not secrets.compare_digest(secret, expected):
            raise HTTPException(404)
        what = await syncer.handle_webhook(await request.json())
        return {"changes": what}

    @app.get("/health")
    async def health():
        return {"ok": True}

    return app
