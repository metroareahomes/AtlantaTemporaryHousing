"""Weekend pricing previews and the explicitly triggered Wright trial."""
import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from starlette.datastructures import FormData

TZ = ZoneInfo('America/New_York')
MONDAY_START = '12:00'
MONDAY_REFRESH = '02:15'
MONDAY_END = '17:00'
DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
DEFAULTS = {'pricing_discount': '10', 'pricing_threshold': '200', 'pricing_window': '24',
            'pricing_property': '0', 'pricing_start_day': '4', 'pricing_start_time': '14:00',
            'pricing_check1_day': '5', 'pricing_check1_time': '06:00',
            'pricing_check2_day': '6', 'pricing_check2_time': '06:00',
            'pricing_end_day': '0', 'pricing_end_time': '00:01'}


def monday_phase(local):
    clock = local.strftime('%H:%M')
    if local.weekday() == 6 and clock >= MONDAY_START:
        return 'initial'
    if local.weekday() == 0 and clock < MONDAY_END:
        return 'refresh' if clock >= MONDAY_REFRESH else 'initial'
    return None


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


def manual_settings(form):
    discount = str(form.get('pricing_manual_discount', '')).strip()
    minimums = []
    try:
        if discount:
            percent = money(discount)
            if not 0 < percent <= 90 or percent != percent.quantize(Decimal('.01')):
                raise ValueError
    except (InvalidOperation, ValueError):
        raise ValueError('Enter a discount above 0 and up to 90 percent, with at most two decimals') from None
    for day in range(3):
        value = str(form.get(f'pricing_minimum_{day}', '')).strip()
        if value and (not re.fullmatch(r'[0-9]{1,2}', value) or int(value) < 1):
            raise ValueError('Enter a minimum stay from 1 to 99 nights, or leave it blank')
        minimums.append(int(value) if value else None)
    if not discount and all(value is None for value in minimums):
        raise ValueError('Enter a discount or a minimum stay to preview')
    return {'property': str(form.get('pricing_property', '')), 'discount': discount, 'minimums': minimums}


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


def vacancy_issue(day):
    if day.get('reservations') or any(
        day.get(key) not in (None, 0, '0') for key in (
            'countPendingUnits', 'countReservedUnits', 'countBlockingReservations', 'countBlockedUnits',
        )
    ):
        return 'Reservation / blocking inventory present'
    status = str(day.get('status') or '').strip().lower()
    availability = day.get('isAvailable')
    if status in ('reserved', 'pending', 'blocked'):
        return f'Hostaway marks this night {status}'
    if availability in (0, '0'):
        return 'Hostaway marks this night unavailable'
    # Hostaway makes isAvailable optional. Only an explicit available status
    # can establish vacancy when that flag is omitted.
    if status != 'available' or availability not in (None, 1, '1'):
        return 'Night not confirmed available: incomplete or unclear Hostaway data'
    return None


def plan(calendar, listing, at, cfg, manual=None):
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
    thursday_special = bool(manual and manual.get('discount_all_dates'))
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
        offset = (checkin.date() - at.astimezone(TZ).date()).days
        inventory_fields_present = any(day.get(key) is not None for key in (
            'countAvailableUnits', 'availableUnitsToSell', 'desiredUnitsToSell',
        ))
        if manual is None and not active:
            row['reason'] = 'Outside promotion window'
        elif manual is None and not at.astimezone(timezone.utc) <= checkin.astimezone(timezone.utc) <= cutoff:
            row['reason'] = 'Check-in outside next-hours window'
        elif manual is None and checkin >= end:
            row['reason'] = 'Check-in after promotion stops'
        elif issue := vacancy_issue(day):
            row['reason'] = issue
        elif not thursday_special and day.get('closedOnArrival') not in (0, False):
            row['reason'] = 'Arrival closed or unknown'
        elif inventory_fields_present and not thursday_special:
            row['reason'] = 'Multi-unit inventory requires separate review'
        elif manual is not None:
            minimum = manual['minimums'][offset]
            if minimum is not None:
                row['proposed_minimum_stay'] = minimum
            if (offset == 0 or manual.get('discount_all_dates')) and manual['discount']:
                proposed = (current * (1 - money(manual['discount']) / 100)).quantize(
                    Decimal('.01'), rounding=ROUND_HALF_UP
                )
                if 0 < proposed < current:
                    row['proposed'] = str(proposed)
            row['reason'] = ('One-time preview' if row['proposed'] or minimum is not None
                             else 'No change proposed')
            if thursday_special:
                if inventory_fields_present:
                    row['reason'] += ' · Inventory counts reported; verify unit setup before applying'
                arrival = day.get('closedOnArrival')
                if arrival in (1, '1'):
                    row['reason'] += ' · Hostaway reports arrivals closed'
                elif arrival not in (0, '0'):
                    row['reason'] += ' · Arrival status unconfirmed'
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

    async def preview(self, at, *, manual=None):
        if self._lock.locked():
            raise ValueError('A pricing simulation is already running')
        async with self._lock:
            now = self.s.now()
            if not self.db.get_bool('pricing_simulation_enabled'):
                raise ValueError('Manual pricing simulations are disabled')
            if self._last_attempt and now - self._last_attempt < timedelta(seconds=30):
                raise ValueError('Wait 30 seconds before another pricing read')
            self._last_attempt = now
            saved_config = read_config(self.db)
            cfg = dict(saved_config)
            if manual is not None:
                cfg['pricing_property'] = manual['property']
            cfg = validate(cfg, self.db)
            prop = self.db.one('SELECT * FROM properties WHERE id=? AND active=1', (int(cfg['pricing_property']),))
            if at.tzinfo is None or abs((at.astimezone(timezone.utc)-now.astimezone(timezone.utc)).total_seconds()) > 14*86400:
                raise ValueError('Choose a simulation time within 14 days of now')
            try:
                # The existing deployment's authenticated client supplies credentials; no new secret.
                listing = await self.s.hostaway.pricing_listing(prop['hostaway_listing_id'])
                last_date = (at.astimezone(timezone.utc) + timedelta(hours=int(cfg['pricing_window']))).astimezone(TZ).date()
                if manual is not None:
                    last_date = at.astimezone(TZ).date() + timedelta(days=2)
                calendar = await self.s.hostaway.pricing_calendar(prop['hostaway_listing_id'], at.astimezone(TZ).date().isoformat(), last_date.isoformat())
                rows = plan(calendar, listing, at, cfg, manual)
            except Exception as exc:
                self.db.log('pricing.preview_failed', 'Pricing simulation could not read or validate calendar data; no prices changed.', level='warning', property_id=prop['id'])
                raise ValueError('Pricing read/validation failed. Confirm listing currency, time zone, check-in hour and calendar API access. No prices changed.') from None
            # Do not save a preview under settings or a test property changed during the read.
            current_property = self.db.one('SELECT hostaway_listing_id FROM properties WHERE id=? AND active=1', (prop['id'],))
            if (not self.db.get_bool('pricing_simulation_enabled') or read_config(self.db) != saved_config
                    or not current_property or current_property['hostaway_listing_id'] != prop['hostaway_listing_id']):
                raise ValueError('Pricing settings changed during the read; run a new simulation')
            rid = self.db.execute('INSERT INTO pricing_simulations(created_at,property_id,simulation_at,config_json,rows_json) VALUES(?,?,?,?,?)',
                                 (self.s.now().isoformat(), prop['id'], at.isoformat(), json.dumps(cfg if manual is None else {'one_time': manual}), json.dumps(rows)))
            self.db.log('pricing.simulated', f'Pricing simulation saved: {len(rows)} dates reviewed; no prices changed.', property_id=prop['id'])
            return rid

    async def thursday_special(self, form, *, applying=False, weekday=3):
        """Preview the current or coming special without changing the calendar."""
        special_day = "Thursday" if weekday == 3 else "Monday"
        owner = form.get('special_owner', '')
        if owner not in ('', 'Kurt', 'Mike'):
            raise ValueError('Choose a valid owner')
        selected = form.getlist('special_property')
        if any(not re.fullmatch(r'[1-9][0-9]{0,12}', value) for value in selected):
            raise ValueError('Choose valid properties')
        selected = {int(value) for value in selected}
        try:
            discount = money(form.get('special_discount', ''))
            floor_input = form.get('special_floor', '').strip()
            floor = money(floor_input) if floor_input else None
            if not 0 < discount <= 90 or discount != discount.quantize(Decimal('.01')):
                raise ValueError
        except (ValueError, InvalidOperation):
            raise ValueError('Enter a discount above 0 and up to 90%, and a valid optional price threshold') from None
        if not selected and floor is None:
            raise ValueError(f'Check at least one home or enter a {special_day} price threshold')
        if self._lock.locked():
            raise ValueError('A pricing preview is already running')
        async with self._lock:
            now = self.s.now()
            if not applying:
                if self._last_attempt and now - self._last_attempt < timedelta(seconds=30):
                    raise ValueError('Wait 30 seconds before another pricing read')
                self._last_attempt = now
            today = now.astimezone(TZ).date()
            start_date = today + timedelta(days=(weekday - today.weekday()) % 7)
            end_date = start_date + timedelta(days=2)
            properties = self.db.query(
                'SELECT id,name,address,hostaway_listing_id FROM properties WHERE active=1 '
                "AND hostaway_listing_id IS NOT NULL AND (? = '' OR owner_scope = ?) ORDER BY name, id",
                (owner, owner),
            )
            if not selected <= {prop['id'] for prop in properties}:
                raise ValueError('A selected property is outside the chosen owner or no longer active')
            results = []
            errors = []
            rule = {'discount': str(discount), 'minimums': [3, None, None] if weekday == 3 else [3, 3, 3], 'discount_all_dates': True}
            at = datetime.combine(start_date, datetime.min.time(), TZ)
            for prop in properties:
                checked = prop['id'] in selected
                if not checked and floor is None:
                    continue
                listing_id = prop['hostaway_listing_id']
                name = prop['name']
                if prop['address'] and prop['address'] != name:
                    name += f" ({prop['address']})"
                try:
                    calendar = await self.s.hostaway.pricing_calendar(
                        listing_id, start_date.isoformat(), end_date.isoformat()
                    )
                    if not checked:
                        first_night_rate = next(day['price'] for day in calendar if day['date'] == start_date.isoformat())
                        if weekday == 0:
                            previous = json.loads(self.db.get_setting(
                                f'pricing_monday_special_{listing_id}_{start_date.isoformat()}', '{}'
                            ))
                            if previous.get('status') == 'verified':
                                last_price = previous['requested'][0]['price']
                                if money(first_night_rate) == money(last_price):
                                    first_night_rate = previous['before'][0]['current']
                        if money(first_night_rate) <= floor:
                            continue
                    listing = await self.s.hostaway.pricing_listing(listing_id)
                    rows = plan(calendar, listing, at, DEFAULTS, rule)
                    expected_dates = {(start_date + timedelta(days=offset)).isoformat() for offset in range(3)}
                    if {row['date'] for row in rows} != expected_dates or any(vacancy_issue(day) for day in calendar):
                        for row in rows:
                            row['proposed'] = ''
                            row.pop('proposed_minimum_stay', None)
                            if row['reason'].startswith('One-time preview'):
                                row['reason'] = ('Skipped because the full weekend is not vacant' if weekday == 3
                                                 else 'Skipped because all three nights are not vacant')
                        results.append({'name': name, 'rows': rows, 'ineligible': True})
                        continue
                except Exception:
                    errors.append(name)
                    continue
                results.append({'id': str(prop['id']), 'name': name, 'rows': rows})
            return {special_day.lower(): start_date.isoformat(), 'properties': results, 'errors': errors}

    def save_thursday_schedule(self, form, *, weekday=3):
        special_day = "Thursday" if weekday == 3 else "Monday"
        clock = form.get('special_time', '') if weekday == 3 else MONDAY_START
        if not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', clock):
            raise ValueError(f'Enter a {special_day} activation time')
        enabled = form.get('special_enabled') == 'on'
        owner = form.get('special_owner', '')
        selected = form.getlist('special_property')
        discount = form.get('special_discount', '').strip()
        floor = form.get('special_floor', '').strip()
        try:
            rate = money(discount)
            if not 0 < rate <= 90 or rate != rate.quantize(Decimal('.01')):
                raise ValueError
            if floor:
                money(floor)
        except (ValueError, InvalidOperation):
            raise ValueError(f'Enter a valid discount and optional {special_day} rate threshold') from None
        if owner not in ('', 'Kurt', 'Mike') or any(
            not re.fullmatch(r'[1-9][0-9]{0,12}', item) for item in selected
        ):
            raise ValueError('Choose a valid owner and properties')
        if enabled and not (selected or floor):
            raise ValueError(f'Check a home or enter a {special_day} rate threshold before enabling')
        config = {
            'clock': clock, 'enabled': enabled, 'owner': owner,
            'selected': selected, 'discount': discount, 'floor': floor,
        }
        self.db.set_setting(f'{special_day.lower()}_special_schedule:{owner}', json.dumps(config))
        return config

    def thursday_schedule(self, owner, *, weekday=3):
        special_day = "thursday" if weekday == 3 else "monday"
        return json.loads(self.db.get_setting(f'{special_day}_special_schedule:{owner}', '{}'))

    async def tick_thursday_schedule(self, *, weekday=3):
        local = self.s.now().astimezone(TZ)
        if weekday == 0 and local.weekday() == 0 and local.strftime('%H:%M') >= MONDAY_END:
            await self.restore_monday_special(local.date().isoformat())
            return
        if (weekday == 3 and local.weekday() != 3) or (weekday == 0 and not monday_phase(local)):
            return
        for owner in ('Mike', 'Kurt'):
            await self.run_due_thursday_schedule(local, owner, weekday=weekday)

    async def run_due_thursday_schedule(self, local, owner, *, weekday=3):
        special_day = "thursday" if weekday == 3 else "monday"
        config = self.thursday_schedule(owner, weekday=weekday)
        phase = monday_phase(local) if weekday == 0 else None
        if not config.get('enabled') or (weekday == 3 and config['clock'] != local.strftime('%H:%M')):
            return
        if weekday == 0 and not phase:
            return
        date = local.date()
        if weekday == 0 and local.weekday() == 6:
            date += timedelta(days=1)
        run_date = date.isoformat() if weekday == 3 else f'{date.isoformat()}:{phase}'
        if self._lock.locked() or self.db.get_setting(f'{special_day}_special_last_run:{owner}') == run_date:
            return
        self.db.set_setting(f'{special_day}_special_last_run:{owner}', run_date)
        form = FormData([
            ('special_owner', config['owner']), ('special_discount', config['discount']),
            ('special_floor', config['floor']),
            *(('special_property', item) for item in config['selected']),
        ])
        try:
            reports = await (self.apply_selected_special(form) if weekday == 3
                             else self.apply_selected_special(form, weekday=weekday))
        except ValueError as exc:
            reports = [str(exc)]
        for report in reports:
            self.db.log(f'pricing.{special_day}_special', report)

    async def apply_selected_special(self, form, *, weekday=3):
        special_day = "Thursday" if weekday == 3 else "Monday"
        local = self.s.now().astimezone(TZ)
        if weekday == 3 and local.weekday() != 3:
            raise ValueError(f'The {special_day} Special can be applied on {special_day} Eastern time')
        if weekday == 0 and not monday_phase(local):
            raise ValueError('The Monday Special runs from Sunday noon to Monday 5 PM Eastern')
        result = await self.thursday_special(form, applying=True, weekday=weekday)
        checked = set(form.getlist('special_property'))
        reports = [f"{name}: Calendar could not be verified" for name in result['errors']]
        for prop in result['properties']:
            if prop.get('ineligible'):
                reports.append(f"{prop['name']}: Skipped; all three nights must be vacant")
                continue
            try:
                rates = await self.apply_wright_special(
                    prop['id'], bulk=True, discount=form['special_discount'].strip(),
                    floor=None if prop['id'] in checked else money(form['special_floor']),
                    owner=form.get('special_owner', ''), expected_date=result[special_day.lower()], weekday=weekday,
                )
                minimum = "Thursday" if weekday == 3 else "each night"
                reports.append(f"{prop['name']}: Verified in Hostaway — {rates}; {minimum} minimum 3 nights")
            except ValueError as exc:
                reports.append(f"{prop['name']}: {exc}")
        return reports or ['No homes qualified']

    async def apply_wright_special(self, property_id, *, bulk=False, discount='20', floor=None, owner='', expected_date=None, weekday=3):
        if self._lock.locked():
            raise ValueError('A pricing action is already running')
        async with self._lock:
            local = self.s.now().astimezone(TZ)
            today = local.date()
            phase = monday_phase(local) if weekday == 0 else None
            special_day = 'Thursday' if weekday == 3 else 'Monday'
            if weekday == 3 and today.weekday() != 3:
                raise ValueError('The live Wright trial can be run on Thursday Eastern time')
            if weekday == 0 and (not bulk or not phase):
                raise ValueError('The Monday Special runs from Sunday noon to Monday 5 PM Eastern')
            matches = [
                prop for prop in self.db.query(
                    "SELECT id,name,address,hostaway_listing_id FROM properties "
                    "WHERE active=1 AND owner_scope='Kurt' AND hostaway_listing_id IS NOT NULL"
                )
                if re.search(r"\bwright\b", f"{prop['name']} {prop['address'] or ''}", re.IGNORECASE)
            ]
            if bulk:
                matches = self.db.query(
                    "SELECT id,hostaway_listing_id FROM properties WHERE active=1 "
                    "AND hostaway_listing_id IS NOT NULL AND id=? AND (?='' OR owner_scope=?)",
                    (int(property_id), owner, owner),
                )
            if len(matches) != 1 or str(matches[0]['id']) != property_id:
                raise ValueError('The selected listing could not be verified')
            listing_id = matches[0]['hostaway_listing_id']
            target = today + timedelta(days=1) if weekday == 0 and today.weekday() == 6 else today
            start = target.isoformat()
            if expected_date is not None and start != expected_date:
                raise ValueError(f'The target {special_day} has changed')
            dates = [(target + timedelta(days=offset)).isoformat() for offset in range(3)]
            end = dates[-1]
            key_prefix = 'pricing_wright_trial' if weekday == 3 else 'pricing_monday_special'
            key = f'{key_prefix}_{listing_id}_{start}'
            previous = json.loads(self.db.get_setting(key, '{}'))
            if previous and (weekday == 3 or previous.get('phase') == phase or previous['status'] != 'verified'):
                raise ValueError('A write was already attempted for this run. Check the Hostaway calendar.')
            try:
                listing = await self.s.hostaway.pricing_listing(listing_id)
                calendar = await self.s.hostaway.pricing_calendar(listing_id, start, end)
                rule = {'discount': discount, 'minimums': [3, None, None] if weekday == 3 else [3, 3, 3], 'discount_all_dates': True}
                baseline = calendar
                if previous:
                    before = {row['date']: row for row in previous['before']}
                    applied = {item['startDate']: item for item in previous['requested']}
                    baseline = []
                    for day in calendar:
                        date = day['date']
                        item = applied.get(date)
                        original = before.get(date)
                        day = dict(day)
                        if item and original:
                            if money(day['price']) == money(item['price']):
                                day['price'] = original['current']
                            if str(day.get('minimumStay')) == str(item['minimumStay']):
                                day['minimumStay'] = original['minimum_stay']
                        baseline.append(day)
                at = datetime.combine(target, datetime.min.time(), TZ)
                rows = plan(baseline, listing, at, DEFAULTS, rule)
            except Exception:
                raise ValueError('Could not verify the calendar. No updates were sent.') from None
            if floor is not None and not any(
                row['date'] == start and row['current'] and money(row['current']) > floor for row in rows
            ):
                raise ValueError(f'{special_day} rate no longer meets the threshold')
            if sorted(row['date'] for row in rows) != dates or any(
                vacancy_issue(day) for day in calendar
            ) or any(not row['proposed'] for row in rows):
                raise ValueError('All three nights must have a valid rate and be confirmed vacant')
            current = self.s.now().astimezone(TZ)
            if current.date() != today or (weekday == 0 and monday_phase(current) != phase):
                raise ValueError('The Eastern calendar date changed. No updates were sent.')
            intervals = []
            for row in sorted(rows, key=lambda row: row['date']):
                date = row['date']
                item = {'startDate': date, 'endDate': date, 'price': float(row['proposed'])}
                if date == start or weekday == 0:
                    item['minimumStay'] = 3
                intervals.append(item)
            # Persist the intent before sending. A timeout can mean the write succeeded.
            record = {'status': 'attempted', 'before': rows, 'requested': intervals}
            if weekday == 0:
                record.update(phase=phase, listing_id=listing_id, start=start, end=end)
            self.db.set_setting(key, json.dumps(record))
            try:
                await self.s.hostaway.apply_calendar_intervals(listing_id, intervals)
                after = await self.s.hostaway.pricing_calendar(listing_id, start, end)
                actual = {day['date']: day for day in after}
                if len(after) != 3 or any(
                    money(actual[item['startDate']]['price']) != money(item['price'])
                    or ('minimumStay' in item and actual[item['startDate']]['minimumStay'] not in (3, '3'))
                    for item in intervals
                ):
                    raise ValueError
            except Exception:
                raise ValueError('The update was attempted but could not be verified. Check the calendar in Hostaway before making further changes.') from None
            record['status'] = 'verified'
            self.db.set_setting(key, json.dumps(record))
            return ', '.join(f"{item['startDate']} at ${item['price']:.2f}" for item in intervals)


    async def restore_monday_special(self, start):
        if self._lock.locked():
            return
        async with self._lock:
            records = self.db.query(
                "SELECT key,value FROM settings WHERE key LIKE 'pricing_monday_special_%' AND key LIKE ?",
                (f'%_{start}',),
            )
            for entry in records:
                record = json.loads(entry['value'])
                if record.get('status') not in ('verified', 'attempted'):
                    continue
                try:
                    listing_id = record['listing_id']
                    calendar = await self.s.hostaway.pricing_calendar(listing_id, start, record['end'])
                    current = {row['date']: row for row in calendar}
                    original = {row['date']: row for row in record['before']}
                    updates = []
                    for item in record['requested']:
                        date = item['startDate']
                        day = current[date]
                        before = original[date]
                        changes = {}
                        if money(day['price']) == money(item['price']):
                            changes['price'] = float(money(before['current']))
                        minimum = before['minimum_stay']
                        if str(day.get('minimumStay')) == '3' and str(minimum).isdigit() and int(minimum) != 3:
                            changes['minimumStay'] = int(minimum)
                        if changes:
                            updates.append({'startDate': date, 'endDate': date, **changes})
                    if updates:
                        await self.s.hostaway.apply_calendar_intervals(listing_id, updates)
                        after = {day['date']: day for day in await self.s.hostaway.pricing_calendar(
                            listing_id, start, record['end']
                        )}
                        for item in updates:
                            day = after[item['startDate']]
                            if ('price' in item and money(day['price']) != money(item['price'])) or (
                                'minimumStay' in item and str(day['minimumStay']) != str(item['minimumStay'])
                            ):
                                raise ValueError('Restore not verified')
                    record['status'] = 'closed'
                    self.db.set_setting(entry['key'], json.dumps(record))
                    self.db.log('pricing.monday_special', f'Listing {listing_id}: Monday Special ended.')
                except Exception:
                    self.db.log('pricing.monday_special', 'Could not verify Monday Special cleanup; will retry.')
