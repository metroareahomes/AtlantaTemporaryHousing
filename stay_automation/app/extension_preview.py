"""Read-only calendar and conversation previews for Kurt's reservations."""
from datetime import date, datetime, time, timedelta

import httpx

from .extension_followup import assess, conversation, followup_due_at, followup_text
from .extension_pricing import TZ, blocked
from .hostaway import HostawayError

LABELS = {
    "considering": "Possible extension",
    "confirmed": "Guest confirmed · Booking pending",
    "declined": "Extension declined · Original checkout",
    "booked": "Guest reports booking · Verify in Hostaway",
    "unclear": "Extension outlook unknown",
}


def preview_checkout_checkin(row, result, checkout, now):
    if result.get("host_followed_up") is True:
        row["next_action"] = "Wait for a reply to the host’s checkout or extension check-in."
        return
    deadline = datetime.combine(checkout - timedelta(days=10), time(9), TZ)
    stamp = deadline.strftime("%b %d, %Y at %I:%M %p %Z")
    row["next_action"] = "Ask the guest to confirm checkout or provide an expected extension date."
    row["followup"] = (
        f"Hi, are you still planning to check out on {checkout:%B %d}, or might you need to stay longer? "
        "If you may need extra nights, could you let us know your expected checkout date "
        "so we can check availability? Thank you!"
    )
    if now >= deadline:
        row["followup_timing"] = f"Due now. The 10-day check-in point was {stamp}."
    else:
        row["followup_timing"] = f"Proposed check-in: {stamp} (10 days before booked checkout)."


async def review_reservation(syncer, client, row):
    row["next_action"] = "Wait for the guest to mention an extension."
    row["block_dates"] = "None proposed"
    rid = row["id"]
    now = syncer.now().astimezone(TZ)
    raw = await syncer.hostaway.reservation(rid)
    if (str(raw.get("id")) != str(rid) or str(raw.get("listingMapId")) != str(row["listing_id"])
            or raw.get("status") not in ("new", "modified")):
        raise ValueError("Reservation changed")
    checkout = date.fromisoformat(raw["departureDate"])
    if checkout < now.date():
        raise ValueError("Reservation ended")
    row["departure_date"] = checkout.isoformat()
    thread = conversation(await syncer.hostaway.guest_messages(rid), rid,
                          syncer.settings.hostaway_account_id, syncer.now())
    outgoing = next(((stamp, body) for stamp, incoming, body in reversed(thread.values())
                     if not incoming), None)
    if outgoing:
        row["last_sent_at"] = outgoing[0].astimezone(TZ).strftime("%b %d, %Y %I:%M %p %Z")
        row["last_sent"] = outgoing[1]
    result = await assess(client, thread, checkout, [], outlook=True) if thread else {}
    status = result.get("status", "unclear")
    evidence = thread.get(result.get("guest_message_id"))
    proposed = result.get("proposed_checkout")
    end = date.fromisoformat(proposed) if proposed else None
    if (not evidence or not evidence[1]
            or evidence[2].strip().casefold().rstrip(".") == "alteration request received"
            or (end is not None and end <= checkout)):
        status = "unclear"
    row["note"] = LABELS.get(status, LABELS["unclear"])
    if evidence and evidence[1] and status != "unclear":
        row["evidence"] = evidence[2]
    if status == "declined":
        row["next_action"] = "Keep the booked checkout. No extension hold or follow-up is proposed."
        return
    if status not in ("considering", "confirmed") or end is None:
        row["note"] = (
            "Possible extension · Dates unknown" if status in ("considering", "confirmed")
            else LABELS["unclear"]
        )
        preview_checkout_checkin(row, result, checkout, now)
        return

    last = end - timedelta(days=1)
    estimate = "Estimated " if result.get("approximate_date") is True else ""
    row["note"] += f" · {estimate}{end:%b %d, %Y} checkout"
    row["block_dates"] = f"{checkout:%b %d, %Y} through {last:%b %d, %Y} (nights)"
    row["next_action"] = "Propose holding the listed nights, subject to calendar availability."
    if status == "confirmed":
        row["next_action"] += " Wait for the extension booking to appear in Hostaway."
        return
    if result.get("host_followed_up") is True:
        row["next_action"] += " Wait for a reply to the host’s follow-up."
        return
    if result.get("update_promised") is True:
        row["next_action"] += " Wait for the update the guest promised or the deadline agreed in the conversation."
        return
    due = followup_due_at(result, thread, checkout)
    if due is None:
        row["next_action"] += " Wait until the conversation gives a clear basis for a follow-up."
        return

    tracked = syncer.db.query(
        "SELECT night FROM extension_rates WHERE reservation_id=? AND listing_id=? AND tracking=1 "
        "AND night>=? AND night<? ORDER BY night",
        (rid, row["listing_id"], checkout.isoformat(), end.isoformat()),
    )
    nights = [rate["night"] for rate in tracked]
    held = len(nights) == (end - checkout).days
    if held:
        days = await syncer.hostaway.pricing_calendar(row["listing_id"], nights[0], nights[-1])
        held = sorted(day["date"] for day in days if blocked(day)) == nights

    row["followup"] = followup_text(checkout, last)
    stamp = due.astimezone(TZ).strftime("%b %d, %Y at %I:%M %p %Z")
    row["followup_timing"] = (
        f"Eligible since {stamp}." if now >= due else f"Earliest eligibility: {stamp}."
    )
    if not held:
        row["followup_timing"] += " Conditional on verifying that these nights are held."
    row["next_action"] += " Recheck the conversation and the hold before sending a follow-up."


async def scan_kurt(syncer, state):
    state.update(rows=[], running=True, completed=0, total=0)
    try:
        rows = syncer.db.query("""
            SELECT r.id, r.listing_id, r.guest_name, r.arrival_date, r.departure_date, p.name AS property
            FROM reservations r JOIN properties p ON p.hostaway_listing_id=r.listing_id
            WHERE r.active=1 AND p.active=1 AND p.owner_scope='Kurt' AND julianday(r.check_out_at) > julianday(?)
            ORDER BY r.arrival_date, p.name, r.id
        """, (syncer.now().isoformat(),))
        state["total"] = len(rows)
        async with httpx.AsyncClient(base_url="https://api.openai.com", timeout=30,
                                     headers={"Authorization": f"Bearer {syncer.settings.extension_ai_api_key}"}) as client:
            for row in rows:
                try:
                    await review_reservation(syncer, client, row)
                except (HostawayError, httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError, IndexError):
                    row["error"] = "Could not evaluate this reservation."
                state["rows"].append(row)
                state["completed"] += 1
    finally:
        state.update(running=False, finished_at=syncer.now().astimezone(TZ).strftime("%b %d, %Y %I:%M %p %Z"))
