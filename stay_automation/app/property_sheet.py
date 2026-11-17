"""One-way, signed snapshots of cached status. Never calls a device or writes automation data."""
import asyncio
import hashlib
import hmac
import json
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import httpx

HEADERS = ['Owner', 'Property', 'Address', 'Occupancy', 'Check-in / next arrival', 'Checkout',
           'Occupied days', 'Days remaining', 'Lock status', 'Lowest lock battery %',
           'Thermostat readings', 'Needs attention', 'Issues', 'Lock read at',
           'Thermostat seen at', 'Hostaway sync at', 'Snapshot at']


def valid_endpoint(url):
    return bool(re.fullmatch(r'https://script\.google\.com/macros/s/[A-Za-z0-9_-]+/exec', url))


def safe_cell(value):
    # Defense in depth: sheet text must never become a formula.
    text = str(value or '')[:4000]
    return "'" + text if text.lstrip().startswith(('=', '+', '-', '@')) else text


def snapshot(syncer):
    from .web import dashboard_rows, missing_code_warning_due
    from .alerts import AlertService
    from .locks import DEFAULTS
    from .reservations import within
    now, db = syncer.now(), syncer.db
    sync_at = db.get_setting('last_sync_at', '') or ''
    if not sync_at:
        raise ValueError('Waiting for the first successful Hostaway sync')
    def stale(stamp, minutes):
        if not stamp:
            return True
        parsed = datetime.fromisoformat(stamp)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=syncer.tz)
        return now - parsed > timedelta(minutes=minutes)
    booking_stale = stale(sync_at, max(45, syncer.settings.sync_minutes * 3))
    rows = []
    owners = AlertService(syncer)
    for board in dashboard_rows(syncer):
        p = db.one('SELECT * FROM properties WHERE id=?', (board['id'],))
        reservations = syncer.reservations_for(p['id'])
        current = next((r for r in reservations if r['active'] and datetime.fromisoformat(r['check_in_at']) <= now < datetime.fromisoformat(r['check_out_at'])), None)
        upcoming = next((r for r in reservations if r['active'] and datetime.fromisoformat(r['check_in_at']) > now), None)
        r = current or upcoming
        def stamp(key):
            return datetime.fromisoformat(r[key]).astimezone(syncer.tz).isoformat(timespec='minutes') if r else ''
        locks = board['locks']
        therms = db.query('SELECT * FROM thermostats WHERE property_id=? ORDER BY id', (p['id'],))
        issues, lock_status, readings = [], [], []
        owner = owners.context(p['id'])['owner'] or 'Unconfirmed'
        if owner == 'Unconfirmed':
            issues.append('Owner tag unconfirmed')
        if booking_stale:
            issues.append('Hostaway data stale; occupancy may be outdated')
        for index, lock in enumerate(locks, 1):
            label = f'Lock {index}'
            state = lock['state'] or 'unknown'
            lock_status.append(f'{label}: {state}')
            if state in ('unavailable', 'unknown'):
                issues.append(f'{label} offline / unknown')
            if lock.get('codes_error'):
                issues.append(f'{label} code read failed')
            if p['lock_automation'] and lock.get('fail_count'):
                issues.append(f'{label} access verification failed ({lock["fail_count"]} tries)')
            if lock['battery'] is not None and lock['battery'] < db.get_int('battery_alert_percent', 20):
                issues.append(f'{label} battery {lock["battery"]}%')
        lock_times = [l['codes_read_at'] for l in locks if l['codes_read_at']]
        if locks and (len(lock_times) < len(locks) or any(stale(t, 720) for t in lock_times)):
            issues.append('Lock readings stale / not yet read')
        if p['lock_automation'] and not locks:
            issues.append('Lock not assigned')
        cfg = {k: db.get_int(k, DEFAULTS[k]) for k in ('add_hour', 'early_lead_hours', 'final_check_minutes')}
        if board['code'] == 'missing' and within(upcoming, now, 36) and missing_code_warning_due(upcoming, now, **cfg):
            issues.append('Upcoming guest code missing')
        if board['code'] == 'no_code' and within(upcoming, now, 36):
            issues.append('Upcoming guest code missing from Hostaway')
        for index, t in enumerate(therms, 1):
            label = f'Thermostat {index}'
            target = t['target_temp'] if t['target_temp'] is not None else f'{t["target_low"]}–{t["target_high"]}'
            readings.append(f'{label}: {t["state"] or "unknown"}; room {t["current_temp"]}; target {target}')
            if t['state'] in (None, 'unavailable', 'unknown'):
                issues.append(f'{label} offline / unknown')
            if t['fail_count']:
                issues.append(f'{label} last adjustment unconfirmed ({t["fail_count"]} tries)')
            if stale(t['seen_at'], 30):
                issues.append(f'{label} cached reading stale')
        if p['thermostat_automation'] and not therms:
            issues.append('Thermostat not assigned')
        elapsed = round((now - datetime.fromisoformat(current['check_in_at'])).total_seconds()/86400, 1) if current else ''
        remaining = round((datetime.fromisoformat(current['check_out_at']) - now).total_seconds()/86400, 1) if current else ''
        raw = [owner, p['name'], p['address'], 'Occupied' if current else 'Vacant', stamp('check_in_at'), stamp('check_out_at'),
               elapsed, remaining, '; '.join(lock_status) or 'Not assigned', board['battery'] if board['battery'] is not None else '',
               '; '.join(readings) or 'Not assigned', 'Yes' if issues else 'No', '; '.join(issues) or 'None',
               min(lock_times) if lock_times else '', min((t['seen_at'] or '' for t in therms), default=''), sync_at,
               now.isoformat(timespec='seconds')]
        rows.append([v if isinstance(v, (int, float)) else safe_cell(v) for v in raw])
    if not rows:
        raise ValueError('No active homes; keeping the existing sheet unchanged')
    rows.sort(key=lambda r: (r[11] != 'Yes', str(r[1]).casefold()))
    return {'schema': 1, 'headers': HEADERS, 'rows': rows}


class PropertySheet:
    def __init__(self, syncer, *, transport=None):
        self.s, self.db, self.transport = syncer, syncer.db, transport
        self._lock = asyncio.Lock()

    async def push(self):
        if self._lock.locked() or not self.db.get_bool('sheet_enabled'):
            return False
        async with self._lock:
            endpoint = self.db.get_setting('sheet_endpoint', '') or ''
            secret = self.db.get_setting('sheet_secret', '') or ''
            if not valid_endpoint(endpoint) or len(secret) < 32:
                self.db.set_setting('sheet_last_error', 'Sheet setup incomplete; check receiver address and connection key')
                return False
            try:
                payload = snapshot(self.s)
                data = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
                sent = str(int(time.time()))
                signature = hmac.new(secret.encode(), (sent + '\n' + data).encode(), hashlib.sha256).hexdigest()
                async with httpx.AsyncClient(timeout=30, follow_redirects=False, transport=self.transport) as client:
                    response = await client.post(endpoint, json={'timestamp': sent, 'data': data, 'signature': signature})
                    # Google's ContentService redirects the response body; never replay the POST.
                    if response.status_code in (302, 303):
                        location = response.headers.get('location', '')
                        parsed = urlsplit(location)
                        if parsed.scheme != 'https' or parsed.netloc != 'script.googleusercontent.com':
                            raise ValueError('Google receiver redirected to sign-in or an unexpected address')
                        response = await client.get(location)
                    response.raise_for_status()
                    result = response.json()
                    digest = hashlib.sha256(data.encode()).hexdigest()
                    if result.get('ok') is not True or result.get('digest') != digest or result.get('rows') != len(payload['rows']):
                        raise ValueError('Sheet did not confirm this snapshot; check receiver deployment and connection key')
                self.db.set_settings({'sheet_last_success': self.s.now().astimezone(timezone.utc).isoformat(timespec='seconds'),
                                      'sheet_last_error': ''})
                return True
            except Exception:
                # Raw HTTP errors can contain the private receiver URL. Never log them.
                self.db.set_setting('sheet_last_error', 'Sheet update not confirmed. Check the receiver deployment and connection key; automatic retry in five minutes. Existing rows retain their original timestamps.')
                return False
