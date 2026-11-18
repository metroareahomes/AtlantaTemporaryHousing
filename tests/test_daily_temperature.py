import asyncio
from datetime import timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.climate import ClimateManager
from app.daily_temperature import DEFAULTS, complaint, current_stay, daily_call
from app.hostaway import has_ntc
from app.web import create_app
from test_climate import cm, at, res, tick as normal_tick, row


def tick(cm, when):
    changed = normal_tick(cm, when)
    return changed + queue_tick(cm, when)


class Hostaway:
    def __init__(self, reservation):
        self.raw = dict(reservation, customFieldValues=[], guestNote=None)
        self.messages = []
        self.calls = 0
        self.note_appends = []
        self.fail = False

    async def reservation(self, rid):
        self.calls += 1
        if self.fail:
            raise RuntimeError('offline')
        return dict(self.raw)

    async def guest_messages(self, rid):
        return list(self.messages)

    async def append_ntc_note(self, rid):
        if self.fail:
            raise RuntimeError('offline')
        self.note_appends.append(rid)
        if not has_ntc(self.raw.get('guestNote')):
            note = self.raw.get('guestNote') or ''
            self.raw['guestNote'] = note + ('' if not note or note.endswith('\n') else '\n') + 'NTC'
        return dict(self.raw)


def enable(cm, winter=False):
    cm.db.set_setting('occupied_reset_enabled', '1')
    if winter:
        cm.db.set_setting('summer_start_month', '7')
        cm.db.set_setting('summer_end_month', '8')
        cm.ha.climate['climate.maple'].update(state='heat', target_temp=76)
    cm.s.hostaway = Hostaway(res())
    cm.db.execute("UPDATE thermostats SET last_mode='occupied', last_reservation_id=1, last_applied_at=?", (at('2030-06-12T13:00').isoformat(),))
    return cm.s.hostaway


def msg(body, **kw):
    return dict(dict(id=8, body=body, isIncoming=1, accountId=1, reservationId=1, listingMapId=100,
                     status='sent', insertedOn='2030-06-13T10:00:00-04:00'), **kw)


@pytest.mark.parametrize('body', [
    'The thermostat keeps resetting.', 'The temperature changes by itself.', 'The AC goes back to 73.',
    'Stop changing the thermostat!', "Please don't adjust our temperature.", 'Leave the thermostat where we set it.',
    'It is too hot in the bedroom.', 'We are freezing.', 'We are sweating and cannot sleep.',
    'The air conditioning is not working.', 'The heater is broken.', 'The temp keeps reverting.',
    'The a/c is stuck.', 'This thermostat override is frustrating.', 'We are uncomfortably warm.',
])
def test_keyword_complaints(body):
    assert complaint(body)


@pytest.mark.parametrize('body', [
    'The hot water is not working.', 'The hot water heater is not working.', 'The cold fridge is too noisy.', 'The pool is cold.',
    'It is not too hot.', "It isn't cold.", 'The temperature is fine.', 'The house is comfortable now.',
    'How do I use the thermostat?', 'We like the cool living room.', 'Thank you!',
    '> The thermostat keeps resetting', '<blockquote>The thermostat keeps resetting</blockquote>Thanks!',
    'Thanks!\nOn Monday John wrote:\nThe thermostat keeps resetting.',
])
def test_keyword_exclusions(body):
    assert not complaint(body)


@pytest.mark.parametrize('summer,state,setting,expected', [
    (True, 'cool', 68, 73), (True, 'cool', 73, None), (True, 'cool', 75, None),
    (False, 'heat', 76, 70), (False, 'heat', 70, None), (False, 'heat', 68, None),
    (True, 'heat', 76, None), (False, 'cool', 68, None),
    (True, 'heat', 68, 73), (False, 'cool', 76, 70), (True, 'off', 68, None),
    (True, 'cool', None, None), (True, 'cool', float('nan'), None),
])
def test_directional_limits(summer, state, setting, expected):
    call = daily_call({'state': state, 'target_temp': setting}, 73 if summer else 70, summer)
    assert call == ({'temperature': float(expected)} if expected is not None else None)


def test_auto_selects_seasonal_bound_for_single_mode_adjustment():
    t = {'state': 'auto', 'target_low': 65, 'target_high': 69}
    assert daily_call(t, 73, True) == {'temperature': 73.0}
    t.update(target_low=75, target_high=80)
    assert daily_call(t, 70, False) == {'temperature': 70.0}
    assert daily_call(dict(t, target_high=69), 73, True) == {'temperature': 73.0}
    assert daily_call({'state': 'cool', 'target_temp': 68, 'max_temp': 72}, 73, True) is None


def test_disabled_has_no_hostaway_reads_or_midstay_changes(cm):
    h = Hostaway(res())
    cm.s.hostaway = h
    tick(cm, at('2030-06-13T11:00'))
    assert h.calls == 0 and cm.ha.sets == []


def test_summer_once_per_day_even_after_guest_changes_and_restart(cm):
    enable(cm)
    cm.ha.climate['climate.maple']['target_temp'] = 68
    assert tick(cm, at('2030-06-13T09:59')) == 0
    assert tick(cm, at('2030-06-13T11:00')) == 1
    assert cm.ha.sets == [('climate.maple', 73, None, None)]
    cm.ha.climate['climate.maple']['target_temp'] = 68
    restarted = ClimateManager(cm.s, cm.alert)
    restarted.verify_delay = 0
    tick(restarted, at('2030-06-13T12:00'))
    assert len(cm.ha.sets) == 1
    tick(restarted, at('2030-06-14T11:00'))
    assert len(cm.ha.sets) == 2 and not cm.ha.modes


def test_winter_uses_configured_limit_only_above_it(cm):
    enable(cm, winter=True)
    cm.db.set_setting('daily_winter', '69')
    tick(cm, at('2030-06-13T11:00'))
    assert cm.ha.sets == [('climate.maple', 69, None, None)]
    cm.ha.climate['climate.maple']['target_temp'] = 67
    tick(cm, at('2030-06-14T11:00'))
    assert len(cm.ha.sets) == 1


def test_reasonable_setting_consumes_daily_check_without_changing_it(cm):
    enable(cm)
    cm.ha.climate['climate.maple']['target_temp'] = 75
    tick(cm, at('2030-06-13T11:00'))
    cm.ha.climate['climate.maple']['target_temp'] = 68
    tick(cm, at('2030-06-13T12:00'))
    assert cm.ha.sets == []
    assert cm.db.one('SELECT result FROM thermostat_daily_checks')['result'] == 'left alone'


def test_arrival_after_reset_time_waits_until_tomorrow(cm):
    enable(cm)
    tick(cm, at('2030-06-12T17:00'))
    assert cm.ha.sets == []
    tick(cm, at('2030-06-13T11:00'))
    assert len(cm.ha.sets) == 1


def test_complaint_pauses_current_stay_and_survives_extension_restart(cm):
    h = enable(cm)
    h.messages = [msg('Stop changing the thermostat.')]
    tick(cm, at('2030-06-13T11:00'))
    assert cm.ha.sets == [] and cm.daily_pauses.paused(1)
    assert len(cm.ha.notes) == 1
    h.raw['departureDate'] = '2030-06-18'
    cm.s.upsert_reservation(h.raw)
    restarted = ClimateManager(cm.s, cm.alert)
    restarted.verify_delay = 0
    tick(restarted, at('2030-06-16T11:00'))
    assert cm.ha.sets == [] and len(cm.ha.notes) == 1
    assert cm.db.one('SELECT reason FROM thermostat_stay_pauses')['reason'] == 'Stop changing the thermostat'


def test_hostaway_field_pause_is_sticky_without_any_message(cm):
    h = enable(cm)
    cm.db.set_setting('temperature_pause_field_id', '167')
    h.raw['customFieldValues'] = [{'customFieldId': 167, 'value': 'YES'}]
    tick(cm, at('2030-06-13T11:00'))
    assert cm.daily_pauses.paused(1) and cm.ha.sets == []
    h.raw['customFieldValues'] = []
    cm.s.upsert_reservation(h.raw)
    assert cm.daily_pauses.paused(1)


@pytest.mark.parametrize('patch', [
    {'isIncoming': 0}, {'accountId': 2}, {'reservationId': 2}, {'listingMapId': 200},
    {'status': 'awaiting'}, {'insertedOn': '2030-06-10 10:00:00'}, {'insertedOn': 'garbage'},
    {'insertedOn': '2030-06-14T10:00:00-04:00'},
])
def test_only_current_inbound_guest_messages_pause(cm, patch):
    h = enable(cm)
    h.messages = [msg('The thermostat keeps resetting.', **patch)]
    tick(cm, at('2030-06-13T11:00'))
    assert not cm.daily_pauses.paused(1)
    assert len(cm.ha.sets) == 1


def test_hostaway_failure_skips_daily_but_preserves_vacant_control(cm):
    h = enable(cm)
    h.fail = True
    tick(cm, at('2030-06-13T11:00'))
    assert cm.ha.sets == [] and not cm.db.one('SELECT 1 FROM thermostat_daily_checks')
    tick(cm, at('2030-06-16T14:00'))
    assert cm.ha.sets[-1][1] == 78


def test_missing_field_resources_fail_closed(cm):
    h = enable(cm)
    cm.db.set_setting('temperature_pause_field_id', '167')
    del h.raw['customFieldValues']
    tick(cm, at('2030-06-13T11:00'))
    assert cm.ha.sets == []


def test_unknown_readback_is_not_confirmed_and_does_not_repeat(cm):
    enable(cm)
    cm.ha.unavailable_after_set = True
    tick(cm, at('2030-06-13T11:00'))
    assert cm.db.one('SELECT result FROM thermostat_daily_checks')['result'] == 'unconfirmed'
    cm.ha.climate['climate.maple'].update(state='cool', target_temp=68)
    tick(cm, at('2030-06-13T12:00'))
    assert len(cm.ha.sets) == 1


def test_checkout_indicator_clears_new_guest_is_unpaused(cm):
    h = enable(cm)
    h.messages = [msg('It is too hot.')]
    tick(cm, at('2030-06-13T11:00'))
    client = TestClient(create_app(cm.s))
    assert '✓ Daily temperature reset paused for this stay' in client.get('/properties').text
    cm.s.now = lambda: at('2030-06-15T10:00')
    assert '✓ Daily temperature reset paused for this stay' not in client.get('/properties').text
    h.raw = dict(res(id=2, arrival='2030-06-15', departure='2030-06-18'), customFieldValues=[], guestNote=None)
    h.messages = []
    cm.s.upsert_reservation(h.raw)
    tick(cm, at('2030-06-15T17:00'))
    cm.ha.sets.clear()
    cm.ha.climate['climate.maple']['target_temp'] = 68
    tick(cm, at('2030-06-16T11:00'))
    assert len(cm.ha.sets) == 1 and not cm.daily_pauses.paused(2)


def test_daily_write_cap_is_shared_across_thermostats(cm):
    enable(cm)
    cm.ha.climate['climate.second'] = dict(cm.ha.climate['climate.maple'], entity_id='climate.second', name='Second')
    cm.db.execute("INSERT INTO thermostats(entity_id,name,property_id,match_source,last_mode,last_applied_at,last_reservation_id) VALUES('climate.second','Second',1,'manual','occupied',?,1)", (at('2030-06-12T13:00').isoformat(),))
    tick(cm, at('2030-06-13T11:00'))
    assert len(cm.ha.sets) == 1
    tick(cm, at('2030-06-13T11:05'))
    assert len(cm.ha.sets) == 2


def test_setup_saves_separate_limits_without_resetting_guest_state(cm):
    enable(cm)
    before = row(cm)
    client = TestClient(create_app(cm.s), follow_redirects=False)
    data = dict(DEFAULTS, daily_winter=69, daily_summer=74, occupied_reset_enabled='on', temperature_complaints_enabled='on')
    assert client.post('/daily-temperature-settings-save', data=data).status_code == 303
    assert cm.db.get_int('daily_winter', 0) == 69 and cm.db.get_int('daily_summer', 0) == 74
    assert row(cm) == before and cm.ha.sets == []
    saved = cm.db.query('SELECT * FROM settings ORDER BY key')
    for patch in ({'daily_winter': 'abc'}, {'daily_summer': '100'}, {'occupied_reset_hour': '24'},
                  {'temperature_pause_field_id': '-1'}, {'daily_winter': '75', 'daily_summer': '73'}):
        assert client.post('/daily-temperature-settings-save', data=dict(data, **patch)).status_code == 400
        assert cm.db.query('SELECT * FROM settings ORDER BY key') == saved


def test_monitor_pauses_before_daily_check_time_and_deduplicates(cm):
    h = enable(cm)
    cm.s.now = lambda: at('2030-06-13T10:30')
    h.messages = [msg('It is too hot.')]
    asyncio.run(cm.daily_pauses.monitor())
    asyncio.run(cm.daily_pauses.monitor())
    assert cm.daily_pauses.paused(1) and len(cm.ha.notes) == 1
    assert cm.ha.sets == [] and h.calls == 1


def test_monitor_does_not_contact_hostaway_when_disabled(cm):
    h = Hostaway(res())
    cm.s.hostaway = h
    asyncio.run(cm.daily_pauses.monitor())
    assert h.calls == 0


def test_cancellation_discovered_during_preflight_prevents_daily_write(cm):
    h = enable(cm)
    h.raw['status'] = 'cancelled'
    tick(cm, at('2030-06-13T11:00'))
    assert cm.ha.sets == [] and not cm.db.one('SELECT 1 FROM thermostat_daily_checks')


def test_complaint_arriving_during_fresh_read_prevents_write(cm):
    enable(cm)
    original = cm.ha.climate_state
    async def read(entity_id):
        cm.daily_pauses._pause(1, 'Guest message', 'Stop changing it')
        return await original(entity_id)
    cm.ha.climate_state = read
    tick(cm, at('2030-06-13T11:00'))
    assert cm.ha.sets == []


def test_changed_guest_setting_during_preflight_is_respected(cm):
    enable(cm)
    original = cm.ha.climate_state
    async def read(entity_id):
        cm.ha.climate[entity_id]['target_temp'] = 75
        return await original(entity_id)
    cm.ha.climate_state = read
    tick(cm, at('2030-06-13T11:00'))
    assert cm.ha.sets == []


def test_adjustment_exception_is_attempted_once_and_logged_unconfirmed(cm):
    enable(cm)
    async def fail(*args, **kwargs):
        cm.ha.sets.append('attempt')
        raise RuntimeError('timeout')
    cm.ha.set_temperature = fail
    tick(cm, at('2030-06-13T11:00'))
    tick(cm, at('2030-06-13T11:05'))
    assert cm.ha.sets == ['attempt']
    assert cm.db.one('SELECT result FROM thermostat_daily_checks')['result'] == 'unconfirmed'


def test_default_setup_temperatures_and_disabled_feature(cm):
    html = TestClient(create_app(cm.s)).get('/setup').text
    assert 'name="daily_winter" value="70"' in html
    assert 'name="daily_summer" value="73"' in html
    assert 'name="occupied_reset_hour" value="10"' in html
    assert 'name="occupied_reset_enabled"  aria-describedby' in html


def test_switching_property_automation_off_during_read_prevents_write(cm):
    enable(cm)
    original = cm.ha.climate_state
    async def read(entity_id):
        cm.db.execute('UPDATE properties SET thermostat_automation=0')
        return await original(entity_id)
    cm.ha.climate_state = read
    tick(cm, at('2030-06-13T11:00'))
    assert cm.ha.sets == []


def test_unreadable_fresh_ha_state_skips_daily_without_consuming_attempt(cm):
    enable(cm)
    async def fail(entity_id):
        raise RuntimeError('HA unavailable')
    cm.ha.climate_state = fail
    tick(cm, at('2030-06-13T11:00'))
    assert cm.ha.sets == [] and not cm.db.one('SELECT 1 FROM thermostat_daily_checks')


def queue_tick(cm, when):
    cm.s.now = lambda: when
    return asyncio.run(cm.daily_tick())


def second_thermostat(cm):
    cm.ha.climate['climate.second'] = dict(cm.ha.climate['climate.maple'], entity_id='climate.second', name='Second')
    cm.db.execute("INSERT INTO thermostats(entity_id,name,property_id,match_source,last_mode,last_applied_at,last_reservation_id) VALUES('climate.second','Second',1,'manual','occupied',?,1)", (at('2030-06-12T13:00').isoformat(),))


@pytest.mark.parametrize('when,checkin,checkout', [
    ('2030-06-12T11:00', 6, 10),  # even an early arrival never qualifies on arrival day
    ('2030-06-12T23:00', 6, 10),
    ('2030-06-15T09:00', 16, 10),
    ('2030-06-15T11:00', 16, 23),  # late checkout must not qualify
    ('2030-06-15T22:00', 16, 23),
])
def test_entire_arrival_and_departure_dates_are_excluded(cm, when, checkin, checkout):
    h = enable(cm)
    h.raw.update(checkInTime=checkin, checkOutTime=checkout)
    cm.s.upsert_reservation(h.raw)
    assert queue_tick(cm, at(when)) == 0
    tick(cm, at(when))
    assert cm.ha.sets == []
    assert h.calls == 0


def test_custom_start_time_is_honored(cm):
    enable(cm)
    cm.db.set_setting('occupied_reset_hour', '14')
    assert queue_tick(cm, at('2030-06-13T13:59')) == 0
    assert queue_tick(cm, at('2030-06-13T14:00')) == 1


def test_queue_resumes_at_90_seconds_across_restart_even_for_same_home(cm):
    enable(cm)
    second_thermostat(cm)
    assert queue_tick(cm, at('2030-06-13T10:00')) == 1
    assert queue_tick(cm, at('2030-06-13T10:01:29')) == 0
    restarted = ClimateManager(cm.s, cm.alert)
    restarted.verify_delay = 0
    assert queue_tick(restarted, at('2030-06-13T10:01:29')) == 0
    assert queue_tick(restarted, at('2030-06-13T10:01:30')) == 1
    assert len(cm.ha.sets) == 2


def test_spacing_is_adjustable_and_large_pass_cap_cannot_bypass_it(cm):
    enable(cm)
    second_thermostat(cm)
    cm.db.set_setting('occupied_reset_spacing_seconds', '180')
    cm.db.set_setting('therm_max_per_tick', '20')
    cm.db.set_setting('therm_gap_seconds', '0')
    tick(cm, at('2030-06-13T10:00'))
    assert len(cm.ha.sets) == 1
    tick(cm, at('2030-06-13T10:02:59'))
    assert len(cm.ha.sets) == 1
    tick(cm, at('2030-06-13T10:03'))
    assert len(cm.ha.sets) == 2


def test_reasonable_thermostat_does_not_use_cloud_slot(cm):
    enable(cm)
    second_thermostat(cm)
    cm.ha.climate['climate.maple']['target_temp'] = 75
    assert queue_tick(cm, at('2030-06-13T10:00')) == 1
    assert cm.ha.sets == []
    assert queue_tick(cm, at('2030-06-13T10:00:15')) == 1
    assert cm.ha.sets == [('climate.second', 73.0, None, None)]


def test_normal_cloud_command_delays_daily_queue_without_changing_normal_schedule(cm):
    enable(cm)
    cm.s.now = lambda: at('2030-06-13T09:59:30')
    cm._note_cloud_attempt()
    assert queue_tick(cm, at('2030-06-13T10:00')) == 0
    assert queue_tick(cm, at('2030-06-13T10:01')) == 1


def test_failure_backs_off_entire_daily_queue_and_survives_restart(cm):
    enable(cm)
    second_thermostat(cm)
    original = cm.ha.set_temperature
    async def fail(*args, **kw):
        cm.ha.sets.append('failed')
        raise RuntimeError('HTTP 429')
    cm.ha.set_temperature = fail
    queue_tick(cm, at('2030-06-13T10:00'))
    cm.ha.set_temperature = original
    restarted = ClimateManager(cm.s, cm.alert)
    restarted.verify_delay = 0
    assert queue_tick(restarted, at('2030-06-13T10:02')) == 0
    assert cm.ha.sets == ['failed']
    assert queue_tick(restarted, at('2030-06-13T11:00')) == 1
    assert len(cm.ha.sets) == 2


def test_slot_is_persisted_before_sending_command(cm):
    enable(cm)
    original = cm.ha.set_temperature
    async def send(*args, **kw):
        assert not cm._daily_slot_ready(cm.s.now())
        assert cm.db.one('SELECT result FROM thermostat_daily_checks')['result'] == 'attempted'
        await original(*args, **kw)
    cm.ha.set_temperature = send
    assert queue_tick(cm, at('2030-06-13T10:00')) == 1


def test_completed_queue_does_not_keep_reading_ha(cm):
    enable(cm)
    queue_tick(cm, at('2030-06-13T10:00'))
    async def fail():
        raise AssertionError('Queue is complete: no cache read needed')
    cm.ha.climate_states = fail
    assert queue_tick(cm, at('2030-06-13T10:02')) == 0


def test_crossing_midnight_during_preflight_cannot_write_on_departure_day(cm):
    enable(cm)
    original = cm.ha.climate_state
    async def read(eid):
        cm.s.now = lambda: at('2030-06-15T00:00')
        return await original(eid)
    cm.ha.climate_state = read
    queue_tick(cm, at('2030-06-14T23:59'))
    assert cm.ha.sets == []


def test_setup_spacing_validation_and_default(cm):
    c = TestClient(create_app(cm.s), follow_redirects=False)
    html = c.get('/setup').text
    assert 'name="occupied_reset_spacing_seconds" value="90"' in html
    for invalid in ('0', '29', '3601', 'abc'):
        assert c.post('/daily-temperature-settings-save', data=dict(DEFAULTS, occupied_reset_spacing_seconds=invalid)).status_code == 400
    assert c.post('/daily-temperature-settings-save', data=dict(DEFAULTS, occupied_reset_spacing_seconds=180)).status_code == 303
    assert cm.db.get_int('occupied_reset_spacing_seconds', 0) == 180


def test_normal_thermostat_pass_does_not_run_hostaway_daily_checks(cm):
    h = enable(cm)
    h.fail = True
    normal_tick(cm, at('2030-06-13T10:00'))
    assert h.calls == 0 and cm.ha.sets == []


def test_one_failed_candidate_does_not_starve_the_next_thermostat(cm):
    enable(cm)
    second_thermostat(cm)
    original = cm.ha.climate_state
    async def read(eid):
        if eid == 'climate.maple':
            raise RuntimeError('unreadable thermostat')
        return await original(eid)
    cm.ha.climate_state = read
    assert queue_tick(cm, at('2030-06-13T10:00')) == 0
    assert queue_tick(cm, at('2030-06-13T10:00:15')) == 1
    assert cm.ha.sets == [('climate.second', 73.0, None, None)]


def test_manual_ntc_in_guest_notes_pauses_without_a_guest_complaint(cm):
    h = enable(cm)
    h.raw['guestNote'] = 'Early arrival approved.\nNTC\nPlease leave the package.'
    cm.s.upsert_reservation(h.raw)
    assert cm.daily_pauses.paused(1)
    queue_tick(cm, at('2030-06-13T10:00'))
    assert cm.ha.sets == [] and h.calls == 0
    assert not cm.db.one('SELECT 1 FROM thermostat_note_updates')


def test_complaint_enqueues_ntc_and_appends_existing_notes_once(cm):
    h = enable(cm)
    h.raw['guestNote'] = 'Late arrival; bring crib.'
    h.messages = [msg('Please stop changing the thermostat.')]
    tick(cm, at('2030-06-13T11:00'))
    assert cm.daily_pauses.paused(1)
    assert cm.db.one('SELECT synced_at FROM thermostat_note_updates')['synced_at'] is None
    assert asyncio.run(cm.daily_pauses.flush_note_updates()) == 1
    assert h.raw['guestNote'] == 'Late arrival; bring crib.\nNTC'
    assert cm.db.one('SELECT synced_at FROM thermostat_note_updates')['synced_at'] is not None
    assert asyncio.run(cm.daily_pauses.flush_note_updates()) == 0
    assert h.note_appends == [1]


def test_ntc_retry_is_durable_and_keeps_local_pause_during_hostaway_failure(cm):
    h = enable(cm)
    h.messages = [msg('It is too hot.')]
    tick(cm, at('2030-06-13T11:00'))
    h.fail = True
    assert asyncio.run(cm.daily_pauses.flush_note_updates()) == 0
    assert cm.daily_pauses.paused(1)
    job = cm.db.one('SELECT * FROM thermostat_note_updates')
    assert job['attempts'] == 1 and job['synced_at'] is None
    restarted = ClimateManager(cm.s, cm.alert)
    assert asyncio.run(restarted.daily_pauses.flush_note_updates()) == 0  # retry delay
    h.fail = False
    cm.s.now = lambda: at('2030-06-13T11:01')
    assert asyncio.run(restarted.daily_pauses.flush_note_updates()) == 1
    assert h.raw['guestNote'] == 'NTC'


def test_pending_ntc_recovers_pause_if_process_stopped_before_local_pause_insert(cm):
    h = enable(cm)
    cm.s.now = lambda: at('2030-06-13T11:00')
    cm.db.execute('INSERT INTO thermostat_note_updates(reservation_id,next_attempt_at) VALUES(1,?)',
                  (cm.s.now().astimezone(timezone.utc).isoformat(timespec='seconds'),))
    assert cm.daily_pauses.paused(1)
    assert queue_tick(cm, at('2030-06-13T11:00')) == 0
    assert asyncio.run(cm.daily_pauses.flush_note_updates()) == 1
    assert cm.db.one('SELECT 1 FROM thermostat_stay_pauses')


def test_pending_note_finishes_even_after_feature_disabled(cm):
    h = enable(cm)
    cm.s.now = lambda: at('2030-06-13T11:00')
    cm.daily_pauses._pause(1, 'Guest message', 'Too hot')
    cm.db.set_setting('occupied_reset_enabled', '0')
    assert asyncio.run(cm.daily_pauses.flush_note_updates()) == 1
    assert h.raw['guestNote'] == 'NTC'


def test_missing_guest_notes_skips_daily_instead_of_ignoring_possible_ntc(cm):
    h = enable(cm)
    del h.raw['guestNote']
    assert queue_tick(cm, at('2030-06-13T11:00')) == 0
    assert cm.ha.sets == []


@pytest.mark.parametrize('patch', [{'isIncoming': 0}, {'sentUsingHostaway': 1},
                                  {'communicationType': 'internal'}, {'communicationEvent': 'checkIn'}])
def test_staff_and_automated_messages_cannot_pause_or_enqueue_ntc(cm, patch):
    h = enable(cm)
    h.messages = [msg('Stop changing the thermostat.', **patch)]
    queue_tick(cm, at('2030-06-13T11:00'))
    assert not cm.daily_pauses.paused(1)
    assert not cm.db.one('SELECT 1 FROM thermostat_note_updates')


def test_properties_shows_pending_ntc_until_it_is_confirmed(cm):
    h = enable(cm)
    cm.s.now = lambda: at('2030-06-13T11:00')
    cm.daily_pauses._pause(1, 'Guest message', 'Too hot')
    client = TestClient(create_app(cm.s))
    assert 'NTC not yet saved in Hostaway' in client.get('/properties').text
    asyncio.run(cm.daily_pauses.flush_note_updates())
    html = client.get('/properties').text
    assert 'NTC not yet saved in Hostaway' not in html
    assert '✓ Daily temperature reset paused for this stay' in html


def test_staff_notes_are_not_keyword_analyzed_without_explicit_ntc(cm):
    h = enable(cm)
    h.raw['guestNote'] = 'Staff note: thermostat keeps resetting; house is too hot.'
    queue_tick(cm, at('2030-06-13T11:00'))
    assert not cm.daily_pauses.paused(1)
    assert len(cm.ha.sets) == 1


@pytest.mark.parametrize('winter,start_mode,initial,wanted', [(True,'cool',76,'heat'),(False,'heat',68,'cool'),(True,'auto',None,'heat')])
def test_daily_mode_then_temperature_uses_separate_paced_slots(cm, winter, start_mode, initial, wanted):
    enable(cm, winter=winter)
    cm.ha.climate['climate.maple'].update(state=start_mode, target_temp=initial, target_low=76, target_high=68)
    queue_tick(cm, at('2030-06-13T10:00'))
    assert cm.ha.modes == [('climate.maple',wanted)]
    assert cm.ha.sets == []
    assert cm.db.one('SELECT result FROM thermostat_daily_checks')['result'] == 'mode_pending'
    queue_tick(cm, at('2030-06-13T10:01'))
    assert cm.ha.sets == []
    # Auto mode exposes a single target only after Home Assistant catches up.
    if start_mode == 'auto':
        cm.ha.climate['climate.maple']['target_temp'] = 76
    restarted = ClimateManager(cm.s, cm.alert)
    restarted.verify_delay = 0
    queue_tick(restarted, at('2030-06-13T10:02'))
    assert cm.ha.sets == [('climate.maple',70.0 if winter else 73.0,None,None)]
    assert len(cm.ha.modes) == 1
    assert cm.db.one('SELECT result FROM thermostat_daily_checks')['result'] == 'confirmed'


def test_daily_unconfirmed_mode_never_sends_temperature_or_repeats_mode(cm):
    enable(cm, winter=True)
    cm.ha.climate['climate.maple'].update(state='cool',target_temp=76)
    cm.ha.mode_fails = True
    queue_tick(cm, at('2030-06-13T10:00'))
    queue_tick(cm, at('2030-06-13T10:02'))
    queue_tick(cm, at('2030-06-13T10:06'))
    assert cm.ha.sets == []
    assert len(cm.ha.modes) == 1
    assert cm.db.one('SELECT result FROM thermostat_daily_checks')['result'] == 'unconfirmed'


def test_ntc_added_between_mode_and_temperature_blocks_second_command(cm):
    h = enable(cm, winter=True)
    cm.ha.climate['climate.maple'].update(state='cool',target_temp=76)
    queue_tick(cm, at('2030-06-13T10:00'))
    h.raw['guestNote']='Existing notes NTC concierge'
    queue_tick(cm, at('2030-06-13T10:02'))
    assert cm.ha.sets == [] and cm.daily_pauses.paused(1)


def test_reasonable_setting_after_mode_switch_is_left_alone(cm):
    enable(cm, winter=True)
    cm.ha.climate['climate.maple'].update(state='cool',target_temp=76)
    queue_tick(cm, at('2030-06-13T10:00'))
    cm.ha.climate['climate.maple']['target_temp']=68
    queue_tick(cm, at('2030-06-13T10:02'))
    assert cm.ha.sets == []
    assert cm.db.one('SELECT result FROM thermostat_daily_checks')['result'] == 'confirmed'


def test_pending_mode_waits_for_single_target_reading(cm):
    enable(cm, winter=True)
    cm.ha.climate['climate.maple'].update(state='auto',target_temp=None,target_low=76,target_high=80)
    queue_tick(cm, at('2030-06-13T10:00'))
    queue_tick(cm, at('2030-06-13T10:02'))
    assert cm.db.one('SELECT result FROM thermostat_daily_checks')['result'] == 'mode_pending'
    assert cm.ha.sets == []
    cm.ha.climate['climate.maple']['target_temp']=76
    queue_tick(cm, at('2030-06-13T10:03'))
    assert cm.ha.sets == [('climate.maple',70.0,None,None)]


def test_checkout_day_between_mode_and_target_cannot_write(cm):
    enable(cm, winter=True)
    cm.ha.climate['climate.maple'].update(state='cool',target_temp=76)
    queue_tick(cm, at('2030-06-14T23:58'))
    queue_tick(cm, at('2030-06-15T00:00'))
    assert len(cm.ha.modes) == 1 and cm.ha.sets == []


def test_confirmed_number_in_wrong_mode_cannot_confirm_daily_target(cm):
    enable(cm, winter=True)
    original = cm.ha.set_temperature
    async def changed_mode(*args, **kwargs):
        await original(*args, **kwargs)
        cm.ha.climate['climate.maple']['state']='cool'
    cm.ha.set_temperature=changed_mode
    queue_tick(cm, at('2030-06-13T10:00'))
    assert cm.db.one('SELECT result FROM thermostat_daily_checks')['result'] == 'unconfirmed'


def test_setup_removes_extra_pause_field_and_keeps_legacy_value_on_save(cm):
    cm.db.set_setting('temperature_pause_field_id','167')
    client = TestClient(create_app(cm.s), follow_redirects=False)
    html = client.get('/setup').text
    assert 'name="temperature_pause_field_id"' not in html
    assert 'Optional extra Hostaway pause field number' not in html
    assert 'NTC' in html
    values = {k:str(v) for k,v in DEFAULTS.items() if k != 'temperature_pause_field_id'}
    assert client.post('/daily-temperature-settings-save',data=values).status_code == 303
    assert cm.db.get_setting('temperature_pause_field_id') == '167'
    assert '/sheet-settings-save' not in html and 'sheet_endpoint' not in html
