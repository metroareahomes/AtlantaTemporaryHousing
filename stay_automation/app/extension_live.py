"""Opt-in Hostaway calendar holds and selected-property checkout messages."""
import json
from datetime import date, timedelta

from httpx import HTTPError

from .extension_preview import scan_extensions, scan_kurt
from .extension_pricing import blocked
from .hostaway import HostawayError


def owner_setting(key, owner):
    return key if owner == "Kurt" else f"{key}_{owner.lower()}"


def calendar_enabled_for(db, owner):
    return db.get_bool(owner_setting("extension_live_calendar", owner))


def selected_properties(db, owner="Kurt"):
    return set(json.loads(db.get_setting(owner_setting("extension_live_properties", owner), "[]")))


def calendar_plan(rows, today):
    desired, releasable = {}, set()
    for row in rows:
        rid = row["id"]
        for night in row.get("booked_turnover", []):
            if night >= today:
                desired.setdefault(night, (rid, "Turnover hold"))
        if row.get("error"):
            continue
        checkout = date.fromisoformat(row["departure_date"])
        end = row.get("hold_end")
        if (end and end == row.get("firm_hold_end")) or row.get("extension_status") == "declined":
            releasable.add(rid)
        if end:
            for offset in range((end - checkout).days):
                night = checkout + timedelta(days=offset)
                if night >= today:
                    desired[night] = (rid, "Guest extension hold")
            for night in row.get("projected_turnover", []):
                if night >= today:
                    desired.setdefault(night, (rid, "Turnover after proposed extension"))
    return desired, releasable


def hold_notes(marker):
    return {marker, *(marker + " · " + label for label in (
        "Turnover hold", "Guest extension hold", "Turnover after proposed extension"
    ))}


def unreserved(day):
    return not day.get("reservations") and all(
        day.get(field) in (None, 0, "0")
        for field in ("countPendingUnits", "countReservedUnits", "countBlockingReservations")
    )


async def update_holds(syncer, listing, rows, today, notes_enabled, *, report=None, owner="Kurt"):
    db, hostaway = syncer.db, syncer.hostaway
    desired, releasable = calendar_plan(rows, today)
    owned = {
        date.fromisoformat(item["night"]): item for item in db.query(
            "SELECT * FROM extension_live_holds WHERE listing_id=? AND night>=?",
            (listing, today.isoformat()),
        )
    }
    if report is None:
        report = {"changes": 0, "candidates": 0, "skipped": 0}
    report["candidates"] += len(desired)
    for night in sorted(desired.keys() | owned.keys()):
        if not live_property(db, listing, owner) or not calendar_enabled_for(db, owner):
            return
        iso = night.isoformat()
        old = owned.get(night)
        target = desired.get(night)
        if not target and (not old or old["reservation_id"] not in releasable):
            continue
        try:
            day, = await hostaway.pricing_calendar(listing, iso, iso)
            if not unreserved(day):
                report["skipped"] += bool(target)
                continue
            note = day.get("note") or ""
            if target:
                rid, label = target
                marker = f"[Stay Automation hold {listing}/{iso}]"
                owned_block = old and blocked(day) and note in hold_notes(old["marker"])
                if not owned_block and (note or day.get("status") not in (None, "", "available", "open")
                                        or day.get("isAvailable") not in (1, "1", True)):
                    report["skipped"] += 1
                    continue
                if owned_block and old["reservation_id"] != rid and old["reservation_id"] not in releasable:
                    continue
                text = f"{marker} · {label}" if notes_enabled else marker
                if owned_block and note == text and old["reservation_id"] == rid:
                    continue
                db.execute(
                    "INSERT OR REPLACE INTO extension_live_holds VALUES(?,?,?,?,?)",
                    (listing, iso, rid, marker, old["original_note"] if owned_block else note),
                )
                payload = {"isAvailable": 0, "note": text}
            else:
                if not blocked(day) or note not in hold_notes(old["marker"]):
                    continue
                payload = {"isAvailable": 1, "note": old["original_note"]}
            if not live_property(db, listing, owner) or not calendar_enabled_for(db, owner):
                return
            await hostaway.apply_calendar_intervals(listing, [{"startDate": iso, "endDate": iso} | payload])
            verified, = await hostaway.pricing_calendar(listing, iso, iso)
            if (bool(target) != blocked(verified) or (verified.get("note") or "") != payload["note"]
                    or (not target and verified.get("isAvailable") not in (1, "1", True))):
                raise ValueError("Hostaway did not verify the calendar change")
            if not target:
                db.execute("DELETE FROM extension_live_holds WHERE listing_id=? AND night=?", (listing, iso))
            report["changes"] += 1
            db.log("extension.live_calendar", f"Verified {'hold' if target else 'release'} for listing {listing} on {iso}.")
        except (HostawayError, HTTPError, ValueError) as exc:
            report["failed_nights"] = report.get("failed_nights", 0) + 1
            db.log("extension.live_calendar_failed",
                   f"Calendar change could not be verified for listing {listing} on {iso} "
                   f"({type(exc).__name__}).", level="warning")

    return


def live_property(db, listing, owner="Kurt"):
    return db.one("SELECT 1 FROM properties WHERE hostaway_listing_id=? AND active=1 AND owner_scope=?",
                  (listing, owner)) is not None


async def run_extension_live(syncer, *, calendar_only=False, owner="Kurt"):
    async with syncer.extension_calendar_lock:
        return await _review_live(syncer, calendar_only, owner)


async def _review_live(syncer, calendar_only, owner):
    db = syncer.db
    calendar_enabled = calendar_enabled_for(db, owner)
    selected = selected_properties(db, owner)
    ai_enabled = bool(syncer.settings.extension_ai_api_key)
    if not calendar_enabled and (calendar_only or not (ai_enabled and selected)):
        return "Calendar holds are disabled."
    state = {}
    if owner == "Kurt":
        await scan_kurt(syncer, state, turnover_only=not ai_enabled)
    else:
        await scan_extensions(syncer, state, owner=owner, turnover_only=not ai_enabled)
    now = syncer.now()
    by_listing = {}
    for row in state["rows"]:
        by_listing.setdefault(row["listing_id"], []).append(row)
    failures = 0
    report = {"changes": 0, "candidates": 0, "skipped": 0}
    for listing, rows in by_listing.items():
        if not live_property(db, listing, owner):
            continue
        if calendar_enabled:
            try:
                await update_holds(syncer, listing, rows, now.date(), listing in selected_properties(db, owner), report=report, owner=owner)
            except Exception:
                failures += 1
                db.log("extension.live_calendar_failed",
                       f"Calendar change could not be verified for listing {listing}.", level="warning")
        if calendar_only:
            continue
        for row in rows:
            try:
                if listing not in selected_properties(db, owner) or not live_property(db, listing, owner):
                    break
                due = row.get("checkout_checkin_due")
                checkout = row["departure_date"]
                if (row.get("error") or not due or not due <= now < due + timedelta(days=1) or
                        not date.fromisoformat(row["arrival_date"]) <= now.date() < date.fromisoformat(checkout)):
                    continue
                if not db.one("INSERT OR IGNORE INTO extension_live_messages VALUES (?,?) RETURNING reservation_id",
                          (row["id"], checkout)):
                    continue
                await syncer.hostaway.send_extension_checkin(row["id"], row["followup"])
                db.log("extension.live_message", "Sent checkout check-in.", reservation_id=row["id"])
            except Exception:
                db.log("extension.live_message_failed", "Checkout check-in could not be verified.",
                       reservation_id=row["id"], level="warning")
    if calendar_only:
        review_errors = sum(bool(row.get("error")) for row in state["rows"])
        return (f"Calendar review complete. {report['candidates']} proposed hold nights, "
                f"{report['changes']} verified calendar changes, {report['skipped']} unavailable or protected nights, "
                f"{report.get('failed_nights', 0)} night write or verification failures, "
                f"{failures} property failures, and "
                f"{review_errors} reservation review errors.")
