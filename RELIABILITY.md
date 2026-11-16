# Reliability changes (development only)

Production remains 0.6.15. Do not merge, release, or install this branch without owner approval.

## Staff notifications
Alert intent is stored in SQLite before sending. Each destination (HA dashboard and each configured mobile
service) has independent delivery state. Successful recipients are not retried. A separate one-minute loop
retries failed recipients with delays of 1, 2, 4, 8, 16, 32, then 60 minutes, capped at 60 minutes.
Retries survive add-on restarts and do not wait for slow lock reconciliation.
Recipient lists are snapshotted when an alert is queued.

Stable HA notification IDs and mobile tags reduce duplicates after a lost response. External delivery is
not exactly-once: a timeout or crash after a remote success but before SQLite records success can repeat
a notification. Tags replace existing mobile notifications where supported; they may still sound again.
A successful HA service response confirms acceptance by HA, not arrival on the phone.

## Hostaway and thermostats
Failed reservation webhooks persist and retry once a minute when due, with backoff capped at 15 minutes.
Full synchronization retains its existing configured interval (15 minutes by default) and now sends a
deduplicated daily failure alert. Cached reservations remain available if fetching fails.
GET transport failures use bounded retries. Startup provider failures no longer skip unrelated discovery
or thermostat refresh work. Failed thermostat wakeups are retained for retry; failures do not drop other homes.
Webhook wiring is saved only after HA accepts it and is rechecked every five minutes.

## Guest backup messages
Known failures before the message POST retry after five minutes, including after restart. Retry processing
checks that the reservation is active, has not checked out, the property is active/automated, and messaging
and backup codes remain enabled. Disabled messaging pauses pending retries.

A message POST is not automatically replayed after a server error, timeout, or ambiguous response.
Send intent is saved first and the backup code is marked used before the POST, so uncertain delivery still
protects the code from normal rotation until that reservation checks out. An interrupted send is surfaced
to staff for a manual Hostaway conversation check. A later confirmed delivery gets its own staff update.
Old terminal guest-notice rows from earlier versions are not automatically resent.

Invalid message templates fall back to the existing default template and log an error without exposing
the code in that error. Existing lock planning, Schlage writes/read-back verification and thermostat
temperature rules are unchanged.

## Database and rollback
New tables and guest-notice columns are additive; existing rows are preserved. No version bump or release
is included. Keep a fresh HA backup before any separately approved installation. Rolling back the application
does not delete queued records; an older version will not process the new queues.
