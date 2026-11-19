"""Read-only, single-property weekend pricing simulations. No calendar-write capability."""
import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from zoneinfo import ZoneInfo

TZ = ZoneInfo('America/New_York')
DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
DEFAULTS = {'pricing_discount': '10', 'pricing_threshold': '200', 'pricing_window': '24',
            'pricing_property': '0', 'pricing_start_day': '4', 'pricing_start_time': '14:00',
            'pricing_check1_day': '5', 'pricing_check1_time': '06:00',
            'pricing_check2_day': '6', 'pricing_check2_time': '06:00',
            'pricing_end_day': '0', 'pricing_end_time': '00:01'}


def money(value):
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError('Invalid rate')
    return result


def read_config(db):
    return {k: db.get_setting(k, v) or v for k, v in DEFAULTS.items()}


def validate(values, db, *, require_property=True):
    result = {k: str(values.get(k, '')).strip() for k in DEFAULTS}
    for key, low, high in [('pricing_discount', Decimal('0.01'), Decimal('90')),
                           ('pricing_threshold', Decimal('0'), Decimal('10000'))]:
        try:
            value = money(result[key])
            if not low <= value <= high or value != value.quantize(Decimal('.01')):
                raise ValueError()
        except (InvalidOperation, ValueError):
            raise ValueError(f'{key}: enter a number between {low} and {high}, with at most two decimal places')
    if not result['pricing_window'].isdigit() or not 1 <= int(result['pricing_window']) <= 72:
        raise ValueError('Check-in window must be a whole number from 1 to 72 hours')
    pid = result['pricing_property']
    if require_property and (not re.fullmatch(r'[1-9][0-9]{0,12}', pid) or not db.one('SELECT id FROM properties WHERE id=? AND active=1 AND hostaway_listing_id IS NOT NULL', (int(pid),))):
        raise ValueError('Choose one active test property')
    if not re.fullmatch(r'[0-9]{1,13}', pid):
        raise ValueError('Choose one test property or leave it unselected')
    for event in ('start', 'check1', 'check2', 'end'):
        if result[f'pricing_{event}_day'] not in [str(d) for d in range(7)]:
            raise ValueError('Choose a valid weekday')
        if not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', result[f'pricing_{event}_time']):
            raise ValueError('Use a time such as 06:00 or 14:00')
    offsets = [event_offset(result, name) for name in ('check1', 'check2', 'end')]
    if not 0 < offsets[0] < offsets[1] < offsets[2] < 7*1440:
        raise ValueError('Checks and stop must follow the start in order within one week')
    return result


def minutes(clock):
    h, m = map(int, clock.split(':'))
    return 60*h+m


def event_offset(cfg, event):
    return ((int(cfg[f'pricing_{event}_day'])-int(cfg['pricing_start_day'])) % 7)*1440 + minutes(cfg[f'pricing_{event}_time'])-minutes(cfg['pricing_start_time'])


def promotion_window(at, cfg):
    local = at.astimezone(TZ)
    start_date = local.date() - timedelta(days=(local.weekday()-int(cfg['pricing_start_day'])) % 7)
    start = datetime.combine(start_date, datetime.strptime(cfg['pricing_start_time'], '%H:%M').time(), TZ)
    if local < start:
        start -= timedelta(days=7)
    return start, start + timedelta(minutes=event_offset(cfg, 'end'))


def next_start(now, cfg):
    start, _ = promotion_window(now, cfg)
    if start < now:
        start += timedelta(days=7)
    return start.strftime('%Y-%m-%dT%H:%M')


def plan(calendar, listing, at, cfg):
    if listing.get('currencyCode') != 'USD':
        raise ValueError('Only a confirmed USD listing can be simulated')
    hour = listing.get('checkInTimeStart')
    if type(hour) is not int or not 0 <= hour <= 23:
        raise ValueError('Hostaway did not return a valid listing check-in hour')
    local_tz = ZoneInfo(listing.get('timeZoneName') or '')
    if str(local_tz) != str(TZ):
        raise ValueError('Test listing must use America/New_York; verify its Hostaway time zone')
    start, end = promotion_window(at, cfg)
    active = start <= at.astimezone(TZ) < end
    cutoff = at.astimezone(timezone.utc) + timedelta(hours=int(cfg['pricing_window']))
    rows, dates = [], set()
    for day in calendar:
        date = str(day.get('date', ''))
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', date) or date in dates:
            raise ValueError('Invalid or duplicate calendar date; no proposal produced')
        dates.add(date)
        checkin = datetime.combine(datetime.fromisoformat(date).date(), datetime.min.time(), local_tz).replace(hour=hour)
        row = {'date': date, 'checkin': checkin.isoformat(), 'current': '', 'proposed': '', 'reason': '',
               'minimum_stay': day.get('minimumStay'), 'processed': day.get('isProcessed'),
               'provider_updated': day.get('updatedAt', day.get('updatedOn')),
               'calendar_id': day.get('id')}
        try:
            current = money(day['price'])
            row['current'] = str(current)
        except (KeyError, InvalidOperation, ValueError, TypeError):
            row['reason'] = 'Missing / invalid nightly rate'
            rows.append(row)
            continue
        if not active:
            row['reason'] = 'Outside promotion window'
        elif not at.astimezone(timezone.utc) <= checkin.astimezone(timezone.utc) <= cutoff:
            row['reason'] = 'Check-in outside next-hours window'
        elif checkin >= end:
            row['reason'] = 'Check-in after promotion stops'
        elif day.get('status') != 'available' or day.get('isAvailable') != 1:
            row['reason'] = 'Night not confirmed available'
        elif day.get('closedOnArrival') not in (0, False):
            row['reason'] = 'Arrival closed or unknown'
        elif day.get('reservations') or any(day.get(k) not in (None, 0) for k in ('countPendingUnits', 'countReservedUnits', 'countBlockingReservations', 'countBlockedUnits')):
            row['reason'] = 'Reservation / blocking inventory present'
        elif any(day.get(k) is not None for k in ('countAvailableUnits', 'availableUnitsToSell', 'desiredUnitsToSell')):
            row['reason'] = 'Multi-unit inventory requires separate review'
        elif current <= money(cfg['pricing_threshold']):
            row['reason'] = 'Rate does not exceed threshold'
        else:
            row['proposed'] = str((current*(1-money(cfg['pricing_discount'])/100)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP))
            if money(row['proposed']) <= 0 or money(row['proposed']) >= current:
                row['proposed'] = ''
                row['reason'] = 'No positive lower rate after cent rounding'
            else:
                row['reason'] = 'Simulation only — calendar date, not first-night-only'
        rows.append(row)
    return rows


class PricingSimulator:
    def __init__(self, syncer):
        self.s, self.db = syncer, syncer.db
        self._lock = asyncio.Lock()
        self._last_attempt = None

    async def preview(self, at):
        if self._lock.locked():
            raise ValueError('A pricing simulation is already running')
        async with self._lock:
            now = self.s.now()
            if not self.db.get_bool('pricing_simulation_enabled'):
                raise ValueError('Manual pricing simulations are disabled')
            if self._last_attempt and now - self._last_attempt < timedelta(seconds=30):
                raise ValueError('Wait 30 seconds before another pricing read')
            self._last_attempt = now
            cfg = validate(read_config(self.db), self.db)
            prop = self.db.one('SELECT * FROM properties WHERE id=? AND active=1', (int(cfg['pricing_property']),))
            if at.tzinfo is None or abs((at.astimezone(timezone.utc)-now.astimezone(timezone.utc)).total_seconds()) > 14*86400:
                raise ValueError('Choose a simulation time within 14 days of now')
            try:
                # The existing deployment's authenticated client supplies credentials; no new secret.
                listing = await self.s.hostaway.pricing_listing(prop['hostaway_listing_id'])
                last_date = (at.astimezone(timezone.utc) + timedelta(hours=int(cfg['pricing_window']))).astimezone(TZ).date()
                calendar = await self.s.hostaway.pricing_calendar(prop['hostaway_listing_id'], at.astimezone(TZ).date().isoformat(), last_date.isoformat())
                rows = plan(calendar, listing, at, cfg)
            except Exception as exc:
                self.db.log('pricing.preview_failed', 'Pricing simulation could not read or validate calendar data; no prices changed.', level='warning', property_id=prop['id'])
                raise ValueError('Pricing read/validation failed. Confirm listing currency, time zone, check-in hour and calendar API access. No prices changed.') from None
            # Do not save a preview under settings or a test property changed during the read.
            current_property = self.db.one('SELECT hostaway_listing_id FROM properties WHERE id=? AND active=1', (prop['id'],))
            if (not self.db.get_bool('pricing_simulation_enabled') or read_config(self.db) != cfg
                    or not current_property or current_property['hostaway_listing_id'] != prop['hostaway_listing_id']):
                raise ValueError('Pricing settings changed during the read; run a new simulation')
            rid = self.db.execute('INSERT INTO pricing_simulations(created_at,property_id,simulation_at,config_json,rows_json) VALUES(?,?,?,?,?)',
                                 (self.s.now().isoformat(), prop['id'], at.isoformat(), json.dumps(cfg), json.dumps(rows)))
            self.db.log('pricing.simulated', f'Pricing simulation saved: {len(rows)} dates reviewed; no prices changed.', property_id=prop['id'])
            return rid
