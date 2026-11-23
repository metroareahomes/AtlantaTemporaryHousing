"""Read-only calendar and conversation previews for Kurt's reservations."""
from datetime import date, timedelta

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
    if not any(incoming for _, incoming, _ in thread.values()):
        row["note"] = "Extension outlook unknown · No guest messages"
        return

    result = await assess(client, thread, checkout, [], outlook=True)
    status = result.get("status", "unclear")
    evidence = thread.get(result.get("guest_message_id"))
    if not evidence or not evidence[1]:
        status = "unclear"
    row["note"] = LABELS.get(status, LABELS["unclear"])
    if evidence and evidence[1] and status != "unclear":
        row["evidence"] = evidence[2]
    if status not in ("considering", "confirmed"):
        row["next_action"] = {
            "declined": "Keep the original checkout. No extension hold or follow-up is proposed.",
            "booked": "Verify the extra nights in Hostaway before changing availability.",
        }.get(status, "Wait for a clear extension update before proposing a hold or a message.")
        return
    proposed = result.get("proposed_checkout")
    if not proposed or date.fromisoformat(proposed) <= checkout:
        row["note"] += " · Dates unknown"
        row["next_action"] = "Wait for the guest to clarify their dates before proposing a hold."
        if followup_due_at(result, thread, checkout) is not None:
            row["followup"] = (
                "Hi, could you let us know which extra nights you might need, or when you expect "
                "to know? That will help us check availability for you. Thank you!"
            )
            row["followup_timing"] = "Optional manual message. The automated follow-up requires specific held nights."
        return

    end = date.fromisoformat(proposed)
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
