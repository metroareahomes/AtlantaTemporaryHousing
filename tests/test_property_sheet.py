import asyncio
import hashlib
import hmac
import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.db import DB
from app.sync import Syncer
from app.web import create_app
from app.property_sheet import HEADERS, PropertySheet, snapshot, valid_endpoint
from test_sync import SETTINGS, FakeHA, FakeHostaway, res
from test_climate import at


@pytest.fixture
def sheet_sync():
    s = Syncer(DB(':memory:'), SETTINGS, FakeHostaway(), FakeHA())
    s.now = lambda: at('2030-01-12T12:00')
    for listing, name, owner in ((100, 'Maple', 'Mike'), (200, '=IMPORTXML("evil")', 'Kurt')):
        s.db.execute('INSERT INTO properties(hostaway_listing_id,name,address,owner_scope) VALUES(?,?,?,?)',
                     (listing, name, 'Example address', owner))
    s.upsert_reservation(res())
    s.db.set_setting('last_sync_at', s.now().isoformat())
    return s


def configure(s):
    s.db.set_settings({'sheet_enabled': '1', 'sheet_endpoint': 'https://script.google.com/macros/s/test/exec', 'sheet_secret': 'k' * 64})


def test_snapshot_minimizes_data_sorts_problems_and_preserves_numeric_zero(sheet_sync):
    s = sheet_sync
    s.db.execute("INSERT INTO locks(entity_id,name,state,property_id,battery,codes_read_at,code_names,last_error,fail_count) VALUES('lock.a','Secret guest','locked',1,0,?,'Secret guest','code 4821 missing',1)", (s.now().isoformat(),))
    value = snapshot(s)
    text = json.dumps(value)
    assert 'Ann Lee' not in text and '4821' not in text and 'Secret guest' not in text
    assert value['rows'][0][0] == 'Mike' and value['rows'][0][9] == 0
    assert value['rows'][0][3] == 'Occupied'
    assert value['rows'][0][6:8] == [1.8, 2.9]
    assert value['rows'][1][1].startswith("'=IMPORTXML")
    assert 'battery 0%' in text
    assert all(len(row) == len(HEADERS) for row in value['rows'])


def test_stale_sources_and_conflicting_owner_are_visible(sheet_sync):
    s = sheet_sync
    s.db.set_setting('last_sync_at', '2029-01-01T00:00:00-05:00')
    s.db.execute("UPDATE reservations SET owner_scope='Kurt'")
    rows = snapshot(s)['rows']
    maple = next(r for r in rows if r[1] == 'Maple')
    assert maple[0] == 'Unconfirmed'
    assert 'occupancy may be outdated' in maple[12]


def test_empty_or_uninitialized_snapshot_cannot_erase_sheet(sheet_sync):
    s = sheet_sync
    s.db.set_setting('last_sync_at', '')
    with pytest.raises(ValueError):
        snapshot(s)
    s.db.set_setting('last_sync_at', s.now().isoformat())
    s.db.execute('UPDATE properties SET active=0')
    with pytest.raises(ValueError):
        snapshot(s)


def test_export_signed_redirect_get_and_exact_ack(sheet_sync):
    s = sheet_sync
    configure(s)
    calls, sent = [], {}
    def handle(request):
        calls.append((request.method, request.url.host))
        if request.method == 'POST':
            sent.update(json.loads(request.content))
            assert hmac.compare_digest(sent['signature'], hmac.new(b'k'*64, (sent['timestamp']+'\n'+sent['data']).encode(), hashlib.sha256).hexdigest())
            return httpx.Response(302, headers={'location': 'https://script.googleusercontent.com/result'})
        assert not request.content
        return httpx.Response(200, json={'ok': True, 'digest': hashlib.sha256(sent['data'].encode()).hexdigest(), 'rows': 2})
    assert asyncio.run(PropertySheet(s, transport=httpx.MockTransport(handle)).push())
    assert calls == [('POST', 'script.google.com'), ('GET', 'script.googleusercontent.com')]
    assert s.db.get_setting('sheet_last_success')
    assert s.db.get_setting('sheet_last_error') == ''


@pytest.mark.parametrize('response', [httpx.Response(302, headers={'location':'https://evil.example/'}),
    httpx.Response(302, headers={'location':'https://accounts.google.com/login'}), httpx.Response(500),
    httpx.Response(200, text='<html>Sign in</html>'), httpx.Response(200,json={'ok':True,'digest':'wrong','rows':2}),
    httpx.Response(200,json={'ok':False})])
def test_failed_push_keeps_success_timestamp_and_retries_next_pass(sheet_sync, response):
    s = sheet_sync
    configure(s)
    s.db.set_setting('sheet_last_success', 'previous')
    calls = []
    def handle(request):
        calls.append(request)
        return response
    service = PropertySheet(s, transport=httpx.MockTransport(handle))
    assert not asyncio.run(service.push())
    assert not asyncio.run(service.push())
    assert len(calls) == 2
    assert s.db.get_setting('sheet_last_success') == 'previous'
    assert 'five minutes' in s.db.get_setting('sheet_last_error')
    assert 'script.google.com' not in s.db.get_setting('sheet_last_error')


def test_disabled_and_invalid_setup_never_sends(sheet_sync):
    def handle(request):
        raise AssertionError('Must not send')
    service = PropertySheet(sheet_sync, transport=httpx.MockTransport(handle))
    assert not asyncio.run(service.push())
    sheet_sync.db.set_setting('sheet_enabled','1')
    assert not asyncio.run(service.push())
    for url in ('http://script.google.com/macros/s/x/exec', 'https://evil.example/exec',
                'https://script.google.com.evil.example/macros/s/x/exec', 'https://script.google.com/macros/s/x/exec?key=bad'):
        assert not valid_endpoint(url)


def test_settings_validate_atomically_and_hide_secret(sheet_sync):
    c = TestClient(create_app(sheet_sync))
    data = {'sheet_enabled':'on', 'sheet_endpoint':'https://script.google.com/macros/s/test/exec', 'sheet_secret':'K'*64}
    assert c.post('/sheet-settings-save', data=data, follow_redirects=False).status_code == 303
    assert sheet_sync.db.get_bool('sheet_enabled')
    page = c.get('/setup').text
    assert 'Shared Google property sheet' not in page and 'K'*64 not in page
    invalid = dict(data, sheet_endpoint='https://example.com')
    assert c.post('/sheet-settings-save',data=invalid).status_code == 400
    assert sheet_sync.db.get_setting('sheet_endpoint') == data['sheet_endpoint']
    invalid = dict(data, sheet_endpoint='https://script.google.com/macros/s/another/exec',sheet_secret='')
    assert c.post('/sheet-settings-save',data=invalid).status_code == 400
    assert c.post('/sheet-settings-save',data={'sheet_endpoint':data['sheet_endpoint']},follow_redirects=False).status_code == 303
    assert not sheet_sync.db.get_bool('sheet_enabled')
    assert sheet_sync.db.get_setting('sheet_secret') == 'K'*64


def test_receiver_rejects_tampering_expiry_and_formulas():
    # Execute the actual Apps Script authentication/literal helpers against independent Node crypto.
    if not shutil.which('node'):
        pytest.skip('Node is needed for the Apps Script contract check')
    script = Path('stay_automation/google_sheet/Code.gs').read_text()
    runner = r'''
const vm = require('vm'), crypto = require('crypto'), assert = require('assert');
const ctx = {Utilities: {Charset: {UTF_8:'utf8'}, computeHmacSha256Signature:
  (data,key) => [...crypto.createHmac('sha256',key).update(data).digest()]}};
vm.createContext(ctx); vm.runInContext(SOURCE,ctx);
const body = {timestamp:'1900000000',data:JSON.stringify({schema:1,headers:HEADERS,rows:[Array(17).fill('')]})};
const sign = b => crypto.createHmac('sha256','k'.repeat(64)).update(b.timestamp+'\n'+b.data).digest('hex');
body.signature=sign(body);
assert.equal(ctx.authenticate(body,'k'.repeat(64),1900000000).rows.length,1);
assert.throws(() => ctx.authenticate(body,'wrong'.repeat(16),1900000000));
assert.throws(() => ctx.authenticate(body,'k'.repeat(64),1900000301));
assert.throws(() => ctx.authenticate({...body,data:body.data+' '},'k'.repeat(64),1900000000));
const bad={...body,data:JSON.stringify({schema:1,headers:HEADERS,rows:[['bad shape']]})}; bad.signature=sign(bad);
assert.throws(() => ctx.authenticate(bad,'k'.repeat(64),1900000000));
assert.equal(ctx.literal(' =IMPORTXML("evil")'),"' =IMPORTXML(\"evil\")");
assert.equal(ctx.literal(0),0);
const props = new Map([['CONNECTION_KEY','k'.repeat(64)],['SHEET_ID','sheet'],['TAB_ID','7']]);
let writes = 0, released = 0, stored;
const range = {setValues(v){writes++; stored=v; return this;},setWrap(){return this;},
  setBackgrounds(){return this;},clearContent(){return this;},setBackground(){return this;}};
const sheet = {getSheetId:()=>7,getLastRow:()=>3,getMaxRows:()=>100,getRange:()=>range};
ctx.PropertiesService={getScriptProperties:()=>({getProperty:k=>props.get(k),setProperties:v=>Object.entries(v).forEach(([k,v])=>props.set(k,v))})};
ctx.LockService={getScriptLock:()=>({waitLock(){},releaseLock(){released++;}})};
ctx.SpreadsheetApp={openById:()=>({getSheets:()=>[sheet]}),flush(){}};
ctx.ContentService={MimeType:{JSON:'json'},createTextOutput:v=>({setMimeType:()=>JSON.parse(v)})};
ctx.Utilities.DigestAlgorithm={SHA_256:'sha256'};
ctx.Utilities.computeDigest=(kind,data)=>[...crypto.createHash('sha256').update(data).digest()];
vm.runInContext('Date.now = () => 1900000000000',ctx);
const event = b => ({postData:{contents:JSON.stringify(b)}});
let response=ctx.doPost(event(body)); assert.equal(response.ok,true); assert.equal(response.rows,1);
assert.equal(writes,1); assert.equal(released,1);
response=ctx.doPost(event(body)); assert.equal(response.ok,true); assert.equal(writes,1);
const older={...body,timestamp:'1899999999'}; older.signature=sign(older);
assert.equal(ctx.doPost(event(older)).ok,false); assert.equal(writes,1);
assert.equal(ctx.doPost(event({...body,signature:'0'.repeat(64)})).ok,false); assert.equal(writes,1);

'''.replace('HEADERS',json.dumps(HEADERS)).replace('SOURCE',json.dumps(script))
    result = subprocess.run(['node','-e',runner],capture_output=True,text=True)
    assert result.returncode == 0, result.stderr
