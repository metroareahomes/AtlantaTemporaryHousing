# Daily occupied temperature limits (development)

Optional and OFF by default. Enable in Setup under “Daily temperature check while guests are staying”.

- Winter heating limit defaults to **70°F**. Only a heating setpoint above the limit is lowered.
- Summer cooling limit defaults to **73°F**. Only a cooling setpoint below the limit is raised.
- Both limits and the daily hour (default **10 AM**, app's configured local timezone) are editable. Existing summer/winter month settings determine the season.
- Measured room temperature does not decide whether to reset: this changes the thermostat's requested setting.
- The global switch applies only to active properties already marked for Thermostat automation, with a guest actually checked in. Installing this update does not enable it. Only dates strictly between the local arrival and departure dates qualify. The entire arrival day and checkout day are excluded, even for early check-in or late checkout. Arrival preconditioning and vacant operation retain their existing temperatures and timing.
- No mode switch: off or opposite-season modes stay alone. Auto/heat-cool changes only the seasonal bound and preserves the other bound; conflicting or missing bounds are left alone.
- One evaluation per thermostat, reservation and local calendar date. A reasonable setting counts as that day's check; a later guest adjustment is left alone until tomorrow.
- A write attempt is saved before sending, including uncertain/failed responses. It will not be repeated that day after a timeout or restart. Confirmed and unconfirmed results are distinguished in the activity log. Normal per-pass cloud-write limits still apply. Daily adjustments use a separate short queue pass, with **at least 90 seconds between thermostat commands by default**, editable in Setup (30–3600 seconds). This is a conservative starting interval, not a published Resideo quota or guarantee. Each thermostat in a multi-thermostat home gets its own slot; reasonable settings do not consume a cloud-write slot. The slot is persisted before sending and survives restart. Daily work also waits after normal arrival/vacant commands; those commands retain their existing priority and timing. A failed or unconfirmed daily command pauses the entire daily queue for the existing thermostat retry delay (default 60 minutes). Other apps and the integration can still generate cloud traffic.

## Guest complaint pause

Enabled by default within this optional feature. Complaint monitoring can detect a complaint on arrival day and preserve that pause for the following full days; it does not change the reset eligibility rule. Reads incoming Hostaway messages without an AI service or token usage. The background check runs every five minutes; a fresh check also runs before a daily adjustment. Only currently occupied automated homes are scanned. Only incoming guest API messages matching the account and reservation, sent during the current stay, are considered. Outgoing staff replies, internal notes, sentUsingHostaway dashboard messages and event-triggered automatic messages are excluded. Outgoing, scheduled, pre-stay, future, quoted email history, and unrelated water/appliance messages are excluded by rules. Rules can miss unusual phrasing or pause on an ambiguous phrase; they do not provide AI-level understanding.

Matches include thermostat resetting/reverting/overrides, requests to stop adjusting, excessive hot/cold discomfort and HVAC faults. The matching text, source and reservation are recorded. A single staff alert is attempted for a newly detected complaint; delivery uses the existing staff-alert behavior. The separate alert retry PR remains independent.

Pauses are local, durable and keyed by reservation ID, so extensions remain paused. Properties displays “✓ Daily temperature reset paused for this stay”. That indicator disappears at checkout; a different reservation starts unpaused. The historical pause remains for audit. A pause never disables property thermostat automation, arrival preparation, or vacant control.

If Hostaway or required resources cannot be read, skip the daily adjustment. Normal occupied/vacant automation continues. Reads are paced and message pagination is bounded; reaching the bound skips the daily reset rather than silently ignoring possible complaints. No guest messages are sent by this feature. Automatic pauses update only the reservation Guest Notes field as described below.

## NTC in reservation Guest Notes

- Staff/concierge can enter **NTC** as a standalone marker anywhere in the reservation Guest Notes. Matching is case-insensitive. It pauses only that reservation's daily occupied adjustment, independently of guest complaint detection.
- An automatic complaint pause is saved locally immediately and enqueues an **NTC** append. Any existing Guest Notes text is retained, and an existing NTC marker is not duplicated. Only `guestNote` is sent in the reservation PUT; dates, codes, status and financial fields are not sent.
- The durable note queue reads fresh notes before every attempt and confirms NTC through a subsequent read. A timeout, 429, 5xx or authentication failure does not blindly replay the old note body; a later attempt starts with a fresh read. A timeout after an applied write therefore discovers the existing marker instead of appending another copy.
- One pending note update is attempted per minute, with exponential retry delay from one minute up to one hour; an interrupted attempt has a five-minute lease. Pending writes survive restart and finish even if the daily-reset feature is later disabled. The local pause stays active throughout; Properties shows when NTC has not yet saved.
- Guest Notes do not have a documented atomic append operation. This is a read-modify-write of fresh text; an overlapping external edit during the read/write window can still race. The app avoids stale retries and never deliberately restores old note text over a later edit.
- At checkout the property's pause indicator clears, while NTC remains in that completed reservation's history. A new reservation starts fresh unless its own notes contain NTC. Removing the marker does not undo a local pause already recorded for that stay.

## Optional Hostaway staff field

Create a **private reservation text field**, for example “Pause daily temperature reset”, and enter its numeric ID in Setup. Default **0** leaves this connection unused. Staff set its reservation value to **YES** (also accepts true, 1, on, pause, paused). This pauses that reservation through checkout. Removing YES does not erase an already-recorded pause. Avoid using a listing-level field, which would carry over to future guests.

The custom field remains an optional alternative; NTC in Guest Notes needs no field ID or extra setup. Reservation reads request `includeResources=1`, which Hostaway requires for custom fields in reservation lists. Message reads use `/conversations?reservationId=...` and `/conversations/{id}/messages`, and verify account/reservation identity.

No production release or Home Assistant deployment is included in this branch.
