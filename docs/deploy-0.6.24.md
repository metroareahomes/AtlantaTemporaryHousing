# Deploy 0.6.24

User authorized release November 18, 2026. Source rollback: stable-0.6.23-before-vacant-drift. Install from Settings → Apps → Stay Automation → Update and confirm version 0.6.24.

Vacant thermostat mode/target mismatches are now corrected on the normal five-minute pass even if already successfully reset today. Actual room temperature does not trigger a command. Correct readings are left alone. Existing automation switches, write cap/gap and failed-attempt retry delay remain; offline devices and unsuccessful corrections still warn. Already handled occupied reservations keep their guest settings.

Routine vacant drift no longer appears in Needs Attention. The All homes view still explains the pending restoration and readings. No guest access, lock scheduling or backup-message changes. No fleet-wide live test was performed; verify the next regular pass for Dustin/Kemp and the thermostat event log. A skipped/failed command remains subject to existing pacing/backoff.

Require release-head automated and browser checks before merge. Source rollback alone is not a data backup. Separate pending Lyric-readback and Google Sheet/Setup cleanup PRs are not included in this release.
