# Deploy 0.6.23

User authorized release November 17, 2026. Source rollback: stable-0.6.22-before-season-mode. The feature passed 364 automated checks and desktop/phone browser checks; require release-head CI before merge.

Install from Home Assistant Settings → Apps → Stay Automation → Update. Confirm version 0.6.23. Watch Creekside on the next normal thermostat pass: an online reading matching vacant winter Heat and the configured target clears its old failure, error and retry timestamp without another device command. A wrong-mode or offline reading must not clear the warning. This was not cleared directly on the Green during development because the available browser required sign-in.

For daily occupied adjustments, an incorrect seasonal mode is changed first; the target waits for matching cached mode and a subsequent paced slot (default 90 seconds). NTC/complaints and full intermediate stay dates are checked again. Restart cannot resend the mode blindly. An unconfirmed mode expires after five minutes and the daily queue backs off. Already reasonable settings are left alone. Observe the next ordinary eligible adjustment rather than generating a fleet-wide test.

No lock-code or backup-message changes. Existing Google sheet connection stays as configured. New mode-stage history table is additive; source rollback is not a data backup. Use the existing Home Assistant backup if app-data restoration is required.

Separate integration-reauthorization monitor response-size issue remains unresolved by this release.
