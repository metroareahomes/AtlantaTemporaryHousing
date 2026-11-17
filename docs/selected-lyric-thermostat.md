# Selected Lyric thermostat support (development only)

Creekside's entity is climate.livingroom_thermostat. No live setting has been changed.

## Cloud behavior found in upstream source
The current sfcodes/ha-resideo integration uses First Alert's consumer API at api.ha.resideo.com.
It uses SignalR push updates, with no scheduled polling. It does use REST bootstrap/reconnect
reads and debounced resynchronizations after writes and some events; those resyncs cover
its discovered thermostats.

Home Assistant's built-in Lyric integration uses a different API path/library, polls its
account coordinator every five minutes, and refreshes that coordinator after thermostat
commands. Its refresh code traverses cloud-returned devices, not just HA's enabled entities.
Disabling 29 HA devices therefore does not itself make the cloud refresh single-device.

The APIs differ, but that is not proof that Resideo never applies a shared account/backend
limit. Installed integration versions have not been verified against upstream source.
No upstream integration code or polling settings are modified by this change.

Sources inspected:
- https://github.com/sfcodes/ha-resideo/blob/main/custom_components/resideo/coordinator.py
- https://github.com/sfcodes/ha-resideo/blob/main/custom_components/resideo/aioresideo/client.py
- https://github.com/home-assistant/core/blob/2026.11.2/homeassistant/components/lyric/coordinator.py
- https://github.com/home-assistant/core/blob/2026.11.2/homeassistant/components/lyric/climate.py

## App behavior
Resideo discovery remains the default. A new Setup field allows specific Lyric climate
entity IDs. The app intersects these IDs with the Lyric integration's entities and HA's
current states. Unselected, disabled-without-state and unrelated thermostats are excluded.
An empty discovery result no longer falls back to importing every climate entity.

Discovery only reads Home Assistant's cached state and entity registry template. It does not
call update_entity or directly contact either cloud. Commands sent later via HA still carry
the Lyric integration's normal cloud refresh behavior.

Extra devices are imported unassigned and cannot auto-match a property. Manual assignment
is required before the existing property automation settings can control them. Removing an
extra entity from Setup also removes its stored thermostat row so it cannot keep receiving
commands. Other thermostats and their assignments remain unchanged.

## After a separately approved deployment
1. In Setup, enter climate.livingroom_thermostat under Extra Lyric thermostats to include.
2. Save extra thermostats. Wait for a regular thermostat refresh.
3. Review it under Unassigned devices on Properties before choosing a property.
4. Confirm the intended home's Thermostat automation setting before saving the assignment.
5. A test command can itself trigger Lyric's account refresh. Schedule any approved live test
   while monitoring Lyric and Resideo errors; do not bulk-toggle devices or temperatures.

This adds opt-in support; it does not solve or guarantee immunity from Lyric rate limiting.
