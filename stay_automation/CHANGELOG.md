# Changelog

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
