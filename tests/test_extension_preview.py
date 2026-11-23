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
