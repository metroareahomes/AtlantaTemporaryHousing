"""Real recipient and SMTP forms, plus stable ingress Properties links. No emails sent."""
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

s = Syncer(DB(':memory:'), SETTINGS, FakeHostaway(), FakeHA())
s.db.set_setting('smtp_password','never-show-this-secret')
c = TestClient(create_app(s))
pages = {p:c.get(p).text for p in ('/staff','/setup','/','/properties')}
static = root / 'stay_automation' / 'app' / 'static'
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    for width,height in [(1280,800),(390,844)]:
        page = browser.new_page(viewport={'width':width,'height':height})
        def route(r):
            path = r.request.url.split('https://ha.example.com')[1].split('?')[0]
            if path.startswith('/static/'):
                r.fulfill(path=str(static / path.rsplit('/',1)[-1]))
            else:
                r.fulfill(body=pages.get(path,pages['/']),content_type='text/html')
        page.route('https://ha.example.com/**',route)
        page.goto('https://ha.example.com/staff')
        page.evaluate("window.sent=[];document.addEventListener('submit',e=>{e.preventDefault();window.sent.push({id:e.target.id,values:Object.fromEntries(new FormData(e.target))})})")
        page.locator('[name=rname_new]').fill('Kurt battery manager')
        page.locator('[name=remail_new]').fill('manager@example.com')
        page.locator('[name=rowner_new_Mike]').uncheck()
        page.locator('[name=rowner_new_Kurt]').check()
        for cb in page.locator('input[name^=rtype_new_]').all(): cb.uncheck()
        page.locator('[name=rtype_new_battery]').check()
        page.locator('.save-bar').get_by_role('button',name='Save recipients',exact=True).click()
        saved = page.evaluate('window.sent[0]')
        assert saved['id']=='save-recipients'
        assert saved['values']['remail_new']=='manager@example.com'
        assert saved['values']['rowner_new_Kurt']=='on' and 'rowner_new_Mike' not in saved['values']
        assert saved['values']['rtype_new_battery']=='on' and 'rtype_new_locks' not in saved['values']
        assert 'smtp_password' not in saved['values']
        assert page.evaluate('document.documentElement.scrollWidth') <= width
        page.goto('https://ha.example.com/setup')
        assert 'never-show-this-secret' not in page.content()
        assert page.locator('[name=smtp_password]').input_value()==''
        page.evaluate("window.sent=[];document.addEventListener('submit',e=>{e.preventDefault();window.sent.push({id:e.target.id,values:Object.fromEntries(new FormData(e.target))})})")
        page.locator('[name=smtp_host]').fill('smtp.example.com')
        page.locator('[name=smtp_port]').fill('587')
        page.locator('.save-bar').get_by_role('button',name='Save email settings',exact=True).click()
        saved = page.evaluate('window.sent[0]')
        assert saved['id']=='save-email-settings' and saved['values']['smtp_host']=='smtp.example.com'
        assert 'occ_summer' not in saved['values'] and 'guest_message' not in saved['values']
        assert page.evaluate('document.documentElement.scrollWidth') <= width
        # Simulate HA's authenticated ingress iframe, even if its proxy strips HTTP Referer.
        page.goto('https://ha.example.com/',referer='https://ha.example.com/hassio/ingress/stay?stay_page=properties')
        page.wait_for_url('**/properties')
        assert page.get_by_role('heading',name='Properties',exact=True).is_visible()
        page.close()
    browser.close()
print('PASS: owner/category recipient forms, SMTP secret masking, separate Save actions and ingress Properties link on desktop/phone')
