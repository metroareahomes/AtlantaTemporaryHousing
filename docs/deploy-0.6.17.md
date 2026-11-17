# 0.6.17 rollout: selected Creekside thermostat

Owner approved proceeding with Lyric support, with the option to disable Lyric if cloud polling
errors appear. This update contains PR #5 only, without PR #2 or PR #4.

Previous source is preserved at stable-0.6.16-before-lyric:
f5e2115d0de12b74ef20d01927778b0d41e8a72e.
The owner previously confirmed a Home Assistant backup including Stay Automation exists in Dropbox
and other locations. A GitHub source branch is not a data backup.

## Update and select
1. Refresh Home Assistant's app/add-on store and update the existing Stay Automation installation to 0.6.17.
   Do not uninstall it or install a second copy.
2. In Stays > System > Setup & rules, find Extra Lyric thermostats to include.
3. Enter climate.livingroom_thermostat and save the extra thermostat selection.
4. Allow the regular thermostat refresh to discover it; open Properties.
5. Assign the unassigned thermostat to creekside 1996 and save. Confirm the intended home's
   Thermostat automation setting before enabling control. No other Lyric thermostat is selected.

Installation restarts the app. No selection, assignment, live command or Home Assistant installation
is performed merely by publishing this GitHub release.

## Polling scope
The current HACS Resideo source uses the First Alert consumer API with SignalR push and REST resyncs.
Lyric uses its own account coordinator with scheduled polling and refreshes after commands.
Disabling individual Lyric devices in HA does not filter the coordinator's cloud refresh.
The app's discovery reads HA's cached states, but its later commands still invoke Lyric's normal behavior.
Distinct API paths do not guarantee independent cloud limits.

## If polling-limit errors occur
- Stop thermostat automation for Creekside in Properties.
- Disable the Honeywell Lyric integration in HA to stop its background polling as well.
  Simply removing Creekside from Stays does not stop Lyric's own background polling.
- Leave the HACS Resideo integration and existing Resideo thermostats enabled.
- Note the error time, integration/logger and HTTP status for diagnosis before trying again.
- The extra-thermostat selection can be cleared in Setup to remove its app control row.
  Reverting this application alone does not disable the separately installed Lyric integration.

Existing locked guest access and its scheduling are not changed by this thermostat-support release.
