"""Hostaway public API client."""
import asyncio
import logging
from typing import Any, Callable

import httpx

BASE_URL = "https://api.hostaway.com/v1"
PAGE_SIZE = 100

log = logging.getLogger(__name__)


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

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
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
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                await asyncio.sleep(2 ** attempt)
                continue
            if resp.status_code != 200:
                raise HostawayError(f"{method} {path}: HTTP {resp.status_code}")
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
        return await self._paged("/listings", {})

    async def reservations(self, departing_from: str) -> list[dict[str, Any]]:
        """All reservations that depart on or after the given date (YYYY-MM-DD)."""
        return await self._paged("/reservations", {"departureStartDate": departing_from})

    async def reservation(self, reservation_id: int) -> dict[str, Any]:
        body = await self._request("GET", f"/reservations/{reservation_id}")
        return body["result"]

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
        body = await self._request(
            "POST", "/webhooks/unifiedWebhooks", json={"isEnabled": 1, "url": url}
        )
        return body["result"]
