"""Dashboard served through HA Ingress. All links are relative so they work under the ingress path."""
import json
import re
import secrets
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .climate import (
    DEFAULTS as CLIMATE_DEFAULTS, LABELS as CLIMATE_LABELS, RANGES as CLIMATE_RANGES,
    TOLERANCE, is_summer, occupancy, occupying_reservation, setpoint,
)
from .hostaway import HostawayError
from .locks import DEFAULT_GUEST_MESSAGE, DEFAULTS, LockManager, _utc, new_backup_code
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
    "missing": "Missing",
    "unknown": "Not checked yet",
    "no_code": "No code from Hostaway",
    "no_lock": "No lock",
    "none": "",
}
BOARD_SHOWS = {"attention", "arriving", "departing", "occupied", "vacant", "all"}
MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June",
               "July", "August", "September", "October", "November", "December"]
LOCK_GUEST_KEYS = ("add_hour", "early_lead_hours", "final_check_minutes")
LOCK_ADVANCED_KEYS = ("afternoon_check_hour", "retry_minutes", "daily_check_hour", "report_hour", "stale_check_minutes")
CLIMATE_GUEST_KEYS = ("occ_summer", "occ_winter", "vac_summer", "vac_winter", "therm_lead_hours", "therm_lag_hours")
CLIMATE_MONTH_KEYS = ("summer_start_month", "summer_end_month")
CLIMATE_ADVANCED_KEYS = (
    "vacant_reset_hour", "therm_offline_minutes", "therm_retry_minutes",
    "therm_max_per_tick", "therm_gap_seconds", "battery_alert_percent",
)


def _fmt(iso: str | None, tz) -> str:
    if not iso:
        return ""
    return datetime.fromisoformat(iso).astimezone(tz).strftime("%a %b %d, %I:%M %p").replace(" 0", " ")


def _deg(value: float | None) -> str:
    return "" if value is None else f"{value:g}°"


def _ago(iso: str | None, now: datetime, stale_after: timedelta) -> tuple[str, bool]:
    """A short age, and whether it is old enough to warn about."""
    if not iso:
        return "never", True
    then = datetime.fromisoformat(iso)
    if then.tzinfo is None:
        then = then.replace(tzinfo=now.tzinfo)
    seconds = max(0, (now - then.astimezone(now.tzinfo)).total_seconds())
    stale = seconds > stale_after.total_seconds()
    if seconds < 45:
        return "just now", stale
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))} min ago", stale
    if seconds < 48 * 3600:
        hours = max(1, int(seconds // 3600))
        return ("1 hour ago" if hours == 1 else f"{hours} hours ago"), stale
    days = max(1, int(seconds // 86400))
    return ("1 day ago" if days == 1 else f"{days} days ago"), stale


def _board_back(show: str, sort: str, q: str, notice: str = "") -> str:
    show = show if show in BOARD_SHOWS else "attention"
    sort = "name" if sort == "name" else "attention"
    args = {"show": show, "sort": sort, "q": q}
    if notice:
        args["notice"] = notice
    return ".?" + urlencode(args)


def _done(url: str, notice: str) -> RedirectResponse:
    """Redirect back to a page with a one-line confirmation the page shows once."""
    return RedirectResponse(url + ("&" if "?" in url else "?") + urlencode({"notice": notice}), status_code=303)


def _set_phrase(t: dict[str, Any]) -> str:
    if t["target_temp"] is not None:
        return f"set {_deg(t['target_temp'])}"
    if t["target_low"] is not None and t["target_high"] is not None:
        return f"set {_deg(t['target_low'])}–{_deg(t['target_high'])}"
    return ""


def _clamped(t: dict[str, Any], wanted: int) -> int:
    low, high = t.get("min_temp"), t.get("max_temp")
    if low is not None and high is not None and not low <= wanted <= high:
        return int(min(max(wanted, low), high))
    return int(wanted)


def _reading(t: dict[str, Any]) -> str:
    bits = []
    if t["current_temp"] is not None:
        bits.append(_deg(t["current_temp"]))
    if t["state"]:
        bits.append(str(t["state"]))
    phrase = _set_phrase(t)
    if phrase:
        bits.append(phrase)
    return " · ".join(bits) or "No reading"


def _climate_views(therms: list[dict], prop: dict, reservations: list[dict], now: datetime,
                   cfg: dict[str, int]) -> list[dict[str, Any]]:
    """What each thermostat should be doing, and the one problem line when it is not."""
    summer = is_summer(now.date(), cfg["summer_start_month"], cfg["summer_end_month"])
    wanted = setpoint(occupancy(reservations, now, cfg["therm_lead_hours"], cfg["therm_lag_hours"]), summer, cfg)
    want_mode = "cool" if summer else "heat"
    occupying = occupying_reservation(reservations, now, cfg["therm_lead_hours"], cfg["therm_lag_hours"])
    views = []
    for t in therms:
        detail = " · ".join(x for x in (t["hvac_action"], f"fan {t['fan_mode']}" if t["fan_mode"] else "") if x)
        if t["offline_since"]:
            ago, _ = _ago(t["offline_since"], now, timedelta(0))
            views.append({
                "name": t["name"], "line": f"Offline since {ago}" if ago != "never" else "Offline",
                "ok": False, "detail": detail, "action": "",
                "problem": f"{t['name']} offline (Home Assistant says {t['state'] or 'nothing'})",
            })
            continue
        if not prop["thermostat_automation"]:
            views.append({"name": t["name"], "line": _reading(t), "ok": True, "detail": detail,
                          "problem": None, "action": ""})
            continue
        adopted = bool(
            occupying and (
                t.get("last_reservation_id") == occupying["id"]
                or (not t.get("last_reservation_id") and occupying["arrival_date"] != now.date().isoformat())
            )
        )
        schedule_temp = _clamped(t, wanted)
        on_target = (
            t["state"] == want_mode and t["target_temp"] is not None
            and abs(float(t["target_temp"]) - schedule_temp) <= TOLERANCE
        )
        schedule = f"{want_mode.title()} {schedule_temp}°"
        now_bits = " ".join(x for x in (_deg(t["current_temp"]), t["state"]) if x)
        set_bit = _set_phrase(t)
        if t["fail_count"] and not adopted:
            views.append({
                "name": t["name"], "line": f"Should be {schedule} · last try failed",
                "ok": False, "detail": detail, "action": "",
                "problem": f"{t['name']}: {t['last_error']} (tries: {t['fail_count']})",
            })
            continue
        if on_target:
            limit = f" · device allows {schedule_temp}°" if schedule_temp != wanted else ""
            views.append({
                "name": t["name"], "line": f"{schedule} · now {now_bits} · on target{limit}",
                "ok": True, "detail": detail, "problem": None, "action": "",
            })
            continue
        if adopted:
            views.append({
                "name": t["name"], "line": f"Now {now_bits} · left for this stay",
                "ok": True, "detail": detail, "problem": None, "action": "",
            })
            continue
        asked = f"{want_mode.title()} {wanted}°"
        views.append({
            "name": t["name"],
            "line": " · ".join(x for x in (f"Should be {asked}", f"now {now_bits}".strip(), set_bit) if x),
            "ok": False, "detail": detail, "action": "",
            "problem": f"{t['name']} should be {asked}. Now {now_bits}" + (f", {set_bit}" if set_bit else ""),
        })
    return views


def _context(state, tz) -> str:
    label = STATUS_LABELS[state.status]
    if state.current:
        return f"{label} · {state.current['guest_name']} out {_fmt(state.current['check_out_at'], tz)}"
    if state.next:
        return f"{label} · {state.next['guest_name']} in {_fmt(state.next['check_in_at'], tz)}"
    return label


def street_key(name: str) -> tuple[str, int, str]:
    """Alphabetical by street name, then street number: "Maple 124b" and "124 Maple" both sort under Maple, by 124."""
    words = " ".join(re.findall(r"(?<!\d)[a-z]+", name.lower()))
    number = re.search(r"\d+", name)
    return words, int(number.group()) if number else 0, name.lower()


def dashboard_rows(syncer: Syncer, query: str = "") -> list[dict[str, Any]]:
    now = syncer.now()
    tz = syncer.tz
    locks_by_property: dict[int, list[dict]] = {}
    for lock in syncer.db.query("SELECT * FROM locks WHERE property_id IS NOT NULL ORDER BY name"):
        locks_by_property.setdefault(lock["property_id"], []).append(lock)

    therms_by_property: dict[int, list[dict]] = {}
    for t in syncer.db.query("SELECT * FROM thermostats WHERE property_id IS NOT NULL ORDER BY name"):
        therms_by_property.setdefault(t["property_id"], []).append(t)
    battery_limit = syncer.db.get_int("battery_alert_percent", CLIMATE_DEFAULTS["battery_alert_percent"])
    cfg = {k: syncer.db.get_int(k, v) for k, v in CLIMATE_DEFAULTS.items()}

    rows = []
    for prop in syncer.db.query("SELECT * FROM properties WHERE active = 1 ORDER BY name"):
        if query and query.lower() not in prop["name"].lower():
            continue
        reservations = syncer.reservations_for(prop["id"])
        state = property_state(reservations, now)
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
        read_at = max((l["codes_read_at"] for l in locks if l["codes_read_at"]), default=None)
        read_ago, read_stale = _ago(read_at, now, timedelta(hours=12))

        problems = []
        soon = within(state.next, now, 36)
        if code == "missing" and soon:
            problems.append({"text": f"Guest code missing. Last read {read_ago}.", "action": "check_lock"})
        if code == "no_code" and soon:
            problems.append({"text": "No door code in Hostaway.", "action": ""})
        for lock in locks:
            if lock["state"] == "unavailable":
                problems.append({"text": f"{lock['name']} offline", "action": "check_lock"})
            elif lock["codes_error"]:
                problems.append({"text": f"{lock['name']}: read failed", "action": "check_lock"})
            if prop["lock_automation"] and lock["fail_count"] and lock["state"] != "unavailable":
                # Hostaway/HA wording stays in the Log; the board gets a short line.
                error = (lock["last_error"] or "").split(" (")[0]
                problems.append({
                    "text": f"{lock['name']}: {error or 'check failed'}, tried {lock['fail_count']}x",
                    "action": "check_lock",
                })
            if lock["battery"] is not None and lock["battery"] < battery_limit:
                problems.append({"text": f"{lock['name']}: battery {lock['battery']}%", "action": ""})
        therms = therms_by_property.get(prop["id"], [])
        climate = _climate_views(therms, prop, reservations, now, cfg)
        for view in climate:
            if view["problem"]:
                problems.append({"text": view["problem"], "action": view["action"]})

        rows.append({
            "id": prop["id"],
            "name": prop["name"],
            "status": state.status,
            "status_label": STATUS_LABELS[state.status],
            "context": _context(state, tz),
            "current_guest": state.current["guest_name"] if state.current else "",
            "current_out": _fmt(state.current["check_out_at"], tz) if state.current else "",
            "next_guest": state.next["guest_name"] if state.next else "",
            "next_in": _fmt(state.next["check_in_at"], tz) if state.next else "",
            "next_out": _fmt(state.next["check_out_at"], tz) if state.next else "",
            "next_id": state.next["id"] if state.next else None,
            "code_custom": bool(state.next and state.next.get("override_code")),
            "next_in_sort": state.next["check_in_at"] if state.next else "9999",
            "door_code": (state.next or {}).get("door_code") or "",
            "code": code,
            "code_label": CODE_LABELS[code],
            "locks": locks,
            "lock_read": read_ago if locks else "",
            "lock_stale": bool(locks and read_at and read_stale),
            "problems": problems,
            "lock_automation": prop["lock_automation"],
            "climate": climate,
            "battery": min((l["battery"] for l in locks if l["battery"] is not None), default=None),
            "battery_low": any(l["battery"] is not None and l["battery"] < battery_limit for l in locks),
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
        now = syncer.now()
        sync_ago, sync_stale = _ago(
            db.get_setting("last_sync_at"), now,
            timedelta(minutes=max(45, syncer.settings.sync_minutes * 3)))
        webhook_ago, _ = _ago(db.get_setting("last_webhook_at"), now, timedelta(days=365))
        return templates.TemplateResponse(request, name, {
            "last_sync": _fmt(db.get_setting("last_sync_at"), syncer.tz),
            "last_webhook": _fmt(db.get_setting("last_webhook_at"), syncer.tz),
            "sync_ago": sync_ago,
            "sync_stale": sync_stale,
            "webhook_ago": webhook_ago,
            "notice": request.query_params.get("notice", "")[:200],
            **ctx,
        })

    @app.get("/", response_class=HTMLResponse)
    async def status(request: Request, q: str = "", show: str = "attention", sort: str = "attention"):
        rows = dashboard_rows(syncer, q)
        counts = {k: sum(1 for r in rows if r["status"] == k) for k in STATUS_LABELS}
        problems = sum(1 for r in rows if r["problems"])
        total = len(rows)
        filters = {
            "attention": lambda r: bool(r["problems"]),
            "arriving": lambda r: r["status"] in ("arriving_today", "turnover_today"),
            "departing": lambda r: r["status"] in ("departing_today", "turnover_today"),
            "occupied": lambda r: r["status"] == "occupied",
            "vacant": lambda r: r["status"] == "vacant",
        }
        show = show if show in filters or show == "all" else "attention"
        if show != "all":
            rows = [r for r in rows if filters[show](r)]
        sort = sort if sort == "name" else "attention"
        if sort == "name":
            rows.sort(key=lambda r: street_key(r["name"]))
        return page(request, "status.html", rows=rows, q=q, counts=counts, problems=problems, total=total,
                    show=show, sort=sort)

    @app.get("/properties", response_class=HTMLResponse)
    async def properties(request: Request):
        props = db.query("SELECT * FROM properties ORDER BY name")
        locks = db.query("SELECT * FROM locks ORDER BY name")
        by_property: dict[int, list[dict]] = {}
        for lock in locks:
            if lock["property_id"]:
                by_property.setdefault(lock["property_id"], []).append(lock)
        thermostats = db.query("SELECT * FROM thermostats ORDER BY name")
        therm_by_property: dict[int, list[dict]] = {}
        for t in thermostats:
            if t["property_id"]:
                therm_by_property.setdefault(t["property_id"], []).append(t)
        gaps = []
        for prop in props:
            prop["lock_list"] = by_property.get(prop["id"], [])
            prop["therm_list"] = therm_by_property.get(prop["id"], [])
            if not prop["active"]:
                continue
            if not prop["lock_list"]:
                gaps.append({"id": prop["id"], "name": prop["name"], "why": "No lock assigned"})
            for lock in prop["lock_list"]:
                if lock["state"] == "unavailable":
                    gaps.append({"id": prop["id"], "name": prop["name"], "why": f"{lock['name']} offline"})
            for t in prop["therm_list"]:
                if t["offline_since"]:
                    gaps.append({"id": prop["id"], "name": prop["name"], "why": f"{t['name']} offline"})
        automation = db.one(
            "SELECT COUNT(*) AS homes, COALESCE(SUM(lock_automation), 0) AS locks, "
            "COALESCE(SUM(thermostat_automation), 0) AS therms FROM properties WHERE active = 1")
        return page(request, "properties.html", properties=props, locks=locks, thermostats=thermostats,
                    gaps=gaps, automation=automation,
                    open_locks=[l for l in locks if not l["property_id"]],
                    open_thermostats=[t for t in thermostats if not t["property_id"]])

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
            for field, column, label in (("auto", "lock_automation", "Lock automation"),
                                         ("tauto", "thermostat_automation", "Thermostat automation"),
                                         ("active", "active", "Active")):
                now_on = 1 if form.get(f"{field}_{pid}") else 0
                was_on = 1 if form.get(f"was_{field}_{pid}") == "1" else 0
                if now_on != was_on and now_on != p[column]:
                    sets.append(f"{column} = ?")
                    args.append(now_on)
                    toggles.append((f"{field}.{'on' if now_on else 'off'}",
                                    f"{label} turned {'ON' if now_on else 'OFF'} for {p['name']} (Properties page)",
                                    pid))
                    if field != "tauto":
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
            if kind == "tauto.on":  # start from what the stay looks like now, not from an old setting
                db.execute("UPDATE thermostats SET last_mode = NULL, last_applied_at = NULL WHERE property_id = ?",
                           (pid,))

        for lock in db.query("SELECT id, property_id FROM locks"):
            if f"was_lock_{lock['id']}" not in form:
                continue
            value, was = str(form.get(f"lock_{lock['id']}", "")), str(form.get(f"was_lock_{lock['id']}", ""))
            if value != was:
                new_pid = int(value) if value.isdigit() else None
                if new_pid != lock["property_id"]:
                    syncer.assign_lock(lock["id"], new_pid)
                    touched |= {x for x in (new_pid, lock["property_id"]) if x}

        for t in db.query("SELECT id, property_id FROM thermostats"):
            if f"was_thermo_{t['id']}" not in form:
                continue
            value, was = str(form.get(f"thermo_{t['id']}", "")), str(form.get(f"was_thermo_{t['id']}", ""))
            if value != was:
                new_pid = int(value) if value.isdigit() else None
                if new_pid != t["property_id"]:
                    db.execute("UPDATE thermostats SET property_id = ?, match_source = 'manual', last_mode = NULL, "
                               "last_applied_at = NULL WHERE id = ?", (new_pid, t["id"]))

        # Only the homes that changed get their locks looked at again (each look is a slow Schlage call).
        if touched:
            db.execute(f"UPDATE locks SET next_check_at = NULL WHERE property_id IN ({', '.join('?' for _ in touched)})",
                       tuple(touched))
        db.log("properties.saved", "Property settings saved")
        return _done("properties", "Property settings saved")

    @app.get("/staff", response_class=HTMLResponse)
    async def staff(request: Request):
        return page(
            request, "staff.html",
            staff=db.query("SELECT * FROM staff ORDER BY name"),
            recipients=db.query("SELECT * FROM alert_recipients ORDER BY name"),
            previews=[{**p, "expires_local": _fmt(p["expires_at"], syncer.tz)} for p in db.query(
                "SELECT v.*, p.name AS property_name FROM preview_codes v JOIN properties p ON p.id = v.property_id "
                "WHERE v.expires_at > ? ORDER BY v.expires_at", (_utc(syncer.now()),))],
            lock_homes=db.query("SELECT id, name FROM properties WHERE lock_automation = 1 AND active = 1 "
                                "ORDER BY name"),
        )

    # ---- guest code override and visitor (preview) codes -----------------------

    def codes_in_use(property_id: int, *, skip_reservation: int | None = None,
                     skip_preview: int | None = None) -> set[str]:
        """Every code this home already hands out, so a new one never collides with it."""
        used = {r["code"] for r in db.query("SELECT code FROM staff WHERE active = 1")}
        prop = db.one("SELECT backup_code FROM properties WHERE id = ?", (property_id,))
        if prop and prop["backup_code"]:
            used.add(prop["backup_code"])
        now = syncer.now()
        for r in syncer.reservations_for(property_id):  # stays that have not ended; past guests do not count
            if (r["active"] and r["door_code"] and r["id"] != skip_reservation
                    and datetime.fromisoformat(r["check_out_at"]) > now):
                used.add(r["door_code"])
        for v in db.query("SELECT id, code FROM preview_codes WHERE property_id = ? AND expires_at > ?",
                          (property_id, _utc(syncer.now()))):
            if v["id"] != skip_preview:
                used.add(v["code"])
        return used

    def opens_lock_already(property_id: int, code: str) -> bool:
        digest = syncer.code_hash(code)
        for lock in db.query("SELECT code_hashes FROM locks WHERE property_id = ? AND code_hashes IS NOT NULL",
                             (property_id,)):
            if digest in json.loads(lock["code_hashes"]):
                return True
        return False

    def recheck_locks(property_id: int) -> None:
        db.execute("UPDATE locks SET next_check_at = NULL WHERE property_id = ?", (property_id,))

    @app.post("/code-override")
    async def code_override(reservation_id: int = Form(...), code: str = Form(""),
                            show: str = Form("attention"), sort: str = Form("attention"), q: str = Form("")):
        """Staff change the code a guest uses (the guest asked for a different one). Blank goes back to Hostaway's."""
        row = db.one("SELECT r.*, p.id AS pid, p.name AS pname FROM reservations r "
                     "JOIN properties p ON p.hostaway_listing_id = r.listing_id WHERE r.id = ?", (reservation_id,))
        if row is None:
            raise HTTPException(404, "That reservation is not in the list any more")
        code = code.strip()
        if code == (row["door_code"] or ""):
            code = ""  # same as Hostaway's: nothing to override
        if code:
            if not code.isdigit() or len(code) != 4:
                raise HTTPException(400, f"{row['pname']}: the code must be exactly 4 digits")
            if code != (row["override_code"] or ""):
                if code in codes_in_use(row["pid"], skip_reservation=reservation_id):
                    raise HTTPException(400, f"{row['pname']}: {code} is already another guest's, a staff, backup "
                                             "or visitor code at this home. Pick a different one")
                if opens_lock_already(row["pid"], code):
                    raise HTTPException(400, f"{row['pname']}: {code} already opens this lock under another name "
                                             "(Master, a cleaner...). Pick a different one")
        db.execute("UPDATE reservations SET override_code = ? WHERE id = ?", (code or None, reservation_id))
        recheck_locks(row["pid"])
        db.log("code.override" if code else "code.override_cleared",
               f"Guest code for {row['guest_name'] or reservation_id} "
               f"{'changed on the dashboard' if code else 'back to the Hostaway code'}",
               property_id=row["pid"], reservation_id=reservation_id)
        return _done(_board_back(show, sort, q), "Guest code saved" if code else "Guest code back to the Hostaway code")

    @app.post("/preview-add")
    async def preview_add(property_id: int = Form(...), label: str = Form(""), code: str = Form(""),
                          hours: str = Form("24")):
        prop = db.one("SELECT * FROM properties WHERE id = ?", (property_id,))
        if prop is None or not prop["lock_automation"] or not prop["active"]:
            raise HTTPException(400, "Choose a home that has Lock automation ticked on the Properties page")
        if not hours.strip().isdigit() or not 1 <= int(hours) <= 168:
            raise HTTPException(400, "Hours must be a whole number from 1 to 168")
        code = code.strip()
        if code and (not code.isdigit() or len(code) != 4):
            raise HTTPException(400, "The code must be exactly 4 digits (leave it blank to pick one)")
        used = codes_in_use(property_id)
        if code and (code in used or opens_lock_already(property_id, code)):
            raise HTTPException(400, f"{code} is already used at this home. Pick a different one or leave it blank")
        code = code or new_backup_code(lambda c: c in used or opens_lock_already(property_id, c))
        now = syncer.now()
        db.execute("INSERT INTO preview_codes(property_id, label, code, expires_at, created_at) VALUES(?, ?, ?, ?, ?)",
                   (property_id, label.strip()[:60], code, _utc(now + timedelta(hours=int(hours))), _utc(now)))
        recheck_locks(property_id)
        db.log("preview.added", f"Visitor code for {label.strip() or 'a visitor'} added to {prop['name']} "
                                f"for {int(hours)} hours", property_id=property_id)
        return _done("staff", "Visitor code added")

    @app.post("/preview-delete")
    async def preview_delete(preview_id: int = Form(...)):
        row = db.one("SELECT * FROM preview_codes WHERE id = ?", (preview_id,))
        if row:  # expire it now; the next lock pass removes it from the lock
            db.execute("UPDATE preview_codes SET expires_at = ? WHERE id = ?", (_utc(syncer.now()), preview_id))
            recheck_locks(row["property_id"])
            db.log("preview.removed", f"Visitor code {row['label'] or preview_id} ended early",
                   property_id=row["property_id"])
        return _done("staff", "Visitor code ended")

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
        return _done("staff", "Staff codes saved")

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
        return _done("staff", "Alert recipients saved")

    @app.get("/events", response_class=HTMLResponse)
    async def events(request: Request, q: str = "", level: str = "", kind: str = ""):
        where: list[str] = []
        args: list[Any] = []
        if q.strip():
            where.append("(IFNULL(p.name, '') LIKE ? OR e.message LIKE ? OR e.kind LIKE ?)")
            like = f"%{q.strip()}%"
            args.extend((like, like, like))
        if level in ("error", "warning", "info"):
            where.append("e.level = ?")
            args.append(level)
        if kind:
            where.append("e.kind = ?")
            args.append(kind)
        sql = ("SELECT e.*, p.name AS property FROM events e "
               "LEFT JOIN properties p ON p.id = e.property_id ")
        if where:
            sql += "WHERE " + " AND ".join(where) + " "
        rows = db.query(sql + "ORDER BY e.id DESC LIMIT 300", tuple(args))
        for r in rows:
            r["at_local"] = _fmt(r["at"], syncer.tz)
        kinds = [r["kind"] for r in db.query("SELECT DISTINCT kind FROM events ORDER BY kind")]
        return page(request, "events.html", events=rows, q=q, level=level, kind=kind, kinds=kinds)

    @app.post("/sync")
    async def sync_now():
        await syncer.import_listings()
        await syncer.discover_locks()
        changed = await syncer.sync_reservations()
        db.log("sync.manual", f"Manual sync: {changed} reservations changed")
        return _done(".", f"Sync finished: {changed} reservation{'' if changed == 1 else 's'} changed")

    @app.post("/lock-refresh")
    async def lock_refresh(property_id: int = Form(...), show: str = Form("attention"),
                           sort: str = Form("attention"), q: str = Form("")):
        for lock in db.query("SELECT * FROM locks WHERE property_id = ?", (property_id,)):
            await syncer.read_lock_codes(lock)
        return _done(_board_back(show, sort, q), "Lock read")

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
            webhook_full=f"{public_url}/api/webhook/{webhook_id}" if webhook_id and public_url else "",
            registered=db.get_setting("hostaway_webhook_registered_at"),
            is_addon=syncer.settings.is_addon,
            lock_guest=[(k, SETTING_LABELS[k], db.get_int(k, DEFAULTS[k])) for k in LOCK_GUEST_KEYS],
            lock_advanced=[(k, SETTING_LABELS[k], db.get_int(k, DEFAULTS[k])) for k in LOCK_ADVANCED_KEYS],
            climate_guest=[(k, CLIMATE_LABELS[k], db.get_int(k, CLIMATE_DEFAULTS[k])) for k in CLIMATE_GUEST_KEYS],
            climate_months=[(k, CLIMATE_LABELS[k], db.get_int(k, CLIMATE_DEFAULTS[k])) for k in CLIMATE_MONTH_KEYS],
            climate_advanced=[(k, CLIMATE_LABELS[k], db.get_int(k, CLIMATE_DEFAULTS[k])) for k in CLIMATE_ADVANCED_KEYS],
            month_names=MONTH_NAMES,
            thermostat_homes=db.one("SELECT COUNT(*) AS n FROM properties WHERE thermostat_automation = 1")["n"],
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
        return _done("setup", "Lock settings saved")

    @app.post("/thermostat-settings-save")
    async def thermostat_settings_save(request: Request):
        form = await request.form()
        values: dict[str, int] = {}
        for key, (low, high) in CLIMATE_RANGES.items():  # check everything before saving anything
            raw = str(form.get(key, "")).strip()
            if not raw.isdigit() or not low <= int(raw) <= high:
                raise HTTPException(400, f"{CLIMATE_LABELS[key]}: enter a whole number from {low} to {high}")
            values[key] = int(raw)
        if values["vac_winter"] > values["occ_winter"]:
            raise HTTPException(400, "Winter vacant must not be warmer than winter occupied")
        if values["vac_summer"] < values["occ_summer"]:
            raise HTTPException(400, "Summer vacant must not be cooler than summer occupied")
        for key, value in values.items():
            db.set_setting(key, str(value))
        # Re-apply vacant homes so a new vacant number is picked up. An occupied stay was already
        # set in the 3 hours before check-in; clearing it would switch the guest's mode and temperature.
        db.execute("UPDATE thermostats SET last_mode = NULL, last_applied_at = NULL "
                   "WHERE last_mode IS NULL OR last_mode != 'occupied'")
        db.log("settings.saved", "Thermostat settings saved")
        return _done("setup", "Thermostat settings saved")

    @app.post("/test-alert")
    async def test_alert():
        if locks is not None:
            await locks.alert("Stay Automation test", "Alerts are working.")
        return _done("setup", "Test alert sent")

    @app.post("/setup-webhook")
    async def setup_webhook(public_url: str = Form(...), addon_slug: str = Form(...),
                            register_hostaway: str = Form("")):
        public_url = public_url.strip().rstrip("/")
        if not public_url.startswith("https://"):
            raise HTTPException(400, "Public URL must start with https://")
        db.set_setting("public_url", public_url)
        slug = addon_slug.strip()
        if syncer.settings.is_addon:  # the real slug beats whatever was typed or left over
            try:
                slug = await syncer.ha.own_slug() or slug
            except Exception:
                pass
        db.set_setting("addon_slug", slug)
        webhook_id = db.get_setting("webhook_id") or secrets.token_urlsafe(32)
        db.set_setting("webhook_id", webhook_id)
        await syncer.ha.create_webhook_automation(webhook_id, slug)
        db.log("setup.webhook", "Home Assistant webhook automation saved")
        if register_hostaway:
            await syncer.hostaway.register_webhook(f"{public_url}/api/webhook/{webhook_id}")
            db.set_setting("hostaway_webhook_registered_at", datetime.now(syncer.tz).isoformat())
            db.log("setup.webhook", "Webhook registered with Hostaway")
        return _done("setup", "Webhook registered")

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
