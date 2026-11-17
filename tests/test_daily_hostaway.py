import asyncio

import httpx
import pytest

from app.hostaway import HostawayClient, HostawayError


def client(handler):
    c = HostawayClient('1', 'unused', load_token=lambda: 'cached',
                       http=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url='https://mock'))
    return c


def success(value):
    return httpx.Response(200, json={'status': 'success', 'result': value})


def test_resources_requested_on_list_and_single_reservation():
    calls = []
    def handler(req):
        calls.append(req)
        return success([] if req.url.path == '/reservations' else {'id': 1})
    c = client(handler)
    asyncio.run(c.reservations('2030-06-01'))
    asyncio.run(c.reservation(1))
    assert all(req.url.params['includeResources'] == '1' for req in calls)
    assert all(req.method == 'GET' for req in calls)


def test_message_scan_paginates_only_exact_reservation_and_read_only(monkeypatch):
    calls = []
    async def no_wait(_):
        pass
    monkeypatch.setattr('app.hostaway.asyncio.sleep', no_wait)
    def handler(req):
        calls.append(req)
        if req.url.path == '/conversations':
            assert req.url.params['reservationId'] == '1'
            return success([{'id': 9, 'reservationId': 1, 'accountId': 1},
                            {'id': 10, 'reservationId': 2, 'accountId': 1}])
        assert req.url.path == '/conversations/9/messages'
        assert req.url.params['includeScheduledMessages'] == '0'
        return success([{'id': i} for i in range(100)] if req.url.params['offset'] == '0' else [{'id': 101}])
    assert len(asyncio.run(client(handler).guest_messages(1))) == 101
    assert len(calls) == 3 and all(req.method == 'GET' for req in calls)


def test_message_scan_rejects_wrong_account():
    c = client(lambda req: success([{'id': 9, 'reservationId': 1, 'accountId': 2}]))
    with pytest.raises(HostawayError, match='account mismatch'):
        asyncio.run(c.guest_messages(1))


def test_message_scan_fails_closed_instead_of_silently_truncating(monkeypatch):
    async def no_wait(_):
        pass
    monkeypatch.setattr('app.hostaway.asyncio.sleep', no_wait)
    c = client(lambda req: success([{'id': 9, 'reservationId': 1, 'accountId': 1}]
                                  if req.url.path == '/conversations' else [{'id': i} for i in range(100)]))
    with pytest.raises(HostawayError, match='safety limit'):
        asyncio.run(c.guest_messages(1))
