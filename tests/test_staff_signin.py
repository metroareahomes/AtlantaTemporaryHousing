from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.staff_signin import staff_username
from app.web import create_app
from test_sync import syncer


def test_staff_signin_rejects_untrusted_peer_and_unlisted_accounts(syncer):
    headers = {"x-remote-user-name": "Mike@AtlantaHomeInvesting.com", "x-remote-user-id": "test-id"}
    assert TestClient(create_app(syncer)).get("/staff-signin/owner", headers=headers).status_code == 403
    ingress = SimpleNamespace(client=SimpleNamespace(host="172.30.32.2"), headers=headers)
    assert staff_username(ingress) == "mike@atlantahomeinvesting.com"
    ingress.headers["x-remote-user-name"] = "other@example.com"
    assert staff_username(ingress) is None
    assert syncer.db.get_setting("staff_signin_private_key") is None


def test_staff_signin_starts_in_ingress_and_posts_to_render_at_top_level(syncer, monkeypatch):
    from app import staff_signin

    monkeypatch.setattr(staff_signin, "staff_username", lambda request: "metroareahomes@gmail.com")
    client = TestClient(create_app(syncer))
    menu = client.get("/").text
    assert 'href="staff-signin/pilot"' not in menu
    assert f'href="staff-signin/owner">' in menu
    response = client.get(f"/staff-signin/owner")
    assert response.status_code == 200
    assert f'action="{staff_signin.RENDER_ORIGIN}/staff-signin" method="post" target="_top"' in response.text
    assert response.headers["referrer-policy"] == "no-referrer"
