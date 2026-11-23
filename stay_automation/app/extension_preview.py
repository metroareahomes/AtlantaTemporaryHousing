"""Read-only calendar and conversation previews for Kurt's reservations."""
from datetime import date, timedelta

import httpx

from .extension_followup import assess, conversation, followup_text, qualifies
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
        return
    proposed = result.get("proposed_checkout")
    if not proposed or date.fromisoformat(proposed) <= checkout:
        row["note"] += " · Dates unknown"
        return
    end = date.fromisoformat(proposed)
    last = end - timedelta(days=1)
    row["note"] += f" · {end:%b %d, %Y} checkout"
    row["span"] = f"{checkout:%b %d, %Y} through {last:%b %d, %Y} (nights)"
    if not qualifies(result, thread, checkout, now):
        return

    tracked = syncer.db.query(
        "SELECT night FROM extension_rates WHERE reservation_id=? AND listing_id=? AND tracking=1 "
        "AND night>=? ORDER BY night", (rid, row["listing_id"], checkout.isoformat()),
    )
    if len(tracked) != (end - checkout).days:
        return
    nights = [(checkout + timedelta(days=i)).isoformat() for i in range((end - checkout).days)]
    if [rate["night"] for rate in tracked] != nights:
        return
    days = await syncer.hostaway.pricing_calendar(row["listing_id"], nights[0], nights[-1])
    if sorted(day["date"] for day in days if blocked(day)) == nights:
        row["followup"] = followup_text(checkout, last)


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
