# 0.6.18 rollout: occupied temperature limits and NTC pauses

Owner explicitly approved deployment. This release contains PR #6, excluding the separate draft
PRs #2 and #4. The pre-release 0.6.17 source is preserved at
stable-0.6.17-before-occupied-reset (3f412ad3c2a6626fdf8c006a611973db4ed7ab76).
The owner previously confirmed a Home Assistant backup including Stay Automation in Dropbox and
other locations. A source rollback branch is not a data backup.

## Install

Update the EXISTING Stay Automation app/add-on to 0.6.18 through Home Assistant Settings > Apps
(or Add-ons on older versions). Refresh the app store if the update is not shown. Do not uninstall
or install a second copy. Updating restarts Stay Automation; Home Assistant and already-installed
lock codes are not deliberately restarted or changed by publishing this release.

The daily occupied reset defaults OFF. Existing home selections, lock scheduling, backup messaging,
Lyric selection, arrival preparation and vacant temperature settings are retained.

## Enable and check

1. Open Stays > System > Setup & rules > Daily temperature check while guests are staying.
2. Confirm winter 70°F, summer 73°F, start 10 AM, and spacing at least 90 seconds. These are editable.
3. Enable the daily occupied check and save its dedicated settings form. It applies only to active
   homes already marked for Thermostat automation, on full dates strictly between arrival and departure.
4. Keep complaint detection enabled if desired. It analyzes incoming guest messages only. Concierge
   staff can type NTC anywhere as a standalone marker in that reservation's Guest Notes; no field ID
   is required. A guest complaint pause automatically appends NTC, with local protection and retry if
   Hostaway is unavailable. Historical NTC stays in that reservation after checkout.
5. Check Activity log and Properties. Reasonable settings should stay alone, arrival and departure
   dates should receive no daily occupied reset, and adjustments should be paced per thermostat.
   Normal arrival/vacant controls retain their prior schedule and priority.

The 90-second interval is a conservative starting point, not a published Resideo quota or guarantee.
A failed/unconfirmed daily command pauses that queue using the existing thermostat retry delay.
If cloud errors appear, disable the new occupied daily check, increase spacing, and inspect the
integration/logs. Do not disable all property thermostat automation merely to stop the new daily check.

## Rollback

Switch off the new occupied daily check in Setup to stop it immediately. Existing arrival/vacant
control and installed guest codes remain under their existing rules. Source version 0.6.17 is available
at the preserved rollback branch; restoring data, if ever needed, uses the existing Home Assistant
backup. Never uninstall the app as a rollback step. New SQLite tables are additive and do not delete
existing settings, reservations, locks, or codes. NTC already appended to Hostaway remains there.
