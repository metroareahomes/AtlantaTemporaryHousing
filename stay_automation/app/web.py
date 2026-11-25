"""Dashboard served through HA Ingress. All links are relative so they work under the ingress path."""
import asyncio
import json
import re
import secrets
import sqlite3

import httpx
from contextlib import asynccontextmanager, nullcontext, suppress
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit, parse_qs

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from starlette.datastructures import FormData
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .extension_live import HOLD_FIELDS, calendar_enabled_for, hold_settings, live_property, owner_setting, run_extension_live, selected_properties
from .extension_preview import interpret_inquiry_reply, local_scenario_time, scan_extensions, simulate_inquiry
from .extension_pricing import OWNERS as EXTENSION_OWNERS, SETTINGS_KEY as EXTENSION_SETTINGS_KEY
from .extension_pricing import read_settings as extension_rules, validate_settings as validate_extension_rules
from .extension_report import report_rows as extension_report_rows
from .booking_report import report_window, thursday_bookings
from .climate import (
    DEFAULTS as CLIMATE_DEFAULTS, LABELS as CLIMATE_LABELS, RANGES as CLIMATE_RANGES,
    VERIFY_GRACE_SECONDS, is_summer, occupancy, occupying_reservation, setpoint, took_effect,
)
from .daily_temperature import DEFAULTS as DAILY_DEFAULTS, RANGES as DAILY_RANGES, current_stay
from .alerts import CATEGORIES, OWNERS, SMTP_FIELDS, email_address
from .hostaway import HostawayError
from .locks import DEFAULT_GUEST_MESSAGE, DEFAULTS, LockManager, _utc, add_time, new_backup_code
from .reservations import code_status, property_state, within
from .sync import Syncer
from .setup_help import SETUP_FIELDS
from .pricing import manual_settings, PricingSimulator, DEFAULTS as PRICING_DEFAULTS, TZ as PRICING_TZ, read_config as pricing_config, validate as validate_pricing
from .property_sheet import PropertySheet, valid_endpoint

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


def _board_back(show: str, sort: str, q: str, notice: str = "", *, owner: str = "") -> str:
    show = show if show in BOARD_SHOWS else "attention"
    sort = "name" if sort == "name" else "attention"
    args = {"show": show, "sort": sort, "q": q}
    if owner in EXTENSION_OWNERS:
        args["owner"] = owner
    if notice:
        args["notice"] = notice
    return ".?" + urlencode(args)


SETUP_SECTIONS = (
    ("connections", "Connections"),
    ("locks", "Locks and guest access"),
    ("thermostats", "Thermostats"),
    ("pricing", "Pricing rules"),
    ("alerts", "Staff email alerts"),
)


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
            t["state"] == want_mode and took_effect({"temperature": schedule_temp}, t)
        )
        schedule = f"{want_mode.title()} {schedule_temp}°"
        now_bits = " ".join(x for x in (_deg(t["current_temp"]), t["state"]) if x)
        set_bit = _set_phrase(t)
        verifying = t.get("verification_started_at")
        if verifying and not on_target:
            try:
                elapsed = now - datetime.fromisoformat(verifying)
            except (TypeError, ValueError):
                elapsed = None
            if elapsed is not None and timedelta(0) <= elapsed < timedelta(seconds=VERIFY_GRACE_SECONDS):
                views.append({"name": t["name"], "line": f"Waiting for thermostat to confirm {schedule}",
                              "ok": True, "detail": detail, "problem": None, "action": ""})
                continue
        if t["fail_count"] and not adopted and not on_target:
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
        if not occupying:
            views.append({"name": t["name"], "line": f"Vacant setting will be restored to {asked} · now {now_bits}" + (f", {set_bit}" if set_bit else ""),
                          "ok": True, "detail": detail, "action": "", "problem": None})
            continue
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


def missing_code_warning_due(reservation: dict[str, Any] | None, now: datetime,
                             add_hour: int, early_lead_hours: int,
                             final_check_minutes: int, grace_minutes: int = 15) -> bool:
    """Only warn after the lock's scheduled guest-code installation plus grace."""
    if reservation is None:
        return False
    scheduled = add_time(reservation, add_hour, early_lead_hours, final_check_minutes)
    return now >= scheduled + timedelta(minutes=grace_minutes)


def dashboard_rows(syncer: Syncer, query: str = "", *, owner: str = "") -> list[dict[str, Any]]:
    now = syncer.now()
    tz = syncer.tz
    locks_by_property: dict[int, list[dict]] = {}
    for lock in syncer.db.query("SELECT * FROM locks WHERE property_id IS NOT NULL ORDER BY name"):
        locks_by_property.setdefault(lock["property_id"], []).append(lock)

    therms_by_property: dict[int, list[dict]] = {}
    for t in syncer.db.query(
        "SELECT t.*, s.value AS verification_started_at FROM thermostats t "
        "LEFT JOIN settings s ON s.key = 'thermostat_verifying:' || t.entity_id "
        "WHERE t.property_id IS NOT NULL ORDER BY t.name"
    ):
        therms_by_property.setdefault(t["property_id"], []).append(t)
    battery_limit = syncer.db.get_int("battery_alert_percent", CLIMATE_DEFAULTS["battery_alert_percent"])
    cfg = {k: syncer.db.get_int(k, v) for k, v in CLIMATE_DEFAULTS.items()}

    rows = []
    for prop in syncer.db.query(
        "SELECT * FROM properties WHERE active=1 AND (?='' OR owner_scope=?) ORDER BY name",
        (owner, owner),
    ):
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
        if code == "missing" and soon and state.next:
            lock_cfg = {key: syncer.db.get_int(key, DEFAULTS[key])
                        for key in ("add_hour", "early_lead_hours", "final_check_minutes")}
            if missing_code_warning_due(state.next, now, **lock_cfg):
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


def create_app(syncer: Syncer, locks: LockManager | None = None, lifespan=None, *, pricing=None) -> FastAPI:
    booking_report_task = None

    @asynccontextmanager
    async def app_lifespan(app):
        async with (lifespan(app) if lifespan else nullcontext()) as state:
            try:
                yield state
            finally:
                if booking_report_task is not None and not booking_report_task.done():
                    booking_report_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await booking_report_task

    app = FastAPI(lifespan=app_lifespan, docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    # The stylesheet URL changes whenever the file does, so a browser or the HA ingress proxy never serves a stale copy.
    templates.env.globals["css_v"] = int((HERE / "static" / "style.css").stat().st_mtime)
    db = syncer.db
    pricing = pricing if pricing is not None else PricingSimulator(syncer)

    @app.exception_handler(HostawayError)
    async def hostaway_failed(request: Request, exc: HostawayError):
        """Show what is wrong (usually the credentials) instead of a 500 stack trace."""
        db.log("hostaway.error", str(exc), level="error")
        return page(request, "error.html", response_status=502, message=str(exc))

    @app.exception_handler(StarletteHTTPException)
    async def bad_input(request: Request, exc: StarletteHTTPException):
        """A rejected form entry (wrong code length, duplicate name...) gets a readable page, not bare JSON.
        Webhooks and other machine callers keep the normal response."""
        if exc.status_code != 400 or "webhook" in request.url.path:
            return await http_exception_handler(request, exc)
        return page(request, "error.html", response_status=400, title="That was not saved", message=exc.detail)

    def page(request: Request, name: str, *, response_status: int = 200, **ctx: Any) -> HTMLResponse:
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
            "setup_sections": SETUP_SECTIONS,
            **ctx,
        }, status_code=response_status)

    @app.get("/", response_class=HTMLResponse)
    async def status(request: Request, q: str = "", show: str = "attention", sort: str = "attention",
                     owner: str = ""):
        ref = urlsplit(request.headers.get('referer', ''))
        public = urlsplit(db.get_setting('public_url', '') or '')
        if public.netloc and ref.netloc == public.netloc and '/hassio/ingress/' in ref.path and parse_qs(ref.query).get('stay_page') == ['properties']:
            return RedirectResponse('properties', status_code=302)
        owner = owner if owner in EXTENSION_OWNERS else ""
        rows = dashboard_rows(syncer, q, owner=owner)
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
                    show=show, sort=sort, selected_owner=owner, owner_options=EXTENSION_OWNERS)

    @app.get("/properties", response_class=HTMLResponse)
    async def properties(request: Request, owner: str = ""):
        owner = owner if owner in EXTENSION_OWNERS else ""
        props = db.query("SELECT * FROM properties WHERE (?='' OR owner_scope=?) ORDER BY name", (owner, owner))
        property_ids = {p["id"] for p in props}
        locks = db.query("SELECT * FROM locks ORDER BY name")
        thermostats = db.query("SELECT * FROM thermostats ORDER BY name")
        if owner:
            locks = [lock for lock in locks if lock["property_id"] in property_ids]
            thermostats = [t for t in thermostats if t["property_id"] in property_ids]
        by_property: dict[int, list[dict]] = {}
        for lock in locks:
            if lock["property_id"]:
                by_property.setdefault(lock["property_id"], []).append(lock)
        therm_by_property: dict[int, list[dict]] = {}
        for t in thermostats:
            if t["property_id"]:
                therm_by_property.setdefault(t["property_id"], []).append(t)
        gaps = []
        for prop in props:
            stay = current_stay(syncer.reservations_for(prop["id"]), syncer.now())
            prop["daily_pause"] = (db.one("SELECT * FROM thermostat_stay_pauses WHERE reservation_id=?", (stay["id"],)) if stay else None)
            prop["daily_ntc_pending"] = bool(stay and db.one("SELECT 1 FROM thermostat_note_updates WHERE reservation_id=? AND synced_at IS NULL", (stay["id"],)))
            if prop["daily_ntc_pending"] and not prop["daily_pause"]:
                prop["daily_pause"] = {"reason": "Daily adjustment paused; NTC is waiting to save in Hostaway.", "source": "Automatic pause"}
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
            "COALESCE(SUM(thermostat_automation), 0) AS therms FROM properties "
            "WHERE active=1 AND (?='' OR owner_scope=?)", (owner, owner))
        return page(request, "properties.html", properties=props, locks=locks, thermostats=thermostats,
                    gaps=gaps, automation=automation, selected_owner=owner, owner_options=EXTENSION_OWNERS,
                    open_locks=[l for l in locks if not l["property_id"]],
                    open_thermostats=[t for t in thermostats if not t["property_id"]])

    @app.get("/listing-preview/{property_id}", response_class=JSONResponse)
    async def download_listing_preview(property_id: int):
        prop = db.one("SELECT hostaway_listing_id FROM properties WHERE id=?", (property_id,))
        if not prop or not prop["hostaway_listing_id"]:
            raise HTTPException(404, "No Hostaway listing linked to this property")
        try:
            preview = await syncer.hostaway.listing_preview(prop["hostaway_listing_id"])
        except (HostawayError, httpx.HTTPError, ValueError):
            raise HTTPException(502, "Hostaway listing preview could not be retrieved") from None
        return JSONResponse(preview, headers={
            "Content-Disposition": f'attachment; filename="listing-preview-{property_id}.json"',
            "Cache-Control": "no-store",
        })

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
        owner = form.get("owner")
        destination = "properties?" + urlencode({"owner": owner}) if owner in EXTENSION_OWNERS else "properties"
        return _done(destination, "Property settings saved")

    @app.get("/staff", response_class=HTMLResponse)
    async def staff(request: Request, owner: str = ""):
        owner = owner if owner in EXTENSION_OWNERS else ""
        return page(
            request, "staff.html",
            selected_owner=owner, owner_options=EXTENSION_OWNERS,
            staff=db.query("SELECT * FROM staff WHERE (?='' OR owner_scope=?) ORDER BY name", (owner, owner)),
            recipients=db.query("SELECT * FROM alert_recipients ORDER BY name"),
            alert_categories=CATEGORIES, alert_owners=OWNERS,
            owner_counts={r['owner_scope'] or 'Unconfirmed': r['n'] for r in db.query("SELECT owner_scope, COUNT(*) AS n FROM properties WHERE active=1 GROUP BY owner_scope")},
            delivery_issues=db.query("SELECT d.*,r.name FROM staff_alert_deliveries d LEFT JOIN alert_recipients r ON r.id=d.recipient_id WHERE d.status IN ('pending','uncertain') ORDER BY d.id DESC LIMIT 30"),
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
                            show: str = Form("attention"), sort: str = Form("attention"), q: str = Form(""),
                            owner: str = Form("")):
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
        return _done(_board_back(show, sort, q, owner=owner), "Guest code saved" if code else "Guest code back to the Hostaway code")

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
        owner = str(form.get("owner", ""))
        if owner and owner not in EXTENSION_OWNERS:
            raise HTTPException(400, "Choose Mike or Kurt for staff lock access")

        def check(name: str, code: str) -> None:
            if name.startswith("HA-"):
                raise HTTPException(400, "Staff names cannot start with HA- (reserved for guest/backup codes)")
            if not code.isdigit() or len(code) != 4:
                raise HTTPException(400, f"{name}: code must be exactly 4 digits")

        # Validate everything first so a typo can never half-save (or silently drop someone's lock code).
        updates, seen, active_codes = [], set(), set()
        rows = db.query("SELECT id, name, code, active, owner_scope FROM staff")
        editable = [
            row for row in rows
            if (not owner or row["owner_scope"] == owner) and f"name_{row['id']}" in form
        ]
        editable_ids = {row["id"] for row in editable}
        for row in rows:
            if row["id"] not in editable_ids:
                seen.add(row["name"])
                if row["active"]:
                    active_codes.add(row["code"])
        for row in editable:
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
                updates.append(("INSERT OR IGNORE INTO staff(name, code, active, owner_scope) VALUES(?, ?, 0, ?)",
                                (row["name"], row["code"], row["owner_scope"])))
        new_name = str(form.get("name_new", "")).strip()
        new_code = str(form.get("code_new", "")).strip()
        if new_name or new_code:
            if not owner:
                raise HTTPException(400, "Choose Mike or Kurt before adding staff")
            if not new_name:
                raise HTTPException(400, "New staff needs a name")
            check(new_name, new_code)
            if new_name in seen or db.one("SELECT 1 FROM staff WHERE name = ?", (new_name,)):
                raise HTTPException(400, f"{new_name} is already in the list (tick Active on that row to bring "
                                         "them back)")
            new_active = 1 if form.get("active_new") else 0
            if new_active and new_code in active_codes:
                raise HTTPException(400, f"Code {new_code} is used by more than one active person")
            updates.append(("INSERT INTO staff(name, code, active, owner_scope) VALUES(?, ?, ?, ?)",
                            (new_name, new_code, new_active, owner)))
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
        return _done("staff?" + urlencode({"owner": owner}) if owner else "staff", "Staff codes saved")

    @app.post("/recipients-save")
    async def recipients_save(request: Request):
        form = await request.form()

        updates = []
        emails = set()
        extended = form.get("recipient_settings_version") == "2"
        rows = db.query("SELECT * FROM alert_recipients") + [{"id": "new", "owners": "Mike,Kurt", "alert_types": "all"}]
        for row in rows:
            rid = row['id']
            name = str(form.get(f"rname_{rid}", "")).strip()
            target = str(form.get(f"rtarget_{rid}", "")).strip()
            email = str(form.get(f"remail_{rid}", row.get('email', ''))).strip().lower()
            active = int(bool(form.get(f"ractive_{rid}")))
            if not name or not (target or email):
                if rid == 'new':
                    if name or target or email:
                        raise HTTPException(400, "New recipient needs a name and an email address or app notify target")
                else:
                    updates.append(("DELETE FROM alert_recipients WHERE id=?", (rid,)))
                continue
            if any(c in name for c in '\r\n') or len(name) > 120:
                raise HTTPException(400, "Enter a short recipient name without line breaks")
            if target and not re.fullmatch(r"notify\.[a-zA-Z0-9_]+", target):
                raise HTTPException(400, f"{name}: enter a valid notify service")
            if email and (not email_address(email) or email in emails):
                raise HTTPException(400, f"{name}: enter a valid, unique email address")
            if email:
                emails.add(email)
            owners = ','.join(o for o in OWNERS if form.get(f"rowner_{rid}_{o}")) if extended else row['owners']
            types = ','.join(c for c in CATEGORIES if form.get(f"rtype_{rid}_{c}")) if extended else row['alert_types']
            if active and not owners:
                raise HTTPException(400, f"{name}: select Mike, Kurt, or both")
            values = (name, target, email, owners, types, active)
            if rid == 'new':
                updates.append(("INSERT INTO alert_recipients(name,target,email,owners,alert_types,active) VALUES(?,?,?,?,?,?)", values))
            else:
                updates.append(("UPDATE alert_recipients SET name=?,target=?,email=?,owners=?,alert_types=?,active=? WHERE id=?", (*values, rid)))
        for sql, args in updates:
            db.execute(sql, args)
        db.log("recipients.saved", "Alert recipients saved")
        return _done("staff", "Alert recipients saved")

    @app.post("/recipient-test")
    async def recipient_test(recipient_id: str = Form('')):
        if not re.fullmatch(r'[1-9][0-9]{0,17}', recipient_id):
            return _done('staff', 'Choose a saved active recipient before sending a test. Add your name and email, check Active, and press Save recipients first.')
        saved_id = int(recipient_id)
        if not db.one("SELECT 1 FROM alert_recipients WHERE id=? AND active=1 AND (email<>'' OR target<>'')", (saved_id,)):
            return _done('staff', 'That recipient is no longer active or has no destination. Save an active recipient, then select them again.')
        if locks is None:
            return _done('staff', 'The alert service is unavailable. No test was queued.')
        await locks.alert_service.test_recipient(saved_id)
        return _done('staff', 'Test alert queued for that recipient; check their inbox or phone')

    @app.post("/alert-delivery-retry")
    async def alert_delivery_retry(delivery_id: int = Form(...), checked_inbox: str = Form('')):
        if not checked_inbox:
            raise HTTPException(400, "Check the recipient inbox first; retrying an uncertain email may send a duplicate")
        db.execute("UPDATE staff_alert_deliveries SET status='pending',next_attempt_at=? WHERE id=? AND status='uncertain'",
                   (syncer.now().astimezone(timezone.utc).isoformat(timespec='seconds'), delivery_id))
        return _done('staff', 'Email retry queued')

    @app.get("/events", response_class=HTMLResponse)
    async def events(request: Request, q: str = "", level: str = "", kind: str = "",
                     property_id: str = ""):
        selected_property = int(property_id) if re.fullmatch(r"[1-9][0-9]{0,17}", property_id) else None
        where: list[str] = []
        args: list[Any] = []
        if selected_property is not None:
            where.append("e.property_id = ?")
            args.append(selected_property)
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
        properties = db.query("SELECT id, name FROM properties ORDER BY name COLLATE NOCASE, id")
        return page(request, "events.html", events=rows, q=q, level=level, kind=kind,
                    kinds=kinds, properties=properties, property_id=selected_property)

    @app.post("/sync")
    async def sync_now():
        await syncer.import_listings()
        await syncer.discover_locks()
        changed = await syncer.sync_reservations()
        db.log("sync.manual", f"Manual sync: {changed} reservations changed")
        return _done(".", f"Sync finished: {changed} reservation{'' if changed == 1 else 's'} changed")

    @app.post("/lock-refresh")
    async def lock_refresh(property_id: int = Form(...), show: str = Form("attention"),
                           sort: str = Form("attention"), q: str = Form(""), owner: str = Form("")):
        locks = db.query("SELECT * FROM locks WHERE property_id = ?", (property_id,))
        if not locks:
            return _done(_board_back(show, sort, q, owner=owner), "No locks found for this property.")
        states_checked = codes_checked = True
        for lock in locks:
            state_checked = await syncer.refresh_lock_state(lock)
            code_checked = await syncer.read_lock_codes(lock)
            states_checked = states_checked and state_checked
            codes_checked = codes_checked and code_checked
            if code_checked:
                outcome = "codes read"
                if not state_checked:
                    outcome += "; current lock state could not be confirmed"
                db.log("lock.checked", f"Manual check of {lock['name']}: {outcome}",
                       property_id=property_id)
        if not codes_checked:
            notice = "Could not read codes from one or more locks. See the activity log."
        elif not states_checked:
            notice = "Codes checked; current lock state could not be confirmed. Previous availability retained."
        else:
            notice = "Lock state and codes checked"
        return _done(_board_back(show, sort, q, owner=owner), notice)

    previews = {owner: {"rows": [], "running": False, "completed": 0, "total": 0}
                for owner in EXTENSION_OWNERS}

    refreshes = {owner: {"running": False, "message": ""} for owner in EXTENSION_OWNERS}

    def validate_owner(owner):
        if owner not in EXTENSION_OWNERS:
            raise HTTPException(404)

    async def refresh_extension_calendar(owner):
        calendar_refresh = refreshes[owner]
        try:
            calendar_refresh["message"] = await run_extension_live(syncer, calendar_only=True, owner=owner)
        except Exception:
            calendar_refresh["message"] = "The calendar review failed. Check the Stay Automation log."
            db.log("extension.live_calendar_failed", "Manual calendar review failed.", level="warning")
        finally:
            calendar_refresh["running"] = False

    @app.post("/extension-calendar-update")
    async def update_extension_calendar(background_tasks: BackgroundTasks, owner: str = "Kurt"):
        validate_owner(owner)
        calendar_refresh = refreshes[owner]
        if calendar_enabled_for(db, owner) and not calendar_refresh["running"]:
            calendar_refresh.update(running=True, message="")
            background_tasks.add_task(refresh_extension_calendar, owner)
        return RedirectResponse(f"extension-preview?owner={owner}#live-calendar", status_code=303, background=background_tasks)

    def extension_live_controls(owner):
        return {
            "calendar_refresh": refreshes[owner],
            "extension_owner": owner,
            "live_calendar": calendar_enabled_for(db, owner),
            "hold_fields": HOLD_FIELDS,
            "hold_rules": hold_settings(db, owner),
            "live_selected": selected_properties(db, owner),
            "live_properties": db.query(
                "SELECT name, hostaway_listing_id FROM properties "
                "WHERE active=1 AND owner_scope=? AND hostaway_listing_id IS NOT NULL ORDER BY name", (owner,)
            ),
        }

    @app.post("/extension-live-settings")
    async def save_extension_live_settings(request: Request, calendar_enabled: bool = Form(False),
                                           properties: list[int] = Form(default=[]), owner: str = "Kurt"):
        validate_owner(owner)
        form = await request.form()
        rules = hold_settings(db, owner)
        for key, label, maximum in HOLD_FIELDS:
            if key not in form:
                continue
            value = str(form[key]).strip()
            if not value:
                rules.pop(key, None)
            elif value.isascii() and value.isdecimal() and len(value) <= 3 and int(value) <= maximum:
                rules[key] = int(value)
            else:
                raise HTTPException(422, f"{label} must be blank or a whole number from 0 to {maximum}.")
        db.set_settings({
            owner_setting("extension_hold_rules", owner): json.dumps(rules),
            owner_setting("extension_live_calendar", owner): "1" if calendar_enabled else "0",
            owner_setting("extension_live_properties", owner): json.dumps(sorted({
                listing for listing in properties if live_property(db, listing, owner)
            })),
        })
        return RedirectResponse(f"extension-preview?owner={owner}", status_code=303)

    def preview_filter(owner, listing):
        validate_owner(owner)
        if listing and not live_property(db, listing, owner):
            raise HTTPException(404)
        return [row for row in previews[owner]["rows"]
                if (not listing or row["listing_id"] == listing)
                and live_property(db, row["listing_id"], owner)]

    @app.get("/extension-preview", response_class=HTMLResponse)
    async def show_extension_preview(request: Request, owner: str = "Kurt", listing: int = 0):
        rows = preview_filter(owner, listing)
        return page(request, "extension_preview.html", scan=previews[owner],
                    preview_rows=rows, preview_listing=listing,
                    configured=bool(syncer.settings.extension_ai_api_key), **extension_live_controls(owner))

    @app.post("/extension-preview-inquiry", response_class=HTMLResponse)
    async def run_extension_inquiry(request: Request, reservation: int = Form(...),
                                    arrival: str = Form(...), departure: str = Form(...),
                                    guest_reply: str = Form(""), guest_checkout: str = Form(""),
                                    reply_text: str = Form(""), reply_at: str = Form(""),
                                    simulate_at: str = Form(""), deadline_at: str = Form(""),
                                    acknowledged: bool = Form(False), reminder_sent: bool = Form(False),
                                    owner: str = "Kurt", listing: int = 0):
        rows = preview_filter(owner, listing)
        result = error = interpretation = None
        row = next((item for item in rows if item["id"] == reservation), None)
        if row is None or row.get("error"):
            error = "Review this reservation before running an inquiry simulation."
        else:
            try:
                start, end = date.fromisoformat(arrival), date.fromisoformat(departure)
                if end <= start:
                    raise ValueError("Inquiry checkout must be after arrival.")
                checkout = date.fromisoformat(guest_checkout) if guest_checkout and not reply_text.strip() else None
                now = syncer.now().astimezone(syncer.tz)
                received = local_scenario_time(reply_at) if reply_at else now
                as_of = local_scenario_time(simulate_at) if simulate_at else now
                deadline = local_scenario_time(deadline_at) if deadline_at else None
                if as_of < received:
                    raise ValueError("The simulated time cannot be before the reply arrived.")
                update_after = None
                if reply_text.strip():
                    key = syncer.settings.extension_ai_api_key
                    if not key:
                        raise ValueError("Configure the OpenAI API key to interpret a written reply.")
                    guest_reply, checkout = await interpret_inquiry_reply(
                        key, reply_text.strip(), received, row["departure_date"],
                    )
                    if guest_reply == "wait_until":
                        update_after, checkout = checkout, None
                        if update_after < received:
                            raise ValueError("The promised update is earlier than the reply. Specify the day in the reply.")
                    interpretation = {
                        "new_checkout": f"Guest gave a checkout date of {checkout}.",
                        "booked_checkout": f"Guest confirmed their booked checkout on {row['departure_date']}.",
                        "undecided": "Guest is still deciding.",
                        "wait_until": (
                            f"Guest expects to know after {update_after:%b %d at %I:%M %p %Z}."
                            if update_after else ""
                        ),
                        "clarify": "The reply did not give one usable checkout date.",
                    }[guest_reply]
                result = simulate_inquiry(
                    row, start, end, guest_reply=guest_reply, guest_checkout=checkout,
                    update_after=update_after, as_of=as_of, inquiry_deadline=deadline,
                    acknowledged=acknowledged, reminder_sent=reminder_sent,
                )
            except ValueError as exc:
                error = str(exc)
            except (httpx.HTTPError, KeyError, TypeError, AttributeError, IndexError):
                error = "Could not interpret the simulated reply. Try again or select a reply option."
        return page(request, "extension_preview.html", scan=previews[owner],
                    preview_rows=rows, preview_listing=listing, configured=bool(syncer.settings.extension_ai_api_key), inquiry_reservation=reservation,
                    inquiry_arrival=arrival, inquiry_departure=departure, inquiry_result=result, inquiry_error=error,
                    guest_reply=guest_reply, guest_checkout=guest_checkout, reply_text=reply_text,
                    inquiry_interpretation=interpretation, reply_at=received.strftime("%Y-%m-%dT%H:%M") if not error else reply_at,
                    simulate_at=simulate_at, deadline_at=deadline_at,
                    acknowledged=acknowledged, reminder_sent=reminder_sent, **extension_live_controls(owner))

    @app.post("/extension-preview")
    async def start_extension_preview(background_tasks: BackgroundTasks, owner: str = "Kurt", listing: int = Form(0)):
        preview_filter(owner, listing)
        extension_preview = previews[owner]
        if syncer.settings.extension_ai_api_key and not extension_preview["running"]:
            extension_preview.update(rows=[], running=True, completed=0, total=0, finished_at=None, listing=listing)
            background_tasks.add_task(scan_extensions, syncer, extension_preview, owner=owner, listing=listing)
        return RedirectResponse(f"extension-preview?owner={owner}&listing={listing}", status_code=303, background=background_tasks)

    @app.get("/extension-pricing", response_class=HTMLResponse)
    async def extension_pricing(request: Request, owner: str = EXTENSION_OWNERS[0]):
        if owner not in EXTENSION_OWNERS:
            raise HTTPException(404)
        rows, totals = extension_report_rows(db, owner)
        return page(request, "extension_pricing.html", owner=owner, owners=EXTENSION_OWNERS,
                    config=extension_rules(db)[owner], report_rows=rows, report_totals=totals)

    @app.post("/extension-pricing-save")
    async def extension_pricing_save(request: Request):
        form = await request.form()
        owner = form.get("owner")
        if owner not in EXTENSION_OWNERS:
            raise HTTPException(404)
        try:
            config = validate_extension_rules(form)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        rules = extension_rules(db)
        rules[owner] = config
        db.set_setting(EXTENSION_SETTINGS_KEY, json.dumps({"owners": rules}))
        return _done(f"extension-pricing?owner={owner}", "Pricing rules saved")

    @app.get("/setup", response_class=HTMLResponse)
    async def setup(request: Request, section: str = "connections"):
        if section == "daily-temperature":
            return RedirectResponse("setup?section=thermostats", status_code=302)
        if section not in dict(SETUP_SECTIONS) and section != "all":
            raise HTTPException(404)
        webhook_id = db.get_setting("webhook_id")
        public_url = db.get_setting("public_url", "")
        ha_ok, automation = False, False
        if section in ("connections", "all"):
            try:
                ha_ok = await syncer.ha.ping()
                automation = await syncer.ha.webhook_automation_exists()
            except Exception:
                ha_ok, automation = False, False
        return page(
            request, "setup.html",
            setup_section=section,
            setup_fields=SETUP_FIELDS,
            smtp_values={k: db.get_setting(k, '587' if k == 'smtp_port' else 'starttls' if k == 'smtp_security' else '') or '' for k in SMTP_FIELDS},
            smtp_enabled=db.get_bool('smtp_enabled'),
            lyric_climate_entities=db.get_setting("lyric_climate_entities", "") or "",
            ha_ok=ha_ok, automation=automation, public_url=public_url,
            addon_slug=db.get_setting("addon_slug", "local_stay_automation"),
            webhook_url=f"{public_url}/api/webhook/{webhook_id[:6]}…" if webhook_id else "",
            webhook_full=f"{public_url}/api/webhook/{webhook_id}" if webhook_id and public_url else "",
            registered=db.get_setting("hostaway_webhook_registered_at"),
            is_addon=syncer.settings.is_addon,
            lock_guest=[(k, SETTING_LABELS[k], db.get_int(k, DEFAULTS[k])) for k in LOCK_GUEST_KEYS],
            lock_advanced=[(k, SETTING_LABELS[k], db.get_int(k, DEFAULTS[k])) for k in LOCK_ADVANCED_KEYS],
            daily_values={k: db.get_int(k, v) for k, v in DAILY_DEFAULTS.items()},
            occupied_reset_enabled=db.get_bool("occupied_reset_enabled"),
            temperature_complaints_enabled=db.get_bool("temperature_complaints_enabled", True),
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

    @app.post("/sheet-settings-save")
    async def sheet_settings_save(request: Request):
        form = await request.form()
        endpoint = str(form.get('sheet_endpoint', '')).strip()
        secret = str(form.get('sheet_secret', '')).strip()
        enabled = bool(form.get('sheet_enabled'))
        if endpoint and not valid_endpoint(endpoint):
            raise HTTPException(400, 'Use the Google Apps Script web app address ending in /exec')
        if secret and (len(secret) < 32 or len(secret) > 256 or not secret.isascii() or any(c.isspace() for c in secret)):
            raise HTTPException(400, 'The connection key must contain 32–256 characters with no spaces')
        if endpoint != (db.get_setting('sheet_endpoint', '') or '') and db.get_setting('sheet_secret') and not secret:
            raise HTTPException(400, 'Enter the connection key again when changing the receiver address')
        if enabled and (not endpoint or not (secret or db.get_setting('sheet_secret'))):
            raise HTTPException(400, 'Add the receiver address and connection key before enabling sheet updates')
        values = {'sheet_endpoint': endpoint, 'sheet_enabled': '1' if enabled else '0'}
        if secret:
            values['sheet_secret'] = secret
        db.set_settings(values)
        db.log('sheet.settings_saved', 'Property sheet connection settings saved (key hidden)')
        return _done('setup?section=connections', 'Sheet connection saved; enabled updates run every five minutes')

    booking_report_key = "thursday_booking_report"

    async def generate_booking_report(start, end):
        snapshot = {"start": start.isoformat(), "end": end.isoformat()}
        try:
            reservations = await syncer.hostaway.booking_report_reservations(snapshot["start"])
            snapshot["report"] = thursday_bookings(reservations, start, end)
        except Exception:
            snapshot["error"] = "Could not read the reservation history from Hostaway. Try running the report again."
        db.set_setting(booking_report_key, json.dumps(snapshot))

    @app.post("/thursday-bookings")
    async def start_thursday_booking_report():
        nonlocal booking_report_task
        if booking_report_task is None or booking_report_task.done():
            start, end = report_window(syncer.now())
            db.set_setting(booking_report_key, json.dumps({
                "start": start.isoformat(), "end": end.isoformat(), "running": True,
            }))
            booking_report_task = asyncio.create_task(generate_booking_report(start, end))
        return RedirectResponse("thursday-bookings", status_code=303)

    @app.get("/thursday-bookings", response_class=HTMLResponse)
    async def thursday_booking_report(request: Request):
        snapshot = json.loads(db.get_setting(booking_report_key) or "{}")
        running = booking_report_task is not None and not booking_report_task.done()
        start, end = report_window(syncer.now())
        error = snapshot.get("error")
        if snapshot.get("running") and not running:
            error = "The report was interrupted. Run it again to get results."
        response = page(request, "thursday_bookings.html",
                        start=snapshot.get("start", start), end=snapshot.get("end", end),
                        report=snapshot.get("report"), error=error, running=running)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/thursday-special", response_class=HTMLResponse)
    @app.post("/thursday-special", response_class=HTMLResponse)
    async def thursday_special(request: Request):
        form = await request.form()
        owner = request.query_params.get('owner', '')
        if owner and owner not in OWNERS:
            raise HTTPException(400, 'Choose Mike or Kurt')
        if owner:
            form = FormData([*(item for item in form.multi_items() if item[0] != 'special_owner'),
                             ('special_owner', owner)])
        result, error, applied, bulk_report = None, None, None, None
        if request.method == 'POST':
            try:
                if form.get('special_action') == 'save_schedule':
                    if not owner:
                        raise ValueError('Open an owner page to save a Thursday schedule')
                    pricing.save_thursday_schedule(form)
                    return _done(f'thursday-special?owner={owner}', 'Thursday schedule saved')
                if form.get('special_action') == 'apply_wright' and owner != 'Mike':
                    choices = form.getlist('special_property')
                    if len(choices) != 1 or form.get('special_floor', '').strip() or form.get('special_discount') != '20':
                        raise ValueError('Check Wright only, enter 20% and leave the threshold blank')
                    applied = await pricing.apply_wright_special(choices[0])
                elif form.get('special_action') == 'apply_selected':
                    bulk_report = await pricing.apply_selected_special(form)
                else:
                    result = await pricing.thursday_special(form)
            except ValueError as exc:
                error = str(exc)
        schedule = pricing.thursday_schedule(owner)
        if request.method == 'GET' and schedule:
            form = FormData([
                ('special_owner', schedule['owner']), ('special_discount', schedule['discount']),
                ('special_floor', schedule['floor']),
                *(('special_property', item) for item in schedule['selected']),
            ])
        properties = db.query(
            'SELECT id,name,address,owner_scope FROM properties WHERE active=1 '
            "AND hostaway_listing_id IS NOT NULL AND (?='' OR owner_scope=?) ORDER BY name, address, id",
            (owner, owner),
        )
        return page(request, 'thursday_special.html', properties=properties, owners=OWNERS,
                    special=form, result=result, error=error, applied=applied, bulk_report=bulk_report, schedule=schedule, special_owner=owner)

    @app.post("/pricing-settings-save")
    async def pricing_settings_save(request: Request):
        form = await request.form()
        try:
            values = validate_pricing(form, db, require_property=bool(form.get("pricing_simulation_enabled")))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        values['pricing_simulation_enabled'] = '1' if form.get('pricing_simulation_enabled') else '0'
        db.set_settings(values)
        db.log('pricing.settings_saved', 'Pricing simulation settings saved; live writes and recurring schedule remain unavailable.')
        return _done('setup?section=pricing', 'Pricing settings saved — simulation only; no recurring schedule activated')

    @app.post("/pricing-simulate")
    async def pricing_simulate(request: Request):
        form = await request.form()
        try:
            if form.get('pricing_action') == 'one_time':
                await pricing.preview(syncer.now(), manual=manual_settings(form))
                return _done('setup?section=pricing', 'One-time simulation saved; no rates or minimum stays changed')
            when = datetime.fromisoformat(str(form.get('pricing_preview_at', '')))
            if when.tzinfo is not None:
                raise ValueError('Use the local Eastern date and time')
            when = when.replace(tzinfo=PRICING_TZ)
            if when.astimezone(timezone.utc).astimezone(PRICING_TZ).replace(tzinfo=None) != when.replace(tzinfo=None):
                raise ValueError('That time does not exist because daylight saving time changes')
            await pricing.preview(when)
        except ValueError as exc:
            return _done('setup?section=pricing', str(exc) or 'Pricing simulation could not run; no prices changed')
        except Exception:
            db.log('pricing.preview_failed', 'Pricing simulation failed; no prices changed.', level='warning')
            return _done('setup?section=pricing', 'Pricing simulation failed; no prices changed')
        return _done('setup?section=pricing', 'Simulation saved — no Hostaway or Airbnb prices changed')

    @app.post("/smtp-settings-save")
    async def smtp_settings_save(request: Request):
        form = await request.form()
        values = {k: str(form.get(k, '')).strip() for k in SMTP_FIELDS}
        password = str(form.get('smtp_password', ''))
        enabled = bool(form.get('smtp_enabled'))
        if values['smtp_security'] not in ('ssl', 'starttls') or not values['smtp_port'].isdigit() or not 1 <= int(values['smtp_port']) <= 65535:
            raise HTTPException(400, "Choose SSL or STARTTLS and a port from 1 to 65535")
        if any('\r' in v or '\n' in v for v in values.values()):
            raise HTTPException(400, "Email settings cannot contain line breaks")
        if enabled:
            try:
                link = urlsplit(values['alert_properties_url'])
            except ValueError:
                raise HTTPException(400, 'Enter a valid HTTPS Properties link')
            if (not email_address(values['smtp_from']) or not values['smtp_host'] or any(c in values['smtp_host'] for c in ' /@') or
                link.scheme != 'https' or not link.netloc or link.username or '/api/hassio_ingress/' in link.path):
                raise HTTPException(400, "Enter the SMTP server, a sender email and a permanent HTTPS Properties link")
            if values['smtp_username'] and not (password or db.get_setting('smtp_password')):
                raise HTTPException(400, "Enter the SMTP password (or app password)")
        if (db.get_setting('smtp_password') and not password and
            any(values[k] != (db.get_setting(k, '') or '') for k in ('smtp_host', 'smtp_username'))):
            raise HTTPException(400, "Re-enter the password when changing the SMTP server or username")
        if password:
            values['smtp_password'] = password
        values['smtp_enabled'] = '1' if enabled else '0'
        db.set_settings(values)
        db.log('settings.saved', 'Email server settings saved (password hidden)')
        return _done('setup?section=alerts', 'Email settings saved')

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
        return _done("setup?section=locks", "Lock settings saved")

    @app.post("/additional-thermostats-save")
    async def additional_thermostats_save(lyric_climate_entities: str = Form("")):
        entities = sorted(set(lyric_climate_entities.replace(",", " ").split()))
        if len(entities) > 10 or any(not re.fullmatch(r"climate\.[a-z0-9_]+", eid) for eid in entities):
            raise HTTPException(400, "Enter up to 10 climate entity IDs, separated by spaces or commas")
        try:
            known_lyric = await syncer.ha.lyric_entity_ids()
        except Exception as exc:
            raise HTTPException(502, "Could not verify Lyric devices in Home Assistant; selection was not changed") from exc
        if any(eid not in known_lyric for eid in entities):
            raise HTTPException(400, "Each extra thermostat must be a climate entity from Honeywell Lyric")
        old = set((db.get_setting("lyric_climate_entities", "") or "").split())
        db.set_setting("lyric_climate_entities", " ".join(entities))
        for eid in old - set(entities):
            db.execute("DELETE FROM thermostats WHERE entity_id=?", (eid,))
        db.log("settings.saved", "Extra thermostat selection saved; manual property assignment required")
        return _done("setup?section=thermostats", "Extra thermostat selection saved. Assign new devices on Properties.")

    @app.post("/daily-temperature-settings-save")
    async def daily_temperature_settings_save(request: Request):
        form = await request.form()
        values = {}
        for key, (low, high) in DAILY_RANGES.items():
            raw = str(form.get(key, "")).strip()
            if not raw.isdigit() or not low <= int(raw) <= high:
                raise HTTPException(400, f"{SETUP_FIELDS[key]['label']}: enter a whole number from {low} to {high}")
            values[key] = int(raw)
        if values["daily_winter"] >= values["daily_summer"]:
            raise HTTPException(400, "Winter heating must be lower than summer cooling")
        for key, value in values.items():
            db.set_setting(key, str(value))
        db.set_setting("temperature_complaints_enabled", "1" if "temperature_complaints_enabled" in form else "0")
        # Enable last, after all validated settings have been saved. Existing daily records and pauses stay.
        db.set_setting("occupied_reset_enabled", "1" if "occupied_reset_enabled" in form else "0")
        db.log("settings.saved", "Daily occupied temperature settings saved")
        return _done("setup?section=thermostats", "Daily temperature settings saved")

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
        return _done("setup?section=thermostats", "Thermostat settings saved")

    @app.post("/test-alert")
    async def test_alert():
        if locks is not None:
            await locks.alert("Stay Automation test", "Alerts are working.")
        return _done("setup?section=alerts", "Test alert sent")

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
        return _done("setup?section=connections", "Webhook registered")

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
