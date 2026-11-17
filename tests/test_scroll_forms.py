"""Persistent Save buttons must submit only the intended existing form."""
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from app.web import create_app
from test_sync import syncer


class Forms(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms = []
        self.ids = []
        self.scripts = []
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if tag == "form" and "data-save-label" in attrs:
            self.forms.append((attrs.get("id"), attrs.get("action"), attrs["data-save-label"], attrs.get("method")))
        if tag == "script" and "src" in attrs:
            self.scripts.append(attrs["src"])


@pytest.mark.parametrize("path,expected", [
    ("/properties", [("save-properties", "properties-save", "Save properties", "post")]),
    ("/staff", [("save-staff", "staff-save", "Save staff", "post"),
                ("save-recipients", "recipients-save", "Save recipients", "post")]),
    ("/setup", [("save-lock-settings", "settings-save", "Save lock settings", "post"),
                ("save-thermostat-settings", "thermostat-settings-save", "Save thermostat settings", "post")]),
    ("/events", []),
])
def test_save_actions_keep_form_ownership(syncer, path, expected):
    response = TestClient(create_app(syncer)).get(path)
    assert response.status_code == 200
    forms = Forms()
    forms.feed(response.text)
    assert forms.forms == expected
    assert len(forms.ids) == len(set(forms.ids))
    assert any(src.startswith("static/scrolling.js?v=") for src in forms.scripts)
