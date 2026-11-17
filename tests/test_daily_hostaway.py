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


def test_ntc_is_a_standalone_case_insensitive_marker():
    from app.hostaway import has_ntc
    for note in ('NTC', 'ntc', 'Notes: NTC.', 'Early arrival\n(NTC)'):
        assert has_ntc(note)
    for note in ('ANTC', 'NTC123', None, 'No changes', 'NTC_extra'):
        assert not has_ntc(note)


@pytest.mark.parametrize('original,expected', [(None, 'NTC'), ('', 'NTC'), ('Bring crib', 'Bring crib\nNTC'),
                                              ('Existing notes\n', 'Existing notes\nNTC'),
                                              ('Arrivée tardive', 'Arrivée tardive\nNTC')])
def test_append_ntc_preserves_notes_and_updates_only_guest_note(original, expected):
    import json
    remote = {'id': 1, 'accountId': 1, 'guestNote': original}
    puts = []
    def handler(req):
        if req.method == 'PUT':
            body = json.loads(req.content)
            puts.append(body)
            assert set(body) == {'guestNote'}
            remote.update(body)
        return success(dict(remote))
    assert asyncio.run(client(handler).append_ntc_note(1))['guestNote'] == expected
    assert puts == [{'guestNote': expected}]


def test_already_marked_guest_notes_are_not_written_again():
    def handler(req):
        assert req.method == 'GET'
        return success({'id': 1, 'accountId': 1, 'guestNote': 'Existing notes\nntc'})
    assert asyncio.run(client(handler).append_ntc_note(1))['guestNote'].endswith('ntc')


def test_timeout_after_applied_note_does_not_duplicate_marker_or_erase_staff_edits():
    import json
    remote = {'id': 1, 'accountId': 1, 'guestNote': 'Original note'}
    puts = []
    def handler(req):
        if req.method == 'PUT':
            puts.append(json.loads(req.content))
            remote.update(puts[-1])
            raise httpx.ReadTimeout('reply lost', request=req)
        return success(dict(remote))
    c = client(handler)
    with pytest.raises(httpx.ReadTimeout):
        asyncio.run(c.append_ntc_note(1))
    remote['guestNote'] += '\nConcierge added this later'
    result = asyncio.run(c.append_ntc_note(1))
    assert result['guestNote'] == 'Original note\nNTC\nConcierge added this later'
    assert len(puts) == 1


@pytest.mark.parametrize('status', [401, 429, 500])
def test_note_put_is_not_blindly_retried_with_stale_text(status):
    puts = []
    def handler(req):
        if req.method == 'PUT':
            puts.append(req)
            return httpx.Response(status, json={'status': 'fail'})
        return success({'id': 1, 'accountId': 1, 'guestNote': 'Existing'})
    with pytest.raises(HostawayError):
        asyncio.run(client(handler).append_ntc_note(1))
    assert len(puts) == 1


def test_unreadable_notes_are_never_replaced_with_empty_text():
    def handler(req):
        assert req.method == 'GET'
        return success({'id': 1, 'accountId': 1})
    with pytest.raises(HostawayError, match='left unchanged'):
        asyncio.run(client(handler).append_ntc_note(1))
