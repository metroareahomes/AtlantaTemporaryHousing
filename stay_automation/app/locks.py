"""Phase 2: keep each automated lock holding exactly the guest codes it should, verified by reading back.

Every few minutes, locks that are due get reconciled: work out which of our codes (named HA-<guest>)
should be in the lock right now, read the lock, add or remove the difference, then read it again. The read-back
is the only thing trusted; Schlage calls often time out yet succeed, or return OK yet do nothing.

Hostaway's own lock automation stays on: it creates the guest code and usually writes it to the lock itself.
A code already in the lock under Hostaway's name counts as present, so we only add our copy when it is missing.
Hostaway's codes are never touched; if one outlives its guest, staff are told.

Staff access codes and HA-BACKUP are managed from the database and pushed to every automated lock.
"""
import asyncio
import json
import logging
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Any, Callable

from .db import utcnow

log = logging.getLogger(__name__)

PREFIX = "HA-"
BACKUP_NAME = "HA-BACKUP"
VERIFY_DELAY_SECONDS = 15
GAP_BETWEEN_LOCKS_SECONDS = 5
# One Schlage account with ~130 locks already times out when Home Assistant polls it, and a burst of reads
# makes that worse (HTTP 500 on get_codes). The most overdue locks go first; the rest wait for the next tick.
MAX_LOCKS_PER_TICK = 15
NAME_MAX = 32  # a guess, NOT confirmed against Schlage's real limit; check on the CC2 test lock

DEFAULTS = {
    "add_hour": 8,              # guest code goes in at 8 AM on arrival day
    "early_lead_hours": 3,      # ...or this long before an earlier check-in
    "afternoon_check_hour": 14,
    "final_check_minutes": 60,  # last check before check-in; backup code goes out after it fails
    "retry_minutes": 15,
    "daily_check_hour": 6,
    "report_hour": 7,
    "stale_check_minutes": 120,  # after checkout, time Hostaway gets to remove its code before staff hear
}

DEFAULT_GUEST_MESSAGE = (
    "Hi {guest}, we could not confirm your personal door code at {property}. "
    "Please use this code instead: {code}. Sorry for the trouble, and welcome!"
)


def _sanitize_name(raw: str | None) -> str:
    text = re.sub(r"\s+", " ", (raw or "").strip())
    text = "".join(c for c in text if c.isalnum() or c in " -'")
    return text.strip()


def code_name(reservation: dict[str, Any], taken: set[str] | None = None) -> str:
    """Lock slot staff can recognize by guest name; falls back to reservation id."""
    guest = _sanitize_name(reservation.get("guest_name"))
    if guest:
        base = f"{PREFIX}{guest}"[:NAME_MAX].rstrip()
    else:
        base = f"{PREFIX}{reservation['id']}"
    taken = taken or set()
    if base not in taken and base != BACKUP_NAME:
        return base
    # Collision (two guests with the same name, or clashes with backup): keep it unique.
    suffix = f" {reservation['id']}"
    room = max(NAME_MAX - len(suffix), len(PREFIX) + 1)
    return f"{(f'{PREFIX}{guest}' if guest else PREFIX)[:room].rstrip()}{suffix}"


def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def add_time(r: dict[str, Any], add_hour: int, lead_hours: int, final_minutes: int = 60) -> datetime:
    """When the guest code should first go in: 8 AM arrival day, or earlier for morning check-ins.
    Never later than the final check (1 hour before check-in). A same-day booking made after 3 PM
    lands in that window; waiting until add_hour would only look at the lock and text the guest."""
    check_in = _dt(r["check_in_at"])
    morning = datetime.combine(check_in.date(), time(add_hour), check_in.tzinfo)
    by_lead = check_in - timedelta(hours=lead_hours)
    by_final = check_in - timedelta(minutes=final_minutes)
    return min(morning, by_lead, by_final)


def slot_names(reservations: list[dict[str, Any]], add_hour: int, lead_hours: int,
               actual: dict[str, str] | None = None, final_minutes: int = 60) -> dict[int, str]:
    """Lock slot name for every booking with a code, stable for the whole stay.

    The earlier booking keeps the plain HA-<guest> name; a later booking whose code window overlaps it (same
    guest name, e.g. a repeat guest) gets the reservation id appended. Decided from the bookings alone, never
    from "who is in the lock right now", so a slot is not renamed (deleted and re-added) mid-stay.

    One exception, for upgrades: a slot the previous version wrote (HA-<reservation id>) that already holds the
    right code is kept as is until checkout, so a guest in the house is never left without a code while it is
    swapped for the new name."""
    live = sorted((r for r in reservations if r["active"] and r["door_code"]),
                  key=lambda r: (_dt(r["check_in_at"]), r["id"]))
    names: dict[int, str] = {}
    windows: list[tuple[str, datetime, datetime]] = []
    for r in live:
        start, end = add_time(r, add_hour, lead_hours, final_minutes), _dt(r["check_out_at"])
        legacy = f"{PREFIX}{r['id']}"
        if actual is not None and actual.get(legacy) == r["door_code"]:
            names[r["id"]] = legacy
        else:
            taken = {n for n, s, e in windows if s < end and start < e}
            names[r["id"]] = code_name(r, taken)
        windows.append((names[r["id"]], start, end))
    return names


def desired_codes(reservations: list[dict[str, Any]], now: datetime, add_hour: int, lead_hours: int,
                  backup_code: str | None = None, staff: dict[str, str] | None = None,
                  actual: dict[str, str] | None = None,
                  previews: dict[str, str] | None = None, final_minutes: int = 60) -> dict[str, str]:
    """Our codes that should be in the lock at `now`, as {name: code}."""
    out: dict[str, str] = {}
    names = slot_names(reservations, add_hour, lead_hours, actual, final_minutes)
    for r in reservations:
        if (r["active"] and r["door_code"]
                and add_time(r, add_hour, lead_hours, final_minutes) <= now < _dt(r["check_out_at"])):
            out[names[r["id"]]] = r["door_code"]
    if backup_code:
        out[BACKUP_NAME] = backup_code
    out.update(previews or {})  # temporary visitor codes, named HA-Preview <label>
    if staff:
        for name, code in staff.items():
            if name.startswith(PREFIX) or name == BACKUP_NAME:
                continue  # never let a staff row steal guest/backup naming
            out[name] = code
    return out


@dataclass
class Plan:
    add: dict[str, str] = field(default_factory=dict)
    delete: list[str] = field(default_factory=list)
    # Wanted codes already present under someone else's name (usually written by Hostaway).
    # Schlage refuses duplicate codes, and the guest can get in either way.
    elsewhere: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.add and not self.delete


def plan(desired: dict[str, str], actual: dict[str, str], managed: set[str] | None = None) -> Plan:
    """Diff desired vs actual. Only delete names we manage (HA-* guest/backup, or staff names)."""
    managed = managed or set()

    def ours(name: str) -> bool:
        return name.startswith(PREFIX) or name in managed

    p = Plan()
    p.delete = [n for n in actual if ours(n) and n not in desired]
    kept_values = {code for name, code in actual.items() if name not in p.delete}
    for name, code in desired.items():
        if actual.get(name) == code:
            continue
        if name in actual:  # ours, but the code changed
            p.delete.append(name)
            p.add[name] = code
        elif code in kept_values:
            p.elsewhere.append(name)
        else:
            p.add[name] = code
            kept_values.add(code)
    return p


def code_present(r: dict[str, Any], actual: dict[str, str]) -> bool:
    return bool(r["door_code"]) and r["door_code"] in actual.values()


def preview_slot_names(rows: list[dict[str, Any]]) -> dict[int, str]:
    """Lock slot names for preview codes: HA-Preview <label>, with the row id added if two share a label."""
    names: dict[int, str] = {}
    used: set[str] = set()
    for row in sorted(rows, key=lambda r: r["id"]):
        label = _sanitize_name(row["label"])
        base = f"{PREFIX}Preview {label}"[:NAME_MAX].rstrip() if label else f"{PREFIX}Preview"
        name = base
        if name in used:
            suffix = f" {row['id']}"
            name = f"{base[:NAME_MAX - len(suffix)].rstrip()}{suffix}"
        used.add(name)
        names[row["id"]] = name
    return names


def next_check(reservations: list[dict[str, Any]], now: datetime, cfg: dict[str, int],
               extra: list[datetime] | None = None) -> datetime:
    """The next moment this lock needs attention: a code going in or out, a scheduled check, or tomorrow.
    `extra` are other moments to wake at (preview codes expiring)."""
    tz = now.tzinfo
    daily = datetime.combine(now.date(), time(cfg["daily_check_hour"]), tz)
    candidates = [daily if daily > now else daily + timedelta(days=1)]
    for r in reservations:
        if not r["active"]:
            continue
        check_in = _dt(r["check_in_at"])
        afternoon = datetime.combine(check_in.date(), time(cfg["afternoon_check_hour"]), check_in.tzinfo)
        times = [
            add_time(r, cfg["add_hour"], cfg["early_lead_hours"], cfg["final_check_minutes"]),
            check_in - timedelta(minutes=cfg["final_check_minutes"]),
            _dt(r["check_out_at"]),
            _dt(r["check_out_at"]) + timedelta(minutes=cfg["stale_check_minutes"]),
        ]
        if afternoon < check_in:
            times.append(afternoon)
        candidates += [t for t in times if t > now]
    candidates += [t for t in extra or [] if t > now]
    return min(candidates)


def in_final_window(r: dict[str, Any], now: datetime, final_minutes: int) -> bool:
    check_in = _dt(r["check_in_at"])
    return r["active"] and check_in - timedelta(minutes=final_minutes) <= now < check_in + timedelta(hours=6)


def new_backup_code(taken: Callable[[str], bool]) -> str:
    while True:
        code = f"{secrets.randbelow(9000) + 1000}"
        if not taken(code):
            return code


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class LockManager:
    def __init__(self, syncer):
        self.s = syncer
        self.db = syncer.db
        self.ha = syncer.ha
        self.hostaway = syncer.hostaway
        self.verify_delay = VERIFY_DELAY_SECONDS
        self.gap = GAP_BETWEEN_LOCKS_SECONDS
        self.max_per_tick = MAX_LOCKS_PER_TICK
        self.read_error = ""

    def cfg(self) -> dict[str, int]:
        return {k: self.db.get_int(k, v) for k, v in DEFAULTS.items()}

    def staff_codes(self, *, active_only: bool = True) -> dict[str, str]:
        sql = "SELECT name, code FROM staff"
        if active_only:
            sql += " WHERE active = 1"
        return {row["name"]: row["code"] for row in self.db.query(sql)}

    def managed_names(self) -> set[str]:
        """Names we are allowed to delete: every staff row (active or not) so deactivated staff leave the lock."""
        return {row["name"] for row in self.db.query("SELECT name FROM staff")}

    # ---- main loop ----------------------------------------------------------

    async def tick(self) -> int:
        now = self.s.now()
        self._prepare_backup_codes(now)
        # Expired visitor codes were removed from the locks long ago; forget the rows after a grace period.
        self.db.execute("DELETE FROM preview_codes WHERE expires_at < ?", (_utc(now - timedelta(days=2)),))
        # Automated homes, plus homes switched off that still hold our codes (wound down, never added to).
        due = self.db.query(
            "SELECT l.*, p.name AS property_name, p.backup_code, p.backup_used_by, "
            "p.lock_automation = 1 AND p.active = 1 AS automated FROM locks l "
            "JOIN properties p ON p.id = l.property_id "
            "WHERE (p.lock_automation = 1 AND p.active = 1 OR l.code_names LIKE '%\"" + PREFIX + "%') "
            "AND (l.next_check_at IS NULL OR l.next_check_at <= ?) ORDER BY l.next_check_at",
            (_utc(now),),
        )
        due = due[:self.max_per_tick]
        for lock in due:
            await self.reconcile(lock, self.s.now())
            await asyncio.sleep(self.gap)
        await self.daily_report(self.s.now())
        return len(due)

    async def reconcile(self, lock: dict[str, Any], now: datetime) -> bool:
        cfg = self.cfg()
        managed = self.managed_names()
        # Claim the lock until a retry would be due. A reservation change during the slow lock calls clears
        # next_check_at; the conditional write at the end then leaves it cleared, so the next tick looks again.
        claim = _utc(now + timedelta(minutes=cfg["retry_minutes"]))
        self.db.execute("UPDATE locks SET next_check_at = ? WHERE id = ?", (claim, lock["id"]))
        reservations = self.s.reservations_for(lock["property_id"])

        actual = await self._read(lock)
        if actual is None:
            return await self._failed(lock, now, reservations, claim, self._why("could not read the lock"), None)
        desired = self._desired(lock, reservations, now, actual, cfg)

        first = plan(desired, actual, managed)
        if BACKUP_NAME in first.elsewhere:
            self._retire_backup(lock)
        if BACKUP_NAME in first.delete and BACKUP_NAME in first.add:
            await self._note_backup_replaced(lock)
        if not first.ok:
            for name in first.delete:
                await self._try(lock, "remove", name, self.ha.delete_code(lock["entity_id"], name))
            for name, code in first.add.items():
                await self._try(lock, "add", name, self.ha.add_code(lock["entity_id"], name, code))
            await asyncio.sleep(self.verify_delay)
            actual = await self._read(lock)
            if actual is None:
                return await self._failed(lock, now, reservations, claim,
                                          self._why("could not read the lock after changes"), None)

        final = plan(desired, actual, managed)
        for name in first.add:
            if name not in final.add:
                self.db.log("code.added", f"{name} added to {lock['name']} (verified)",
                            property_id=lock["property_id"])
        for name in first.delete:
            if name not in final.delete and name not in final.add:
                self.db.log("code.removed", f"{name} removed from {lock['name']} (verified)",
                            property_id=lock["property_id"])
        self._log_present_elsewhere(lock, final.elsewhere, reservations, cfg)

        if not final.ok:
            problems = [f"missing {n}" for n in final.add] + [f"still has {n}" for n in final.delete]
            return await self._failed(lock, now, reservations, claim, ", ".join(problems), actual)

        self._finish(lock, claim, self._next(lock, reservations, now, cfg), 0, None)
        await self._final_checks(lock, now, reservations, actual, "")
        return True

    # ---- helpers ------------------------------------------------------------

    def preview_codes(self, property_id: int, now: datetime) -> dict[str, str]:
        """Visitor codes valid right now for this home, as {slot name: code}."""
        rows = self.db.query("SELECT * FROM preview_codes WHERE property_id = ? AND expires_at > ? ORDER BY id",
                             (property_id, _utc(now)))
        names = preview_slot_names(rows)
        return {names[r["id"]]: r["code"] for r in rows}

    def _next(self, lock, reservations: list[dict[str, Any]], now: datetime, cfg: dict[str, int]) -> datetime:
        expiries = [_dt(r["expires_at"]) for r in self.db.query(
            "SELECT expires_at FROM preview_codes WHERE property_id = ?", (lock["property_id"],))]
        return next_check(reservations, now, cfg, expiries)

    def _desired(self, lock, reservations, now, actual, cfg) -> dict[str, str]:
        backup = lock["backup_code"] if self.db.get_bool("backup_codes_enabled") else None
        staff = self.staff_codes() if lock["automated"] else None
        if lock["automated"]:
            return desired_codes(reservations, now, cfg["add_hour"], cfg["early_lead_hours"], backup, staff, actual,
                                 self.preview_codes(lock["property_id"], now), cfg["final_check_minutes"])
        # Automation switched off: add nothing, but guest codes already handed out stay until checkout.
        # Staff codes and HA-BACKUP already in the lock stay: the backup is permanent, and a box that gets
        # unticked by accident must never strip a home of its safety net.
        keep = {n: c for n, c in desired_codes(reservations, now, cfg["add_hour"], cfg["early_lead_hours"],
                                               actual=actual, final_minutes=cfg["final_check_minutes"]).items()
                if n in actual}
        keep.update({n: actual[n] for n in self.staff_codes() if n in actual})
        if BACKUP_NAME in actual:
            keep[BACKUP_NAME] = actual[BACKUP_NAME]
        keep.update({n: c for n, c in self.preview_codes(lock["property_id"], now).items() if n in actual})
        return keep

    async def _note_backup_replaced(self, lock) -> None:
        """The lock held an HA-BACKUP with a different code than ours, so we are overwriting it. Once is normal
        (an older version's code); again and again means something else is changing it: a second copy of this
        add-on with its own database, or someone editing the code in the Schlage app."""
        self.db.log("backup.replaced", f"HA-BACKUP on {lock['name']} had a different code; replaced it",
                    level="warning", property_id=lock["property_id"])
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
        n = self.db.one("SELECT COUNT(*) AS n FROM events WHERE kind = 'backup.replaced' AND property_id = ? "
                        "AND at >= ?", (lock["property_id"], since))["n"]
        if n >= 3:
            await self.alert(
                f"HA-BACKUP keeps changing: {lock['property_name']}",
                f"The backup code on {lock['name']} was replaced {n} times in 24 hours. Something else is "
                "changing it: check Home Assistant for a second 'Stays' / Stay Automation add-on (only one "
                "should run), or whether someone is editing the code in the Schlage app.",
                key=f"backupflap:{lock['id']}:{self.s.now().date()}")

    def _log_present_elsewhere(self, lock, names: list[str], reservations: list[dict[str, Any]],
                               cfg: dict[str, int]) -> None:
        """Once per booking: the guest code was already in the lock (Hostaway wrote it), so we added nothing.
        Together with code.added this shows how often Hostaway alone would have left a guest without a code."""
        by_name = {n: rid for rid, n in slot_names(reservations, cfg["add_hour"], cfg["early_lead_hours"],
                                                   final_minutes=cfg["final_check_minutes"]).items()}
        for name in names:
            if name == BACKUP_NAME or name in self.managed_names():
                continue
            rid = by_name.get(name)
            if rid is None:
                continue
            if not self.db.one("SELECT 1 FROM events WHERE kind = 'code.present' AND reservation_id = ?", (rid,)):
                self.db.log("code.present", f"Guest code for {rid} already in {lock['name']} (from Hostaway)",
                            property_id=lock["property_id"], reservation_id=rid)

    def _retire_backup(self, lock) -> None:
        """The backup code already opens this lock under another name (e.g. Master); never hand that out."""
        self.db.execute("UPDATE properties SET backup_code = NULL, backup_used_by = NULL WHERE id = ?",
                        (lock["property_id"],))
        self.db.log("backup.clash", f"Backup code matched another code in {lock['name']}; replacing it",
                    level="warning", property_id=lock["property_id"])

    def _finish(self, lock, claim: str, next_at: datetime, fail_count: int, error: str | None) -> None:
        self.db.execute(
            "UPDATE locks SET fail_count = ?, last_error = ?, last_reconciled_at = ?, "
            "next_check_at = CASE WHEN next_check_at = ? THEN ? ELSE next_check_at END WHERE id = ?",
            (fail_count, error, utcnow(), claim, _utc(next_at), lock["id"]),
        )

    async def _read(self, lock: dict[str, Any]) -> dict[str, str] | None:
        self.read_error = ""
        try:
            actual = await self.ha.get_codes(lock["entity_id"])
        except Exception as exc:
            reason = " ".join((str(exc) or type(exc).__name__).split())[:200]
            log.warning("read %s failed: %s", lock["entity_id"], reason)
            self.read_error = reason  # the failure line in the Log says why, not just that it failed
            return None
        self.s.store_lock_snapshot(lock["id"], actual)
        return actual

    def _why(self, what: str) -> str:
        return f"{what} ({self.read_error})" if self.read_error else what

    async def _try(self, lock: dict[str, Any], verb: str, name: str, call) -> None:
        try:
            await call
        except Exception as exc:  # judged by the read-back, not by this
            log.info("%s %s on %s returned %s", verb, name, lock["entity_id"], exc or type(exc).__name__)

    async def _failed(self, lock, now, reservations, claim: str, error: str, actual) -> bool:
        cfg = self.cfg()
        fail_count = (lock["fail_count"] or 0) + 1
        retry_at = min(now + timedelta(minutes=cfg["retry_minutes"]), self._next(lock, reservations, now, cfg))
        self._finish(lock, claim, retry_at, fail_count, error)
        self.db.log("lock.failed", f"{lock['name']}: {error} (attempt {fail_count})",
                    level="warning", property_id=lock["property_id"])
        if fail_count == 1:
            await self.alert(f"Lock problem: {lock['property_name']}",
                             f"{lock['name']}: {error}. Retrying every {cfg['retry_minutes']} minutes.",
                             key=f"lockfail:{lock['id']}:{now.date()}")
        await self._final_checks(lock, now, reservations, actual, error)
        return False

    async def _final_checks(self, lock, now, reservations, actual, error: str) -> None:
        """Within the hour before check-in, a guest whose code is not confirmed gets the backup code."""
        if not lock["automated"]:
            return
        final_minutes = self.cfg()["final_check_minutes"]
        for r in reservations:
            if not in_final_window(r, now, final_minutes):
                continue
            if actual is not None and code_present(r, actual):
                continue
            if not r["door_code"]:
                reason = "Hostaway has no door code for this booking"
            elif actual is None:
                reason = f"the lock could not be checked ({error})"
            else:
                reason = "the code is not in the lock"
            await self.fallback(r, lock, reason)
        if actual is not None:
            await self._stale_checks(lock, now, reservations, actual)

    async def _stale_checks(self, lock, now, reservations, actual: dict[str, str]) -> None:
        """Hostaway removes its own code at checkout. If a departed guest's code is still in the lock under
        another name, staff are told; we never delete codes we did not write."""
        grace = timedelta(minutes=self.cfg()["stale_check_minutes"])
        still_wanted = {r["door_code"] for r in reservations if r["active"] and _dt(r["check_out_at"]) > now}
        for r in reservations:
            out = _dt(r["check_out_at"])
            if not (r["active"] and r["door_code"] and out + grace <= now < out + timedelta(days=1)):
                continue
            names = [n for n, c in actual.items() if c == r["door_code"] and not n.startswith(PREFIX)]
            if names and r["door_code"] not in still_wanted:
                left = out.astimezone(self.s.tz).strftime("%a %I:%M %p").replace(" 0", " ")
                await self.alert(
                    f"Old guest code still in lock: {lock['property_name']}",
                    f"{r['guest_name'] or 'The guest'} checked out {left}, but their code is still in "
                    f"{lock['name']} as \"{names[0]}\". Please remove it in the Schlage app.",
                    key=f"stale:{r['id']}:{lock['id']}")

    async def fallback(self, r: dict[str, Any], lock: dict[str, Any], reason: str) -> None:
        if self.db.one("SELECT 1 FROM guest_notices WHERE reservation_id = ?", (r["id"],)):
            return
        prop = self.db.one("SELECT * FROM properties WHERE id = ?", (lock["property_id"],))
        arrival = _dt(r["check_in_at"]).astimezone(self.s.tz).strftime("%I:%M %p").lstrip("0")
        staff = f"{r['guest_name'] or 'Guest'} arrives at {prop['name']} at {arrival}: {reason}."

        backup = prop["backup_code"] if self.db.get_bool("backup_codes_enabled") else None
        if not backup:
            await self.alert(f"Guest code NOT confirmed: {prop['name']}",
                             staff + " No backup code is set up, so the guest has NOT been messaged.",
                             key=f"fallback:{r['id']}")
            self._record_notice(r, prop, delivered=False, message="(no backup code)")
            return

        template = self.db.get_setting("guest_message") or DEFAULT_GUEST_MESSAGE
        message = template.format(guest=(r["guest_name"] or "there").split()[0], property=prop["name"], code=backup)
        delivered = False
        if self.db.get_bool("guest_messages_enabled"):
            try:
                await self.hostaway.send_guest_message(r["id"], message)
                delivered = True
            except Exception as exc:
                staff += f" Sending the backup code FAILED ({exc}); please contact the guest."
        else:
            staff += " Guest messages are switched off, so nothing was sent; please give the guest the backup code."
        if delivered:
            staff += " The backup code was sent to the guest through Hostaway."
            self.db.execute("UPDATE properties SET backup_used_by = ? WHERE id = ?", (r["id"], prop["id"]))
        self._record_notice(r, prop, delivered, message)
        await self.alert(f"Guest code NOT confirmed: {prop['name']}", staff, key=f"fallback:{r['id']}")

    def _record_notice(self, r, prop, delivered: bool, message: str) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO guest_notices(reservation_id, property_id, sent_at, delivered, message) "
            "VALUES(?, ?, ?, ?, ?)",
            (r["id"], prop["id"], utcnow(), 1 if delivered else 0, message),
        )
        self.db.log("guest.backup_sent" if delivered else "guest.backup_not_sent",
                    f"Reservation {r['id']}: " + ("backup code sent to guest" if delivered
                                                  else "backup code NOT sent to guest"),
                    level="warning", property_id=prop["id"], reservation_id=r["id"])

    def _prepare_backup_codes(self, now: datetime) -> None:
        """Give each automated home a backup code; replace it once the guest who received it has left."""
        if not self.db.get_bool("backup_codes_enabled"):
            return
        props = self.db.query("SELECT * FROM properties WHERE lock_automation = 1 AND active = 1")
        taken = {p["backup_code"] for p in props if p["backup_code"]}
        for prop in props:
            used_by = prop["backup_used_by"] and self.db.one(
                "SELECT check_out_at FROM reservations WHERE id = ?", (prop["backup_used_by"],))
            if prop["backup_code"] and not (used_by and _dt(used_by["check_out_at"]) <= now):
                continue
            in_lock = set()  # hashes of every code last read from this home's locks (Master, Cleaners, guests)
            for row in self.db.query("SELECT code_hashes FROM locks WHERE property_id = ? AND code_hashes IS NOT NULL",
                                     (prop["id"],)):
                in_lock |= set(json.loads(row["code_hashes"]))
            code = new_backup_code(lambda c: c in taken or c == prop["backup_code"]
                                   or self.s.code_hash(c) in in_lock)
            taken.add(code)
            self.db.execute("UPDATE properties SET backup_code = ?, backup_used_by = NULL WHERE id = ?",
                            (code, prop["id"]))
            self.db.execute("UPDATE locks SET next_check_at = NULL WHERE property_id = ?", (prop["id"],))
            self.db.log("backup.rotated" if prop["backup_code"] else "backup.created",
                        "New backup code set", property_id=prop["id"])

    def alert_targets(self) -> list[str]:
        """HA notify services to fan out to: Setup fallback plus every active recipient row."""
        targets: list[str] = []
        fallback = (self.db.get_setting("alert_service", "") or "").strip()
        if fallback:
            targets.append(fallback)
        for row in self.db.query("SELECT target FROM alert_recipients WHERE active = 1 ORDER BY id"):
            target = (row["target"] or "").strip()
            if target and target not in targets:
                targets.append(target)
        return targets

    async def alert(self, title: str, message: str, key: str | None = None) -> None:
        if key and self.db.one("SELECT 1 FROM alerts_sent WHERE key = ?", (key,)):
            return
        try:
            await self.ha.notify(title, message, services=self.alert_targets(), notification_id=key or "")
        except Exception as exc:
            log.error("alert failed: %s", exc)
            self.db.log("alert.failed", f"Could not deliver alert '{title}': {exc}", level="error")
        if key:
            self.db.execute("INSERT OR IGNORE INTO alerts_sent(key, at) VALUES(?, ?)", (key, utcnow()))
        self.db.log("alert", f"{title}: {message}", level="warning")

    async def daily_report(self, now: datetime) -> None:
        cfg = self.cfg()
        today = now.date().isoformat()
        if now.hour < cfg["report_hour"] or self.db.get_setting("last_report_date") == today:
            return
        from .web import dashboard_rows  # the report is the dashboard's view of today's arrivals

        rows = [r for r in dashboard_rows(self.s)
                if r["status"] in ("arriving_today", "turnover_today")]
        if rows:
            lines = [f"- {r['name']}: {r['next_guest'] or 'guest'} at {r['next_in'].split(', ')[-1]}"
                     f" — {r['code_label'] or 'n/a'}"
                     + (f" ({'; '.join(r['problems'])})" if r["problems"] else "") for r in rows]
            not_ready = sum(1 for r in rows if r["code"] != "in_lock")
            message = f"{len(rows)} arrivals today, {not_ready} without a confirmed code.\n" + "\n".join(lines)
        else:
            message = "No arrivals today."
        self.db.set_setting("last_report_date", today)
        await self.alert(f"Arrivals today ({now.strftime('%a %b %d')})", message, key=f"report:{today}")
