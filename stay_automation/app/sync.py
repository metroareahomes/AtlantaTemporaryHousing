"""Keeps the local tables in step with Hostaway and Home Assistant."""
import asyncio
import hashlib
import hmac
import json
import logging
import secrets
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .config import Settings
from .db import DB, utcnow
from .matching import match_lock
from .reservations import changes, normalize, property_state, within

log = logging.getLogger(__name__)

RESERVATION_FIELDS = (
    "listing_id", "guest_name", "status", "active", "arrival_date", "departure_date",
    "check_in_at", "check_out_at", "door_code",
)
LOCK_CHECK_HOURS = 36
LOCK_CHECK_GAP_SECONDS = 5


class Syncer:
    def __init__(self, db: DB, settings: Settings, hostaway, ha):
        self.db = db
        self.settings = settings
        self.hostaway = hostaway
        self.ha = ha
        self.tz = ZoneInfo(settings.timezone)
        self._code_key = self._secret("code_hash_key")

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def _secret(self, key: str) -> str:
        value = self.db.get_setting(key)
        if not value:
            value = secrets.token_hex(32)
            self.db.set_setting(key, value)
        return value

    def code_hash(self, code: str) -> str:
        """Lock codes are stored only as keyed hashes; enough to answer "is this code in the lock?"."""
        return hmac.new(self._code_key.encode(), code.encode(), hashlib.sha256).hexdigest()

    # ---- properties -------------------------------------------------------

    async def import_listings(self) -> int:
        listings = await self.hostaway.listings()
        added = 0
        for listing in listings:
            hostaway_name = listing.get("internalListingName") or listing.get("name") or str(listing["id"])
            address = listing.get("address") or listing.get("street")
            existing = self.db.one(
                "SELECT id FROM properties WHERE hostaway_listing_id = ?", (listing["id"],)
            )
            if existing:
                self.db.execute(
                    "UPDATE properties SET hostaway_name = ?, address = ? WHERE id = ?",
                    (hostaway_name, address, existing["id"]),
                )
            else:
                self.db.execute(
                    "INSERT INTO properties(hostaway_listing_id, hostaway_name, name, address) VALUES(?, ?, ?, ?)",
                    (listing["id"], hostaway_name, hostaway_name, address),
                )
                added += 1
        if added:
            self.db.log("properties.imported", f"{added} new properties from Hostaway")
        return added

    # ---- locks ------------------------------------------------------------

    async def discover_locks(self) -> int:
        locks = await self.ha.schlage_locks()
        properties = self.db.query("SELECT id, name, hostaway_name, address FROM properties")
        matched = 0
        for lock in locks:
            existing = self.db.one("SELECT * FROM locks WHERE entity_id = ?", (lock["entity_id"],))
            if existing is None:
                self.db.execute(
                    "INSERT INTO locks(entity_id, name, state, seen_at) VALUES(?, ?, ?, ?)",
                    (lock["entity_id"], lock["name"], lock["state"], utcnow()),
                )
                self.db.log("lock.found", f"New lock {lock['name']} ({lock['entity_id']})")
                existing = self.db.one("SELECT * FROM locks WHERE entity_id = ?", (lock["entity_id"],))
            else:
                self.db.execute(
                    "UPDATE locks SET name = ?, state = ?, seen_at = ? WHERE id = ?",
                    (lock["name"], lock["state"], utcnow(), existing["id"]),
                )
            if existing["match_source"] is None:
                property_id = match_lock(lock["name"], properties)
                if property_id is not None:
                    self.db.execute(
                        "UPDATE locks SET property_id = ?, match_source = 'auto' WHERE id = ?",
                        (property_id, existing["id"]),
                    )
                    self.db.log("lock.matched", f"{lock['name']} matched automatically",
                                property_id=property_id)
                    matched += 1
        return matched

    def assign_lock(self, lock_id: int, property_id: int | None) -> None:
        """Manual assignment from the dashboard; auto-matching never overrides it."""
        self.db.execute(
            "UPDATE locks SET property_id = ?, match_source = 'manual' WHERE id = ?",
            (property_id, lock_id),
        )

    async def read_lock_codes(self, lock: dict[str, Any]) -> None:
        try:
            codes = await self.ha.get_codes(lock["entity_id"])
        except Exception as exc:  # network, HA or Schlage cloud
            self.db.execute("UPDATE locks SET codes_error = ? WHERE id = ?",
                            (str(exc)[:300] or type(exc).__name__, lock["id"]))
            self.db.log("lock.read_failed", f"Could not read codes from {lock['name']}: {exc}",
                        level="warning", property_id=lock["property_id"])
            return
        self.store_lock_snapshot(lock["id"], codes)

    def store_lock_snapshot(self, lock_id: int, codes: dict[str, str]) -> None:
        self.db.execute(
            "UPDATE locks SET code_hashes = ?, code_names = ?, codes_read_at = ?, codes_error = NULL "
            "WHERE id = ?",
            (json.dumps(sorted(self.code_hash(c) for c in codes.values())),
             json.dumps(sorted(codes)), utcnow(), lock_id),
        )

    async def check_arrival_locks(self) -> int:
        """Read the codes of locks whose next guest arrives within LOCK_CHECK_HOURS."""
        now = self.now()
        checked = 0
        for lock in self.db.query(
            "SELECT l.* FROM locks l JOIN properties p ON p.id = l.property_id "
            "WHERE l.state != 'unavailable' AND p.lock_automation = 0"  # automated locks are read by LockManager
        ):
            reservations = self.reservations_for(lock["property_id"])
            state = property_state(reservations, now)
            if within(state.next, now, LOCK_CHECK_HOURS) and state.next["door_code"]:
                await self.read_lock_codes(lock)
                checked += 1
                await asyncio.sleep(LOCK_CHECK_GAP_SECONDS)
        return checked

    # ---- reservations -----------------------------------------------------

    def reservations_for(self, property_id: int) -> list[dict[str, Any]]:
        """Bookings of a home. door_code is the code the guest should use: one staff typed in on the dashboard
        wins over the one Hostaway generated (kept as hostaway_code)."""
        rows = self.db.query(
            "SELECT r.* FROM reservations r JOIN properties p ON p.hostaway_listing_id = r.listing_id "
            "WHERE p.id = ? ORDER BY r.check_in_at",
            (property_id,),
        )
        for r in rows:
            r["hostaway_code"] = r["door_code"]
            if r["override_code"]:
                r["door_code"] = r["override_code"]
        return rows

    def _note_missing_code(self, raw: dict[str, Any], new: dict[str, Any]) -> None:
        """A booking arriving soon has no doorCode in the Hostaway API. Record which other fields carry a value
        (names only, never the values) so we can tell whether Hostaway keeps the code somewhere else."""
        if not new["active"] or new["door_code"]:
            return
        if new["arrival_date"] > (self.now().date() + timedelta(days=2)).isoformat():
            return
        if self.db.one("SELECT 1 FROM events WHERE kind = 'hostaway.nocode' AND reservation_id = ?", (new["id"],)):
            return
        words = ("code", "lock", "access", "pin")
        hints = [k for k, v in raw.items() if k != "doorCode" and v not in (None, "", [], {}, 0)
                 and any(w in k.lower() for w in words)]
        for field in raw.get("customFieldValues") or []:
            name = ((field or {}).get("customField") or {}).get("name") or ""
            if (field or {}).get("value") and any(w in name.lower() for w in words):
                hints.append(f"custom field '{name}'")
        self.db.log("hostaway.nocode",
                    f"Reservation {new['id']} has no doorCode in the Hostaway API. "
                    f"Other code-like fields with a value: {', '.join(hints) if hints else 'none'}",
                    level="warning", reservation_id=new["id"])

    def upsert_reservation(self, raw: dict[str, Any]) -> list[str]:
        new = normalize(raw, self.tz, self.settings.default_checkin_hour,
                        self.settings.default_checkout_hour)
        self._note_missing_code(raw, new)
        old = self.db.one("SELECT * FROM reservations WHERE id = ?", (new["id"],))
        what = changes(old, new)
        if old is None:
            self.db.execute(
                f"INSERT INTO reservations(id, {', '.join(RESERVATION_FIELDS)}, updated_at) "
                f"VALUES(?, {', '.join('?' for _ in RESERVATION_FIELDS)}, ?)",
                (new["id"], *(new[f] for f in RESERVATION_FIELDS), utcnow()),
            )
        elif any(old[f] != new[f] for f in RESERVATION_FIELDS):
            self.db.execute(
                f"UPDATE reservations SET {', '.join(f'{f} = ?' for f in RESERVATION_FIELDS)}, "
                "updated_at = ? WHERE id = ?",
                (*(new[f] for f in RESERVATION_FIELDS), utcnow(), new["id"]),
            )
        if what:
            # Let the lock logic look at this home on its next pass, and at the old home if the booking moved.
            listing_ids = {new["listing_id"], old["listing_id"] if old else new["listing_id"]}
            homes = self.db.query(
                f"SELECT id, hostaway_listing_id FROM properties "
                f"WHERE hostaway_listing_id IN ({', '.join('?' for _ in listing_ids)})",
                tuple(listing_ids),
            )
            for home in homes:
                self.db.execute("UPDATE locks SET next_check_at = NULL WHERE property_id = ?", (home["id"],))
            prop = next((h for h in homes if h["hostaway_listing_id"] == new["listing_id"]), None)
            self.db.log(
                "reservation." + what[0],
                f"Reservation {new['id']}: {', '.join(what)} "
                f"({new['arrival_date']} to {new['departure_date']})",
                property_id=prop["id"] if prop else None,
                reservation_id=new["id"],
            )
        return what

    async def sync_reservations(self) -> int:
        since = (self.now().date() - timedelta(days=1)).isoformat()
        changed = 0
        for raw in await self.hostaway.reservations(since):
            if self.upsert_reservation(raw):
                changed += 1
        self.db.set_setting("last_sync_at", utcnow())
        return changed

    async def handle_webhook(self, payload: Any) -> list[str]:
        """Hostaway unified webhook. The payload only tells us which reservation; the API is the truth."""
        reservation_id = webhook_reservation_id(payload, self.settings.hostaway_account_id)
        if reservation_id is None:
            return []
        raw = await self.hostaway.reservation(reservation_id)
        self.db.set_setting("last_webhook_at", utcnow())
        return self.upsert_reservation(raw)


def webhook_reservation_id(payload: Any, account_id: str) -> int | None:
    # addon_stdin may deliver the JSON body re-encoded as a JSON string.
    for _ in range(2):
        if not isinstance(payload, str):
            break
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, dict):
        return None
    if payload.get("accountId") and str(payload["accountId"]) != str(account_id):
        return None
    if payload.get("object") not in (None, "reservation"):
        return None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    try:
        return int(data["id"]) if "listingMapId" in data or payload.get("object") == "reservation" else None
    except (KeyError, TypeError, ValueError):
        return None
