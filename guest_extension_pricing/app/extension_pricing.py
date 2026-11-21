"""Reapply extension rates to the contiguous Hostaway block after checkout."""
import html
import json
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from zoneinfo import ZoneInfo

from .report import guest_key, record_booking, record_price_window, reconcile_bookings

OWNERS = ("Mike", "Kurt")
TZ = ZoneInfo("America/New_York")

DEFAULTS = {
    "enabled": False, "percent": "0", "save": "00:45", "restore": "04:00",
    "min_nights": None, "max_nights": None, "tags": [],
}
SETTINGS_KEY = "extension_pricing"
SYNC_TIME = "02:00"

EXTENSION = re.compile(
    r"\b(?:extend(?:ing)? (?:our|my|the|this) (?:stay|booking|reservation|trip)"
    r"|(?:can|could|may|might|need to|want to|would like to|hoping to|have to|able to) "
    r"(?:we |i )?(?:extend\b(?!\s+(?:checkout|check.out|our check.out|my check.out))"
    r"|stay (?:longer|until|through|another|an? (?:extra|additional)|(?:\d+|one|two|three|four|five|six|seven|a few|a couple of) more)"
    r"|(?:add|book|get|keep) (?:us )?(?:another|one more|an? (?:extra|additional)|\d+ more) "
    r"(?:night|day|week))"
    r"|(?:another|one more|extra|additional|\d+ more) (?:night|week)s?\b"
    r"|(?:can|could) (?:we|i) (?:leave|check.out) (?:on|a day later|tomorrow)"
    r"|(?:is|would) (?:it|the (?:house|place|unit)) (?:still )?available (?:after|through|until)\b"
    r"|(?:push|move|change) (?:our|my|the) (?:check.out|departure) (?:date |day )?(?:to|back|by)\b)",
    re.IGNORECASE,
)
NEGATED = re.compile(
    r"\b(?:won't|will not|don't need to|do not need to|can't|cannot|unable to|no need to|"
    r"no longer (?:need|want|plan) to) (?:(?:need|want|plan) to )?(?:extend|stay|add|book|keep)\b",
    re.IGNORECASE,
)


def owner_tags(raw):
    """Read exact owner tag names from Hostaway resources."""
    key = next((k for k in ('listingTags', 'reservationTags', 'tags') if k in raw), None)
    if key is None:
        return None
    names = set()
    def visit(value):
        if isinstance(value, str):
            for owner in OWNERS:
                if value.strip().casefold() == owner.casefold():
                    names.add(owner)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, dict):
            for k in ('name', 'tagName', 'tag', 'listingTag'):
                if k in value:
                    visit(value[k])
    visit(raw[key])
    return names


def money(value):
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError('Invalid rate')
    return result


def extension_intent(body):
    body = re.sub(r"<blockquote\b[^>]*>.*?</blockquote>", "", body, flags=re.I | re.S)
    body = html.unescape(re.sub(r"<[^>]+>", " ", body)).replace("’", "'")
    body = re.split(r"(?im)^\s*(?:>+|On .+ wrote:|From:|[- ]*Original Message)", body, maxsplit=1)[0]
    return any(EXTENSION.search(clause) and not NEGATED.search(clause)
               for clause in re.split(r"[.!?;\n]+", body))


def read_settings(db):
    return {**DEFAULTS, **json.loads(db.get_setting(SETTINGS_KEY, "{}"))}


def validate_settings(form):
    config = {key: str(form.get(f"extension_{key}", "")).strip() for key in ("percent", "save", "restore")}
    try:
        percent = Decimal(config["percent"])
        if not percent.is_finite() or percent <= -100 or percent != percent.quantize(Decimal(".01")):
            raise ValueError
    except (InvalidOperation, ValueError):
        raise ValueError("Enter a percentage above -100 with up to two decimal places.") from None
    if any(not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", config[key])
           for key in ("save", "restore")):
        raise ValueError("Enter valid save and rewrite times.")
    if not config["save"] < SYNC_TIME < config["restore"]:
        raise ValueError("Save before the 2:00 a.m. PriceLabs sync and rewrite after it.")
    for key in ("min_nights", "max_nights"):
        value = str(form.get(f"extension_{key}", "")).strip()
        if value and not re.fullmatch(r"[1-9][0-9]{0,8}", value):
            raise ValueError("Stay lengths must be positive whole numbers or blank.")
        config[key] = int(value) if value else None
    if config["min_nights"] and config["max_nights"] and config["min_nights"] > config["max_nights"]:
        raise ValueError("Minimum stay length must be at most the maximum.")
    config["tags"] = [tag for tag in OWNERS if form.get(f"extension_tag_{tag.lower()}") == "on"]
    config["enabled"] = form.get("extension_enabled") == "on"
    return config


def blocked(day):
    return (
        day.get("status") == "blocked"
        and day.get("isAvailable") in (None, 0, "0")
        and not day.get("reservations")
        and all(day.get(key) in (None, 0, "0") for key in
                ("countPendingUnits", "countReservedUnits", "countBlockingReservations"))
    )


class ExtensionPricing:
    def __init__(self, syncer):
        self.s = syncer
        self.db = syncer.db

    def receive(self, payload):
        for _ in range(2):
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except ValueError:
                    return False
        if not isinstance(payload, dict):
            return False
        message = payload.get("data", payload)
        if not isinstance(message, dict) or any(
            str(item.get("accountId")) != str(self.s.settings.hostaway_account_id)
            for item in (payload, message) if item.get("accountId") is not None
        ):
            return False
        event = payload.get("event")
        if payload.get("object") == "reservation" and event in ("reservation.created", "reservation.updated"):
            record_booking(self.db, message, event, self.s.now())
            return False
        if payload.get("object") != "conversationMessage":
            return False
        if event != "message.received" or not read_settings(self.db)["enabled"]:
            return True
        body = message.get("body")
        if (message.get("isIncoming") not in (1, "1")
                or not isinstance(body, str) or not extension_intent(body)):
            return True
        try:
            message_id = int(message.get("id") or payload["objectId"])
            reservation_id = int(message["reservationId"])
        except (ValueError, TypeError, KeyError):
            return True
        if message_id > 0 and reservation_id > 0:
            self.db.execute(
                "INSERT OR IGNORE INTO extension_messages(id,reservation_id) VALUES (?,?)",
                (message_id, reservation_id),
            )
        return True

    async def start_extension(self, reservation_id, today, config):
        percent = Decimal(config["percent"])
        if not percent:
            return
        reservation = await self.s.hostaway.reservation(reservation_id)
        if reservation.get("status") not in ("new", "modified"):
            return
        listing_id = int(reservation["listingMapId"])
        first = date.fromisoformat(reservation["departureDate"])
        if first < today:
            return
        minimum, maximum = config["min_nights"], config["max_nights"]
        if minimum is not None or maximum is not None:
            nights = (first - date.fromisoformat(reservation["arrivalDate"])).days
            if nights < (minimum or 1) or (maximum is not None and nights > maximum):
                return
        if config["tags"]:
            listing = await self.s.hostaway.listing_details(listing_id)
            if not set(config["tags"]).intersection(owner_tags(listing) or ()):
                return
        while True:
            last = first + timedelta(days=4)
            calendar = await self.s.hostaway.pricing_calendar(listing_id, first.isoformat(), last.isoformat())
            for day in sorted(calendar, key=lambda item: item["date"]):
                if not blocked(day):
                    return
                if self.db.query("SELECT 1 FROM extension_rates WHERE listing_id=? AND night=?",
                                 (listing_id, day["date"])):
                    continue
                base = money(day["price"])
                rate = (base * (1 + percent / 100)).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
                if rate <= 0 or rate == base:
                    continue
                self.db.execute(
                    "INSERT OR IGNORE INTO extension_rates(listing_id,night,rate,base_rate,reservation_id,guest_key) "
                    "VALUES (?,?,?,?,?,?)",
                    (listing_id, day["date"], str(rate), str(base), reservation_id, guest_key(reservation)),
                )
            first = last + timedelta(days=1)

    async def apply_rate(self, row, day, service_date, clock, config):
        listing_id, night = row["listing_id"], row["night"]
        key = (listing_id, night)
        if not blocked(day):
            if day.get("status") in ("available", "reserved", "pending"):
                self.db.execute("UPDATE extension_rates SET tracking=0 WHERE listing_id=? AND night=?", key)
            return

        if config["save"] <= clock < SYNC_TIME and row["captured_on"] != service_date:
            self.db.execute(
                "UPDATE extension_rates SET captured_on=?, snapshot_rate=rate WHERE listing_id=? AND night=?",
                (service_date, *key),
            )
        if SYNC_TIME <= clock < config["restore"]:
            return
        restore_due = clock >= config["restore"] and row["restored_on"] != service_date
        if row["applied"] and not restore_due:
            return
        target = (row["snapshot_rate"] if restore_due and row["captured_on"] == service_date
                  else row["rate"])
        if money(day["price"]) != money(target):
            live = self.s.now().astimezone(TZ)
            if live.date().isoformat() != service_date or SYNC_TIME <= live.strftime("%H:%M") < config["restore"]:
                return
            await self.s.hostaway.apply_calendar_intervals(listing_id, [
                {"startDate": night, "endDate": night, "price": float(target)},
            ])
            after, = await self.s.hostaway.pricing_calendar(listing_id, night, night)
            if money(after["price"]) != money(target):
                raise ValueError("Extension price readback did not match")
        verified_at = self.s.now().astimezone(TZ)
        if verified_at.date().isoformat() != service_date or SYNC_TIME <= verified_at.strftime("%H:%M") < config["restore"]:
            return
        record_price_window(self.db, row, verified_at)
        self.db.execute(
            "UPDATE extension_rates SET applied=1, restored_on=? WHERE listing_id=? AND night=?",
            (service_date if restore_due else row["restored_on"], *key),
        )
        self.db.log("pricing.extension_rate", f"Verified listing {listing_id} on {night} at {target}")

    async def tick(self):
        config = read_settings(self.db)
        local = self.s.now().astimezone(TZ)
        today, clock = local.date(), local.strftime("%H:%M")
        if not config["enabled"]:
            reconcile_bookings(self.db, local)
            return
        service_date = today.isoformat()
        self.db.execute("UPDATE extension_rates SET tracking=0 WHERE tracking=1 AND night < ?", (service_date,))
        if not SYNC_TIME <= clock < config["restore"]:
            for item in self.db.query(
                "SELECT * FROM extension_messages WHERE processed=0 ORDER BY attempts,id LIMIT 20"
            ):
                try:
                    await self.start_extension(item["reservation_id"], today, config)
                except Exception:
                    self.db.execute("UPDATE extension_messages SET attempts=attempts+1 WHERE id=?", (item["id"],))
                    self.db.log("pricing.extension_failed",
                                "Could not check extension dates; will retry", level="warning",
                                reservation_id=item["reservation_id"])
                    continue
                self.db.execute("UPDATE extension_messages SET processed=1 WHERE id=?", (item["id"],))

        for row in self.db.query("SELECT * FROM extension_rates WHERE tracking=1 ORDER BY listing_id,night"):
            night = row["night"]
            try:
                day, = await self.s.hostaway.pricing_calendar(row["listing_id"], night, night)
                await self.apply_rate(row, day, service_date, clock, config)
            except Exception:
                self.db.log("pricing.extension_failed", f"Could not verify extension price for "
                            f"listing {row['listing_id']} on {night}; will retry", level="warning")
        reconcile_bookings(self.db, self.s.now())
