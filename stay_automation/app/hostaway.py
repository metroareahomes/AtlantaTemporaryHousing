"""Hostaway public API client."""
import asyncio
import logging
import re
from typing import Any, Callable

import httpx

BASE_URL = "https://api.hostaway.com/v1"
PAGE_SIZE = 100

log = logging.getLogger(__name__)


def has_ntc(note: Any) -> bool:
    return isinstance(note, str) and bool(re.search(r"(?<!\w)NTC(?!\w)", note, re.I))


class HostawayError(Exception):
    pass


class HostawayClient:
    def __init__(self, account_id: str, api_key: str, *,
                 load_token: Callable[[], str | None] = lambda: None,
                 save_token: Callable[[str], None] = lambda token: None,
                 http: httpx.AsyncClient | None = None):
        self._account_id = account_id
        self._api_key = api_key
        self._load_token = load_token
        self._save_token = save_token
        self._token: str | None = None
        self._note_lock = asyncio.Lock()
        self._http = http or httpx.AsyncClient(base_url=BASE_URL, timeout=30)

    async def close(self) -> None:
        await self._http.aclose()

    async def _fetch_token(self) -> str:
        if not self._account_id.strip() or not self._api_key.strip():
            raise HostawayError(
                "Hostaway login failed: the account ID and/or API key are empty. "
                "Enter them on the add-on's Configuration tab, save, then restart the add-on.")
        resp = await self._http.post(
            "/accessTokens",
            data={
                "grant_type": "client_credentials",
                "client_id": self._account_id,
                "client_secret": self._api_key,
                "scope": "general",
            },
        )
        if resp.status_code in (400, 401, 403):
            raise HostawayError(
                f"Hostaway login failed: HTTP {resp.status_code}. Hostaway rejected the account ID / API key. "
                "Check them on the add-on's Configuration tab (account ID = the numeric Client ID, "
                "API key = the Client Secret from Hostaway Settings > Hostaway API), save, then restart the add-on.")
        if resp.status_code != 200:
            raise HostawayError(f"Hostaway login failed: HTTP {resp.status_code}")
        token = resp.json()["access_token"]
        self._save_token(token)
        return token

    async def _request(self, method: str, path: str, *, retry_transient: bool = True, **kwargs: Any) -> dict[str, Any]:
        if self._token is None:
            self._token = self._load_token()
        for attempt in range(4):
            if self._token is None:
                self._token = await self._fetch_token()
            resp = await self._http.request(
                method, path, headers={"Authorization": f"Bearer {self._token}"}, **kwargs
            )
            if resp.status_code in (401, 403):
                self._token = None
                if not retry_transient:
                    raise HostawayError(f"{method} {path}: HTTP {resp.status_code}; retry after a fresh read")
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                if not retry_transient:
                    raise HostawayError(f"{method} {path}: HTTP {resp.status_code}; retry after a fresh read")
                await asyncio.sleep(2 ** attempt)
                continue
            if resp.status_code != 200:
                detail = " ".join((resp.text or "").split())[:300]  # Hostaway says why it refused
                raise HostawayError(f"{method} {path}: HTTP {resp.status_code}" + (f" - {detail}" if detail else ""))
            body = resp.json()
            if body.get("status") != "success":
                raise HostawayError(f"{method} {path}: {body.get('message', 'failed')}")
            return body
        raise HostawayError(f"{method} {path}: gave up after retries")

    async def _paged(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        offset = 0
        while True:
            body = await self._request(
                "GET", path, params={**params, "limit": PAGE_SIZE, "offset": offset}
            )
            page = body.get("result") or []
            results.extend(page)
            if len(page) < PAGE_SIZE:
                return results
            offset += PAGE_SIZE

    async def listings(self) -> list[dict[str, Any]]:
        return await self._paged("/listings", {"includeResources": 1})

    async def pricing_listing(self, listing_id: int) -> dict[str, Any]:
        """Read only the metadata required for a pricing simulation."""
        if type(listing_id) is not int or listing_id <= 0:
            raise HostawayError("Invalid pricing listing")
        body = await self._request("GET", f"/listings/{listing_id}")
        raw = body.get("result")
        if not isinstance(raw, dict) or str(raw.get("id")) != str(listing_id):
            raise HostawayError("Pricing listing identity mismatch")
        return {k: raw.get(k) for k in ('id', 'currencyCode', 'timeZoneName', 'checkInTimeStart')}

    async def pricing_calendar(self, listing_id: int, start: str, end: str) -> list[dict[str, Any]]:
        """GET nightly calendar prices, never base prices."""
        from datetime import date, timedelta
        first, last = date.fromisoformat(start), date.fromisoformat(end)
        if type(listing_id) is not int or listing_id <= 0 or not 0 <= (last-first).days <= 4:
            raise HostawayError("Pricing calendar read range invalid")
        body = await self._request("GET", f"/listings/{listing_id}/calendar",
                                   params={"startDate": start, "endDate": end, "includeResources": 1})
        raw = body.get("result")
        if not isinstance(raw, list) or len(raw) > 5:
            raise HostawayError("Unexpected pricing calendar response")
        allowed = ('id', 'date', 'price', 'status', 'isAvailable', 'isProcessed', 'minimumStay', 'maximumStay',
                   'closedOnArrival', 'closedOnDeparture', 'countPendingUnits', 'countReservedUnits',
                   'countBlockingReservations', 'countBlockedUnits', 'countAvailableUnits',
                   'availableUnitsToSell', 'desiredUnitsToSell', 'updatedAt', 'updatedOn')
        days = []
        for day in raw:
            if not isinstance(day, dict) or not first <= date.fromisoformat(day.get('date', '')) <= last:
                raise HostawayError("Pricing calendar date mismatch")
            item = {k: day.get(k) for k in allowed}
            item['reservations'] = bool(day.get('reservations'))
            days.append(item)
        if {day['date'] for day in days} != {(first + timedelta(days=i)).isoformat() for i in range((last-first).days+1)}:
            raise HostawayError("Pricing calendar dates incomplete")
        return days

    async def apply_calendar_intervals(self, listing_id: int, intervals: list[dict]) -> None:
        await self._request("PUT", f"/listings/{listing_id}/calendarIntervals",
                            json=intervals, retry_transient=False)

    async def booking_report_reservations(self, departing_from: str) -> list[dict[str, Any]]:
        # A booking created in the window may have a future arrival/departure.
        return await self._paged("/reservations", {"departureStartDate": departing_from})

    async def reservations(self, departing_from: str) -> list[dict[str, Any]]:
        """All reservations that depart on or after the given date (YYYY-MM-DD)."""
        return await self._paged("/reservations", {"departureStartDate": departing_from, "includeResources": 1})

    async def reservation(self, reservation_id: int) -> dict[str, Any]:
        body = await self._request("GET", f"/reservations/{reservation_id}", params={"includeResources": 1})
        return body["result"]

    async def append_ntc_note(self, reservation_id: int) -> dict[str, Any]:
        """Read-modify-write Guest Notes only; retries must re-read instead of replaying stale text."""
        async with self._note_lock:
            raw = await self.reservation(reservation_id)
            self._validate_note_reservation(raw, reservation_id)
            if has_ntc(raw["guestNote"]):
                return raw
            note = raw["guestNote"] or ""
            updated = note + ("" if not note or note.endswith("\n") else "\n") + "NTC"
            await self._request("PUT", f"/reservations/{reservation_id}", retry_transient=False,
                                json={"guestNote": updated})
            fresh = await self.reservation(reservation_id)
            self._validate_note_reservation(fresh, reservation_id)
            if not has_ntc(fresh["guestNote"]):
                raise HostawayError("NTC Guest Notes update was not confirmed")
            return fresh

    def _validate_note_reservation(self, raw: dict[str, Any], reservation_id: int) -> None:
        if str(raw.get("id")) != str(reservation_id):
            raise HostawayError("Reservation ID mismatch while updating Guest Notes")
        if raw.get("accountId") is not None and str(raw["accountId"]) != str(self._account_id):
            raise HostawayError("Reservation account mismatch while updating Guest Notes")
        if "guestNote" not in raw or raw["guestNote"] is not None and not isinstance(raw["guestNote"], str):
            raise HostawayError("Guest Notes were not returned as text; left unchanged")

    async def guest_messages(self, reservation_id: int) -> list[dict[str, Any]]:
        """Read only this reservation's sent messages. Bound work and fail closed if truncated."""
        found = await self._request("GET", "/conversations", params={"reservationId": reservation_id, "limit": 100})
        conversations = found.get("result") or []
        if len(conversations) >= 100:
            raise HostawayError("Too many conversations to safely scan")
        messages = []
        for c in conversations:
            if str(c.get("reservationId")) != str(reservation_id):
                continue
            if str(c.get("accountId")) != str(self._account_id):
                raise HostawayError("Conversation account mismatch")
            for offset in range(0, 1000, 100):
                await asyncio.sleep(0.25)  # pace read-only requests across occupied homes
                body = await self._request("GET", f"/conversations/{int(c['id'])}/messages",
                                           params={"limit": 100, "offset": offset, "includeScheduledMessages": 0})
                page = body.get("result") or []
                messages.extend(page)
                if len(page) < 100:
                    break
            else:
                raise HostawayError("Message scan reached its safety limit")
        return messages

    async def send_guest_message(self, reservation_id: int, body: str) -> None:
        """Post a message into the guest's conversation (goes out on the booking channel)."""
        found = await self._request("GET", "/conversations", params={"reservationId": reservation_id})
        conversations = [c for c in found.get("result") or []
                         if str(c.get("reservationId")) == str(reservation_id)]
        if not conversations:
            raise HostawayError(f"No conversation for reservation {reservation_id}")
        await self._request(
            "POST", f"/conversations/{conversations[0]['id']}/messages",
            json={"body": body, "communicationType": "channel"},
        )

    async def unified_webhooks(self) -> list[dict[str, Any]]:
        body = await self._request("GET", "/webhooks/unifiedWebhooks")
        return body.get("result") or []

    async def register_webhook(self, url: str) -> dict[str, Any]:
        """Point Hostaway's unified webhook at `url`. Already registered (same URL) is success, not an error."""
        existing = await self.unified_webhooks()
        for hook in existing:
            if hook.get("url") == url:
                if not hook.get("isEnabled"):
                    body = await self._request("PUT", f"/webhooks/unifiedWebhooks/{hook['id']}",
                                               json={"isEnabled": 1, "url": url})
                    return body["result"]
                return hook
        try:
            body = await self._request("POST", "/webhooks/unifiedWebhooks", json={"isEnabled": 1, "url": url})
        except HostawayError as exc:
            if existing:
                hosts = ", ".join(sorted({(h.get("url") or "").split("/")[2] if "//" in (h.get("url") or "") else "?"
                                          for h in existing}))
                raise HostawayError(f"{exc}. This Hostaway account already has {len(existing)} unified webhook(s) "
                                    f"(pointing to {hosts}). Remove or edit it in Hostaway under Settings > "
                                    "Hostaway API > Webhooks, then try again.") from exc
            raise
        return body["result"]
