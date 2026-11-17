# Shared property status sheet

Development feature; not installed on Home Assistant. One shared Google spreadsheet with All homes, Mike, Kurt and Needs attention filter views. Viewer permissions allow the team to see status; controls remain in Stay Automation.

## What it shows

Owner (exact Hostaway tag or Unconfirmed), property/address, actual occupancy, current/next arrival and checkout, occupied days/days remaining, each lock's state, lowest lock battery percentage, each thermostat's current/target values, generic actionable issues, oldest lock/thermostat reading time, last Hostaway sync and snapshot time. Problems sort first. Headers and the first three columns stay visible.

Exports contain no guest names, reservation IDs, door codes, backup/staff codes, raw errors, messages or credentials. Numeric device labels avoid exporting personal names in device names. Owner tag conflicts remain Unconfirmed. Arrival/departure times reflect actual check-in/out, not thermostat preparation windows. Temperatures use Home Assistant's configured device units; this export does not convert them.

## One-time Google setup (computer recommended)

1. Create a NEW blank Google Sheet named **Stay Automation — Property Status** in the Google account that will maintain it. Open **Extensions → Apps Script**.
2. Replace Code.gs with the repository's [receiver script](../stay_automation/google_sheet/Code.gs). Under **Services → +**, add **Google Sheets API** (identifier Sheets). Save.
3. Select and run **setupPropertySheet**. Approve access to the spreadsheet. The script creates only its dedicated Property status tab, frozen headers/columns and four saved filter views. Existing tabs are not replaced. A setup message includes view URLs; copy these if you want owner-specific email links. Re-running setup deliberately stops instead of changing the connection key.
4. Open the script's **Project Settings → Script properties**. Copy **CONNECTION_KEY**. Keep it private; do not put it into cells, email or chat.
5. Choose **Deploy → New deployment → Web app**. Execute as **Me**; access **Anyone**. This permits the Green to POST without an interactive Google login. Every update still requires a signed request using the secret key, and GET does not expose sheet data. If Workspace policies prevent anonymous web apps, stop here; this receiver cannot work with a sign-in wall.
6. Copy the deployment URL ending in **/exec**, not /dev or a temporary googleusercontent response URL.

## App connection (after this feature is approved and installed)

In Setup → **Shared Google property sheet**, paste the receiver URL and CONNECTION_KEY. Tick **Keep the shared sheet up to date**, then **Save sheet connection**. The key is blank on reload and preserved when left blank. Changing the receiver requires re-entering its key. Updates start disabled and run every five minutes when enabled. No manual export button or extra device polling is required.

After five minutes check **Last confirmed update** and the Google rows. Share the spreadsheet itself with team members as **Viewer**. Choose **Data → Filter views** to use Mike, Kurt or Needs attention; a personal view does not change everyone else's view. On mobile, direct saved-view URLs are easier; native app handling of filter views must be verified with your users. The full sheet remains visible to both owners as requested.

Copy the Google sheet share link into the existing **Permanent Properties link** under Email server settings. That changes the alert destination link without changing recipient owner/category routing. One common link opens All homes; an owner-specific link can be copied from its saved view. Automatic per-recipient Google filter-view URLs are not configured by this feature.

## Refresh and failures

The exporter reads only the local database and sends one signed snapshot every five minutes. It does not call Hostaway, Schlage, Resideo or Home Assistant APIs, and does not change codes, scheduling, occupied-temperature pauses or thermostat control. An independent worker keeps failures away from device-control loops.

Connection failures retry on the next five-minute pass; the same snapshot overwrites rows rather than appending duplicates. Only a matching signed-payload digest and row-count acknowledgement update the app's success time. The receiver serializes writes, rejects old/tampered requests, escapes formula-like text, removes leftover rows on smaller snapshots and keeps unrelated tabs intact. Google can interrupt a write between data and formatting; a later complete snapshot repairs it. Failed or empty/uninitialized exports do not deliberately clear existing rows. Existing values retain their snapshot/source timestamps, which must be checked if updates stop.

The times under Lock read / Thermostat seen are cached app observation times, not proof of a new cloud-device poll. Hostaway data older than the app's stale threshold is explicitly flagged, as are old or missing device observations. The snapshot time is the export time and must not be read as a fresh device measurement.

No new Python dependencies. Shared key remains in the app's protected settings DB and Google Script Properties; backups may contain it. The receiver URL/key are not printed in failure messages. Disabling the feature stops future exports; it does not revoke the sheet's sharing permissions or delete existing rows. Edit sharing in Google if needed.

## Verification before production

Automated tests cover snapshot privacy, zero-percent batteries, actual occupancy, stale-source flags, unknown ownership, formula escaping, signature verification, replay window, malformed rows, limited redirect handling, failed acknowledgement/retry and separate masked Setup form. Browser checks cover phone/desktop field/save behavior. Real Google authorization, receiver execution, filter views and sharing require live verification after the account owner completes setup. Do not mark the live integration connected until both app acknowledgement and rows have been checked.

Reference: https://developers.google.com/apps-script/guides/web and https://developers.google.com/apps-script/guides/content (ContentService returns its body through a googleusercontent redirect).
