"""Opt-in occupied setpoint limits and reservation-scoped, token-free complaint pauses."""
import asyncio
import html
import math
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

DEFAULTS = {"daily_winter": 70, "daily_summer": 73, "occupied_reset_hour": 11,
            "temperature_pause_field_id": 0}
RANGES = {"daily_winter": (50, 90), "daily_summer": (50, 90), "occupied_reset_hour": (0, 23),
          "temperature_pause_field_id": (0, 2147483647)}


def current_stay(reservations, now):
    return next((r for r in reservations if r["active"] and
                 datetime.fromisoformat(r["check_in_at"]) <= now < datetime.fromisoformat(r["check_out_at"])), None)


def complaint(body):
    """Return a matching sentence, never quoted email history or non-HVAC hot/cold words."""
    body = re.sub(r'<blockquote\b[^>]*>.*?</blockquote>', '', str(body), flags=re.I | re.S)
    body = html.unescape(re.sub(r'<[^>]+>', ' ', body)).replace('’', "'")
    body = re.split(r'\n\s*(?:On .+wrote:|From:|[- ]*Original Message[- ]*)', body, flags=re.I)[0]
    for sentence in re.split(r'[.!?\n]+', body):
        if sentence.lstrip().startswith('>'):
            continue
        s = re.sub(r'\s+', ' ', sentence.lower()).strip()
        if not s:
            continue
        # Remove unrelated objects before matching room comfort.
        s = re.sub(r'\b(?:hot water heater|water heater)\b', '', s)
        s = re.sub(r'\b(?:hot|cold|warm|cool)(?:\s+\w+){0,1}\s+(?:water|shower|tub|coffee|food|fridge|refrigerator|pool)\b', '', s)
        s = re.sub(r"\b(?:not|isn't|isnt|wasn't|wasnt)\s+(?:too |very )?(?:hot|cold|warm|cool)\b", '', s)
        if re.search(r'\b(?:fine now|comfortable now|temperature is fine|no (?:temperature|thermostat|ac) (?:issues|problems))\b', s):
            continue
        hvac = bool(re.search(r'\b(?:thermostat|thermostate|thermostat\w*|temp(?:erature)?|a[ /-]?c|air condition\w*|heat(?:ing|er)?|cooling|hvac)\b', s))
        override = r"\b(?:reset\w*|revert\w*|overrid\w*|overrode|by itself|on its own|keeps changing|go(?:es|ing) back)\b"
        stop = r"\b(?:stop|quit|leave|don't|do not|please don't|can you not)\b"
        if hvac and (re.search(override, s) or re.search(stop, s) and re.search(r'\b(?:chang\w*|adjust\w*|touch\w*|set\w*|alone|where)\b', s)):
            return sentence.strip()[:400]
        discomfort = r"\b(?:too (?:hot|cold|warm|cool)|freezing|sweating|sweltering|boiling|unbearabl\w*|uncomfortabl\w*)\b"
        if re.search(discomfort, s):
            return sentence.strip()[:400]
        if hvac and re.search(r"\b(?:not working|isn't working|isnt working|won't|wont|can't|cant|cannot|broken|stuck|unhappy|frustrat\w*|annoy\w*)\b", s):
            return sentence.strip()[:400]
    return None


def daily_call(t, target, summer):
    """Only move the seasonal setpoint in the energy-saving direction. Never switch mode."""
    mode = t.get('state')
    if mode not in (('cool', 'auto', 'heat_cool') if summer else ('heat', 'auto', 'heat_cool')):
        return None
    field = ('target_high' if summer else 'target_low') if mode in ('auto', 'heat_cool') else 'target_temp'
    seen = t.get(field)
    if seen is None or not math.isfinite(float(seen)):
        return None
    if (summer and float(seen) >= target) or (not summer and float(seen) <= target):
        return None
    if t.get('min_temp') is not None and target < t['min_temp'] or t.get('max_temp') is not None and target > t['max_temp']:
        return None  # do not invent a different policy when the device rejects this limit
    if mode in ('auto', 'heat_cool'):
        low, high = t.get('target_low'), t.get('target_high')
        if low is None or high is None:
            return None
        low, high = (float(low), float(target)) if summer else (float(target), float(high))
        if not math.isfinite(low) or not math.isfinite(high) or low >= high:
            return None
        return {'low': low, 'high': high}
    return {'temperature': float(target)}


class StayTemperaturePauses:
    def __init__(self, syncer, alert):
        self.s, self.db, self.alert = syncer, syncer.db, alert
        self._checked = {}
        self._failed_at = {}
        self._lock = asyncio.Lock()

    def paused(self, reservation_id):
        return bool(self.db.one('SELECT 1 FROM thermostat_stay_pauses WHERE reservation_id = ?', (reservation_id,)))

    def remember_field(self, raw):
        field_id = self.db.get_int('temperature_pause_field_id', 0)
        if not field_id:
            return
        for f in raw.get('customFieldValues') or []:
            if str(f.get('customFieldId')) == str(field_id) and str(f.get('value', '')).strip().lower() in ('1', 'true', 'yes', 'on', 'pause', 'paused'):
                self._pause(int(raw['id']), 'Hostaway field', 'Staff paused daily temperature resets in Hostaway.')

    def _pause(self, rid, source, reason):
        if self.paused(rid):
            return False
        self.db.execute('INSERT OR IGNORE INTO thermostat_stay_pauses(reservation_id, source, reason, paused_at) VALUES(?, ?, ?, ?)',
                        (rid, source, reason, self.s.now().isoformat()))
        self.db.log('thermostat.stay_paused', reason, reservation_id=rid)
        return True

    async def check(self, reservation, *, force=False):
        """Fail closed for daily resets only. Successful scans are cached five minutes."""
        rid = reservation['id']
        if self.paused(rid):
            return False
        now = self.s.now()
        old = self._checked.get(rid)
        if not force and old and old + timedelta(minutes=5) > now:
            return True
        async with self._lock:
            if self.paused(rid):
                return False
            try:
                raw = await self.s.hostaway.reservation(rid)
                if raw.get('accountId') is not None and str(raw['accountId']) != str(self.s.settings.hostaway_account_id):
                    raise ValueError('Reservation account mismatch')
                if int(raw['id']) != rid:
                    raise ValueError('Reservation ID mismatch')
                self.s.upsert_reservation(raw)
                if self.db.get_int('temperature_pause_field_id', 0) and not isinstance(raw.get('customFieldValues'), list):
                    raise ValueError('Hostaway did not supply custom fields')
                self.remember_field(raw)
                if self.paused(rid):
                    return False
                fresh = self.db.one('SELECT * FROM reservations WHERE id = ?', (rid,))
                if not current_stay([fresh], now) or fresh['listing_id'] != reservation['listing_id']:
                    return False
                if self.db.get_bool('temperature_complaints_enabled', True):
                    for msg in await self.s.hostaway.guest_messages(rid):
                        if not self._inbound(msg, fresh, now):
                            continue
                        match = complaint(msg.get('body', ''))
                        if match and self._pause(rid, 'Guest message', match):
                            home = self.db.one('SELECT name FROM properties WHERE hostaway_listing_id=?', (fresh['listing_id'],))
                            label = home['name'] if home else f'Reservation {rid}'
                            try:
                                await self.alert('Daily temperature reset paused',
                                                 f"{label} · reservation {rid}: {match}. Paused until checkout.",
                                                 key=f'temperature_pause:{rid}')
                            except Exception:
                                self.db.log('thermostat.pause_alert_failed', 'Could not send the pause alert; the pause is saved.',
                                            level='warning', reservation_id=rid)
                            return False
                self._checked[rid] = now
                return True
            except Exception as exc:
                # No locks, guest messages, arrival settings, or vacant control are disabled by this.
                last_error = self._failed_at.get(rid)
                if not last_error or last_error + timedelta(minutes=30) <= now:
                    self._failed_at[rid] = now
                    self.db.log('thermostat.pause_check_failed',
                                f'Daily reset skipped: could not check reservation {rid} ({type(exc).__name__}).',
                                level='warning', reservation_id=rid)
                return False

    def _inbound(self, msg, reservation, now):
        if msg.get('isIncoming') not in (1, True, '1') or msg.get('status', 'sent') != 'sent':
            return False
        if str(msg.get('reservationId')) != str(reservation['id']):
            return False
        if str(msg.get('accountId')) != str(self.s.settings.hostaway_account_id):
            return False
        if msg.get('listingMapId') is not None and str(msg['listingMapId']) != str(reservation['listing_id']):
            return False
        try:
            # Hostaway's timestamps are UTC unless an explicit offset is supplied.
            stamp = datetime.fromisoformat(msg.get('insertedOn') or msg.get('date') or '')
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=ZoneInfo('UTC'))
            return datetime.fromisoformat(reservation['check_in_at']) <= stamp <= now
        except (ValueError, TypeError):
            return False

    async def monitor(self):
        if not self.db.get_bool('occupied_reset_enabled'):
            return
        for p in self.db.query('SELECT id FROM properties WHERE active = 1 AND thermostat_automation = 1'):
            if not self.db.get_bool('occupied_reset_enabled'):
                break
            r = current_stay(self.s.reservations_for(p['id']), self.s.now())
            if r:
                await self.check(r)
