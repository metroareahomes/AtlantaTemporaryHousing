# Deploy 0.6.25

User authorized release November 19, 2026. Source rollback: stable-0.6.24-before-lock-state-refresh.

376 automated tests and desktop/phone browser checks passed on the feature commit. Require release-head CI before merge. Install from Settings → Apps → Stay Automation → Update; confirm version 0.6.25.

On Sandtown 1386 (ST2), press Check lock once after installation. It reads the selected lock's current cached Home Assistant entity state and its access codes. An available HA state clears the old offline label. Unknown/unavailable, missing entities and read failures do not imply recovery; a failed state lookup gives a notice and retains previous availability. If the warning remains, compare that exact HA entity's availability to the Schlage app. This does not force a cloud refresh or operate a lock.

No guest codes, schedules, backup messages, thermostat controls or other property assignments are changed. No live lock commands were used in testing. Source rollback is not an app-data backup. Separate pending PRs remain excluded.
