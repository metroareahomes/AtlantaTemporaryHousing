"""Hostaway public API client."""
import asyncio
from typing import Any, Callable

import httpx

BASE_URL = "https://api.hostaway.com/v1"



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

    async def listing_details(self, listing_id: int) -> dict[str, Any]:
        """Read one verified listing with its Hostaway resources."""
        if type(listing_id) is not int or listing_id <= 0:
            raise HostawayError("Invalid pricing listing")
        body = await self._request("GET", f"/listings/{listing_id}", params={"includeResources": 1})
        raw = body.get("result")
        if not isinstance(raw, dict) or str(raw.get("id")) != str(listing_id):
            raise HostawayError("Pricing listing identity mismatch")
        return raw

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

    async def reservation(self, reservation_id: int) -> dict[str, Any]:
        body = await self._request("GET", f"/reservations/{reservation_id}", params={"includeResources": 1})
        return body["result"]

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
        body = await self._request("POST", "/webhooks/unifiedWebhooks",
                                   json={"isEnabled": 1, "url": url}, retry_transient=False)
        return body["result"]
