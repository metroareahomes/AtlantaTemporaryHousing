# Guest Extension Pricing

Install this app from the same Home Assistant app repository as Stay Automation. In Configuration, enter a Hostaway account ID and API key and your Home Assistant public HTTPS address (the origin only). Start the app and open its web UI. Choose **Connect Hostaway** to create the app's webhook and Home Assistant forwarding automation.

Hostaway must accept an additional webhook. If registration fails, check the app's connection details and the account's webhook capacity. The connection button registers only this app's webhook URL.

The pricing automation is off on first install. Confirm an incoming Hostaway message appears under **Hostaway connection**, then set the adjustment, booked-night range, and optional listing tags before enabling. The automation uses the booked arrival and departure dates and adjusts consecutive blocked nights beginning on checkout day. It recognizes common English extension phrases in incoming message text.

The app saves the adjusted rates at 12:45 a.m. Eastern by default and reapplies them at 4:00 a.m. after the 2:00 a.m. PriceLabs run. Both app times are editable. An opened date is dropped from tracking and keeps its current rate until PriceLabs next updates it.

The app records one adjustment per listing and night. The overnight rewrite uses that same saved rate. Opening and later reblocking a date does not create a new adjustment.

Booked marked-up nights lists each date taken by the requesting guest before the next 2:00 a.m. Eastern PriceLabs sync. The report assumes the booking took the saved rate and credits the difference from the original nightly rate. It uses Hostaway reservation webhooks and matches the original reservation or the same channel guest account on a separate booking. Other guests' bookings do not count. After the night opens, the app stops rewriting it.

Select **Base extension prices on the booked stay’s average nightly rate** to divide the reservation’s Hostaway accommodation charge by its booked nights. Enter a positive percentage to mark up that average, or leave the percentage blank to use the average alone. This option only raises blocked nights priced below the resulting target. The app needs the booking’s accommodation charge to calculate the target.

The ledger compares the increase from the unmarked stay-average target with the net increase at the final saved rate, both relative to the original calendar rate. The average-only increase is zero when the calendar rate was already at or above the stay average. A dash indicates that no stay average was recorded.

Use **Mike’s homes** and **Kurt’s homes** to save separate rate adjustments and see each owner’s booked-night ledger. The app assigns the owner from the listing’s Hostaway tag when it saves a rate. Blank owner adjustments mean zero markup. The shared percentage applies only outside the owner tabs. A saved owner adjustment uses the owner’s existing listing tag automatically. Legacy ledger entries without a recorded owner are visible on **All homes**. Listings with both owner tags are skipped when an owner adjustment is configured.
