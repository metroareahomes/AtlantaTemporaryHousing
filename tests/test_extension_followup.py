from datetime import date, datetime, timedelta, timezone

import pytest

from app.extension_followup import qualifies


@pytest.mark.parametrize(("patch", "expected"), [
    ({}, True),
    ({"status": "declined"}, False),
    ({"status": "confirmed"}, False),
    ({"status": "unclear"}, False),
    ({"host_followed_up": True}, False),
    ({"update_promised": True}, False),
    ({"guest_message_id": 2}, False),
    ({"last_update_message_id": 2}, False),
])
def test_preview_requires_clear_stale_interest(patch, expected):
    now = datetime(2026, 11, 22, 15, tzinfo=timezone.utc)
    thread = {
        1: (now - timedelta(days=3), True, "We might need two extra nights."),
        2: (now - timedelta(hours=5), False, "I can check."),
    }
    result = dict(status="considering", host_followed_up=False, update_promised=False,
                  guest_message_id=1, last_update_message_id=1)
    assert qualifies(result | patch, thread, date(2026, 11, 30), now) is expected
