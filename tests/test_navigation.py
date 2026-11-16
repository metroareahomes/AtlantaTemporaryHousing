"""Render the navigation to check grouping, ingress-safe links and current-page state."""
from html.parser import HTMLParser
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader


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
    ("status", "."), ("properties", "properties"), ("staff", "staff"),
    ("setup", "setup"), ("events", "events"),
])
def test_navigation_separates_daily_work_from_system_pages(active, href):
    templates = Path(__file__).resolve().parents[1] / "stay_automation" / "app" / "templates"
    env = Environment(loader=FileSystemLoader(templates), autoescape=True)
    html = env.get_template("base.html").render(active=active, notice=None)
    nav = Navigation()
    nav.feed(html)
    assert [(link["href"], link["text"]) for link in nav.links if not link["nested"]] == [
        (".", "Status"), ("properties", "Properties"), ("staff", "Staff & access")]
    assert [(link["href"], link["text"]) for link in nav.links if link["nested"]] == [
        ("setup", "Setup & rules"), ("events", "Activity log")]
    assert [link["href"] for link in nav.links if link["current"] == "page"] == [href]
    # Relative links preserve Home Assistant ingress prefixes.
    assert all(not link["href"].startswith("/") for link in nav.links)
    assert '<summary>System</summary>' in html
