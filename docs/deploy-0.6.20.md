# 0.6.20 rollout: owner-scoped email alerts

Owner explicitly approved deployment of PR #8. Source 0.6.19 is preserved at
stable-0.6.19-before-owner-email-alerts (f26dd67409b35644cc12bf360c09f6cdc764a144).
Existing Home Assistant backup copies were previously confirmed. A GitHub branch is source
rollback, not a backup of configuration or the running database.

## Install the existing app

In Home Assistant Settings > Apps (or Add-ons), update the EXISTING Stay Automation installation
to 0.6.20. Refresh the app store if needed. Do not uninstall or install a second copy.
Installation restarts the app. Existing credentials, code scheduling, backup messaging,
thermostat settings and NTC pauses retain their saved behavior. SQLite changes are additive.
Email remains OFF until SMTP is configured and enabled. Existing app-alert targets continue.

## Configure and verify with one recipient first

1. In Staff & access, review Hostaway owner counts. Confirm Mike/Kurt labels on Properties against
   known homes. Missing, conflicting and ID-only tags remain unconfirmed rather than being guessed.
   Owner-only recipients do not receive unconfirmed homes. Both-owner recipients with the matching
   category and the administrator app fallback can receive those alerts for review.
2. In Setup > Email server for staff alerts, enter SMTP host, port, SSL/STARTTLS, username,
   password/app password and an authorized From address. Enter the permanent Properties URL.
   For HA use its external HTTPS address followed by /hassio/ingress/APP_SLUG?stay_page=properties.
   Do not use a temporary /api/hassio_ingress/... URL. Passwords are entered only in the app.
3. Add your own recipient with name/email, owner and category selections. Save it, enable SMTP
   when ready, then use Test a saved recipient. Only that selected person receives the test.
4. Confirm inbox arrival and open the Properties link on the phone, signing into HA if necessary.
   No live SMTP acceptance, inbox delivery or phone session can be verified from GitHub alone.
5. Add other recipients after the first successful test. Review any pending/uncertain deliveries
   on Staff. An uncertain email may already have arrived; retry only after checking the inbox.

Existing Setup app-alert fallback is an all-owner administrator destination unless a saved recipient
matches that notify target. A matching recipient's settings take precedence, including inactive status.
Clear the fallback if all outgoing destinations should use recipient-specific preferences.

Integration monitoring reads actual Home Assistant reauth flows and entity-registry membership
without restarting/reloading integrations. Confirm its permissions and results in Activity log on
this Green. Unavailable entities alone are not labeled reauthorization. Shared-account alerts are
filtered per recipient so a Mike-only email does not include Kurt homes, and vice versa.

## Rollback/stop email

Uncheck Send staff alerts by email in Setup to stop SMTP attempts; pending deliveries remain saved.
Existing app notifications and property automations continue. The preserved 0.6.19 source can be used
for source rollback while keeping existing data; never uninstall as a rollback step. Restoring data,
if needed, uses the existing Home Assistant backup. No live data restore is performed by publication.

Draft PRs #2 and #4 remain separate. PR #2 needs reconciliation of its notification portion before
any later merge; its other reliability work is not part of this update.
