"""Exercise the real Setup page and its third persistent Save action on a phone."""
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(root / 'stay_automation'), str(root / 'tests')]
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright
from app.db import DB
from app.sync import Syncer
from app.web import create_app
from test_sync import SETTINGS, FakeHA, FakeHostaway

syncer = Syncer(DB(':memory:'), SETTINGS, FakeHostaway(), FakeHA())
html = TestClient(create_app(syncer)).get('/setup').text
static = root / 'stay_automation' / 'app' / 'static'

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    for width, height in [(1280, 800), (390, 844)]:
        page = browser.new_page(viewport={'width': width, 'height': height})
        def route(req):
            if '/static/' in req.request.url:
                name = req.request.url.split('/static/')[1].split('?')[0]
                req.fulfill(path=str(static / name))
            else:
                req.fulfill(body=html, content_type='text/html')
        page.route('https://stay.test/**', route)
        page.goto('https://stay.test/setup')
        page.wait_for_selector('.save-bar')
        page.evaluate("window.submissions=[]; document.addEventListener('submit', e=>{e.preventDefault(); window.submissions.push({id:e.target.id, values:Object.fromEntries(new FormData(e.target))});})")
        assert page.locator('[name=daily_winter]').input_value() == '70'
        assert page.locator('[name=daily_summer]').input_value() == '73'
        assert page.locator('[name=occupied_reset_hour]').input_value() == '10'
        assert page.locator('[name=occupied_reset_spacing_seconds]').input_value() == '90'
        assert not page.locator('[name=occupied_reset_enabled]').is_checked()
        page.locator('[name=daily_summer]').fill('74')
        page.locator('[aria-label="Help: Summer: lowest reasonable cooling setting (°F)"]').click()
        assert page.locator('.field-help[open]').is_visible()
        assert abs(page.locator('body > header').bounding_box()['y']) < 1
        assert page.get_by_role('navigation', name='Main navigation').is_visible()
        bar = page.locator('.save-bar')
        bar.get_by_role('button', name='Save daily temperature settings', exact=True).click()
        assert page.evaluate('window.submissions[0].id') == 'save-daily-temperature-settings'
        values = page.evaluate('window.submissions[0].values')
        assert values['daily_winter'] == '70' and values['daily_summer'] == '74'
        assert 'occ_summer' not in values and 'guest_message' not in values
        assert abs(bar.bounding_box()['y'] + bar.bounding_box()['height'] - height) < 1
        assert page.evaluate('document.documentElement.scrollWidth') <= width
        page.close()
    browser.close()
print('PASS: real Setup on desktop/phone, 70/73 limits, 10 AM start, 90-second spacing, opt-in, help and independent fixed Save action')
