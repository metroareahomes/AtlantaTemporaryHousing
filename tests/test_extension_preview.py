from types import SimpleNamespace

from app import extension_preview
from test_sync import run, syncer


def test_scan_limits_to_current_and_upcoming_kurt_reservations(syncer, monkeypatch):
    for lid, owner in [(101, "Kurt"), (102, "Mike"), (103, "Kurt")]:
        syncer.db.execute("INSERT INTO properties(hostaway_listing_id,name,owner_scope,active) VALUES(?,?,?,1)",
                          (lid, owner, owner))
        checkout = "2000-01-01" if lid == 103 else "2099-01-03"
        syncer.db.execute(
            "INSERT INTO reservations(id,listing_id,status,active,arrival_date,departure_date,"
            "check_in_at,check_out_at,updated_at) VALUES(?,?,'new',1,?,?,?,?,?)",
            (lid, lid, "2000-01-01", checkout, "2000-01-01", checkout, checkout),
        )
    async def review(syncer, client, row):
        row["note"] = "Possible extension"

    monkeypatch.setattr(extension_preview, "review_reservation", review)
    monkeypatch.delenv("ALL_PROXY", raising=False)
    monkeypatch.delenv("all_proxy", raising=False)
    monkeypatch.setattr(syncer, "settings", SimpleNamespace(extension_ai_api_key="test"))
    state = {}
    run(extension_preview.scan_kurt(syncer, state))
    assert state["completed"] == state["total"] == 1
    assert state["rows"][0]["id"] == 101
    assert state["rows"][0]["note"] == "Possible extension"


def test_turnover_nights_by_stay_length():
    from datetime import date, timedelta

    friday = date(2026, 11, 27)
    for length, offsets in [(1, []), (3, []), (4, [0]), (14, [0]), (15, [1, 2, 3])]:
        assert extension_preview.turnover_nights(friday - timedelta(days=length), friday) == [
            friday + timedelta(days=offset) for offset in offsets
        ]
    saturday = friday + timedelta(days=1)
    assert extension_preview.turnover_nights(saturday - timedelta(days=15), saturday)[-1].weekday() == 0


def test_inquiry_dry_run_respects_minimum_stay_and_roleplayed_checkout():
    from datetime import date

    row = {"arrival_date": "2025-12-28", "departure_date": "2026-11-27",
           "hold_end": date(2026, 12, 26), "projected_turnover": [],
           "inquiry_message": "Do you need any nights beyond Christmas?"}
    simulate = extension_preview.simulate_inquiry
    overlap = simulate(row | {"firm_hold_end": row["hold_end"]}, date(2026, 12, 23), date(2026, 12, 28))
    assert "unavailable" in overlap["inquirer"]
    assert overlap["current_guest"] is None

    later = simulate(row, date(2026, 12, 26), date(2027, 1, 2))
    assert later["current_guest"] == row["inquiry_message"]

    reply = simulate(row, date(2026, 12, 26), date(2027, 1, 2),
                     guest_reply="new_checkout", guest_checkout=date(2026, 12, 26))
    assert "turnover" in reply["reason"]
    assert reply["current_guest"] is None
    assert "Dec 28, 2026" in reply["calendar"]


def test_ambiguous_roleplay_preserves_hold_and_asks_for_checkout():
    from datetime import date

    row = {"arrival_date": "2025-12-28", "departure_date": "2026-11-27",
           "hold_end": date(2026, 12, 26), "projected_turnover": []}
    reply = extension_preview.simulate_inquiry(
        row, date(2026, 12, 26), date(2027, 1, 2), guest_reply="clarify",
    )
    assert "Keep the proposed holds" in reply["calendar"]
    assert "What date" in reply["current_guest"]


def test_inquiry_waits_then_reminds_once_and_closes_at_deadline():
    from datetime import date, timedelta

    row = {"arrival_date": "2025-12-28", "departure_date": "2026-11-27",
           "hold_end": date(2026, 12, 26), "projected_turnover": []}
    update = extension_preview.local_scenario_time("2026-11-23T17:00")
    def simulate(hours, **kwargs):
        return extension_preview.simulate_inquiry(
            row, date(2026, 12, 26), date(2027, 1, 2),
            guest_reply="wait_until", update_after=update,
            as_of=update + timedelta(hours=hours), inquiry_deadline=update + timedelta(hours=2),
            **kwargs,
        )

    assert simulate(0)["current_guest"] is None
    assert simulate(1)["current_guest"]
    assert simulate(1, reminder_sent=True, acknowledged=True)["current_guest"] is None
    assert simulate(1, acknowledged=True)["inquirer"] is None
    assert "couldn’t confirm" in simulate(2)["inquirer"]
    assert simulate(2)["current_guest"] is None


def test_next_wednesday_is_one_week_after_the_nearest_wednesday():
    from datetime import date

    dates = extension_preview.relative_weekdays(date(2026, 11, 23))
    assert dates["this Wednesday"] == "2026-11-25"
    assert dates["next Wednesday"] == "2026-12-02"
    assert extension_preview.relative_weekdays(date(2026, 11, 22))["next Wednesday"] == "2026-12-02"



def test_unclear_reply_keeps_tentative_overlap_pending():
    from datetime import date

    row = {"arrival_date": "2026-11-01", "departure_date": "2026-11-27",
           "hold_end": date(2026, 12, 26)}
    result = extension_preview.simulate_inquiry(
        row, date(2026, 12, 23), date(2026, 12, 28), guest_reply="clarify",
    )
    assert "waiting for our current guest" in result["inquirer"]
    assert "What date" in result["current_guest"]
    assert "Keep the proposed holds" in result["calendar"]


def test_short_stay_inquiry_only_prompts_with_possible_overlap():
    from datetime import date

    row = {"arrival_date": "2026-11-27", "departure_date": "2026-11-29",
           "short_stay": True, "hold_end": date(2026, 11, 30),
           "inquiry_message": "Do you still need Sunday night?"}
    simulate = extension_preview.simulate_inquiry
    assert simulate(row, date(2026, 11, 29), date(2026, 11, 30))["current_guest"]
    assert simulate(row, date(2026, 11, 30), date(2026, 12, 2))["current_guest"] is None
