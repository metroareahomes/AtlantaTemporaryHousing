"""Match HA lock names ("124 Maple", "Cc6 Schlage") to Hostaway listings ("Maple 124b", "CC6") and addresses."""
import re

# Words people add to lock names that never appear in listing names.
NOISE = {
    "new", "encode", "lock", "schlage", "front", "side", "back", "door", "main",
    "pkwy", "parkway", "rd", "road", "st", "street", "dr", "drive", "ln", "lane",
    # thermostat names
    "thermostat", "hvac", "climate", "upstairs", "downstairs", "ecobee", "honeywell", "t5", "t6", "t9",
}
MONTH_TAG = re.compile(r"^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\d{0,4}$")


def tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def key_tokens(lock_name: str) -> list[str]:
    """The tokens of a lock name that identify the property."""
    out = []
    for t in tokens(lock_name):
        if t in NOISE or MONTH_TAG.match(t):
            continue
        if t.isdigit() and (len(t) <= 2 or re.fullmatch(r"20[2-3]\d", t)):
            continue  # dates like "9 26" or "2026" in "Cedar 302 Encode 9 2026"
        out.append(t)
    return out


def _token_in(token: str, listing_tokens: list[str]) -> bool:
    # "124" matches "124b", "cc1" matches "cc1d"; "12" does not match "124".
    return any(re.fullmatch(re.escape(token) + r"[a-z]?", lt) for lt in listing_tokens)


def match_lock(lock_name: str, listings: list[dict]) -> int | None:
    """Property id of the single listing whose names contain every key token, else None."""
    keys = key_tokens(lock_name)
    if not keys:
        return None
    hits = []
    for listing in listings:
        # The street address carries the house number most lock names start with ("1114 Falling Water Front").
        listing_tokens = tokens(" ".join(listing.get(k) or "" for k in ("hostaway_name", "name", "address")))
        if all(_token_in(k, listing_tokens) for k in keys):
            hits.append(listing["id"])
    return hits[0] if len(hits) == 1 else None
