"""Render the navigation to check grouping, ingress-safe links and current-page state."""
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from app.web import SETUP_SECTIONS, create_app
from test_sync import syncer


class Navigation(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_nav = False
        self.in_system = False
        self.links = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "nav":
            self.in_nav = True
        elif self.in_nav and tag == "details":
            self.in_system = True
        elif self.in_nav and tag == "a":
            self.current = {"href": attrs["href"], "nested": self.in_system,
                            "current": attrs.get("aria-current"), "text": ""}

    def handle_data(self, data):
        if self.current is not None:
            self.current["text"] += data

    def handle_endtag(self, tag):
        if tag == "a" and self.current is not None:
            self.links.append(self.current)
            self.current = None
        elif tag == "details":
            self.in_system = False
        elif tag == "nav":
            self.in_nav = False


@pytest.mark.parametrize("active,href", [
    ("/", "."), ("/properties", "properties"), ("/staff", "staff"),
    ("/setup?section=locks", "setup?section=locks"), ("/events", "events"),
])
def test_navigation_separates_daily_work_from_setup_and_pricing(syncer, active, href):
    syncer.db.set_setting("addon_slug", "123456ab_stay_automation")
    html = TestClient(create_app(syncer)).get(active).text
    nav = Navigation()
    nav.feed(html)
    assert [(link["href"], link["text"]) for link in nav.links if not link["nested"]][:3] == [
        (".", "Status"), ("properties", "Properties"), ("staff", "Staff & access")]
    assert [(link["href"], link["text"]) for link in nav.links if link["nested"]] == [
        *((f"setup?section={key}", title) for key, title in SETUP_SECTIONS),
        ("events", "Activity log"),
        ("monday-special", "The Monday Special"),
        ("thursday-special", "The Thursday Special"),
        ("extension-pricing", "Guest Extension Pricing"),
        ("extension-preview", "Who’s extending? - Kurt"),
        ("extension-preview?owner=Mike", "Who’s extending? - Mike"),
        ("turnover-rules?owner=Kurt", "Turnover rules - Kurt"),
        ("turnover-rules?owner=Mike", "Turnover rules - Mike"),
        ("https://atlanta-temporary-housing-extension-pilot.onrender.com/extend/kurt", "Extending direct"),
        ("https://atlanta-temporary-housing-map.beamingcacao18.chatgpt.site/", "Web direct"),
        ("staff-signin/owner", "Owner settings"),
        ("staff-signin/pilot", "Pilot settings"),
        ("https://hostaway-complaint-analyzer.onrender.com/", "Hostaway Complaint Analyzer"),
    ]
    assert [link["href"] for link in nav.links if link["nested"] and link["current"] == "page"] == (
        [href] if active in ("/setup?section=locks", "/events") else []
    )
    if active == "/setup?section=locks":
        assert 'action="settings-save"' in html
        assert 'action="thermostat-settings-save"' not in html
        assert 'action="smtp-settings-save"' not in html
    assert '<summary>Setup</summary>' in html
    assert '<summary>Pricing</summary>' in html
    assert 'href="extension-pricing"' in html
