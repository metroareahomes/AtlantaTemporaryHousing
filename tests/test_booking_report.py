import asyncio
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.web import create_app
from test_sync import syncer


def test_report_uses_original_booking_time_in_eastern_and_excludes_cancellations(syncer):
    syncer.now = lambda: datetime(2026, 11, 19, 18, tzinfo=timezone.utc)
    calls = []
    release = asyncio.Event()
    started = asyncio.Event()

    async def reservations(start):
        calls.append(start)
        started.set()
        await release.wait()
        records = [
            {'id': 1, 'status': 'new', 'reservationDate': '2026-11-13 00:30:00',
             'arrivalDate': '2026-11-12', 'guestName': 'Private name'},
            {'id': 2, 'status': 'modified', 'reservationDate': '2026-07-02 17:30:00',
             'arrivalDate': '2026-07-02'},
            {'id': 3, 'status': 'new', 'reservationDate': '2026-11-12 19:15:00',
             'arrivalDate': '2027-01-01'},
            {'id': 4, 'status': 'cancelled', 'reservationDate': '2026-11-12 19:15:00',
             'arrivalDate': '2026-11-12'},
        ]
        return records + [records[0]]

    syncer.hostaway.booking_report_reservations = reservations
    with TestClient(create_app(syncer)) as client:
        assert client.get('/thursday-bookings').status_code == 200
        assert calls == []
        response = client.post('/thursday-bookings', follow_redirects=False)
        assert response.status_code == 303
        assert response.headers['location'] == 'thursday-bookings'
        client.portal.call(asyncio.wait_for, started.wait(), 2)
        assert 'Running report…' in client.get('/thursday-bookings').text
        assert client.post('/thursday-bookings', follow_redirects=False).status_code == 303
        assert calls == ['2025-11-19']

        async def finish():
            release.set()
            while '"report"' not in (syncer.db.get_setting('thursday_booking_report') or ''):
                await asyncio.sleep(0.01)

        client.portal.call(asyncio.wait_for, finish(), 2)
        assert 'Bookings before and after 2 PM' in client.get('/thursday-bookings').text
    # The completed aggregate remains accessible after reopening the app.
    response = TestClient(create_app(syncer)).get('/thursday-bookings')
    assert '<td>13:00</td><td>1</td><td>50.0%</td><td>1</td>' in response.text
    assert '<td>19:00</td><td>1</td><td>100.0%</td><td>1</td>' in response.text
    assert '<td>14:00</td><td>0</td><td>50.0%</td><td>1</td>' in response.text
    assert '1 were before 2 PM and 1 were at or after 2 PM' in response.text
    assert 'Private name' not in response.text
