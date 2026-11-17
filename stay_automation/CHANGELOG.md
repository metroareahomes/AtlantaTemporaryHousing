# Changelog

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
