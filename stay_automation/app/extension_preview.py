"""Read-only calendar and conversation previews for Kurt's reservations."""
from datetime import date, datetime, time, timedelta

import httpx

from .extension_followup import assess, conversation, followup_due_at, followup_text
from .extension_pricing import TZ, blocked
from .hostaway import HostawayError

LABELS = {
    "requested": "Extension requested · Awaiting host confirmation",
    "considering": "Possible extension",
    "confirmed": "Guest confirmed · Booking pending",
    "declined": "Extension declined · Original checkout",
    "booked": "Guest reports booking · Verify in Hostaway",
    "unclear": "Extension outlook unknown",
}


def turnover_nights(arrival, checkout):
    length = (checkout - arrival).days
    if length <= 3:
        return []
    if length <= 14:
        return [checkout]
    if checkout.weekday() == 4:
        return [checkout + timedelta(days=i) for i in (1, 2, 3)]
    count = 3 if checkout.weekday() == 5 else 2
    return [checkout + timedelta(days=i) for i in range(count)]


def preview_checkout_checkin(row, result, checkout, now):
    if result.get("host_followed_up") is True:
        row["next_action"] = "Wait for a reply to the host’s checkout or extension check-in."
        return
    deadline = datetime.combine(checkout - timedelta(days=10), time(9), TZ)
    stamp = deadline.strftime("%b %d, %Y at %I:%M %p %Z")
    row["next_action"] = (
        "Review checkout plans internally. Contact the guest only if a checkout confirmation is needed."
    )
    if result.get("checkout_checkin_appropriate") is True and result.get("update_promised") is not True:
        row["next_action"] = "Confirm the guest’s checkout plans before releasing any extension hold."
        row["followup"] = (
            f"Hi, we wanted to check in one last time before we make the house available for other bookings. "
            f"Is {checkout:%B} {checkout.day} still your planned checkout date? Thank you!"
        )
        row["followup_timing"] = f"Due now. The 10-day check-in point was {stamp}." if now >= deadline else (
            f"Proposed check-in: {stamp} (10 days before booked checkout)."
        )
        return
    if now >= deadline:
        row["followup_timing"] = f"Internal review due now. The 10-day review point was {stamp}."
    else:
        row["followup_timing"] = f"Internal review: {stamp} (10 days before booked checkout)."


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
    arrival = date.fromisoformat(raw.get("arrivalDate") or row["arrival_date"])
    row["turnover_dates"] = ", ".join(
        day.strftime("%a, %b %d") for day in turnover_nights(arrival, checkout)
    ) or "None"
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
    quote = result.get("extension_evidence_quote")
    proposed = result.get("proposed_checkout")
    end = date.fromisoformat(proposed) if proposed else None
    minimum = result.get("minimum_stay_through")
    minimum = date.fromisoformat(minimum) if minimum else None
    minimum_only = minimum is not None and minimum >= checkout and (end is None or end <= minimum)
    if minimum_only:
        end = minimum + timedelta(days=1)
    if (not evidence or not evidence[1] or not isinstance(quote, str)
            or not quote.strip() or quote.strip() not in evidence[2]
            or evidence[2].strip().casefold().rstrip(".") == "alteration request received"
            or (end is not None and end <= checkout)):
        status = "unclear"
    row["note"] = LABELS.get(status, LABELS["unclear"])
    if evidence and evidence[1] and status != "unclear":
        row["evidence"] = quote.strip()
    if arrival <= now.date():
        row["inquiry_action"] = {
            "declined": "Use the confirmed checkout and check availability after turnover.",
            "confirmed": "Check the inquiry against the extension and turnover dates first.",
            "requested": "Resolve the existing extension request before committing overlapping dates.",
            "booked": "Verify the extension booking in Hostaway before responding.",
        }.get(status, "Review the requested dates and current guest’s plans internally.")
        if (status not in ("declined", "booked", "confirmed", "requested")
                and end is None
                and result.get("inquiry_checkin_appropriate") is True
                and result.get("host_followed_up") is False
                and result.get("update_promised") is False):
            row["inquiry_action"] = (
                "If the inquiry overlaps dates the current guest may need, confirm their plans before responding."
            )
            row["inquiry_message"] = (
                f"Hi, we’re checking availability for dates after your scheduled checkout on "
                f"{checkout:%B} {checkout.day}. Could you confirm whether you’re still planning "
                "to check out that day? Thank you!"
            )
    if status == "declined":
        row["next_action"] = (
            "Keep the booked checkout. Release any extension-only hold for nights the guest no longer needs. "
            "Keep the turnover nights blocked."
        )
        return
    if status not in ("considering", "requested", "confirmed") or end is None:
        row["note"] = (
            "Possible extension · Dates unknown" if status in ("considering", "requested", "confirmed")
            else LABELS["unclear"]
        )
        preview_checkout_checkin(row, result, checkout, now)
        if (status == "considering" and result.get("contact_appropriate") is True
                and "followup" not in row and followup_due_at(result, thread, checkout) is not None):
            row["followup"] = (
                "Following up on the extra nights you mentioned. If you still need them, "
                "could you share your expected checkout date so we can check availability?"
            )
        return

    last = end - timedelta(days=1)
    estimate = "Estimated " if result.get("approximate_date") is True else ""
    if minimum_only:
        row["note"] = f"Guest plans to stay through at least {last:%b %d, %Y} · Final checkout unknown"
        row["block_dates"] = f"{checkout:%b %d, %Y} through {last:%b %d, %Y} (minimum nights)"
        row["next_action"] = (
            "Hold at least the stated extension nights, subject to calendar availability. "
            "Reassess turnover when the guest provides a final checkout date."
        )
        return
    row["note"] += f" · {estimate}{end:%b %d, %Y} checkout"
    row["block_dates"] = f"{checkout:%b %d, %Y} through {last:%b %d, %Y} (nights)"
    row["extended_turnover_dates"] = ", ".join(
        day.strftime("%a, %b %d") for day in turnover_nights(arrival, end)
    ) or "None"
    row["next_action"] = (
        "Hold the proposed extension and subsequent turnover nights, subject to calendar availability. "
        "Release extension-only nights when the guest’s plans indicate they’re unlikely to need them."
    )
    if result.get("contact_appropriate") is not True:
        row["next_action"] += " Review checkout plans internally before contacting the guest."
        return
    alteration = thread.get(result.get("pending_alteration_message_id"))
    if (alteration and not alteration[1] and result.get("approval_reminder_appropriate") is True
            and result.get("host_followed_up") is False and result.get("update_promised") is False):
        row["note"] = f"Alteration sent · Awaiting guest approval · {end:%b %d, %Y} checkout"
        row["followup"] = (
            f"Hi, just checking in on the alteration request we sent for a {end:%B} {end.day} checkout. "
            "If those dates still work for you, could you approve the request when you have a chance? "
            "If your plans have changed, just let us know. Thank you!"
        )
        row["followup_timing"] = "Draft for host review."
        row["next_action"] += " Follow up on the outstanding alteration request."
        return
    if status == "requested":
        row["next_action"] += " Confirm availability before agreeing to the request."
        return
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
