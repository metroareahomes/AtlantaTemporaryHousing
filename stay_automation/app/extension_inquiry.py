"""Send the initial inquiry messages for explicitly enabled Kurt listings."""
from datetime import date, datetime, timedelta

import httpx

from .extension_followup import conversation
from .extension_live import live_property, selected_properties
from .extension_preview import WAITING_FOR_GUEST, review_reservation, simulate_inquiry
from .hostaway import HostawayError


def enabled(syncer, listing):
    return (bool(getattr(syncer.settings, "extension_ai_api_key", None))
            and listing in selected_properties(syncer.db)
            and live_property(syncer.db, listing))


def queue_inquiry(syncer, reservation, reservation_id):
    listing = reservation.get("listingMapId")
    if (reservation.get("status") == "inquiry" and type(listing) is int and enabled(syncer, listing)
            and str(reservation.get("id")) == str(reservation_id)
            and str(reservation.get("accountId")) == str(syncer.settings.hostaway_account_id)):
        syncer.db.execute("INSERT OR IGNORE INTO extension_live_inquiries(id,listing_id,received_at) VALUES(?,?,?)",
                          (reservation_id, listing, syncer.now().isoformat()))


async def prepare_inquiry(syncer, item, client):
    hostaway = syncer.hostaway
    rid, listing = item["id"], item["listing_id"]
    raw = await hostaway.reservation(rid)
    account = syncer.settings.hostaway_account_id
    if (raw.get("status") != "inquiry" or str(raw.get("id")) != str(rid)
            or str(raw.get("accountId")) != str(account) or str(raw.get("listingMapId")) != str(listing)):
        return
    arrival, departure = (date.fromisoformat(raw[key]) for key in ("arrivalDate", "departureDate"))
    now = syncer.now()
    if not now.date() <= arrival < departure:
        return
    guests = syncer.db.query(
        "SELECT id, listing_id, arrival_date, departure_date FROM reservations "
        "WHERE listing_id=? AND active=1 AND julianday(check_in_at)<=julianday(?) "
        "AND julianday(check_out_at)>julianday(?)",
        (listing, now.isoformat(), now.isoformat()),
    )
    if len(guests) != 1:
        return
    guest = guests[0]
    await review_reservation(syncer, client, guest)
    result = simulate_inquiry(guest, arrival, departure)
    if not result.get("current_guest") or result.get("inquirer") != WAITING_FOR_GUEST:
        return
    checkout = guest["departure_date"]
    if syncer.db.one("SELECT 1 FROM extension_live_messages WHERE reservation_id=? AND checkout=?",
                     (guest["id"], checkout)):
        return
    thread = conversation(await hostaway.guest_messages(rid), rid, account, now)
    if any(not incoming for _, incoming, _ in thread.values()):
        return
    if not thread:
        raise HostawayError("Inquiry conversation is not ready")
    guest_thread = await hostaway.extension_conversation(guest["id"], listing)
    inquiry_thread = await hostaway.extension_conversation(rid, listing)
    if guest_thread == inquiry_thread:
        raise ValueError("Inquiry and reservation share a conversation")
    return guest, guest_thread, inquiry_thread, result["current_guest"]


async def run_inquiries(syncer):
    db = syncer.db
    items = db.query("SELECT * FROM extension_live_inquiries WHERE attempts BETWEEN 0 AND 7 ORDER BY received_at LIMIT 20")
    if not items:
        return
    key = syncer.settings.extension_ai_api_key
    async with httpx.AsyncClient(base_url="https://api.openai.com", timeout=30,
                                 headers={"Authorization": f"Bearer {key}"}) as client:
        for item in items:
            rid, listing = item["id"], item["listing_id"]
            done = "UPDATE extension_live_inquiries SET attempts=-1 WHERE id=?"
            if not enabled(syncer, listing) or syncer.now() - datetime.fromisoformat(item["received_at"]) > timedelta(minutes=10):
                db.execute(done, (rid,))
                continue
            try:
                action = await prepare_inquiry(syncer, item, client)
            except (HostawayError, httpx.HTTPError, ValueError, KeyError, TypeError, IndexError):
                db.execute("UPDATE extension_live_inquiries SET attempts=attempts+1 WHERE id=?", (rid,))
                if item["attempts"] == 7:
                    db.log("extension.inquiry_review_failed", "Could not verify inquiry messaging.", level="warning",
                           reservation_id=rid)
                continue
            db.execute(done, (rid,))
            if not action or not enabled(syncer, listing):
                continue
            guest, guest_thread, inquiry_thread, message = action
            claim = db.one("INSERT OR IGNORE INTO extension_live_messages VALUES (?,?) RETURNING reservation_id",
                               (guest["id"], guest["departure_date"]))
            if not claim:
                continue
            try:
                await syncer.hostaway.send_extension_message(guest_thread, message)
                if enabled(syncer, listing):
                    await syncer.hostaway.send_extension_message(inquiry_thread, WAITING_FOR_GUEST)
                    db.log("extension.inquiry_messages", "Asked the current guest and acknowledged the inquiry.",
                           reservation_id=rid)
            except (HostawayError, httpx.HTTPError):
                db.log("extension.inquiry_send_failed", "Inquiry messaging could not be completed. Check both conversations.",
                       level="warning", reservation_id=rid)
