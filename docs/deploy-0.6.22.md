# Deploy 0.6.22

User authorized publication November 17, 2026. Source rollback: stable-0.6.21-before-property-sheet. No production device commands are used during release verification.

351 automated checks and desktop/phone browser checks passed on feature commit da4e7ac4c945ea299fc8a8087d54ad7f1d126c5d. Require release-head checks before merge.

Install in Home Assistant: Settings → Apps → Stay Automation → Update. Confirm version 0.6.22 and the Shared Google property sheet section under Setup. Export is disabled unless explicitly enabled after configuration. The receiver script is not automatically installed in Google.

Follow [Google setup instructions](shared-property-sheet.md) to create the spreadsheet, authorize Apps Script, deploy its signed receiver and enter its URL/key in Setup. Check the first confirmed app acknowledgement AND matching sheet rows before treating the mirror as live. No live Google account, receiver, filter views or sharing were verified in development. Keep the key private.

Sheet updates use cached readings every five minutes. Existing sheet timestamps persist if updates stop. Use Viewer sharing and the Google share URL in the existing email Properties-link field after testing access. No change to lock/backup-code or thermostat automation rules.

Disabling the sheet export stops future writes but does not delete rows or change Google sharing. Source rollback is not an app data backup; use the preserved Home Assistant backup if restoring app data is required. Existing settings are retained.

Known separate issue: integration reauthorization monitor can exceed its WebSocket 1 MB response limit on large inventories; this release does not repair that monitor.
