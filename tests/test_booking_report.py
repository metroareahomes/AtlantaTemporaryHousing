from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.web import create_app
from test_sync import syncer


def test_report_uses_original_booking_time_in_eastern_and_excludes_cancellations(syncer):
    syncer.now = lambda: datetime(2026, 11, 19, 18, tzinfo=timezone.utc)
    calls = []

    async def reservations(start):
        calls.append(start)
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
    client = TestClient(create_app(syncer))
    assert client.get('/thursday-bookings').status_code == 200
    assert calls == []
    response = client.post('/thursday-bookings')
    assert calls == ['2025-11-19']
    assert '<td>13:00</td><td>1</td><td>50.0%</td><td>1</td>' in response.text
    assert '<td>19:00</td><td>1</td><td>100.0%</td><td>1</td>' in response.text
    assert '<td>14:00</td><td>0</td><td>50.0%</td><td>1</td>' in response.text
    assert '1 were before 2 PM and 1 were at or after 2 PM' in response.text
    assert 'Private name' not in response.text
