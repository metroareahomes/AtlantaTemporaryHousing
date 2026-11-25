"""Opt-in Hostaway calendar holds and selected-property checkout messages."""
import json
from datetime import date, timedelta
from itertools import groupby

from httpx import HTTPError

from .extension_preview import scan_extensions, scan_kurt, turnover_nights
from .extension_pricing import blocked
from .hostaway import HostawayError


def owner_setting(key, owner):
    return key if owner == "Kurt" else f"{key}_{owner.lower()}"


def calendar_enabled_for(db, owner):
    return db.get_bool(owner_setting("extension_live_calendar", owner))


def selected_properties(db, owner="Kurt"):
    return set(json.loads(db.get_setting(owner_setting("extension_live_properties", owner), "[]")))


HOLD_FIELDS = (
    ("turnover_short", "1–3 nights: turnover nights", 30),
    ("turnover_medium", "4–14 nights: turnover nights", 30),
    ("turnover_long", "15+ nights: turnover nights", 30),
    ("turnover_long_friday", "15+ nights, Friday checkout: turnover nights", 30),
    ("turnover_long_saturday", "15+ nights, Saturday checkout: turnover nights", 30),
    ("turnover_30_plus", "30+ nights: turnover nights", 30),
)


def hold_settings(db, owner):
    return json.loads(db.get_setting(owner_setting("extension_hold_rules", owner), "{}"))


def stay_band(nights):
    if nights <= 3:
        return "short"
    return "medium" if nights <= 14 else "long"


def turnover_override(arrival, checkout, rules):
    nights = (checkout - arrival).days
    band = stay_band(nights)
    key = f"turnover_{band}"
    if band == "long":
        suffix = {4: "_friday", 5: "_saturday"}.get(checkout.weekday(), "")
        count = rules.get(key + suffix, rules.get(key))
    else:
        count = rules.get(key)
    if nights >= 30:
        count = rules.get("turnover_30_plus", count)
    if count is None:
        return None
    start = 1 if band == "long" and checkout.weekday() == 4 else 0
    return [checkout + timedelta(days=start + offset) for offset in range(count)]


def displayed_turnover_nights(rules):
    # Use the same calculation as the live calendar for each relevant checkout day.
    thursday = date(2026, 1, 1)
    friday, saturday = (thursday + timedelta(days=i) for i in (1, 2))

    def count(length, checkout):
        arrival = checkout - timedelta(days=length)
        override = turnover_override(arrival, checkout, rules)
        return len(turnover_nights(arrival, checkout) if override is None else override)

    return {
        "turnover_short": str(count(3, thursday)),
        "turnover_medium": str(count(14, thursday)),
        "turnover_long": f"{count(15, thursday)} (Sun–Thu)",
        "turnover_long_friday": str(count(15, friday)),
        "turnover_long_saturday": str(count(15, saturday)),
        "turnover_30_plus": (
            str(rules["turnover_30_plus"]) if "turnover_30_plus" in rules else
            f"{count(30, thursday)} (Sun–Thu), {count(30, friday)} (Fri), {count(30, saturday)} (Sat)"
        ),
    }


def apply_hold_rules(row, rules):
    """Return the live plan and which kinds of existing holds the policy may trim."""
    if not rules or "booked_turnover" not in row or not row.get("arrival_date"):
        return row, set()
    arrival = date.fromisoformat(row["arrival_date"])
    checkout = date.fromisoformat(row["departure_date"])
    plan, trim = row.copy(), set()
    booked = turnover_override(arrival, checkout, rules)
    if booked is not None:
        plan["booked_turnover"] = booked
        trim.add("Turnover hold")
    end = row.get("hold_end")
    if row.get("error") or not end:
        return plan, trim
    if row.get("extended_turnover_dates") is not None:
        projected = turnover_override(arrival, end, rules)
        if projected is not None:
            plan["projected_turnover"] = projected
            trim.add("Turnover after proposed extension")
    return plan, trim


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


HOLD_LABELS = ("Turnover hold", "Guest extension hold", "Turnover after proposed extension")
CALENDAR_NOTE_LIMIT = 255


def is_hold_note(note, marker):
    if note == marker:
        return True
    return any(
        note in (f"{marker} · {label}", f"{label} {marker}")
        or (note.startswith(f"{label} · ") and note.endswith(f" {marker}"))
        for label in HOLD_LABELS
    )


def calendar_hold_note(marker, label, outlook):
    # Keep the ownership marker intact and put the human-readable context first.
    prefix = f"{label} · "
    suffix = f" {marker}"
    if not outlook:
        return f"{label}{suffix}"
    budget = CALENDAR_NOTE_LIMIT - len(prefix) - len(suffix)
    if len(outlook) > budget:
        outlook = outlook[:budget - 1].rstrip() + "…"
    return f"{prefix}{outlook}{suffix}"


def unreserved(day):
    return not day.get("reservations") and all(
        day.get(field) in (None, 0, "0")
        for field in ("countPendingUnits", "countReservedUnits", "countBlockingReservations")
    )


def owns_hold(day, record, daily_marker, listing):
    note = day.get("note") or ""
    return blocked(day) and any(is_hold_note(note, marker) for marker in (
        record["marker"], daily_marker, f"[Stay Automation hold {listing}/stay-{record['reservation_id']}]",
    ))


async def update_holds(syncer, listing, rows, today, notes_enabled, *, report=None, owner="Kurt"):
    db, hostaway = syncer.db, syncer.hostaway
    rules = hold_settings(db, owner)
    adjusted = [apply_hold_rules(row, rules) for row in rows]
    desired, _ = calendar_plan([plan for plan, _ in adjusted], today)
    original, releasable = calendar_plan(rows, today)
    trimmable = {plan["id"]: labels for plan, labels in adjusted}
    outlooks = {row["id"]: row.get("note", "") for row in rows if not row.get("error")}
    owned = {
        date.fromisoformat(item["night"]): item for item in db.query(
            "SELECT * FROM extension_live_holds WHERE listing_id=? AND night>=?",
            (listing, today.isoformat()),
        )
    }
    if report is None:
        report = {"changes": 0, "candidates": 0, "skipped": 0}
    report["candidates"] += len(desired)
    changes = []
    for night in sorted(desired.keys() | owned.keys()):
        if not live_property(db, listing, owner) or not calendar_enabled_for(db, owner):
            return
        iso = night.isoformat()
        old = owned.get(night)
        target = desired.get(night)
        if not target and not old:
            continue
        if not target and old["reservation_id"] not in releasable:
            previous = original.get(night)
            label = old["hold_label"]
            if not label and previous and previous[0] == old["reservation_id"]:
                label = previous[1]
            if label not in trimmable.get(old["reservation_id"], ()):
                continue
        try:
            day, = await hostaway.pricing_calendar(listing, iso, iso)
            if not unreserved(day):
                report["skipped"] += bool(target)
                continue
            note = day.get("note") or ""
            daily_marker = f"[Stay Automation hold {listing}/{iso}]"
            owned_block = old and owns_hold(day, old, daily_marker, listing)
            if target:
                rid, label = target
                marker = f"[Stay Automation hold {listing}/stay-{rid}]" if notes_enabled else daily_marker
                if not owned_block and (note or day.get("status") not in (None, "", "available", "open")
                                        or day.get("isAvailable") not in (1, "1", True)):
                    report["skipped"] += 1
                    continue
                if owned_block and old["reservation_id"] != rid and old["reservation_id"] not in releasable:
                    continue
                text = calendar_hold_note(marker, label, outlooks.get(rid)) if notes_enabled else marker
                if owned_block and note == text and old["reservation_id"] == rid:
                    if old["hold_label"] != label:
                        db.execute("UPDATE extension_live_holds SET hold_label=? WHERE listing_id=? AND night=?",
                                   (label, listing, iso))
                    continue
                db.execute(
                    "INSERT OR REPLACE INTO extension_live_holds "
                    "(listing_id, night, reservation_id, marker, original_note, hold_label) VALUES(?,?,?,?,?,?)",
                    (listing, iso, rid, marker, old["original_note"] if owned_block else note, label),
                )
                payload = {"isAvailable": 0, "note": text}
            else:
                if not owned_block:
                    continue
                payload = {"isAvailable": 1, "note": old["original_note"]}
            changes.append((night, payload))
        except (HostawayError, HTTPError, ValueError) as exc:
            log_failed_night(db, listing, iso, report, exc)

    def interval_key(item):
        index, (night, payload) = item
        # Any skipped or protected night creates a gap between intervals.
        if payload["isAvailable"]:
            return night.toordinal(), 1, payload["note"]
        return night.toordinal() - index, 0, payload["note"]

    for _, group in groupby(enumerate(changes), key=interval_key):
        interval = [change for _, change in group]
        first, payload = interval[0]
        last = interval[-1][0]
        if not live_property(db, listing, owner) or not calendar_enabled_for(db, owner):
            return
        try:
            if payload["isAvailable"]:
                iso = first.isoformat()
                current, = await hostaway.pricing_calendar(listing, iso, iso)
                if not owns_hold(current, owned[first], f"[Stay Automation hold {listing}/{iso}]", listing):
                    report["skipped"] += 1
                    continue
            await hostaway.apply_calendar_intervals(listing, [
                {"startDate": first.isoformat(), "endDate": last.isoformat()} | payload
            ])
        except (HostawayError, HTTPError, ValueError) as exc:
            for night, _ in interval:
                log_failed_night(db, listing, night.isoformat(), report, exc)
            continue

        for night, _ in interval:
            iso = night.isoformat()
            holding = payload["isAvailable"] == 0
            try:
                verified, = await hostaway.pricing_calendar(listing, iso, iso)
                if (holding != blocked(verified) or (verified.get("note") or "") != payload["note"]
                        or (not holding and verified.get("isAvailable") not in (1, "1", True))):
                    raise ValueError("Hostaway did not verify the calendar change")
            except (HostawayError, HTTPError, ValueError) as exc:
                log_failed_night(db, listing, iso, report, exc)
                continue
            if not holding:
                db.execute("DELETE FROM extension_live_holds WHERE listing_id=? AND night=?", (listing, iso))
            report["changes"] += 1
            db.log("extension.live_calendar", f"Verified {'hold' if holding else 'release'} for listing {listing} on {iso}.")

def log_failed_night(db, listing, iso, report, exc):
    report["failed_nights"] = report.get("failed_nights", 0) + 1
    db.log("extension.live_calendar_failed",
           f"Calendar change could not be verified for listing {listing} on {iso} "
           f"({type(exc).__name__}).", level="warning")


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
