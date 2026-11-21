# Guest Extension Pricing

Install this app alongside Stay Automation and enter the Hostaway account ID and API key in its Configuration. In the app, select **Use Stay Automation webhook** to forward the existing webhook to both apps. A previously registered Guest Extension Pricing webhook is disabled after the shared route is linked.

The pricing automation is off on first install. Confirm an incoming Hostaway message appears under **Hostaway connection**, then set the adjustment, booked-night range, and optional listing tags before enabling. The automation uses the booked arrival and departure dates and adjusts consecutive blocked nights beginning on checkout day. It recognizes common English extension phrases in incoming message text.

The app saves the adjusted rates at 12:45 a.m. Eastern by default and reapplies them at 4:00 a.m. after the 2:00 a.m. PriceLabs run. Both app times are editable. An opened date is dropped from tracking and keeps its current rate until PriceLabs next updates it.

The app records one adjustment per listing and night. The overnight rewrite uses that same saved rate. Opening and later reblocking a date does not create a new adjustment.

Booked marked-up nights lists each date taken by the requesting guest before the next 2:00 a.m. Eastern PriceLabs sync. The report assumes the booking took the saved rate and credits the difference from the original nightly rate. It uses Hostaway reservation webhooks and matches the original reservation or the same channel guest account on a separate booking. Other guests' bookings do not count. After the night opens, the app stops rewriting it.

Select **Base extension prices on the booked stay’s average nightly rate** to divide the reservation’s Hostaway accommodation charge by its booked nights. Enter a positive percentage to mark up that average, or leave the percentage blank to use the average alone. This option only raises blocked nights priced below the resulting target. The app needs the booking’s accommodation charge to calculate the target.

The ledger compares the increase from the unmarked stay-average target with the net increase at the final saved rate, both relative to the original calendar rate. The average-only increase is zero when the calendar rate was already at or above the stay average. A dash indicates that no stay average was recorded.

Use **Mike’s homes** and **Kurt’s homes** to set independent enable switches, markups, booked-night ranges, pricing methods, and schedules. Each tab shows that owner’s ledger. Blank markups mean zero. The app uses existing listing tags. Listings with neither tag or both tags are skipped.
