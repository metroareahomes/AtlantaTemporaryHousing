# Changelog

## 0.6.51 — Inquiry dry run

- Simulate inquiry dates, guest replies, follow-up timing, and proposed messages without changing Hostaway.

## 0.6.50 — Minimum extension dates

- Preview a guest’s minimum stay when their final checkout is unknown.

## 0.6.49 — Extension and inquiry previews

- Show proposed Hostaway notes, turnover after a proposed extension, and drafts for eligible alteration reminders and overlapping inquiries.

## 0.6.48 — Turnover holds and checkout wording

- Preview turnover holds separately from extension holds and use a neutral checkout check-in.

## 0.6.47 — Checkout check-in preview

- Preview a check-in ten days before the latest booked checkout when the guest’s plans are unknown.

## 0.6.46 — Extension preview next actions

- Show proposed blocked nights, next actions, and follow-up wording and timing for each reservation.

## 0.6.45 — Kurt’s Extension Preview

- Add a read-only dashboard for proposed calendar notes and guest follow-ups across Kurt’s homes.

## 0.6.44 — Extension follow-up previews

- Preview guest extension follow-ups through staff alerts when an OpenAI API key is configured.

## 0.6.43 — Manual lock check results

- Log successful manual lock checks and report failed code reads on the Status page.

## 0.6.42 — Pricing Setup cleanup

- Remove the old weekend pricing simulation and its Save button from Pricing Setup.

## 0.6.41 — Built-in Guest Extension Pricing

- Open Guest Extension Pricing inside Stay Automation with separate Mike and Kurt rules and ledgers.
- Use the existing Hostaway connection and webhook for extension pricing.

## 0.6.40 — Shared Hostaway webhook

- Preserve Guest Extension Pricing delivery when rebuilding the Stay Automation webhook configuration.

## 0.6.39 — Setup navigation

- Split Setup into Connections, Locks and guest access, Thermostats, Pricing rules, and Staff email alerts.
- Keep daily temperature checks and extra thermostat selection on the Thermostats page.
- Add Guest Extension Pricing to the Pricing menu.

## 0.6.38 — Thermostat verification grace for both integrations

- Give Honeywell Lyric and HACS Resideo up to two minutes to confirm mode and setpoint changes.
- Show verification as pending on the dashboard before flagging a mismatch. Offline warnings still appear.

Source rollback: stable-0.6.37-before-thermostat-grace.

## 0.6.37 — Slower Lyric temperature verification

- Give configured Honeywell Lyric thermostats up to two minutes to report a requested setpoint.
- Use the active Heat or Cool setpoint if the single target is absent.

Source rollback: stable-0.6.36-before-lyric-verification.

## 0.6.36 — Activity log property filters

- Search the Activity log as you type and filter by an exact property.
- Open a property’s activity by tapping its name in a log entry.

Source rollback: stable-0.6.35-before-activity-filters.

## 0.6.35 — Thursday booking reports run in the background

- Keep fetching reservations when the browser closes or the page times out.
- Show report progress and save completed results for later viewing.

Source rollback: stable-0.6.34-before-background-report.

## 0.6.34 — Thursday report running status

- Show when the Thursday report is running and disable repeated submissions.

Source rollback: stable-0.6.33-before-report-status.

## 0.6.33 — Thursday booking times report

- Use the existing Hostaway connection to report 12 months of Thursday booking times in Eastern Time.
- Compare same-day arrivals with all Thursday bookings and show the split at 2 PM.

Source rollback: stable-0.6.32-before-booking-report.

## 0.6.32 — Hostaway calendar availability

- Accept an explicit available status when Hostaway omits its optional availability flag.
- Distinguish unavailable nights from unclear calendar data. All three nights must still qualify.

Source rollback: stable-0.6.31-before-availability-fix.

## 0.6.31 — Apply immediately after preview

- Allow Thursday Special Apply immediately after a preview while retaining live eligibility and duplicate-discount checks.
- Remove Google Sheets controls from Setup.

Source rollback: stable-0.6.30-before-apply-fix.

## 0.6.30 — Owner schedules for the Thursday Special

- Give Mike and Kurt separate property selections, discounts and Eastern activation times.
- Allow one-time updates for qualifying homes and keep weekly schedules off by default.

Source rollback: stable-0.6.29-before-owner-schedules.

## 0.6.29 — One-time Wright trial

- Apply the Thursday Special to Kurt’s Wright listing on demand, with three vacant nights required and Hostaway readback.
- Retain a local record of the attempt to prevent duplicate discounts.

## 0.6.28 — Thursday Special preview rates

- Show proposed rates on available nights and report arrival restrictions separately.
- Preserve the existing booked and blocked inventory checks.

## 0.6.27 — The Thursday Special

- Preview Thursday through Saturday rate discounts and a three-night Thursday arrival minimum.
- Select homes by checkbox, Thursday calendar rate, or both.
- The preview does not write calendar updates or schedule repeat runs.

## 0.6.26 — Hostaway pricing previews

- Preview weekend pricing on one property with configurable rates and timing.
- Filter properties by owner and preview one-time discounts and arrival minimums.
- This release reads calendar data only. Live pricing and recurring runs are unavailable.

Source rollback: stable-0.6.25-before-pricing-preview.

## 0.6.25 — refresh lock availability on Check lock

- Read the selected lock's current Home Assistant availability as well as its access codes when Check lock is pressed.
- Clear stale offline labels only when Home Assistant confirms an available state; preserve warnings when state is missing, unknown, unavailable or unreadable.
- Show a helpful notice when availability cannot be confirmed; retain genuine code-verification errors.
- Do not operate locks, change codes, force cloud refreshes or alter other property assignments.

Source rollback: stable-0.6.24-before-lock-state-refresh. Live availability still depends on Home Assistant's integration state.


## 0.6.24 — quietly restore vacant temperatures

- Correct vacant-home mode/target drift on the normal five-minute thermostat pass, including after a successful reset earlier the same day.
- Remove routine vacant mismatch warnings from Needs Attention; retain offline and failed-correction warnings.
- Preserve occupied guest settings, thermostat automation switches, per-pass command limits and failed-command retry delays.

Source rollback: stable-0.6.23-before-vacant-drift. Separate pending PRs are not included.


## 0.6.23 — confirm seasonal mode and clear corrected thermostat warnings

- Confirm Heat in winter or Cool in summer before an occupied temperature adjustment.
- Pace mode and temperature commands in separate persisted slots; recheck NTC, eligibility and mode before the target command.
- Withhold temperature commands if seasonal mode is unconfirmed, including normal arrival/vacant changes.
- Clear stale failures without a new device command when an online reading confirms the current occupancy-policy mode and target.
- Keep already reasonable settings alone and preserve arrival/checkout-day exclusions, complaint pauses and lock operations.

Source rollback: stable-0.6.22-before-season-mode. Live Home Assistant state requires verification after installation.


## 0.6.22 — shared Google property status sheet

- Add an optional view-only Google Sheet mirror, updated every five minutes from cached app readings without extra device polling.
- Include owner, address, occupancy/dates, lock status/battery, thermostat readings, issues and source/update timestamps; flag stale data and sort problems first.
- Provide a Google receiver script with All homes, Mike, Kurt and Needs attention filter views, frozen headers and property columns.
- Add plain-language Setup controls for the receiver URL and hidden connection key, with last confirmed update and readable failures.
- Authenticate snapshots, escape formula-like text, reject old requests and retry failed updates without appending duplicate rows.
- Exclude guest names, door codes, messages, raw errors and credentials from sheet data.
- Sheet updates start OFF; one-time Google authorization/deployment is required. Live Google execution and sharing still need verification.

Source rollback: stable-0.6.21-before-property-sheet. The separate reauthorization-monitor size-limit issue remains unresolved.


## 0.6.21 — email test validation and delayed thermostat verification

- Show a helpful message when no saved active recipient is selected instead of a raw validation error.
- Require a saved recipient selection before sending a test.
- Recheck a mismatched thermostat target after another 40 seconds (about one minute total) before recording failure; send no additional temperature command or forced cloud refresh.
- Preserve existing retry timing, lock scheduling and guest access behavior.

Known issue: the reauthorization monitor can exceed the WebSocket response-size limit on large inventories. This release does not change that monitor.


## 0.6.20 — owner-scoped staff email alerts

- Add recipient names, email addresses, Mike/Kurt/both selections, interruption categories and individual tests.
- Add SMTP host, port, SSL/STARTTLS, username, hidden password and permanent Properties link in Setup.
- Include owner, address, event details, battery percentage/device, occupancy, check-in/out and elapsed/remaining stay in emails.
- Import exact named Hostaway owner tags and show owner counts/unconfirmed properties for review.
- Identify actual integration reauthorization flows by account and affected homes/owners; add Hostaway reservation-sync failure alerts.
- Track delivery per destination with persisted retries, isolating failures from successful recipients.
- Hold ambiguous SMTP acceptance for inbox review instead of blindly resending; keep secrets out of logs and HTML.
- Keep existing app notifications and saved automation settings; email stays OFF until configured and enabled.

The 0.6.19 source is preserved at stable-0.6.19-before-owner-email-alerts. Live SMTP acceptance,
Hostaway tag shape, integration-monitor permissions and the phone link require verification on the Green.
This overlaps draft PR #2's notification work but does not merge its separate webhook/backup retry changes.
PR #4 remains excluded. Lock scheduling, backup messaging and thermostat control rules are retained.

## 0.6.19 — menu stays visible while scrolling

- Keep the main menu and sync controls at the top while scrolling.
- Position floating column headers directly beneath the menu, including wrapped phone layouts.
- Reposition column headers automatically when the menu height changes.
- Keep the System dropdown above table headers and preserve the fixed bottom Save actions.
- Keep printed pages in their normal layout.

Interface-only update: automation, settings, guest access, database and thermostat rules are unchanged.
The confirmed working 0.6.18 source is preserved at stable-0.6.18-before-sticky-menu.

## 0.6.18 — occupied temperature limits and NTC pauses

- Add optional daily occupied temperature limits: winter 70°F, summer 73°F, with an editable 10 AM start.
- Apply only on full days between arrival and departure; preserve arrival preparation and vacant control.
- Stagger thermostat adjustments at least 90 seconds apart by default, editable in Setup, with persistent pacing and failure backoff.
- Leave reasonable settings and HVAC mode alone; record attempts before sending to avoid repeat adjustments that day.
- Detect complaints only in incoming guest messages, excluding staff replies, internal messages, automated messages and quoted history.
- Respect NTC anywhere as a standalone marker in reservation Guest Notes as a concierge override.
- Append NTC after an automatic pause using a durable retry queue, preserving existing notes and avoiding duplicate markers.
- Keep the pause for that reservation through extensions and clear its property indicator at checkout.
- Add Setup help and pending-note visibility on Properties.

The new feature is OFF until enabled in Setup. Only homes already marked for thermostat automation
are eligible. Version 0.6.17 is preserved at stable-0.6.17-before-occupied-reset. Existing guest access,
lock scheduling, backup messaging and normal arrival/vacant thermostat controls are retained.
PR #2's reliability work and PR #4's final check-in changes are not included.

## 0.6.17 — selected Lyric thermostat support

- Add individual Honeywell Lyric thermostats through Setup, while excluding unselected Lyric devices.
- Verify Lyric entity membership before saving; require manual property assignment before control.
- Keep native Resideo thermostats as the default and preserve their normal automation rules.
- Read Home Assistant's cached thermostat state without forcing a cloud refresh during discovery.
- Prevent empty discovery from importing unrelated climate entities.

Lyric's own account-wide polling and command-triggered refresh behavior remain unchanged. Shared
cloud rate limits cannot be ruled out. All extras remain excluded until explicitly selected.
PR #2's reliability work and PR #4's final check-in changes are not included.

## 0.6.16 — interface improvements

- Put Properties and Staff & access directly in the main menu.
- Group Setup & rules and Activity log under System.
- Explain all 29 Setup fields with simpler labels, short descriptions and question-mark help with examples.
- Show AM/PM equivalents for saved hour settings.
- Keep list column headings visible and aligned during vertical and horizontal scrolling.
- Keep clearly named Save buttons at the bottom of Properties, Staff and Setup.
- Mark forms with unsaved changes and keep native validation and separate form ownership.

This release does not include PR #2's alert and Hostaway retry changes. Lock-code timing, Schlage
verification, backup-message rules, thermostat rules and the database schema remain unchanged.
