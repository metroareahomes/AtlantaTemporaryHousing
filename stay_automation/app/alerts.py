"""Owner-scoped staff alerts. Durable per-destination delivery; no guest messages."""
import asyncio
import hashlib
import html
import json
import re
import smtplib
import ssl
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email import policy
from urllib.parse import urlsplit

OWNERS = ('Mike', 'Kurt')
CATEGORIES = {'battery': 'Low lock batteries', 'locks': 'Lock failures / offline',
              'guest_access': 'Guest codes / backup access', 'thermostats': 'Thermostat failures / offline',
              'integration': 'Integration reauthorization', 'hostaway': 'Hostaway sync failures',
              'temperature_pauses': 'Occupied temperature pauses', 'arrivals': 'Daily arrivals',
              'extension_followups': 'Extension follow-up previews',
              'system': 'System / unassigned-owner alerts'}
SMTP_FIELDS = ('smtp_host', 'smtp_port', 'smtp_security', 'smtp_username', 'smtp_from', 'alert_properties_url')


def email_address(value):
    return bool(re.fullmatch(r'[^\s<>@,;\r\n]+@[^\s<>@,;\r\n]+\.[^\s<>@,;\r\n]+', value or ''))


def owner_tag(raw):
    """Exact tag names only. Never infer ownership from names, addresses or portfolio counts."""
    key = next((k for k in ('listingTags', 'reservationTags', 'tags') if k in raw), None)
    if key is None:
        return None
    names = set()
    def visit(value):
        if isinstance(value, str):
            for owner in OWNERS:
                if value.strip().casefold() == owner.casefold():
                    names.add(owner)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, dict):
            for k in ('name', 'tagName', 'tag', 'listingTag'):
                if k in value:
                    visit(value[k])
    visit(raw[key])
    return next(iter(names)) if len(names) == 1 else ''


class UncertainEmail(Exception):
    """Connection lost during DATA: server may have accepted it; never blindly replay."""


def send_smtp(cfg, destination, subject, text, event_key):
    msg = EmailMessage()
    msg['From'], msg['To'], msg['Subject'] = cfg['smtp_from'], destination, subject.replace('\r',' ').replace('\n',' ')[:200]
    msg['Message-ID'] = '<' + hashlib.sha256((event_key + destination).encode()).hexdigest() + '@' + cfg['smtp_from'].split('@')[-1] + '>'
    msg.set_content(text)
    msg.add_alternative('<html><body><pre style="white-space:pre-wrap;font-family:system-ui">' + html.escape(text) + '</pre><p><a href="' + html.escape(cfg['alert_properties_url'], quote=True) + '">Open Properties</a></p></body></html>', subtype='html')
    context = ssl.create_default_context()
    cls = smtplib.SMTP_SSL if cfg['smtp_security'] == 'ssl' else smtplib.SMTP
    kwargs = {'context': context} if cls is smtplib.SMTP_SSL else {}
    smtp = cls(cfg['smtp_host'], int(cfg['smtp_port']), timeout=30, **kwargs)
    try:
        smtp.ehlo()
        if cfg['smtp_security'] == 'starttls':
            smtp.starttls(context=context)
            smtp.ehlo()
        if cfg['smtp_username']:
            smtp.login(cfg['smtp_username'], cfg['smtp_password'])
        code, _ = smtp.mail(cfg['smtp_from'])
        if code != 250:
            raise smtplib.SMTPSenderRefused(code, 'Sender rejected', cfg['smtp_from'])
        code, _ = smtp.rcpt(destination)
        if code not in (250, 251):
            raise smtplib.SMTPRecipientsRefused({destination: (code, 'Recipient rejected')})
        try:
            smtp.data(msg.as_bytes(policy=policy.SMTP))
        except smtplib.SMTPDataError:
            raise  # explicit rejection is safe to retry
        except (OSError, smtplib.SMTPServerDisconnected) as exc:
            raise UncertainEmail('SMTP acceptance unknown after DATA') from exc
    finally:
        try:
            smtp.close()
        except OSError:
            pass
    # No recipient list is ever shared: one envelope and one To header per delivery.


class AlertService:
    def __init__(self, syncer):
        self.s, self.db, self.ha = syncer, syncer.db, syncer.ha
        self.send_email = send_smtp
        self._flush_locks = {'ha': asyncio.Lock(), 'email': asyncio.Lock()}

    def stamp(self):
        return self.s.now().astimezone(timezone.utc).isoformat(timespec='seconds')

    def context(self, pid):
        p = self.db.one('SELECT * FROM properties WHERE id=?', (pid,))
        if not p:
            return None
        now = self.s.now()
        reservations = self.s.reservations_for(pid)
        r = next((r for r in reservations if r['active'] and datetime.fromisoformat(r['check_in_at']) <= now < datetime.fromisoformat(r['check_out_at'])), None)
        owner = p.get('owner_scope', '')
        if r and r.get('owner_scope'):
            owner = owner or r['owner_scope']
            if p.get('owner_scope') and p['owner_scope'] != r['owner_scope']:
                owner = ''
        lines = [f"Home: {p['name']}", f"Address: {p['address'] or 'Address not available'}", f"Owner: {owner or 'UNCONFIRMED — check Hostaway tags'}"]
        if r:
            start, end = datetime.fromisoformat(r['check_in_at']), datetime.fromisoformat(r['check_out_at'])
            elapsed, remaining = max(0, (now-start).total_seconds()/3600), max(0, (end-now).total_seconds()/3600)
            lines += [f"Occupied: yes — {r['guest_name'] or 'Guest'} (reservation {r['id']})",
                      f"Check-in: {start.astimezone(self.s.tz):%a %b %d, %I:%M %p %Z}",
                      f"Occupied for: {elapsed/24:.1f} days ({elapsed:.1f} hours)",
                      f"Checkout: {end.astimezone(self.s.tz):%a %b %d, %I:%M %p %Z}",
                      f"Time remaining: {remaining/24:.1f} days ({remaining:.1f} hours)"]
        else:
            lines.append('Occupied: no')
            upcoming = next((r for r in reservations if r['active'] and datetime.fromisoformat(r['check_in_at']) > now), None)
            if upcoming:
                lines.append(f"Next check-in: {datetime.fromisoformat(upcoming['check_in_at']).astimezone(self.s.tz):%a %b %d, %I:%M %p %Z}")
        return {'property_id': pid, 'owner': owner, 'lines': lines}

    def _allowed(self, row, category, owner):
        categories = set(row['alert_types'].split(','))
        owners = set(row['owners'].split(','))
        return (row['active'] and ('all' in categories or category in categories)
                and (owner in owners if owner else set(OWNERS) <= owners))

    def render(self, event, recipient=None):
        testing = recipient is not None and event.get('test_recipient') == recipient['id']
        def permitted(item):
            if recipient is None:
                return True
            current = self.context(item['property_id'])
            if set(OWNERS) - set(recipient['owners'].split(',')) and (not current or current['owner'] != item['owner']):
                return False  # a queued event must not cross portfolios after ownership changes
            return self._allowed(recipient, event['category'], item['owner'])
        items = [item for item in event['items'] if permitted(item)]
        if event['items'] and not items:
            return None
        if not testing and not event['items'] and recipient is not None and not self._allowed(recipient, event['category'], ''):
            return None
        message = event['message'] if event['category'] != 'arrivals' else f'{len(items)} arrivals today for your selected homes.'
        scope = ', '.join(sorted({item['owner'] for item in items if item['owner']})) or 'System / unconfirmed owner'
        lines = [event['title'], f'Owner scope: {scope}', f"Category: {CATEGORIES[event['category']]}", f"Reported: {event['at']}", '', message]
        for item in items:
            lines += ['', *item['lines']]
            if item.get('detail'):
                lines.append(item['detail'])
        link = self.db.get_setting('alert_properties_url', '') or ''
        if link:
            lines += ['', 'Open Properties for more information:', link]
        return '\n'.join(lines)

    async def emit(self, title, message, key=None, *, category='system', property_id=None,
                   property_ids=None, details=None):
        if category not in CATEGORIES:
            raise ValueError('Unknown alert category')
        key = key or 'alert:' + uuid.uuid4().hex
        existing = self.db.one('SELECT payload FROM staff_alert_events WHERE event_key=?', (key,))
        if existing:
            event = json.loads(existing['payload'])
        else:
            items = []
            for pid in dict.fromkeys(property_ids or ([property_id] if property_id else [])):
                item = self.context(pid)
                if item:
                    item['detail'] = (details or {}).get(pid, '')
                    items.append(item)
            event = {'title': title, 'message': message, 'category': category, 'at': self.s.now().isoformat(), 'items': items}
            self.db.execute('INSERT OR IGNORE INTO staff_alert_events VALUES(?, ?)', (key, json.dumps(event)))
            self.db.log('alert', f'{title}: {message}', level='warning', property_id=property_id)
        # Keep the HA persistent notice for administrators; outbound recipients are filtered separately.
        if not self.db.one('SELECT 1 FROM alerts_sent WHERE key=?', (key,)):
            try:
                await self.ha.notify(title, self.render(event), services=[], notification_id=key)
            except Exception:
                self.db.log('alert.failed', 'HA persistent alert was not accepted.', level='error')
            else:
                self.db.execute('INSERT OR IGNORE INTO alerts_sent VALUES(?, ?)', (key, self.stamp()))
        rows = self.db.query('SELECT * FROM alert_recipients WHERE active=1')
        for row in rows:
            if not self.render(event, row):
                continue
            for channel, destination in (('email', row['email']), ('ha', row['target'])):
                if destination:
                    self.db.execute('INSERT OR IGNORE INTO staff_alert_deliveries(event_key,recipient_id,channel,destination,next_attempt_at) VALUES(?,?,?,?,?)',
                                    (key, row['id'], channel, destination, self.stamp()))
        fallback = (self.db.get_setting('alert_service', '') or '').strip()
        if fallback and not self.db.one('SELECT 1 FROM alert_recipients WHERE target=?', (fallback,)):
            self.db.execute('INSERT OR IGNORE INTO staff_alert_deliveries(event_key,recipient_id,channel,destination,next_attempt_at) VALUES(?,NULL,?,?,?)',
                            (key, 'ha', fallback, self.stamp()))
        await self.flush(event_key=key, channel='ha')

    async def flush(self, event_key=None, channel='email'):
        async with self._flush_locks[channel]:
            now = self.stamp()
            sql = "SELECT * FROM staff_alert_deliveries WHERE status='pending' AND next_attempt_at<=? AND channel=?"
            args = (now, channel)
            if event_key:
                sql += ' AND event_key=?'
                args += (event_key,)
            for job in self.db.query(sql + ' ORDER BY id LIMIT 20', args):
                row = self.db.one('SELECT * FROM alert_recipients WHERE id=?', (job['recipient_id'],)) if job['recipient_id'] else None
                event = json.loads(self.db.one('SELECT payload FROM staff_alert_events WHERE event_key=?', (job['event_key'],))['payload'])
                body = self.render(event, row) if row else self.render(event)
                destination_current = row and row['email' if job['channel']=='email' else 'target'] == job['destination']
                if job['recipient_id'] and (not destination_current or not body):
                    self.db.execute("UPDATE staff_alert_deliveries SET status='cancelled' WHERE id=?", (job['id'],))
                    continue
                # Crash during SMTP delivery is ambiguous; HA notifications have replaceable IDs.
                self.db.execute('UPDATE staff_alert_deliveries SET status=?, attempts=attempts+1 WHERE id=?',
                                ('uncertain' if job['channel']=='email' else 'pending', job['id']))
                try:
                    if job['channel'] == 'email':
                        cfg = {k: self.db.get_setting(k, '') or '' for k in (*SMTP_FIELDS, 'smtp_password')}
                        if not self.db.get_bool('smtp_enabled') or not cfg['smtp_host'] or not cfg['smtp_from'] or not cfg['alert_properties_url']:
                            raise ValueError('Email setup incomplete')
                        await asyncio.to_thread(self.send_email, cfg, job['destination'], event['title'], body, job['event_key'])
                    else:
                        if hasattr(self.ha, 'notify_target'):
                            await self.ha.notify_target(event['title'], body, job['destination'])
                        else:
                            await self.ha.notify(event['title'], body, services=[job['destination']], notification_id=job['event_key'])
                except UncertainEmail:
                    self.db.execute("UPDATE staff_alert_deliveries SET status='uncertain', last_error='SMTP acceptance unknown; review before retrying' WHERE id=?", (job['id'],))
                except Exception as exc:
                    delay = min(3600, 60 * 2 ** min(job['attempts'], 6))
                    due = (self.s.now().astimezone(timezone.utc) + timedelta(seconds=delay)).isoformat(timespec='seconds')
                    label = ('Email login rejected; check username or app password' if isinstance(exc, smtplib.SMTPAuthenticationError)
                             else 'Email setup incomplete or disabled' if isinstance(exc, ValueError)
                             else 'Recipient rejected by email server' if isinstance(exc, smtplib.SMTPRecipientsRefused)
                             else 'Sender address rejected by email server' if isinstance(exc, smtplib.SMTPSenderRefused)
                             else type(exc).__name__)
                    self.db.execute("UPDATE staff_alert_deliveries SET status='pending', next_attempt_at=?, last_error=? WHERE id=?", (due, label, job['id']))
                else:
                    self.db.execute("UPDATE staff_alert_deliveries SET status='sent', sent_at=?, last_error=NULL WHERE id=?", (self.stamp(), job['id']))

    async def test_recipient(self, recipient_id):
        row = self.db.one('SELECT * FROM alert_recipients WHERE id=? AND active=1', (recipient_id,))
        if not row:
            raise ValueError('Active recipient not found')
        key = 'test:' + uuid.uuid4().hex
        event = {'title': 'Stay Automation test alert', 'message': 'This is a test for ' + row['name'] + '. Your alert destination is working. No lock or thermostat was changed.',
                 'category': 'system', 'at': self.s.now().isoformat(), 'items': [], 'test_recipient': recipient_id}
        self.db.execute('INSERT INTO staff_alert_events VALUES(?,?)', (key, json.dumps(event)))
        for channel, destination in (('email', row['email']), ('ha', row['target'])):
            if destination:
                self.db.execute('INSERT INTO staff_alert_deliveries(event_key,recipient_id,channel,destination,next_attempt_at) VALUES(?,?,?,?,?)',
                                (key, recipient_id, channel, destination, self.stamp()))
        await self.flush(event_key=key, channel='ha')

    async def integrations(self):
        entries = await self.ha.reauthorization_entries()
        for entry in entries:
            pids = set()
            for eid in entry['entities']:
                for table in ('locks', 'thermostats'):
                    r = self.db.one(f'SELECT property_id FROM {table} WHERE entity_id=?', (eid,))
                    if r and r['property_id']:
                        pids.add(r['property_id'])
            if not pids and entry['domain'] not in ('schlage', 'resideo', 'lyric'):
                continue
            await self.emit(f"Reauthorize {entry['domain']}: {entry['title']}",
                            f"Home Assistant requests reauthorization for integration account {entry['title']} ({entry['domain']}, entry {entry['entry_id']}). Open Settings > Devices & services and reauthorize that account. Affected homes and owner scope are listed below.",
                            key=f"reauth:{entry['entry_id']}:{entry['flow_id']}", category='integration', property_ids=sorted(pids))
