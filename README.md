# AtlantaTemporaryHousing

Repository for Atlanta Temporary Housing
Home Assistant add-on that keeps Schlage lock codes and Honeywell thermostats in step with Hostaway reservations.

## Status

- Phase 1: Hostaway sync and webhooks, property and lock tables with automatic lock matching,
  dashboard (Status, Properties, Log, Setup).
- Phase 2: for homes with "Lock automation" on, guest codes (named `HA-<guest name>`) are added on the
  morning of arrival and removed at checkout. Every change is verified by reading the lock back; failures
  retry every 15 minutes and alert staff. An hour before check-in, a guest whose code is not confirmed gets
  the home's backup code through Hostaway (when enabled), and staff are alerted. Daily arrivals report.
  Codes this add-on did not write are never touched. Staff permanent codes and alert recipients live in
  SQLite tables and are edited on the Staff dashboard page.
- Hostaway's own lock automation stays on: Hostaway creates each guest code and usually writes it to the lock.
  A code Hostaway already wrote counts as present, so the add-on only adds its own copy when the code is missing.
  If Hostaway has not removed a guest's code two hours after checkout, staff are alerted.
- Phase 3 (thermostats) is not built yet.

## Run locally

```sh
pip install -r requirements-dev.txt
python -m pytest
```

Create `.env` in the repo root (never committed):

```
HOSTAWAY_ACCOUNT_ID=...
HOSTAWAY_API_KEY=...
HA_URL=https://your-home-assistant.example.com
```

Put a Home Assistant long-lived access token in `.ha_token`, then:

```sh
PYTHONPATH=stay_automation python -m app.main
```

Open http://127.0.0.1:8099. The local database is `data/stay.db`.

## Install on Home Assistant Green

1. Copy the `stay_automation` folder into the `/addons` share on the Green (Samba share or SSH add-on).
2. Settings → Add-ons → Add-on Store → ⋮ → Check for updates. "Stay Automation" appears under Local add-ons.
3. Install, then set `hostaway_account_id` and `hostaway_api_key` on the Configuration tab.
4. Start it and turn on "Show in sidebar". The dashboard is the "Stays" tab.
5. On the Setup page, save the webhook. Tick "Also register the webhook with Hostaway" once you are ready
   for Hostaway to start sending changes.
