# Changelog

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
