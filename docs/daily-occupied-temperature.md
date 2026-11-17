# Daily occupied temperature limits (development)

Optional and OFF by default. Enable in Setup under “Daily temperature check while guests are staying”.

- Winter heating limit defaults to **70°F**. Only a heating setpoint above the limit is lowered.
- Summer cooling limit defaults to **73°F**. Only a cooling setpoint below the limit is raised.
- Both limits and the daily hour (default **11 AM**, app's configured local timezone) are editable. Existing summer/winter month settings determine the season.
- Measured room temperature does not decide whether to reset: this changes the thermostat's requested setting.
- Applies only to active, automated properties with a guest actually checked in. A stay arriving after the selected hour first qualifies the next day. Arrival preconditioning and vacant operation retain their existing temperatures and timing.
- No mode switch: off or opposite-season modes stay alone. Auto/heat-cool changes only the seasonal bound and preserves the other bound; conflicting or missing bounds are left alone.
- One evaluation per thermostat, reservation and local calendar date. A reasonable setting counts as that day's check; a later guest adjustment is left alone until tomorrow.
- A write attempt is saved before sending, including uncertain/failed responses. It will not be repeated that day after a timeout or restart. Confirmed and unconfirmed results are distinguished in the activity log. Normal per-pass cloud-write limits still apply.

## Guest complaint pause

Enabled by default within this optional feature. Reads incoming Hostaway messages without an AI service or token usage. The background check runs every five minutes; a fresh check also runs before a daily adjustment. Only currently occupied automated homes are scanned. Only authenticated API messages matching the account and reservation, sent during the current stay, are considered. Outgoing, scheduled, pre-stay, future, quoted email history, and unrelated water/appliance messages are excluded by rules. Rules can miss unusual phrasing or pause on an ambiguous phrase; they do not provide AI-level understanding.

Matches include thermostat resetting/reverting/overrides, requests to stop adjusting, excessive hot/cold discomfort and HVAC faults. The matching text, source and reservation are recorded. A single staff alert is attempted for a newly detected complaint; delivery uses the existing staff-alert behavior. The separate alert retry PR remains independent.

Pauses are local, durable and keyed by reservation ID, so extensions remain paused. Properties displays “✓ Daily temperature reset paused for this stay”. That indicator disappears at checkout; a different reservation starts unpaused. The historical pause remains for audit. A pause never disables property thermostat automation, arrival preparation, or vacant control.

If Hostaway or required resources cannot be read, skip the daily adjustment. Normal occupied/vacant automation continues. Reads are paced and message pagination is bounded; reaching the bound skips the daily reset rather than silently ignoring possible complaints. No guest messages are sent by this feature.

## Optional Hostaway staff field

Create a **private reservation text field**, for example “Pause daily temperature reset”, and enter its numeric ID in Setup. Default **0** leaves this connection unused. Staff set its reservation value to **YES** (also accepts true, 1, on, pause, paused). This pauses that reservation through checkout. Removing YES does not erase an already-recorded pause. Avoid using a listing-level field, which would carry over to future guests.

The app reads the custom field; automatic complaint pauses are stored in Stay Automation and shown on Properties, rather than writing reservation data back to Hostaway. Reservation reads request `includeResources=1`, which Hostaway requires for custom fields in reservation lists. Message reads use `/conversations?reservationId=...` and `/conversations/{id}/messages`, and verify account/reservation identity.

No production release or Home Assistant deployment is included in this branch.
