import asyncio
import json
import smtplib
from datetime import timedelta
from email import policy
from email.parser import BytesParser

import httpx
import pytest
from fastapi.testclient import TestClient

from app.alerts import AlertService, CATEGORIES, OWNERS, SMTP_FIELDS, UncertainEmail, owner_tag, send_smtp
from app.db import DB
from app.ha import HAClient
from app.sync import Syncer
from app.web import create_app
from app.locks import LockManager
from test_sync import SETTINGS, FakeHA, FakeHostaway, res
from test_climate import at


class HA(FakeHA):
    def __init__(self):
        super().__init__()
        self.notes = []
    async def notify(self, title, message, **kw):
        self.notes.append((title, message, kw))


@pytest.fixture
def service():
    s = Syncer(DB(':memory:'), SETTINGS, FakeHostaway(), HA())
    s.now = lambda: at('2030-01-12T12:00')
    for listing, name, owner in ((100, 'Mike house', 'Mike'), (200, 'Kurt house', 'Kurt'), (300, 'Unknown house', '')):
        s.db.execute('INSERT INTO properties(hostaway_listing_id,name,address,owner_scope) VALUES(?,?,?,?)',
                     (listing, name, name + ' address', owner))
    a = AlertService(s)
    for k, v in smtp_data().items():
        s.db.set_setting(k, v)
    a.sent = []
    a.send_email = lambda cfg, dest, title, body, key: a.sent.append((dest, title, body, key))
    return a


def smtp_data():
    return {'smtp_enabled': '1', 'smtp_host': 'smtp.example.com', 'smtp_port': '587',
            'smtp_security': 'starttls', 'smtp_username': 'sender@example.com', 'smtp_password': 'test-secret-123',
            'smtp_from': 'sender@example.com', 'alert_properties_url': 'https://ha.example.com/hassio/ingress/test?stay_page=properties'}


def recipient(a, owner='Mike', types='all', email=None):
    return a.db.execute("INSERT INTO alert_recipients(name,target,email,owners,alert_types,active) VALUES(?,'',?,?,?,1)",
                        (owner, email or owner.lower().replace(',', '-') + '@example.com', owner, types))


def run(job):
    return asyncio.run(job)


@pytest.mark.parametrize('raw,expected', [
    ({'listingTags': ['Mike']}, 'Mike'), ({'listingTags': [{'name': ' kurt '}]}, 'Kurt'),
    ({'listingTags': [{'tag': {'name': 'Mike'}}]}, 'Mike'),
    ({'reservationTags': [{'tagName': 'Kurt'}]}, 'Kurt'),
    ({'tags': ['Mike', 'Pool']}, 'Mike'), ({'tags': ['Mike', 'Kurt']}, ''),
    ({'tags': ['Mike house']}, ''), ({'tags': [123]}, ''), ({'guestName': 'Mike'}, None),
])
def test_exact_owner_tags_only(raw, expected):
    assert owner_tag(raw) == expected


def test_listings_request_resources_and_owner_sync_preserves_omitted_tags(service):
    h = service.s.hostaway
    h.listings_data = [{'id': 100, 'name': 'Mike house', 'listingTags': [{'name': 'Mike'}]},
                       {'id': 200, 'name': 'Kurt house', 'listingTags': [{'name': 'Kurt'}]}]
    run(service.s.import_listings())
    assert service.db.one('SELECT owner_scope FROM properties WHERE id=2')['owner_scope'] == 'Kurt'
    h.listings_data = [{'id': 100, 'name': 'Mike house'}]
    run(service.s.import_listings())
    assert service.db.one('SELECT owner_scope FROM properties WHERE id=1')['owner_scope'] == 'Mike'
    h.listings_data = [{'id': 100, 'name': 'Mike house', 'listingTags': []}]
    run(service.s.import_listings())
    assert service.db.one('SELECT owner_scope FROM properties WHERE id=1')['owner_scope'] == ''


def test_owner_and_category_filters_never_leak_another_portfolio(service):
    recipient(service, 'Mike', 'battery')
    recipient(service, 'Kurt', 'battery')
    recipient(service, 'Mike,Kurt', 'thermostats', 'other@example.com')
    run(service.emit('Battery low', 'Lock is at 14%. Replace batteries.', 'battery-test', category='battery', property_id=1))
    assert service.sent == []  # delivery does not block lock operations
    run(service.flush())
    assert len(service.sent) == 1
    dest, title, body, _ = service.sent[0]
    assert dest == 'mike@example.com' and '14%' in body and 'Mike house address' in body
    assert 'Kurt house' not in body and 'Open Properties' in body


def test_shared_integration_email_is_filtered_per_owner(service):
    recipient(service, 'Mike', 'integration')
    recipient(service, 'Kurt', 'integration')
    run(service.emit('Reauthorize Resideo account', 'Reauthorize the named integration account.', 'shared', category='integration', property_ids=[1, 2]))
    run(service.flush())
    bodies = {dest: body for dest, _, body, _ in service.sent}
    assert 'Mike house' in bodies['mike@example.com'] and 'Kurt house' not in bodies['mike@example.com']
    assert 'Kurt house' in bodies['kurt@example.com'] and 'Mike house' not in bodies['kurt@example.com']


def test_unknown_owner_is_not_sent_to_a_single_owner_recipient(service):
    recipient(service, 'Mike')
    recipient(service, 'Kurt')
    recipient(service, 'Mike,Kurt', email='admin@example.com')
    run(service.emit('Battery low', '10%', 'unknown', category='battery', property_id=3))
    run(service.flush())
    assert [x[0] for x in service.sent] == ['admin@example.com']
    assert 'UNCONFIRMED' in service.sent[0][2]


def test_battery_context_includes_current_occupancy_and_stay_lengths(service):
    service.s.upsert_reservation(res())
    recipient(service)
    run(service.emit('Battery low', 'Front lock battery: 8%', 'occupied', category='battery', property_id=1))
    run(service.flush())
    body = service.sent[0][2]
    for fragment in ('8%', 'Mike house address', 'Occupied: yes', 'Check-in:', 'Checkout:', 'Occupied for:', 'Time remaining:', 'Ann Lee'):
        assert fragment in body
    assert '4821' not in body  # no door codes in the contextual email


def test_one_failed_email_retries_without_resending_successes(service):
    recipient(service, 'Mike,Kurt', email='bad@example.com')
    recipient(service, 'Mike,Kurt', email='good@example.com')
    calls = []
    def send(cfg, dest, *rest):
        calls.append(dest)
        if dest == 'bad@example.com' and calls.count(dest) == 1:
            raise OSError('connection refused before DATA')
    service.send_email = send
    run(service.emit('Battery', '10%', 'retry', category='battery', property_id=1))
    run(service.flush())
    run(service.emit('Battery', '10%', 'retry', category='battery', property_id=1))
    assert calls == ['bad@example.com', 'good@example.com']
    service.s.now = lambda: at('2030-01-12T12:01')
    run(service.flush())
    assert calls == ['bad@example.com', 'good@example.com', 'bad@example.com']
    assert all(r['status'] == 'sent' for r in service.db.query('SELECT * FROM staff_alert_deliveries'))


def test_ambiguous_smtp_delivery_is_not_automatically_replayed(service):
    recipient(service)
    def uncertain(*args):
        raise UncertainEmail()
    service.send_email = uncertain
    run(service.emit('Battery', '10%', 'uncertain', category='battery', property_id=1))
    run(service.flush())
    assert service.db.one('SELECT status FROM staff_alert_deliveries')['status'] == 'uncertain'
    service.s.now = lambda: at('2030-01-13T12:00')
    service.send_email = lambda *args: pytest.fail('Ambiguous DATA must not be replayed')
    run(service.flush())


def test_preference_change_or_deletion_cancels_pending_email(service):
    rid = recipient(service)
    run(service.emit('Battery', '10%', 'changed', category='battery', property_id=1))
    service.db.execute("UPDATE alert_recipients SET owners='Kurt' WHERE id=?", (rid,))
    run(service.flush())
    assert service.sent == []
    assert service.db.one('SELECT status FROM staff_alert_deliveries')['status'] == 'cancelled'


def test_test_message_goes_only_to_selected_recipient_even_with_no_alert_categories(service):
    rid = recipient(service, types='')
    recipient(service, 'Kurt')
    run(service.test_recipient(rid))
    run(service.flush())
    assert len(service.sent) == 1 and service.sent[0][0] == 'mike@example.com'
    assert service.sent[0][1] == 'Stay Automation test alert'


def test_smtp_password_is_write_only_and_blank_preserves_it(service):
    client = TestClient(create_app(service.s))
    html = client.get('/setup').text
    assert 'test-secret-123' not in html
    data = dict(smtp_data(), smtp_password='')
    assert client.post('/smtp-settings-save', data=data).status_code == 200
    assert service.db.get_setting('smtp_password') == 'test-secret-123'
    assert 'test-secret-123' not in json.dumps(service.db.query('SELECT * FROM events'))
    assert client.post('/smtp-settings-save', data=dict(data, smtp_host='other.example.com')).status_code == 400


@pytest.mark.parametrize('patch', [{'smtp_port': '0'}, {'smtp_security': 'none'}, {'smtp_from': 'bad'},
                                  {'alert_properties_url': 'http://example.com'},
                                  {'alert_properties_url': 'https://ha/api/hassio_ingress/temp/properties'}])
def test_invalid_smtp_setup_does_not_partially_save(service, patch):
    before = service.db.query('SELECT * FROM settings ORDER BY key')
    client = TestClient(create_app(service.s))
    assert client.post('/smtp-settings-save', data=dict(smtp_data(), **patch)).status_code == 400
    assert service.db.query('SELECT * FROM settings ORDER BY key') == before


def test_recipient_email_scopes_and_categories_are_saved(service):
    data = {'recipient_settings_version': '2', 'rname_new': 'Battery manager', 'remail_new': 'manager@example.com',
            'rowner_new_Kurt': 'on', 'rtype_new_battery': 'on', 'ractive_new': 'on'}
    c = TestClient(create_app(service.s))
    assert c.post('/recipients-save', data=data).status_code == 200
    row = service.db.one('SELECT * FROM alert_recipients')
    assert (row['email'], row['owners'], row['alert_types']) == ('manager@example.com', 'Kurt', 'battery')


def test_reauthorization_monitor_uses_actual_entry_entities_to_scope_owners(service):
    recipient(service, 'Mike', 'integration')
    recipient(service, 'Kurt', 'integration')
    service.db.execute("INSERT INTO locks(entity_id,property_id) VALUES('lock.mike',1)")
    service.db.execute("INSERT INTO locks(entity_id,property_id) VALUES('lock.kurt',2)")
    async def entries():
        return [{'entry_id': 'account-m', 'flow_id': 'flow1', 'domain': 'schlage', 'title': 'Mike account', 'entities': ['lock.mike']}]
    service.ha.reauthorization_entries = entries
    run(service.integrations())
    run(service.flush())
    assert len(service.sent) == 1 and service.sent[0][0] == 'mike@example.com'
    assert 'schlage' in service.sent[0][2] and 'account-m' in service.sent[0][2]


def test_pending_email_is_cancelled_when_the_home_changes_owner(service):
    recipient(service)
    run(service.emit('Battery', '9%', 'owner-changed', category='battery', property_id=1))
    service.db.execute("UPDATE properties SET owner_scope='Kurt' WHERE id=1")
    run(service.flush())
    assert service.sent == []


def test_smtp_protocol_uses_tls_one_recipient_and_verified_data(monkeypatch):
    calls = []
    class SMTP:
        def __init__(self, host, port, **kw): calls.append(('connect', host, port, kw))
        def ehlo(self): pass
        def starttls(self, context):
            import ssl
            assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
            calls.append(('tls',))
        def login(self, user, password): calls.append(('login', user, password))
        def mail(self, sender): return 250, b'OK'
        def rcpt(self, dest):
            calls.append(('rcpt', dest))
            return 250, b'OK'
        def data(self, data):
            assert b'\n' not in data.replace(b'\r\n', b'')
            message = BytesParser(policy=policy.default).parsebytes(data)
            assert message['To'] == 'one@example.com'
            assert message['Message-ID']
            assert 'https://ha.example.com' in message.get_body(preferencelist=('html',)).get_content()
            return 250, b'Accepted'
        def close(self): calls.append(('close',))
    monkeypatch.setattr('app.alerts.smtplib.SMTP', SMTP)
    send_smtp(smtp_data(), 'one@example.com', 'Battery low', 'Battery at 8%', 'k')
    assert sum(c[0]=='rcpt' for c in calls) == 1
    assert any(c[0]=='tls' for c in calls)


def test_smtp_data_connection_loss_is_ambiguous_not_a_safe_retry(monkeypatch):
    class SMTP:
        def __init__(self,*a,**kw): pass
        def ehlo(self): pass
        def starttls(self, **kw): pass
        def login(self,*a): pass
        def mail(self,*a): return 250, b'OK'
        def rcpt(self,*a): return 250, b'OK'
        def data(self,*a): raise smtplib.SMTPServerDisconnected('lost')
        def close(self): pass
    monkeypatch.setattr('app.alerts.smtplib.SMTP', SMTP)
    with pytest.raises(UncertainEmail):
        send_smtp(smtp_data(), 'one@example.com', 'Title', 'body', 'k')


def test_manual_retry_requires_inbox_acknowledgment(service):
    recipient(service)
    def uncertain(*a): raise UncertainEmail()
    service.send_email = uncertain
    run(service.emit('Battery','9%','ambiguous',category='battery',property_id=1))
    run(service.flush())
    job = service.db.one('SELECT * FROM staff_alert_deliveries')
    c = TestClient(create_app(service.s))
    assert c.post('/alert-delivery-retry',data={'delivery_id': job['id']}).status_code == 400
    assert c.post('/alert-delivery-retry',data={'delivery_id': job['id'],'checked_inbox':'on'}).status_code == 200
    assert service.db.one('SELECT status FROM staff_alert_deliveries')['status'] == 'pending'


def test_stable_ha_email_link_redirects_to_properties(service):
    service.db.set_setting('public_url','https://ha.example.com')
    c = TestClient(create_app(service.s), follow_redirects=False)
    ref = 'https://ha.example.com/hassio/ingress/stay?stay_page=properties'
    r = c.get('/', headers={'referer':ref})
    assert r.status_code == 302 and r.headers['location'] == 'properties'
    assert c.get('/',headers={'referer':'https://other.example.com/hassio/ingress/stay?stay_page=properties'}).status_code == 200


def test_ws_monitor_reads_only_real_reauth_flows_and_entity_membership(monkeypatch):
    payloads = []
    replies = [{'type':'auth_required'}, {'type':'auth_ok'},
               {'id':1,'success':True,'result':[{'flow_id':'f','handler':'schlage','context':{'source':'reauth','entry_id':'m'}},
                                               {'flow_id':'user','handler':'resideo','context':{'source':'user','entry_id':'k'}}]},
               {'id':2,'success':True,'result':[{'entity_id':'lock.m','config_entry_id':'m'},{'entity_id':'lock.k','config_entry_id':'k'}]},
               {'id':3,'success':True,'result':[{'entry_id':'m','title':'Mike Schlage'},{'entry_id':'k','title':'Kurt Resideo'}]}]
    class WS:
        async def __aenter__(self): return self
        async def __aexit__(self,*a): pass
        async def recv(self): return json.dumps(replies.pop(0))
        async def send(self,data): payloads.append(json.loads(data))
    def connect(url, **kw):
        assert url == 'ws://supervisor/core/websocket'
        return WS()
    monkeypatch.setattr('websockets.asyncio.client.connect',connect)
    c = HAClient('http://supervisor/core','test-token', http=httpx.AsyncClient(base_url='http://supervisor/core', headers={'Authorization':'Bearer test-token'}, transport=httpx.MockTransport(lambda req: httpx.Response(200))))
    entries = run(c.reauthorization_entries())
    assert entries == [{'entry_id':'m','flow_id':'f','domain':'schlage','title':'Mike Schlage','entities':['lock.m']}]
    assert [p['type'] for p in payloads[1:]] == ['config_entries/flow/progress','config/entity_registry/list','config_entries/get']


def test_recipient_input_validation_is_atomic(service):
    rid = recipient(service)
    data = {'recipient_settings_version':'2', f'rname_{rid}':'Mike', f'remail_{rid}':'mike@example.com',
            f'rowner_{rid}_Mike':'on', f'rtype_{rid}_battery':'on', f'ractive_{rid}':'on',
            'rname_new':'Bad', 'remail_new':'bad\r\nBcc:attacker@example.com','rowner_new_Kurt':'on','ractive_new':'on'}
    c = TestClient(create_app(service.s))
    before = service.db.query('SELECT * FROM alert_recipients')
    assert c.post('/recipients-save',data=data).status_code == 400
    assert service.db.query('SELECT * FROM alert_recipients') == before


def test_arrival_summary_cannot_leak_other_owner_names(service):
    recipient(service,'Mike','arrivals')
    recipient(service,'Kurt','arrivals')
    run(service.emit('Arrivals today', 'Mike and Kurt raw full summary must not appear', 'arrivals', category='arrivals',
                     property_ids=[1,2], details={1:'Mike guest arrives at 4 PM',2:'Kurt guest arrives at 5 PM'}))
    run(service.flush())
    for dest, _, body, _ in service.sent:
        if dest == 'mike@example.com':
            assert 'Kurt' not in body
        else:
            assert 'Mike' not in body


def test_durable_delivery_survives_service_restart(service):
    recipient(service)
    run(service.emit('Battery','8%','restart',category='battery',property_id=1))
    restarted = AlertService(service.s)
    restarted.send_email = service.send_email
    run(restarted.flush())
    assert len(service.sent) == 1
    run(restarted.flush())
    assert len(service.sent) == 1


def test_email_failure_does_not_claim_recipient_success(service):
    recipient(service)
    def fail(*a): raise smtplib.SMTPAuthenticationError(535,b'bad credentials')
    service.send_email = fail
    run(service.emit('Battery','8%','failed',category='battery',property_id=1))
    run(service.flush())
    job = service.db.one('SELECT * FROM staff_alert_deliveries')
    assert job['status'] == 'pending' and job['sent_at'] is None


def test_setup_fallback_does_not_bypass_a_disabled_matching_recipient(service):
    service.db.set_setting('alert_service','notify.my_phone')
    service.db.execute("INSERT INTO alert_recipients(name,target,active) VALUES('Disabled','notify.my_phone',0)")
    run(service.emit('Battery','8%','disabled',category='battery',property_id=1))
    assert not service.db.one('SELECT 1 FROM staff_alert_deliveries')


def test_smtp_ssl_context_is_verified(monkeypatch):
    import ssl
    calls = []
    class SMTP_SSL:
        def __init__(self,host,port,context,**kw):
            assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
            calls.append(port)
        def ehlo(self): pass
        def login(self,*a): pass
        def mail(self,*a): return 250,b'OK'
        def rcpt(self,*a): return 250,b'OK'
        def data(self,*a): return 250,b'OK'
        def close(self): pass
    monkeypatch.setattr('app.alerts.smtplib.SMTP_SSL',SMTP_SSL)
    send_smtp(dict(smtp_data(),smtp_security='ssl',smtp_port='465'),'one@example.com','Title','body','k')
    assert calls == [465]


def test_reservation_owner_tag_can_fill_a_missing_listing_tag(service):
    service.db.execute("UPDATE properties SET owner_scope='' WHERE id=1")
    raw = res()
    raw['reservationTags'] = [{'name':'Mike'}]
    service.s.upsert_reservation(raw)
    recipient(service)
    run(service.emit('Battery','8%','reservation-owner',category='battery',property_id=1))
    run(service.flush())
    assert len(service.sent) == 1 and service.sent[0][0] == 'mike@example.com'


def test_conflicting_listing_and_reservation_owner_tags_are_not_guessed(service):
    raw = res()
    raw['reservationTags'] = [{'name':'Kurt'}]
    service.s.upsert_reservation(raw)
    recipient(service, 'Mike')
    recipient(service, 'Kurt')
    run(service.emit('Battery','8%','conflict',category='battery',property_id=1))
    run(service.flush())
    assert service.sent == []


def test_one_outbound_ha_target_does_not_recreate_the_admin_notice():
    calls = []
    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(200,json=[])
    c = HAClient('https://mock','token',http=httpx.AsyncClient(base_url='https://mock',transport=httpx.MockTransport(handler)))
    run(c.notify_target('Title','body','notify.mobile_app_kurt'))
    assert calls == ['/api/services/notify/mobile_app_kurt']
