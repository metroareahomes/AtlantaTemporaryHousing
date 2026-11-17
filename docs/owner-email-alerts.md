# Owner-scoped email alerts (0.6.20)

Published as 0.6.20 after explicit owner deployment approval. No real SMTP credentials or recipient addresses are included in the repository. Installing on the Green and configuring live delivery are separate steps; see docs/deploy-0.6.20.md.

## Recipients and routing

Staff & access supports each person's name, email, active status, Mike/Kurt/both checkboxes, and checkboxes for low lock batteries, lock problems, guest/backup access, thermostat problems, integration reauthorization, Hostaway sync failures, occupied-temperature pauses, arrivals, and system issues. Existing Home Assistant notify services remain optional. Each saved active recipient has an explicit test action; it sends only to the selected person.

Listing reads request includeResources=1. Named Hostaway listing/reservation tags are matched exactly, case-insensitively; home names, addresses and portfolio counts are never used to guess an owner. Nested named tags are supported. Missing/ambiguous/ID-only tags remain unconfirmed. A current reservation's owner tag may fill a missing listing tag; conflicting listing/reservation tags are unconfirmed. Staff shows listing-owner counts, and Properties shows the confirmed listing owner or a review warning. Actual Hostaway payloads must be checked against the live account before relying on owner-only routing.

Single-owner recipients never receive the other portfolio's property details. Shared integration and arrival alerts are rendered separately per recipient. Unconfirmed and account-wide alerts go only to recipients selecting BOTH owners (and the corresponding category), plus the existing administrator app fallback. If ownership or recipient preferences change while delivery is pending, the queued delivery is rechecked and cancelled when it no longer matches. Recipient emails must be unique.

The existing Setup app-alert fallback remains an all-owner administrator destination unless it matches a saved recipient; matching saved recipients' preferences take precedence, including inactive status. Clear the fallback if all outgoing destinations should use recipient-specific controls. Owner filtering limits alert delivery, not Home Assistant account permissions.

## Email setup

Setup contains SMTP host, port, SSL/STARTTLS, username, password/app password, allowed From address, and a permanent HTTPS Properties link. Email is OFF by default. TLS certificates are verified; plaintext SMTP authentication is unsupported. Password fields are blank on every render and preserve the stored secret when left blank. Changing SMTP server or username requires re-entering the secret. Secrets are stored in the local app database, protected as an administrator configuration file; backups can contain them. No password is written into logs or HTML.

One SMTP envelope/To header per person prevents address-list disclosure. Emails include the category, reported timestamp, event details, property name/address/owner, current-stay context at alert creation (actual occupancy, check-in/out, elapsed and remaining stay), or next check-in if vacant. Battery alerts include the actual reported percentage and device name. Door codes are not added to the context. Both plain text and escaped HTML are supplied, with a clickable Properties link.

For Home Assistant, use its external HTTPS address followed by `/hassio/ingress/APP_SLUG?stay_page=properties`. The app handles the authenticated ingress entry and forwards to its Properties list using the same-origin parent referrer. A client-side fallback covers ingress proxies that strip HTTP Referer. Do not use temporary `/api/hassio_ingress/...` session URLs. Recipients still need their own Home Assistant access; verify the live link on a phone. A direct externally hosted app can use its own permanent `/properties` URL.

## Delivery reliability

Events and each destination's delivery state are persisted independently. SMTP runs in a separate background worker thread and does not block lock scheduling. A failed recipient does not cause successful recipients to be resent. Failed pre-DATA attempts and explicit SMTP rejections retry with exponential backoff (one minute up to one hour). Accepted means the SMTP server accepted DATA, not proof of inbox delivery.

A connection lost during DATA, or a process interruption during an email attempt, is UNCERTAIN: the server may already have accepted it. Such messages are not automatically replayed. Staff displays them with a retry action requiring acknowledgment that the inbox was checked; duplicates remain possible if manually retried. Stable Message-ID values help trace messages but do not promise provider deduplication. Pending email can also be delayed by incomplete SMTP settings or a disabled email switch.

HA persistent administrator notices and per-recipient outbound services are separate. Only accepted individual deliveries are marked sent. Existing recipients migrate to both owners/all categories to preserve existing notification behavior. The new alert tables and owner metadata are additive.

## Integration reauthorization

Every five minutes, a read-only HA WebSocket monitor checks ACTUAL `reauth` flows, config-entry titles/domains, and entity-registry membership. It maps the affected entry's tracked locks/thermostats to properties and owners. This distinguishes separate Schlage/Resideo/Lyric accounts; a shared account can legitimately affect both portfolios, with each email containing only the recipient's permitted homes. Unavailable entities alone are not described as reauthorization. No integration is reloaded, disabled or reconfigured.

The monitor uses existing Home Assistant API permission and the Supervisor proxy (`/core/websocket`) inside the app. An unsupported/denied monitor read is logged without disrupting automation; live permission and flow visibility still require verification on the Green. Hostaway reservation-sync failures generate account-scoped alerts without including credentials.

This overlaps the notification-delivery portion of old draft PR #2; it does not merge that PR or its webhook, backup-message and thermostat-retry changes. PR #2 will need reconciliation before a future merge. PR #4 remains separate.
