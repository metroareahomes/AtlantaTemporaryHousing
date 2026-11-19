"""Real recipient and SMTP forms, plus stable ingress Properties links. No emails sent."""
from pathlib import Path
import json
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
s.db.set_setting('sheet_secret','never-show-sheet-secret')
c = TestClient(create_app(s))
pages = {p:c.get(p).text for p in ('/staff','/setup','/','/properties')}
s.db.execute("INSERT INTO alert_recipients(name,target,email,active) VALUES('Saved recipient','','saved@example.com',1)")
pages['/staff-saved'] = c.get('/staff').text
pid = s.db.execute("INSERT INTO properties(hostaway_listing_id,name,address,owner_scope) VALUES(100,'Test home','10 Wright St','Kurt')")
mike_id = s.db.execute("INSERT INTO properties(hostaway_listing_id,name,owner_scope) VALUES(101,'Wright backup','Mike')")
s.db.execute("INSERT INTO properties(hostaway_listing_id,name) VALUES(102,'Untagged property')")
s.db.execute("INSERT INTO pricing_simulations(created_at,property_id,simulation_at,config_json,rows_json) VALUES(?,?,?,?,?)", ('2030-06-14T14:00:00-04:00',pid,'2030-06-14T14:00:00-04:00','{}',json.dumps([{'date':'2030-06-14','current':'250','proposed':'225.00','minimum_stay':1,'reason':'Simulation only — calendar date, not first-night-only'}])))
pages['/setup-pricing'] = c.get('/setup').text
s.db.set_setting('pricing_simulation_enabled', '1')
pages['/setup-pricing-enabled'] = c.get('/setup').text
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
        assert page.get_by_text('No active recipients saved yet.',exact=False).is_visible()
        assert page.get_by_role('button',name='Send recipient test',exact=True).count()==0
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
        page.goto('https://ha.example.com/staff-saved')
        page.evaluate("window.tests=[];document.addEventListener('submit',e=>{e.preventDefault();window.tests.push(Object.fromEntries(new FormData(e.target)))})")
        page.get_by_role('button',name='Send recipient test',exact=True).click()
        assert page.evaluate('window.tests.length')==0  # native required selection blocks an empty request
        page.get_by_role('combobox',name='Recipient to test').select_option('1')
        page.get_by_role('button',name='Send recipient test',exact=True).click()
        assert page.evaluate('window.tests[0].recipient_id')=='1'
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
        assert 'never-show-sheet-secret' not in page.content()
        assert page.locator('[name=sheet_secret]').input_value()==''
        page.locator('[name=sheet_endpoint]').fill('https://script.google.com/macros/s/example/exec')
        page.locator('[name=sheet_secret]').fill('a'*64)
        page.locator('[name=sheet_enabled]').check()
        page.locator('.save-bar').get_by_role('button',name='Save sheet connection',exact=True).click()
        saved = page.evaluate('window.sent[1]')
        assert saved['id']=='save-sheet-settings' and saved['values']['sheet_secret']=='a'*64
        assert 'smtp_password' not in saved['values'] and 'guest_message' not in saved['values']
        assert page.evaluate('document.documentElement.scrollWidth') <= width
        page.goto('https://ha.example.com/setup-pricing')
        assert page.get_by_text('Live price writes: unavailable. Recurring schedule: off.',exact=True).is_visible()
        page.evaluate("window.pricing=[];document.addEventListener('submit',e=>{e.preventDefault();window.pricing.push({id:e.target.id,values:Object.fromEntries(new FormData(e.target))})})")
        owner_filter = page.get_by_label('Filter test properties by owner')
        picker = page.locator('[name=pricing_property]')
        assert owner_filter.input_value() == 'Kurt'
        assert picker.input_value() == str(pid)
        assert picker.locator('option').count() == 2
        owner_filter.select_option('Mike')
        assert picker.input_value() == '0'
        assert picker.locator('option').all_text_contents() == ['Choose one test property', 'Wright backup']
        picker.select_option(str(mike_id))
        owner_filter.select_option('')
        assert picker.input_value() == str(mike_id)
        assert picker.locator('option').count() == 4
        owner_filter.select_option('Kurt')
        picker.select_option(str(pid))
        page.locator('[name=pricing_discount]').fill('12.5')
        page.locator('[name=pricing_threshold]').fill('210')
        page.locator('[name=pricing_check1_time]').fill('07:00')
        page.locator('.save-bar').get_by_role('button',name='Save pricing settings',exact=True).click()
        saved=page.evaluate('window.pricing[0]')
        assert saved['id']=='save-pricing-settings'
        assert saved['values']['pricing_property'] == str(pid)
        assert 'pricing_simulation_enabled' not in saved['values']
        assert saved['values']['pricing_discount']=='12.5' and saved['values']['pricing_check1_time']=='07:00'
        assert 'smtp_password' not in saved['values'] and 'guest_message' not in saved['values']
        assert page.get_by_text('225.00',exact=True).count()==1
        assert page.evaluate('document.documentElement.scrollWidth') <= width
        page.goto('https://ha.example.com/setup-pricing-enabled')
        page.evaluate("""document.addEventListener('submit', event => {
            event.preventDefault();
            window.override = {
                action: event.submitter.getAttribute('formaction'),
                fields: Object.fromEntries(new FormData(event.target, event.submitter))
            };
        });""")
        page.locator('[name=pricing_manual_discount]').fill('20')
        page.locator('[name=pricing_minimum_0]').fill('3')
        page.get_by_role('button', name='Preview one-time override').click()
        override = page.evaluate('window.override')
        assert override['action'] == 'pricing-simulate'
        assert override['fields']['pricing_action'] == 'one_time'
        assert override['fields']['pricing_property'] == str(pid)
        assert override['fields']['pricing_manual_discount'] == '20'
        assert override['fields']['pricing_minimum_0'] == '3'
        # Simulate HA's authenticated ingress iframe, even if its proxy strips HTTP Referer.
        page.goto('https://ha.example.com/',referer='https://ha.example.com/hassio/ingress/stay?stay_page=properties')
        page.wait_for_url('**/properties')
        assert page.get_by_role('heading',name='Properties',exact=True).is_visible()
        page.close()
    browser.close()
print('PASS: owner/category recipient forms and empty-test protection, SMTP secret masking, separate Save actions and ingress Properties link on desktop/phone')
