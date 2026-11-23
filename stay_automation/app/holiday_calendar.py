"""Keep Thanksgiving and Christmas turnovers off the Hostaway calendar."""
from datetime import date

from httpx import HTTPError

from .extension_followup import next_us_thanksgiving
from .extension_live import unreserved
from .hostaway import HostawayError

HOLIDAY_BLOCK_NOTE = "[Stay Automation holiday checkout block]"
TRUE_VALUES = (1, "1", True)
FALSE_VALUES = (0, "0", False)
DELETE_CHANGE = "DELETE FROM holiday_calendar_changes WHERE listing_id=? AND night=? AND field=?"


def is_holiday(day):
    return (day.month, day.day) == (12, 25) or (
        day.month == 11 and day == next_us_thanksgiving(day)
    )


def desired_changes(db, today):
    changes = set()
    rows = db.query(
        "SELECT r.listing_id, r.arrival_date, r.departure_date "
        "FROM reservations r JOIN properties p ON p.hostaway_listing_id=r.listing_id "
        "WHERE p.active=1 AND p.owner_scope IN ('Mike','Kurt') "
        "AND r.status IN ('new','modified','confirmed','ownerStay') AND r.departure_date>=?",
        (today.isoformat(),),
    )
    for row in rows:
        arrival = date.fromisoformat(row["arrival_date"])
        departure = date.fromisoformat(row["departure_date"])
        if arrival >= today and is_holiday(arrival):
            changes.add((row["listing_id"], arrival.isoformat(), "closedOnDeparture"))
        if is_holiday(departure):
            changes.add((row["listing_id"], departure.isoformat(), "closedOnArrival"))
    return changes


def matches(day, payload):
    for field, expected in payload.items():
        value = day.get(field)
        if field == "note":
            if (value or "") != expected:
                return False
        elif expected == 1:
            if value not in TRUE_VALUES:
                return False
        elif value not in FALSE_VALUES and not (field != "isAvailable" and value is None):
            return False
    return True


async def reconcile_change(syncer, key, wanted, existing, *, protect_checkout=False):
    listing, night, field = key
    db, hostaway = syncer.db, syncer.hostaway
    day, = await hostaway.pricing_calendar(listing, night, night)
    block = field == "isAvailable"
    if block:
        # Protect confirmed reservations, including a same-day arrival. The
        # no-checkout rule is independent and can still apply to that arrival.
        if not unreserved(day) or (protect_checkout and not matches(day, {"closedOnArrival": 1})):
            return
        note = day.get("note") or ""
        original_note = existing["original_note"] if existing else note
        marked_note = HOLIDAY_BLOCK_NOTE + (" " + original_note if original_note else "")
        applied = {"isAvailable": 0, "note": marked_note}
        original = {"isAvailable": 1, "note": original_note}
        if (len(marked_note) > 255
                or not (matches(day, applied) and day.get("status") == "blocked"
                        or matches(day, original) and day.get("status") in (None, "", "available", "open"))):
            return
    else:
        original = {field: 0}
        applied = {field: 1}
        original_note = ""
    target = applied if wanted else original
    if matches(day, target):
        if not wanted:
            db.execute(DELETE_CHANGE, key)
        return
    if not matches(day, original if wanted else applied):
        return
    if wanted and not existing:
        # Record ownership before sending, so an accepted write is recoverable
        # even if the response or verification read fails.
        db.execute("INSERT INTO holiday_calendar_changes VALUES(?,?,?,?)", (*key, original_note))
    await hostaway.apply_calendar_intervals(listing, [
        {"startDate": night, "endDate": night} | target,
    ])
    verified, = await hostaway.pricing_calendar(listing, night, night)
    if not matches(verified, target):
        raise ValueError("Hostaway did not verify the holiday change")
    if not wanted:
        db.execute(DELETE_CHANGE, key)
    action = f"enabled {field}" if wanted else f"restored {field}"
    db.log("holiday.calendar", f"Listing {listing} on {night}: {action}.")


async def reconcile_holidays(syncer):
    async with syncer.extension_calendar_lock:
        db = syncer.db
        today = syncer.now().date()
        wanted = desired_changes(db, today)
        db.execute("DELETE FROM holiday_calendar_changes WHERE night<?", (today.isoformat(),))
        existing = {
            (row["listing_id"], row["night"], row["field"]): row
            for row in db.query("SELECT * FROM holiday_calendar_changes")
        }
        for key in sorted(wanted | existing.keys()):
            listing, night, field = key
            has_checkout = (listing, night, "closedOnArrival") in wanted
            if field == "closedOnDeparture" and key in wanted and has_checkout:
                message = f"Listing {listing} already has a check-in and checkout on {night}. Review the holiday turnover."
                if not db.one("SELECT 1 FROM events WHERE kind=? AND message=? LIMIT 1",
                              ("holiday.turnover_conflict", message)):
                    db.log("holiday.turnover_conflict", message, level="warning")
            try:
                await reconcile_change(syncer, key, key in wanted, existing.get(key), protect_checkout=has_checkout)
            except (HostawayError, HTTPError, ValueError) as exc:
                db.log("holiday.calendar_failed",
                       f"Listing {listing} on {night}: could not verify {field} ({type(exc).__name__}).",
                       level="warning")
