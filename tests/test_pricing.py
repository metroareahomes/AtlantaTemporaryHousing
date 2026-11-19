import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from app.hostaway import HostawayClient, HostawayError
from app.pricing import DEFAULTS, TZ, PricingSimulator, plan, validate, promotion_window
from app.web import create_app
from test_sync import syncer, run

LISTING = {'id':100,'currencyCode':'USD','timeZoneName':'America/New_York','checkInTimeStart':15}
FRIDAY = datetime(2030, 6, 14, 14, tzinfo=TZ)


def day(date='2030-06-14', **kw):
    return dict({'id':1,'date':date,'price':250,'isAvailable':1,'status':'available',
                 'closedOnArrival':0,'minimumStay':1,'isProcessed':1}, **kw)


@pytest.mark.parametrize('price,proposed',[(250,'225.00'),(200,''),(199.99,''),(210,'189.00'),(201.01,'180.91'),(0,''),('NaN',''),(None,'')])
def test_current_calendar_price_threshold_and_rounding(price, proposed):
    cfg = dict(DEFAULTS)
    assert plan([day(price=price)], LISTING, FRIDAY, cfg)[0]['proposed']==proposed


@pytest.mark.parametrize('patch,reason',[
    ({'isAvailable':0},'unavailable'),({'status':'reserved'},'reserved'),
    ({'status':None},'not confirmed available'),
    ({'closedOnArrival':1},'Arrival closed'),({'closedOnArrival':None},'Arrival closed'),
    ({'reservations':[{'guestName':'private'}]},'blocking inventory'),({'countPendingUnits':1},'blocking inventory'),
    ({'countBlockedUnits':1},'blocking inventory'),({'availableUnitsToSell':2},'Multi-unit'),
])
def test_unavailable_or_ambiguous_inventory_never_proposed(patch,reason):
    r=plan([day(**patch)],LISTING,FRIDAY,DEFAULTS)[0]
    assert r['proposed']=='' and reason in r['reason']


def test_checkin_window_uses_listing_hour_not_midnight():
    rows=plan([day(),day('2030-06-15')],LISTING,FRIDAY,DEFAULTS)
    assert rows[0]['proposed']=='225.00'
    assert rows[1]['reason']=='Check-in outside next-hours window'  # 25 hours away
    at=FRIDAY+timedelta(hours=1)
    assert plan([day('2030-06-15')],LISTING,at,DEFAULTS)[0]['proposed']=='225.00'
    assert plan([day()],LISTING,FRIDAY+timedelta(hours=2),DEFAULTS)[0]['proposed']==''


@pytest.mark.parametrize('at,active',[(FRIDAY-timedelta(minutes=1),False),(FRIDAY,True),
    (datetime(2030,6,17,0,0,tzinfo=TZ),True),(datetime(2030,6,17,0,1,tzinfo=TZ),False)])
def test_exact_weekend_window(at,active):
    start,end=promotion_window(at,DEFAULTS)
    assert (start<=at<end) is active


def test_checkin_after_promotion_stop_excluded_even_with_longer_window():
    at=datetime(2030,6,16,6,tzinfo=TZ)
    row=plan([day('2030-06-17')],LISTING,at,dict(DEFAULTS,pricing_window='48'))[0]
    assert row['reason']=='Check-in after promotion stops'


@pytest.mark.parametrize('date,offset',[(datetime(2030,1,11,14,tzinfo=TZ),-5),(FRIDAY,-4)])
def test_eastern_schedule_respects_daylight_saving(date,offset):
    start,end=promotion_window(date,DEFAULTS)
    assert start.hour==14 and end.hour==0 and end.minute==1
    assert start.utcoffset()==timedelta(hours=offset)


@pytest.mark.parametrize('metadata',[dict(LISTING,currencyCode='EUR'),dict(LISTING,currencyCode=None),
    dict(LISTING,timeZoneName='Europe/Berlin'),dict(LISTING,checkInTimeStart=None)])
def test_unverified_currency_timezone_or_checkin_rejected(metadata):
    with pytest.raises(ValueError):
        plan([day()],metadata,FRIDAY,DEFAULTS)


def configure(s):
    run(s.import_listings())
    prop=s.db.one('SELECT * FROM properties WHERE hostaway_listing_id=100')
    cfg=dict(DEFAULTS,pricing_property=str(prop['id']))
    s.db.set_settings({**cfg,'pricing_simulation_enabled':'1'})
    s.now=lambda:FRIDAY
    async def listing(lid):
        assert lid==100
        return dict(LISTING)
    async def calendar(lid,start,end):
        assert lid==100 and start=='2030-06-14' and end=='2030-06-15'
        return [day(),day('2030-06-15')]
    s.hostaway.pricing_listing=listing
    s.hostaway.pricing_calendar=calendar
    return cfg


def test_repeat_simulations_do_not_compound_or_change_devices_or_reservations(syncer):
    s=syncer; configure(s)
    before={t:s.db.query(f'SELECT * FROM {t}') for t in ('properties','locks','thermostats','reservations')}
    service=PricingSimulator(s)
    run(service.preview(FRIDAY))
    s.now=lambda:FRIDAY+timedelta(seconds=31)
    run(service.preview(FRIDAY))
    rows=s.db.query('SELECT * FROM pricing_simulations')
    assert len(rows)==2
    assert all(json.loads(r['rows_json'])[0]['proposed']=='225.00' for r in rows)
    assert before=={t:s.db.query(f'SELECT * FROM {t}') for t in before}
    assert 'guestName' not in json.dumps(rows)


def test_manual_read_is_opt_in_and_rate_limited(syncer):
    service=PricingSimulator(syncer)
    with pytest.raises(ValueError,match='disabled'):
        run(service.preview(FRIDAY))
    configure(syncer)
    run(service.preview(FRIDAY))
    with pytest.raises(ValueError,match='30 seconds'):
        run(service.preview(FRIDAY))


def test_api_failure_is_generic_and_cannot_write_audit_or_leak_secret(syncer):
    configure(syncer)
    async def fail(lid):
        raise HostawayError('SECRET access token')
    syncer.hostaway.pricing_listing=fail
    with pytest.raises(ValueError) as exc:
        run(PricingSimulator(syncer).preview(FRIDAY))
    assert 'SECRET' not in str(exc.value)
    assert not syncer.db.query('SELECT * FROM pricing_simulations')
    assert 'SECRET' not in json.dumps(syncer.db.query('SELECT * FROM events'))


def test_property_or_configuration_changed_during_io_discards_preview(syncer):
    configure(syncer)
    original=syncer.hostaway.pricing_calendar
    async def read(*args):
        syncer.db.set_setting('pricing_property','2')
        return await original(*args)
    syncer.hostaway.pricing_calendar=read
    with pytest.raises(ValueError,match='changed'):
        run(PricingSimulator(syncer).preview(FRIDAY))
    assert not syncer.db.query('SELECT * FROM pricing_simulations')


@pytest.mark.parametrize('patch',[{'pricing_discount':'NaN'},{'pricing_discount':'100'},{'pricing_threshold':'-1'},
    {'pricing_window':'0'},{'pricing_property':'0'},{'pricing_start_time':'25:00'},
    {'pricing_check1_day':'4'},{'pricing_check2_time':'1:00'},{'pricing_end_day':'5'}])
def test_invalid_settings_are_atomic(syncer,patch):
    cfg=configure(syncer)
    old=syncer.db.query('SELECT * FROM settings ORDER BY key')
    c=TestClient(create_app(syncer))
    response=c.post('/pricing-settings-save',data={**cfg,**patch,'pricing_simulation_enabled':'on'})
    assert response.status_code==400
    assert old==syncer.db.query('SELECT * FROM settings ORDER BY key')


def test_setup_variables_save_and_simulate_with_no_live_activation_route(syncer):
    cfg=configure(syncer)
    c=TestClient(create_app(syncer))
    assert 'Weekend last-minute pricing' in c.get('/setup').text
    r=c.post('/pricing-settings-save',data={**cfg,'pricing_discount':'12.5','pricing_simulation_enabled':'on'},follow_redirects=False)
    assert r.status_code==303
    assert not syncer.db.get_bool('pricing_enabled')
    r=c.post('/pricing-simulate',data={'pricing_preview_at':'2030-06-14T14:00'},follow_redirects=False)
    assert r.status_code==303 and syncer.db.one('SELECT id FROM pricing_simulations')
    html=c.get('/setup').text
    assert '218.75' in html and 'Simulation only' in html
    assert c.post('/pricing-activate').status_code==404
    c.post('/pricing-settings-save',data=dict(cfg,pricing_property='0'),follow_redirects=False)
    assert not syncer.db.get_bool('pricing_simulation_enabled')


def test_calendar_client_uses_only_get_and_sanitizes_source():
    calls=[]
    def handle(request):
        calls.append((request.method,request.url.path))
        if request.url.path.endswith('/calendar'):
            data=[day(reservations=[{'guestName':'private','doorCode':'4821'}])]
        else:
            data=dict(LISTING,wifiPassword='PRIVATE')
        return httpx.Response(200,json={'status':'success','result':data})
    h=HostawayClient('1','secret',load_token=lambda:'stored',http=httpx.AsyncClient(base_url='https://hostaway.test',transport=httpx.MockTransport(handle)))
    listing=run(h.pricing_listing(100)); rows=run(h.pricing_calendar(100,'2030-06-14','2030-06-14'))
    assert 'PRIVATE' not in json.dumps(listing) and 'private' not in json.dumps(rows) and '4821' not in json.dumps(rows)
    assert rows[0]['reservations'] is True
    assert calls==[('GET','/listings/100'),('GET','/listings/100/calendar')]


def test_calendar_duplicates_and_missing_days_rejected():
    def handle(request):
        return httpx.Response(200,json={'status':'success','result':[day()]})
    h=HostawayClient('1','secret',load_token=lambda:'stored',http=httpx.AsyncClient(base_url='https://hostaway.test',transport=httpx.MockTransport(handle)))
    with pytest.raises(HostawayError):
        run(h.pricing_calendar(100,'2030-06-14','2030-06-15'))
    with pytest.raises(ValueError):
        plan([day(),day()],LISTING,FRIDAY,DEFAULTS)


def test_rounded_proposal_cannot_be_zero_or_unchanged():
    cfg=dict(DEFAULTS,pricing_threshold='0',pricing_discount='90')
    assert plan([day(price='0.01')],LISTING,FRIDAY,cfg)[0]['proposed']==''
    cfg['pricing_discount']='0.01'
    assert plan([day(price='0.01')],LISTING,FRIDAY,cfg)[0]['proposed']==''


def test_deactivated_property_during_read_is_not_recorded(syncer):
    configure(syncer)
    original=syncer.hostaway.pricing_calendar
    async def read(*args):
        syncer.db.execute('UPDATE properties SET active=0')
        return await original(*args)
    syncer.hostaway.pricing_calendar=read
    with pytest.raises(ValueError,match='changed'):
        run(PricingSimulator(syncer).preview(FRIDAY))
    assert not syncer.db.query('SELECT * FROM pricing_simulations')


def test_no_pricing_worker_or_live_route_is_registered():
    from pathlib import Path
    main=Path('stay_automation/app/main.py').read_text()
    assert 'pricing.preview' not in main


def test_spring_dst_gap_rejected_before_api_read(syncer):
    configure(syncer)
    c=TestClient(create_app(syncer))
    result=c.post('/pricing-simulate',data={'pricing_preview_at':'2030-03-10T02:30'},follow_redirects=False)
    assert 'does+not+exist' in result.headers['location']
    assert not syncer.db.query('SELECT * FROM pricing_simulations')


def test_one_time_override_uses_selected_home_and_leaves_weekend_settings_intact(syncer):
    configure(syncer)
    property_id = syncer.db.execute(
        "INSERT INTO properties(hostaway_listing_id,name,owner_scope) VALUES(303,'Wright','Kurt')"
    )
    syncer.now = lambda: FRIDAY - timedelta(days=1)

    async def listing(listing_id):
        assert listing_id == 303
        return dict(LISTING, id=303)

    async def calendar(listing_id, start, end):
        assert (listing_id, start, end) == (303, '2030-06-13', '2030-06-15')
        return [day(date, price=180) for date in ('2030-06-13', '2030-06-14', '2030-06-15')]

    syncer.hostaway.pricing_listing = listing
    syncer.hostaway.pricing_calendar = calendar
    before = syncer.db.query('SELECT * FROM settings')
    client = TestClient(create_app(syncer))
    response = client.post('/pricing-simulate', data={
        'pricing_action': 'one_time', 'pricing_property': str(property_id),
        'pricing_manual_discount': '20', 'pricing_minimum_0': '3', 'pricing_minimum_2': '2',
    }, follow_redirects=False)
    assert response.is_redirect
    result = syncer.db.one('SELECT * FROM pricing_simulations')
    assert result['property_id'] == property_id
    rows = json.loads(result['rows_json'])
    assert [row['proposed'] for row in rows] == ['144.00', '', '']
    assert [row.get('proposed_minimum_stay') for row in rows] == [3, None, 2]
    assert syncer.db.query('SELECT * FROM settings') == before


@pytest.mark.parametrize('blocked_listing', [None, 301, 302])
def test_thursday_special_combines_checked_homes_and_rate_matches(syncer, blocked_listing):
    for lid, name, owner in [(301, 'Wright', 'Kurt'), (302, 'Matching', 'Kurt'),
                             (303, 'At threshold', 'Kurt'), (304, 'Other owner', 'Mike')]:
        syncer.db.execute('INSERT INTO properties(hostaway_listing_id,name,owner_scope) VALUES(?,?,?)',
                          (lid, name, owner))
    wright = syncer.db.one('SELECT id FROM properties WHERE hostaway_listing_id=301')['id']
    syncer.now = lambda: FRIDAY - timedelta(days=1)

    async def calendar(lid, start, end):
        assert lid != 304
        assert (start, end) == ('2030-06-13', '2030-06-15')
        return [day(start, price={301: 180, 302: 250, 303: 200}[lid], closedOnArrival=None, isAvailable=None, countAvailableUnits=1,
                    availableUnitsToSell=1, desiredUnitsToSell=1),
                day('2030-06-14', price=300, isAvailable='1', closedOnArrival=1),
                day(end, price=350, countReservedUnits=1 if lid == blocked_listing else '0')]

    async def listing(lid):
        return dict(LISTING, id=lid)

    syncer.hostaway.pricing_calendar = calendar
    syncer.hostaway.pricing_listing = listing
    client = TestClient(create_app(syncer))
    response = client.post('/thursday-special', data={
        'special_owner': 'Kurt', 'special_property': str(wright),
        'special_discount': '20', 'special_floor': '200',
    })
    assert response.status_code == 200
    assert '<h3>Wright</h3>' in response.text
    assert '<h3>Matching</h3>' in response.text
    assert '<h3>At threshold</h3>' not in response.text
    assert '<h3>Other owner</h3>' not in response.text
    if blocked_listing:
        assert 'Skipped: all three nights must be confirmed vacant.' in response.text
        start = response.text.index('<h3>Wright</h3>' if blocked_listing == 301 else '<h3>Matching</h3>')
        end = response.text.index('</table>', start)
        assert '<td>3</td>' not in response.text[start:end]
        assert '<td>240.00</td>' not in response.text[start:end]
        return
    assert '<td>180</td><td>144.00</td><td>1</td><td>3</td>' in response.text
    assert '<td>300</td><td>240.00</td><td>1</td><td>—</td>' in response.text
    assert '<td>350</td><td>280.00</td><td>1</td><td>—</td>' in response.text
    assert 'Arrival status unconfirmed' in response.text
    assert 'Hostaway reports arrivals closed' in response.text
    assert 'Inventory counts reported' in response.text


@pytest.mark.parametrize('blocked', [False, True])
def test_live_wright_trial_checks_all_nights_and_applies_only_once(syncer, blocked):
    pid = syncer.db.execute(
        "INSERT INTO properties(hostaway_listing_id,name,owner_scope) VALUES(301,'Wright','Kurt')"
    )
    syncer.now = lambda: FRIDAY - timedelta(days=1)
    calls = []

    async def listing(lid):
        assert lid == 301
        return dict(LISTING, id=lid)

    async def calendar(lid, start, end):
        assert lid == 301
        return [
            day(start, price=200 if calls else 250, minimumStay=3 if calls else 1),
            day('2030-06-14', price=240 if calls else 300, minimumStay=2),
            day(end, price=280 if calls else 350, countReservedUnits=int(blocked)),
        ]

    async def write(lid, intervals):
        assert lid == 301
        calls.append(intervals)

    syncer.hostaway.pricing_listing = listing
    syncer.hostaway.pricing_calendar = calendar
    syncer.hostaway.apply_calendar_intervals = write
    service = PricingSimulator(syncer)
    if blocked:
        with pytest.raises(ValueError, match='confirmed vacant'):
            run(service.apply_wright_special(str(pid)))
        assert calls == []
        return
    assert '$200.00' in run(service.apply_wright_special(str(pid)))
    assert calls == [[
        {'startDate': '2030-06-13', 'endDate': '2030-06-13', 'price': 200, 'minimumStay': 3},
        {'startDate': '2030-06-14', 'endDate': '2030-06-14', 'price': 240},
        {'startDate': '2030-06-15', 'endDate': '2030-06-15', 'price': 280},
    ]]
    with pytest.raises(ValueError, match='already attempted'):
        run(PricingSimulator(syncer).apply_wright_special(str(pid)))


def test_bulk_special_rechecks_threshold_and_preserves_manual_selection(syncer):
    from starlette.datastructures import FormData

    for lid in (301, 302):
        syncer.db.execute(
            "INSERT INTO properties(hostaway_listing_id,name,owner_scope) VALUES(?,?,'Kurt')",
            (lid, str(lid)),
        )
    service = PricingSimulator(syncer)
    syncer.now = lambda: FRIDAY - timedelta(days=1)
    checked = syncer.db.one('SELECT id FROM properties WHERE hostaway_listing_id=301')['id']
    reads = {301: 0, 302: 0}
    writes = {}

    async def listing(lid):
        return dict(LISTING, id=lid)

    async def calendar(lid, start, end):
        reads[lid] += 1
        current = 180 if lid == 301 or reads[lid] > 2 else 250
        if lid in writes:
            return [day(item['startDate'], price=item['price'],
                        minimumStay=item.get('minimumStay', 1)) for item in writes[lid]]
        return [day(start, price=current), day('2030-06-14'), day(end)]

    async def write(lid, intervals):
        writes[lid] = intervals

    syncer.hostaway.pricing_listing = listing
    syncer.hostaway.pricing_calendar = calendar
    syncer.hostaway.apply_calendar_intervals = write
    form = FormData({
        'special_owner': 'Kurt', 'special_property': str(checked),
        'special_discount': '15', 'special_floor': '200',
    })
    preview = run(service.thursday_special(form))
    assert len(preview['properties']) == 2
    assert writes == {}
    reports = run(service.apply_selected_special(form))
    assert set(writes) == {301}
    assert writes[301][0]['price'] == 153
    assert any('Verified in Hostaway' in report for report in reports)
    assert any('no longer meets the threshold' in report for report in reports)


def test_thursday_schedule_uses_saved_selection_and_runs_once_after_restart(syncer):
    from starlette.datastructures import FormData

    service = PricingSimulator(syncer)
    thursday = FRIDAY - timedelta(days=1)
    calls = []

    async def apply(form):
        calls.append(dict(form))
        return ['Test home verified']

    service.apply_selected_special = apply
    syncer.now = lambda: thursday
    run(service.tick_thursday_schedule())
    assert calls == []
    service.save_thursday_schedule(FormData({
        'special_time': '14:00', 'special_enabled': 'on', 'special_owner': 'Kurt',
        'special_discount': '15', 'special_floor': '200',
    }))
    syncer.now = lambda: thursday - timedelta(minutes=1)
    run(service.tick_thursday_schedule())
    assert calls == []
    syncer.now = lambda: thursday
    run(service.tick_thursday_schedule())
    restart = PricingSimulator(syncer)
    restart.apply_selected_special = apply
    run(restart.tick_thursday_schedule())
    syncer.now = lambda: FRIDAY
    run(restart.tick_thursday_schedule())
    assert calls == [{'special_owner': 'Kurt', 'special_discount': '15', 'special_floor': '200'}]


def test_owner_pages_scope_selection_and_save_distinct_discounts(syncer):
    for lid, owner in [(610, 'Mike'), (611, 'Kurt')]:
        syncer.db.execute(
            'INSERT INTO properties(hostaway_listing_id,name,owner_scope) VALUES(?,?,?)',
            (lid, f'{owner} Test Home', owner),
        )
    service = PricingSimulator(syncer)
    client = TestClient(create_app(syncer, pricing=service))
    for owner, other, discount in [('Mike', 'Kurt', '12'), ('Kurt', 'Mike', '20')]:
        url = f'/thursday-special?owner={owner}'
        html = client.get(url).text
        assert f'{owner} Test Home' in html
        assert f'{other} Test Home' not in html
        response = client.post(url, data={
            'special_action': 'save_schedule', 'special_owner': other,
            'special_time': '14:00', 'special_discount': discount, 'special_floor': '200',
            'special_enabled': 'on',
        }, follow_redirects=False)
        assert response.status_code == 303
        assert service.thursday_schedule(owner)['owner'] == owner
    assert service.thursday_schedule('Mike')['discount'] == '12'
    assert service.thursday_schedule('Kurt')['discount'] == '20'
