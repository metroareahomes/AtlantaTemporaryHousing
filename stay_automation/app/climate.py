"""Thermostats: set each home "occupied" or "vacant" from its Hostaway reservations, and watch device health.

Rules (from the client, all numbers editable on the Setup page):
- Occupied temperature starts 3 hours before check-in and ends 3 hours after check-out, then vacant.
- When one guest leaves and another arrives the same day, the home goes occupied at the first guest's check-out
  and stays that way.
- A home that stays vacant is set to vacant again every day, in case cleaners changed it.
- Summer and winter have separate temperatures (summer = a range of months).
- A thermostat that stays offline, or a lock battery under the limit, alerts the staff recipients.

Only homes with "Thermostat automation" ticked are touched, and a thermostat is only changed when the
read-back shows the new temperature stuck. A home whose stay is already under way when it is first seen is
adopted as it is, so turning this on never changes a guest's temperature mid-stay.
"""
import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .matching import match_lock

log = logging.getLogger(__name__)

OFFLINE_STATES = {"unavailable", "unknown", None}
VERIFY_DELAY_SECONDS = 20
TOLERANCE = 0.6  # thermostats round to whole or half degrees

DEFAULTS = {
    "occ_summer": 72,
    "occ_winter": 66,
    "vac_summer": 78,
    "vac_winter": 55,
    "summer_start_month": 5,
    "summer_end_month": 10,  # winter starts in November, per the client
    "therm_lead_hours": 3,       # occupied this long before check-in
    "therm_lag_hours": 3,        # vacant this long after check-out
    "vacant_reset_hour": 11,     # vacant homes are set to vacant again daily after this hour
    "therm_offline_minutes": 30,
    "therm_retry_minutes": 60,   # a failed change is tried again after this long, not every tick
    "therm_max_per_tick": 1,    # Honeywell's coordinator refreshes the whole account per write
    "therm_gap_seconds": 30,    # pause between writes in the same tick (unused when max is 1)
    "battery_alert_percent": 15,
}

LABELS = {
    "occ_summer": "Occupied, summer (°F)",
    "occ_winter": "Occupied, winter (°F)",
    "vac_summer": "Vacant, summer (°F)",
    "vac_winter": "Vacant, winter (°F)",
    "summer_start_month": "Summer starts in month (1-12)",
    "summer_end_month": "Summer ends in month (1-12)",
    "therm_lead_hours": "Occupied this many hours before check-in",
    "therm_lag_hours": "Vacant this many hours after check-out",
    "vacant_reset_hour": "Re-set vacant homes daily after (hour, 0-23)",
    "therm_offline_minutes": "Alert when a thermostat has been offline this many minutes",
    "therm_retry_minutes": "Try a failed thermostat change again after (minutes)",
    "therm_max_per_tick": "Honeywell writes per 5-minute pass (keep at 1 while rate-limited)",
    "therm_gap_seconds": "Seconds to wait between Honeywell writes in the same pass",
    "battery_alert_percent": "Alert when a lock battery is below (%)",
}

RANGES = {  # (min, max) accepted on the Setup page
    "occ_summer": (50, 90), "occ_winter": (50, 90), "vac_summer": (50, 95), "vac_winter": (40, 90),
    "summer_start_month": (1, 12), "summer_end_month": (1, 12),
    "therm_lead_hours": (0, 24), "therm_lag_hours": (0, 24), "vacant_reset_hour": (0, 23),
    "therm_offline_minutes": (5, 1440), "therm_retry_minutes": (5, 1440),
    "therm_max_per_tick": (1, 20), "therm_gap_seconds": (0, 300),
    "battery_alert_percent": (1, 100),
}


# ---- pure rules -----------------------------------------------------------

def is_summer(day: date, start_month: int, end_month: int) -> bool:
    if start_month <= end_month:
        return start_month <= day.month <= end_month
    return day.month >= start_month or day.month <= end_month  # e.g. Nov-Mar


def occupancy(reservations: list[dict[str, Any]], now: datetime, lead_hours: int, lag_hours: int) -> str:
    """"occupied" or "vacant" for this moment."""
    return "occupied" if occupying_reservation(reservations, now, lead_hours, lag_hours) else "vacant"


def occupying_reservation(reservations: list[dict[str, Any]], now: datetime, lead_hours: int,
                          lag_hours: int) -> dict[str, Any] | None:
    """The booking that makes the home occupied right now (check-in minus lead through checkout plus lag)."""
    active = [r for r in reservations if r["active"]]
    for r in active:
        start = datetime.fromisoformat(r["check_in_at"]) - timedelta(hours=lead_hours)
        end = datetime.fromisoformat(r["check_out_at"]) + timedelta(hours=lag_hours)
        for other in active:  # someone arriving the day this guest leaves: no vacant gap in between
            if other["id"] != r["id"] and other["arrival_date"] == r["departure_date"]:
                end = max(end, datetime.fromisoformat(other["check_in_at"]))
        if start <= now < end:
            return r
    return None


def setpoint(mode: str, summer: bool, cfg: dict[str, int]) -> int:
    return cfg[f"{'occ' if mode == 'occupied' else 'vac'}_{'summer' if summer else 'winter'}"]


def should_apply(mode: str, last_mode: str | None, last_applied: datetime | None, now: datetime,
                 reset_hour: int, occupying_id: int | None = None,
                 last_reservation_id: int | None = None) -> bool:
    """Set on every change; on a new booking even if the last stay was also occupied; and re-set a
    vacant home once a day (cleaners may have changed it)."""
    if mode == "occupied" and occupying_id and occupying_id != last_reservation_id:
        return True
    if mode != last_mode or last_applied is None:
        return True
    return mode == "vacant" and last_applied.date() < now.date() and now.hour >= reset_hour


def _bound(value: Any, fallback: Any, last_resort: float) -> float:
    if value is not None:
        return float(value)
    if fallback is not None:
        return float(fallback)
    return last_resort


def target_call(t: dict[str, Any], target: int, summer: bool) -> dict[str, float]:
    """Arguments for climate.set_temperature. Auto/heat_cool thermostats have two setpoints: in summer we move
    the cooling one, in winter the heating one, and leave the other as it was. Honeywell Lyric rejects a
    single `temperature` while the device is in Auto."""
    if t.get("state") in ("heat_cool", "auto"):
        if summer:
            high, low = float(target), _bound(t.get("target_low"), t.get("min_temp"), target - 10)
        else:
            low, high = float(target), _bound(t.get("target_high"), t.get("max_temp"), target + 10)
        if low >= high:
            low, high = (high - 1, high) if summer else (low, low + 1)
        return {"high": high, "low": low}
    return {"temperature": float(target)}


def took_effect(call: dict[str, float], t: dict[str, Any]) -> bool:
    for key, field in (("temperature", "target_temp"), ("low", "target_low"), ("high", "target_high")):
        if key in call:
            seen = t.get(field)
            if seen is None or abs(float(seen) - call[key]) > TOLERANCE:
                return False
    return True


# ---- manager ------------------------------------------------------------------

class ClimateManager:
    def __init__(self, syncer, alert):
        self.s = syncer
        self.db = syncer.db
        self.ha = syncer.ha
        self.alert = alert  # LockManager.alert: same recipients, same once-only keys
        self.verify_delay = VERIFY_DELAY_SECONDS

    def cfg(self) -> dict[str, int]:
        return {k: self.db.get_int(k, v) for k, v in DEFAULTS.items()}

    def _stamp(self) -> str:
        """Timestamps come from the same clock the schedule uses."""
        return self.s.now().astimezone(timezone.utc).isoformat(timespec="seconds")

    def _automation_on(self, row: dict[str, Any]) -> bool:
        if not row.get("property_id"):
            return False
        auto = self.db.one("SELECT thermostat_automation FROM properties WHERE id = ?", (row["property_id"],))
        return bool(auto and auto["thermostat_automation"])

    async def _read(self, entity_id: str) -> dict[str, Any] | None:
        if hasattr(self.ha, "climate_state"):
            return await self.ha.climate_state(entity_id)
        states = await self.ha.climate_states()
        return next((c for c in states if c["entity_id"] == entity_id), None)

    async def refresh(self) -> int:
        """Find Honeywell thermostats in Home Assistant, store readings, and drop leftovers from the old account."""
        states = await self.ha.climate_states()
        if not states and self.db.one("SELECT 1 FROM thermostats"):
            self.db.log("thermostat.refresh_empty",
                        "Home Assistant returned no Honeywell thermostats; left the existing list alone",
                        level="warning")
            return 0
        properties = self.db.query("SELECT id, name, hostaway_name, address FROM properties")
        live = {c["entity_id"]: c for c in states}
        for c in states:
            row = self.db.one("SELECT * FROM thermostats WHERE entity_id = ?", (c["entity_id"],))
            offline = c["state"] in OFFLINE_STATES
            if row is None:
                if offline:
                    continue  # leftover unavailable climate.* from devices no longer on this account
                self.db.execute("INSERT INTO thermostats(entity_id, name) VALUES(?, ?)", (c["entity_id"], c["name"]))
                self.db.log("thermostat.found", f"New thermostat {c['name']} ({c['entity_id']})")
                row = self.db.one("SELECT * FROM thermostats WHERE entity_id = ?", (c["entity_id"],))
            elif offline and not self._automation_on(row):
                continue
            since = row["offline_since"] if offline else None
            if offline and not since:
                since = self._stamp()
                if self._automation_on(row):
                    self.db.log("thermostat.offline",
                                f"{c['name']} reported as '{c['state']}' by Home Assistant "
                                f"(current temp {c['current_temp']})",
                                level="warning", property_id=row["property_id"])
            self.db.execute(
                "UPDATE thermostats SET name = ?, state = ?, current_temp = ?, target_temp = ?, target_low = ?, "
                "target_high = ?, hvac_action = ?, fan_mode = ?, offline_since = ?, seen_at = ?, "
                "min_temp = ?, max_temp = ? WHERE id = ?",
                (c["name"], c["state"], c["current_temp"], c["target_temp"], c["target_low"], c["target_high"],
                 c["hvac_action"], c["fan_mode"], since, self._stamp(), c.get("min_temp"), c.get("max_temp"),
                 row["id"]),
            )
            if not offline and row["match_source"] is None:
                property_id = match_lock(c["name"], properties)
                if property_id is not None:
                    self.db.execute("UPDATE thermostats SET property_id = ?, match_source = 'auto' WHERE id = ?",
                                    (property_id, row["id"]))
                    self.db.log("thermostat.matched", f"{c['name']} matched automatically", property_id=property_id)
        for row in self.db.query(
                "SELECT t.id, t.entity_id, t.name, t.property_id, p.thermostat_automation "
                "FROM thermostats t LEFT JOIN properties p ON p.id = t.property_id"):
            c = live.get(row["entity_id"])
            keep = c is not None and (c["state"] not in OFFLINE_STATES or bool(row["thermostat_automation"]))
            if keep:
                continue
            self.db.execute("DELETE FROM thermostats WHERE id = ?", (row["id"],))
            self.db.log("thermostat.gone",
                        f"{row['name']} is no longer on this Honeywell account ({row['entity_id']})")
        return len(states)

    def assign(self, thermostat_id: int, property_id: int | None) -> None:
        """Manual pairing from the dashboard; auto-matching never overrides it."""
        self.db.execute("UPDATE thermostats SET property_id = ?, match_source = 'manual', last_mode = NULL, "
                        "last_applied_at = NULL WHERE id = ?", (property_id, thermostat_id))

    async def tick(self) -> int:
        """Refresh HA's cached readings, apply occupied/vacant where due. Honeywell is only contacted for
        homes that actually need a change, and only a few per pass (Lyric refreshes the whole account
        on every write)."""
        return await self._run(None)

    async def tick_property(self, property_id: int) -> int:
        """One home after a Hostaway create/cancel/change. Same rules as tick, no extra Honeywell polling."""
        return await self._run(property_id)

    async def _run(self, property_id: int | None) -> int:
        await self.refresh()
        now = self.s.now()
        cfg = self.cfg()
        changed = 0
        writes = 0
        cap = cfg["therm_max_per_tick"]
        sql = (
            "SELECT t.*, p.name AS property_name, p.hostaway_listing_id FROM thermostats t "
            "JOIN properties p ON p.id = t.property_id "
            "WHERE p.thermostat_automation = 1 AND p.active = 1"
        )
        due = (self.db.query(sql + " AND t.property_id = ? ORDER BY t.id", (property_id,))
               if property_id is not None else
               self.db.query(sql + " ORDER BY t.id"))
        for t in due:
            if t["state"] in OFFLINE_STATES:
                continue
            result = await self._apply(t, now, cfg, allow_write=writes < cap)
            if result == "skip":
                continue
            changed += 1
            if result == "write":
                writes += 1
                if writes < cap and cfg["therm_gap_seconds"]:
                    await asyncio.sleep(cfg["therm_gap_seconds"])
        await self._offline_alerts(now, cfg)
        return changed

    async def _apply(self, t: dict[str, Any], now: datetime, cfg: dict[str, int],
                     allow_write: bool = True) -> str:
        reservations = self.s.reservations_for(t["property_id"])
        occupying = occupying_reservation(reservations, now, cfg["therm_lead_hours"], cfg["therm_lag_hours"])
        mode = "occupied" if occupying else "vacant"
        last_applied = None
        if t["last_applied_at"]:
            last_applied = datetime.fromisoformat(t["last_applied_at"]).astimezone(self.s.tz)

        # Automation switched on mid-stay for a guest who arrived on an earlier day: leave their temp.
        # A same-day booking (check-in is usually 3pm, guest often arrives at 8pm, booked after 3pm)
        # is not "mid-stay" — set occupied now.
        if (t["last_mode"] is None and occupying and not t.get("last_reservation_id")
                and occupying["arrival_date"] != now.date().isoformat()):
            self.db.execute(
                "UPDATE thermostats SET last_mode = 'occupied', last_applied_at = ?, last_reservation_id = ? "
                "WHERE id = ?", (self._stamp(), occupying["id"], t["id"]))
            self.db.log("thermostat.adopted", f"{t['name']}: stay already under way, left as it is",
                        property_id=t["property_id"])
            return "skip"
        if not should_apply(mode, t["last_mode"], last_applied, now, cfg["vacant_reset_hour"],
                            occupying["id"] if occupying else None, t.get("last_reservation_id")):
            return "skip"

        summer = is_summer(now.date(), cfg["summer_start_month"], cfg["summer_end_month"])
        wanted = setpoint(mode, summer, cfg)
        want_mode = "cool" if summer else "heat"
        waiting = bool(t["retry_after"] and datetime.fromisoformat(t["retry_after"]) > now)

        logged_limit = False

        def _clamp(current: dict[str, Any], raw: int) -> int:
            nonlocal logged_limit
            low, high = current.get("min_temp"), current.get("max_temp")
            if low is not None and high is not None and not low <= raw <= high:
                limited = int(min(max(raw, low), high))
                if not logged_limit:
                    self.db.log("thermostat.limited",
                                f"{current['name']}: wanted {raw}° but the thermostat only allows "
                                f"{low:g}-{high:g}°; using {limited}°",
                                level="warning", property_id=current["property_id"])
                    logged_limit = True
                return limited
            return raw

        if t["state"] == want_mode:
            target = _clamp(t, wanted)
            if took_effect(target_call(t, target, summer), t):
                self._recorded(t, mode, target, summer, already=True,
                               reservation_id=occupying["id"] if occupying else None)
                return "record"
        if waiting:
            return "skip"
        if not allow_write:
            return "skip"

        t = await self._set_season_mode(t, want_mode)
        target = _clamp(t, wanted)
        call = target_call(t, target, summer)
        if took_effect(call, t):
            self._recorded(t, mode, target, summer, already=True,
                           reservation_id=occupying["id"] if occupying else None)
            return "record"
        try:
            await self.ha.set_temperature(t["entity_id"], **call)
        except Exception as exc:
            # Lyric often returns HTTP 500/429 on the POST even when Honeywell already applied the
            # change (cancel → vacant on CC4). Re-read before counting a failure.
            await asyncio.sleep(self.verify_delay)
            fresh = await self._read(t["entity_id"])
            if fresh is None or fresh.get("state") in OFFLINE_STATES or took_effect(call, fresh):
                self._recorded(t, mode, target, summer, already=False,
                               reservation_id=occupying["id"] if occupying else None)
                return "write"
            await self._failed(t, now, str(exc) or type(exc).__name__)
            return "skip"
        await asyncio.sleep(self.verify_delay)
        fresh = await self._read(t["entity_id"])
        # Lyric refreshes the whole account after a set. A 429 makes every climate.* go
        # unavailable; that is not proof this write failed.
        if fresh is None or fresh.get("state") in OFFLINE_STATES:
            self._recorded(t, mode, target, summer, already=False,
                           reservation_id=occupying["id"] if occupying else None)
            return "write"
        if not took_effect(call, fresh):
            await self._failed(t, now, "the thermostat did not take the new temperature")
            return "skip"
        self._recorded(t, mode, target, summer, already=False,
                       reservation_id=occupying["id"] if occupying else None)
        return "write"

    def _recorded(self, t: dict[str, Any], mode: str, target: int, summer: bool, *, already: bool,
                  reservation_id: int | None) -> None:
        self.db.execute(
            "UPDATE thermostats SET last_mode = ?, last_applied_at = ?, fail_count = 0, last_error = NULL, "
            "retry_after = NULL, last_reservation_id = ? WHERE id = ?",
            (mode, self._stamp(), reservation_id if mode == "occupied" else None, t["id"]))
        how = "already at" if already else "set to"
        self.db.log("thermostat.set",
                    f"{t['property_name']}: {mode} {'summer' if summer else 'winter'}, {how} {target}°",
                    property_id=t["property_id"])

    async def _set_season_mode(self, t: dict[str, Any], want: str) -> dict[str, Any]:
        """Cool in summer, heat in winter. A thermostat left in Auto applies its heating limit (74°) even when we ask
        for a cooling temperature, so a vacant home could never reach 78°. Returns the thermostat as it is now,
        because switching mode changes the temperature range it accepts."""
        if t["state"] == want:
            return t
        try:
            await self.ha.set_hvac_mode(t["entity_id"], want)
            await asyncio.sleep(self.verify_delay)
            fresh = await self._read(t["entity_id"])
        except Exception as exc:  # the temperature is still attempted; a failure there is judged on its own
            self.db.log("thermostat.mode_failed", f"{t['name']}: could not switch to {want} ({exc or type(exc).__name__})",
                        level="warning", property_id=t["property_id"])
            return t
        self.db.log("thermostat.mode", f"{t['name']}: switched {t['state']} to {want}", property_id=t["property_id"])
        if fresh is None:
            return t
        keep = ("state", "current_temp", "target_temp", "target_low", "target_high", "min_temp", "max_temp")
        return {**t, **{k: fresh.get(k) for k in keep}}

    async def _failed(self, t: dict[str, Any], now: datetime, error: str) -> None:
        fails = t["fail_count"] + 1
        retry = (now + timedelta(minutes=self.cfg()["therm_retry_minutes"])).astimezone(timezone.utc)
        self.db.execute("UPDATE thermostats SET fail_count = ?, last_error = ?, retry_after = ? WHERE id = ?",
                        (fails, error[:300], retry.isoformat(timespec="seconds"), t["id"]))
        self.db.log("thermostat.failed", f"{t['name']}: {error}", level="warning", property_id=t["property_id"])
        if fails >= 3:  # retried every few minutes; three misses in a row is worth a person's attention
            await self.alert(f"Thermostat not responding: {t['property_name']}",
                             f"{t['name']} could not be set after {fails} tries ({error}).",
                             key=f"therm_fail:{t['id']}:{now.date()}")

    async def _offline_alerts(self, now: datetime, cfg: dict[str, int]) -> None:
        for t in self.db.query(
                "SELECT t.*, p.name AS property_name FROM thermostats t JOIN properties p ON p.id = t.property_id "
                "WHERE p.active = 1 AND p.thermostat_automation = 1 AND t.offline_since IS NOT NULL"):
            down = datetime.fromisoformat(t["offline_since"])
            if now - down >= timedelta(minutes=cfg["therm_offline_minutes"]):
                await self.alert(
                    f"Thermostat offline: {t['property_name']}",
                    f"{t['name']} has been offline since {down.astimezone(self.s.tz):%a %b %d, %I:%M %p}.",
                    key=f"therm_offline:{t['id']}:{t['offline_since']}")

    # ---- lock batteries --------------------------------------------------------

    async def check_batteries(self) -> int:
        """Store each lock's battery and tell staff once a week about any below the limit."""
        levels = await self.ha.lock_batteries()
        limit = self.db.get_int("battery_alert_percent", DEFAULTS["battery_alert_percent"])
        now = self.s.now()
        week = now.isocalendar()
        low = 0
        for lock in self.db.query("SELECT l.*, p.name AS property_name FROM locks l "
                                  "LEFT JOIN properties p ON p.id = l.property_id"):
            level = levels.get(lock["entity_id"])
            if level is None:
                continue
            self.db.execute("UPDATE locks SET battery = ? WHERE id = ?", (level, lock["id"]))
            if level < limit:
                low += 1
                await self.alert(
                    f"Lock battery low: {lock['property_name'] or lock['name']}",
                    f"{lock['name']} is at {level}%. Replace the batteries.",
                    key=f"battery:{lock['id']}:{week.year}-W{week.week}")
        return low
