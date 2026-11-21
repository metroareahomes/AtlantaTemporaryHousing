"""Attribute booked nights to the price in effect before the next PriceLabs sync."""
import hashlib
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/New_York")
PRICE_SYNC = time(2)


def record_price_window(db, row, verified_at):
    if row["base_rate"] is None or Decimal(row["rate"]) <= Decimal(row["base_rate"]):
        return
    local = verified_at.astimezone(TZ)
    cutoff = datetime.combine(local.date(), PRICE_SYNC, TZ)
    if local >= cutoff:
        cutoff = datetime.combine(local.date() + timedelta(days=1), PRICE_SYNC, TZ)
    db.execute(
        "INSERT OR IGNORE INTO extension_price_windows VALUES (?,?,?,?)",
        (row["listing_id"], row["night"], verified_at.timestamp(), cutoff.timestamp()),
    )


def guest_key(reservation):
    channel = reservation.get("channelId")
    guest = reservation.get("guestExternalAccountId")
    if channel and guest and str(guest).strip():
        return hashlib.sha256(f"{channel}:{guest}".encode()).hexdigest()
    return None


def record_booking(db, reservation, event, observed_at):
    if reservation.get("status") not in ("new", "modified"):
        return
    try:
        reservation_id = int(reservation["id"])
        listing_id = int(reservation["listingMapId"])
        arrival = date.fromisoformat(reservation["arrivalDate"])
        departure = date.fromisoformat(reservation["departureDate"])
        field = "updatedOn" if event == "reservation.updated" else "insertedOn"
        if raw := reservation.get(field):
            booked_at = datetime.fromisoformat(raw)
            if booked_at.tzinfo is None:
                booked_at = booked_at.replace(tzinfo=timezone.utc)
        else:
            booked_at = observed_at
    except (KeyError, TypeError, ValueError):
        return
    if arrival >= departure or booked_at > observed_at:
        return
    if not db.query(
        "SELECT 1 FROM extension_rates WHERE listing_id=? AND night>=? AND night<? "
        "AND (reservation_id=? OR guest_key=?) AND base_rate IS NOT NULL LIMIT 1",
        (listing_id, arrival.isoformat(), departure.isoformat(), reservation_id, guest_key(reservation)),
    ):
        return
    currency = str(reservation.get("currency") or reservation.get("currencyCode") or "").upper()
    if len(currency) != 3 or not currency.isalpha():
        currency = "Unknown currency"
    db.execute(
        "INSERT OR IGNORE INTO extension_booking_events VALUES (?,?,?,?,?,?,?)",
        (reservation_id, listing_id, arrival.isoformat(), departure.isoformat(),
         guest_key(reservation), booked_at.timestamp(), currency),
    )


def reconcile_bookings(db, now):
    events = db.query("SELECT * FROM extension_booking_events ORDER BY booked_at, reservation_id")
    for event in events:
        rows = db.query(
            "SELECT r.night,r.rate,r.base_rate FROM extension_price_windows w "
            "JOIN extension_rates r ON r.listing_id=w.listing_id AND r.night=w.night "
            "WHERE w.listing_id=? AND w.night>=? AND w.night<? AND w.starts_at<=? AND w.cutoff>? "
            "AND (r.reservation_id=? OR r.guest_key=?)",
            (event["listing_id"], event["arrival"], event["departure"], event["booked_at"], event["booked_at"],
             event["reservation_id"], event["guest_key"]),
        )
        for row in rows:
            delta = Decimal(row["rate"]) - Decimal(row["base_rate"])
            db.execute(
                "INSERT OR IGNORE INTO extension_booked_nights VALUES (?,?,?,?,?,?)",
                (event["listing_id"], row["night"], event["reservation_id"], event["currency"],
                 row["rate"], str(delta.quantize(Decimal(".01")))),
            )
        db.execute(
            "DELETE FROM extension_booking_events WHERE reservation_id=? AND listing_id=? AND arrival=? "
            "AND departure=? AND booked_at=?",
            (event["reservation_id"], event["listing_id"], event["arrival"], event["departure"], event["booked_at"]),
        )
    db.execute("DELETE FROM extension_price_windows WHERE cutoff < ?", ((now - timedelta(days=30)).timestamp(),))


def report_rows(db, owner=None):
    rows = db.query(
        "SELECT booked.*, rates.stay_average_rate, rates.base_rate FROM extension_booked_nights booked "
        "LEFT JOIN extension_rates rates USING (listing_id, night) "
        "WHERE (? IS NULL OR rates.owner=?) ORDER BY booked.night DESC, booked.listing_id",
        (owner, owner),
    )
    totals = {}
    for row in rows:
        total = totals.setdefault(row["currency"], {"net_rent": Decimal(0), "rent_increase": Decimal(0), "average_only": None})
        total["net_rent"] += Decimal(row["net_rent"])
        total["rent_increase"] += Decimal(row["rent_increase"])
        row["average_only"] = None
        if row["stay_average_rate"] is not None and row["base_rate"] is not None:
            difference = Decimal(row["stay_average_rate"]) - Decimal(row["base_rate"])
            average_only = max(Decimal("0.00"), difference.quantize(Decimal(".01"), rounding=ROUND_HALF_UP))
            row["average_only"] = average_only
            total["average_only"] = (total["average_only"] or Decimal("0.00")) + average_only
    return rows, sorted(totals.items())
