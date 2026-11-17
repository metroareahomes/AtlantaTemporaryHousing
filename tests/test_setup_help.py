"""Setup help describes each field without changing the stored settings or form contract."""
from html.parser import HTMLParser

from fastapi.testclient import TestClient

from app.setup_help import SETUP_FIELDS
from app.web import create_app
from app.locks import DEFAULTS as LOCK_DEFAULTS
from app.climate import DEFAULTS as CLIMATE_DEFAULTS
from test_sync import syncer


class SetupControls(HTMLParser):
    def __init__(self):
        super().__init__()
        self.controls = {}
        self.ids = []
        self.labels = []
        self.help_labels = []
        self.forms = []
        self.open_help = []
        self.in_help = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if tag in ("input", "select", "textarea"):
            self.controls[attrs["name"]] = attrs
        if tag == "label" and "for" in attrs:
            self.labels.append(attrs["for"])
        if tag == "form":
            self.forms.append((attrs.get("method"), attrs.get("action")))
        if tag == "details":
            self.in_help = attrs.get("class") == "field-help"
            if self.in_help and "open" in attrs:
                self.open_help.append(attrs)
        if tag == "summary" and self.in_help:
            self.help_labels.append(attrs.get("aria-label"))
    def handle_endtag(self, tag):
        if tag == "details":
            self.in_help = False


def page(syncer):
    response = TestClient(create_app(syncer)).get("/setup")
    assert response.status_code == 200
    parsed = SetupControls()
    parsed.feed(response.text)
    return response.text, parsed


def test_every_setup_control_has_named_help_and_a_visible_hint(syncer):
    html, parsed = page(syncer)
    expected = set(LOCK_DEFAULTS) | set(CLIMATE_DEFAULTS) | {
        "public_url", "addon_slug", "register_hostaway", "alert_service",
        "backup_codes_enabled", "guest_messages_enabled", "guest_message", "lyric_climate_entities"}
    assert set(SETUP_FIELDS) == expected == set(parsed.controls)
    assert len(parsed.ids) == len(set(parsed.ids))
    assert not parsed.open_help  # help stays out of the way until tapped
    for key, info in SETUP_FIELDS.items():
        control = parsed.controls[key]
        assert control["id"] == "setting-" + key
        assert control["id"] in parsed.labels
        assert control["aria-describedby"] == "hint-" + key
        assert control["aria-describedby"] in parsed.ids
        assert "Help: " + info["label"] in parsed.help_labels
        assert info["brief"] in html
        assert info["more"] and info["example"]


def test_help_preserves_saved_values_and_does_not_write_settings(syncer):
    db = syncer.db
    db.set_setting("add_hour", "14")
    db.set_setting("occ_summer", "74")
    db.set_setting("alert_service", "notify.mobile_app_kurts_iphone")
    db.set_setting("backup_codes_enabled", "1")
    db.set_setting("guest_messages_enabled", "0")
    before = db.query("SELECT * FROM settings ORDER BY key")
    html, parsed = page(syncer)
    assert parsed.controls["add_hour"]["value"] == "14"
    assert "Current choice: 2 PM." in html
    assert parsed.controls["occ_summer"]["value"] == "74"
    assert parsed.controls["alert_service"]["value"] == "notify.mobile_app_kurts_iphone"
    assert "checked" in parsed.controls["backup_codes_enabled"]
    assert "checked" not in parsed.controls["guest_messages_enabled"]
    assert db.query("SELECT * FROM settings ORDER BY key") == before


def test_help_keeps_existing_form_routes_and_does_not_add_submit_buttons(syncer):
    html, parsed = page(syncer)
    assert parsed.forms == [
        ("post", "sync"), ("post", "setup-webhook"), ("post", "settings-save"),
        ("post", "thermostat-settings-save"), ("post", "additional-thermostats-save"), ("post", "test-alert")]
    assert html.count("<button") == 6
    assert html.count('class="field-help"') == len(SETUP_FIELDS)
    assert "Save your alert destination above" in html


def test_saved_message_is_escaped_and_template_tokens_remain_visible(syncer):
    syncer.db.set_setting("guest_message", "<script>alert(1)</script> {guest} {property} {code}")
    html, _ = page(syncer)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "{guest}" in html and "{property}" in html and "{code}" in html
