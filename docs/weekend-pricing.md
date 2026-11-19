# Weekend last-minute pricing: staged design and simulation

## Current deliverable

Development branch only. No production deployment, pricing PUT, restoration write or recurring pricing job is included. Manual simulations start disabled, with no test property selected. Existing Hostaway credentials/token/client are reused; no credential is copied into pricing settings, records, HTML or logs. A separate pricing-only lock protects manual simulations; no lock/thermostat work lock is acquired. No device methods are called. There is no change to the main scheduler.

Setup → Weekend last-minute pricing contains editable controls, plain-language question-mark help and its own Save button. Defaults:

| Control | Default |
| --- | --- |
| Discount | 10% |
| Current nightly rate must exceed | $200 USD |
| Nominal check-in within | Next 24 elapsed hours |
| Promotion start / first check | Friday, 14:00 Eastern |
| Second check | Saturday, 06:00 Eastern |
| Third check | Sunday, 06:00 Eastern |
| Stop | Monday, 00:01 Eastern |
| Test home | None; one active Hostaway property must be chosen |
| Manual simulations | Disabled |
| Live writes / recurring activation | Not available |

The days, times, percentage, threshold, hours and property can be saved without activating a recurring schedule. Eastern is America/New_York and follows DST. The simulation time defaults to the next start; it can be changed to within 14 days of now. It reads rates available NOW for the chosen dates, not a forecast of what PriceLabs will send before that future time. Checks and stop must be in order within one week of start. A scheduled time repeated by a fall DST change uses its first occurrence; the usual defaults are outside the repeated/missing hour. The manual preview rejects nonexistent spring-forward local times.

$200 is an eligibility threshold, not a final-price floor: $200 is excluded, while $210 becomes $189. Round to cents using decimal half-up arithmetic. The eligibility window uses the listing's confirmed checkInTimeStart and local date, not midnight; for example, Friday 14:00 includes Friday 15:00 but not Saturday 15:00 (25 hours later). Passed nominal check-in times are excluded, including late same-day bookings after that time. The test listing must explicitly report USD and America/New_York. No base price is used.

The preview includes current rate, proposed rate, date/check-in, minimum stay, result/reason, observed channel-processing flag, optional provider update value and calendar ID. The audit stores settings, observation time and simulated time. It does not store calendar reservation objects, guest names, door codes, Wi-Fi details or API tokens. Reports are history: saving new settings does not retroactively change the old proposal. Re-running a simulation always rereads Hostaway and never changes/compounds calendar prices.

## API findings

Verified against Hostaway's public API documentation:

- `GET /v1/listings/{listingId}`: confirm identity, currencyCode, timeZoneName and checkInTimeStart. Return only these fields to the pricing module.
- `GET /v1/listings/{listingId}/calendar?startDate=YYYY-MM-DD&endDate=YYYY-MM-DD&includeResources=1`: source actual nightly `price`, date, availability/status, arrival restriction and minimum stay. The code limits the read to one listing and up to four date intervals, rejects missing/duplicate/out-of-range dates and does not retain returned reservation details.
- Future approved writes would use `PUT /v1/listings/{listingId}/calendar` with identical startDate/endDate for one date and only the absolute `price`. Do NOT copy the documentation's availability/restriction fields into a price-only update. Do NOT write `isProcessed`.
- `isProcessed` is described as an informational flag indicating processing to all connected channels. It is useful evidence, not independent proof of the guest-facing Airbnb quote or of a particular channel's final tax/fee/markup/discount-adjusted rate.
- The client currently authenticates using `scope=general`. The account's actual calendar permissions and listing coverage have NOT been verified with live credentials. A read-only simulation verifies reads only; a later approved write test verifies write access.

Sources:

- https://api.hostaway.com/documentation — calendar day object, Retrieve a calendar, Update a calendar; listing object metadata.
- https://support.hostaway.com/hc/en-us/articles/360003084814-How-to-connect-with-PriceLabs — PriceLabs → Hostaway rates and checking Hostaway/Airbnb after syncing.
- https://support.hostaway.com/hc/en-us/articles/49780010006043-Channel-Integration-Requirements-and-Limitations — API channel restrictions, including Airbnb custom promotions.
- https://help.pricelabs.co/portal/en/kb/articles/timed-sync — overnight/timed syncs and additional updates after bookings.

## First-night-only limitation

A calendar night has a date-based price. Lowering Friday's rate can affect a Thursday-arriving reservation's later night as well as a Friday arrival's first night. The verified calendar endpoint has no reservation-night-position condition. Therefore first-night-only eligibility cannot be guaranteed by this mechanism. Do not market the preview as a first-night-only promotion or deploy writes under that assumption.

The user must either approve the wider calendar-date effect or a different reservation/channel-specific mechanism must be verified. Airbnb custom promotions are not automatically an alternative: Hostaway documents restrictions on them for connected listings. Do not alter length-of-stay rules or minimum stays just to simulate a first-night-only offer.

## Availability and bookability

Only days with status `available`, isAvailable=1, explicitly open arrivals, no returned reservations/blocking inventory and no multi-unit fields receive a proposal. Unknown arrival restrictions, blocked/pending/reserved dates and unsupported multi-unit inventory are skipped with a reason. Missing data is never interpreted as permission to write. Nullable arrival fields need their live semantics confirmed; this first version conservatively skips null.

A proposal means the DATE passed these checks. It is not a guarantee that an entire future stay is bookable under minimum nights, later availability, departure restrictions, channel cutoff times or occupancy rules. The minimum stay is shown, not changed. Full stay/quote validation belongs in the one-property channel test.

## PriceLabs ownership and restoration: blockers before any live activation

The original suggestion that PriceLabs runs only overnight is not enough to coordinate writers. PriceLabs documents timed extra syncs and booking-triggered recalculations. Hostaway's documented calendar object does not provide a reliable last-writer identity or conditional compare-and-set guarantee. A price difference cannot be assumed to be a new PriceLabs baseline, and equality with our last write does not prove PriceLabs has not legitimately written the same value (the ABA/equal-value problem).

Before a one-property live test, establish a controlled ownership window for that property: confirm actual PriceLabs sync settings, additional syncs/booking triggers, other pricing automations and manual writers. Pause or otherwise coordinate that property's PriceLabs writes only with user authorization; do not change fleet-wide PriceLabs settings. A pricing lock in this app cannot exclude another platform. Without coordination, this version must remain a simulation.

The future write ledger must persist listing/date, promotion ID, source rate and observation metadata, absolute intended discounted rate, request time, confirmation time, processing observation and restoration state. Persist intent BEFORE sending; an uncertain POST/PUT outcome requires a fresh GET rather than blind replay. Dates already at the recorded promotion rate must not receive another percentage discount. If an external change is observed, mark conflict and stop writing; do not assume authorship and discount again automatically. Re-read availability, arrival rules, current rate, campaign state and property authorization immediately before any approved write. Verify afterward with a fresh read. Do not replay a stale absolute update through generic transient retries.

At promotion end, stop discount writes. Never blindly restore the first observed price. A candidate future restoration needs provable ownership/coordination, a fresh read matching the owned rate, current unbooked status and a still-relevant future date. Newer legitimate PriceLabs/manual changes must be left alone. If provenance is uncertain, skip and flag for review instead of overwriting. Even a last-price comparison alone cannot prevent an external writer racing between GET and PUT; this requires source coordination, not an optimistic-compare claim.

With the default window and Monday 00:01 end, nominal check-ins for promoted dates have already passed by the stop. Past or booked nights should not be rewritten just to tidy up a promotion. There may be no future rate to restore. Restoring calendar rates must never modify an existing reservation's contracted price. Changing/shortening a promotion mid-run also needs owned-future-date cleanup and cannot be treated as merely disabling a checkbox.

Restoration is consequently NOT implemented or scheduled in the read-only stage. Future activation must not be approved until the ownership/conflict policy and any required cleanup process are tested.

## Staged validation

1. Review this design, especially calendar-date scope versus first-night-only and the threshold-versus-floor interpretation.
2. Approve installation of the simulation-only code when ready; it does not enable recurring runs or writes. Select one test property in Setup, save controls, allow manual simulations, then run the Friday/Saturday/Sunday previews. No property was selected arbitrarily during development.
3. Check the proposal's current rate against the same Hostaway calendar date. Confirm currency, local check-in hour, restrictions, channel mapping, existing markup/discounts and the listing-specific PriceLabs sync timing. Actual live API permissions still need this step.
4. Only after explicit approval, implement/enable a tightly bounded one-property live test with the persistent intent/ownership ledger and controlled writer window. No such route exists in this branch.
5. Read back the Hostaway rate; inspect its processing flag over time. Independently verify Airbnb using the mapped listing, dates, guest count and an equivalent guest-facing quote. Compare nightly accommodation rate separately from taxes, cleaning, platform fees, markups and existing discounts. Hostaway readback alone does not verify Airbnb.
6. Exercise re-run/no compounding, concurrent external updates, uncertain outcomes, booking between reads, disable mid-promotion, Monday cutoff and any applicable restoration. Do not expand or activate the recurring schedule until the user reviews this test and approves it.

## Scheduling to implement only after approval

An independent worker may eventually evaluate the three configured event instants and the end instant in Eastern, using persisted promotion/event IDs to prevent duplicate execution after restart. Startup catch-up must not replay an expired discount or ignore required cleanup. Discount evaluation happens at Friday activation and Saturday/Sunday checks; it is not continuous enforcement between those instants. PriceLabs changes between runs can remove a discount unless the ownership/sync arrangement addresses that. The end/cleanup path must remain able to run even if new discounts are disabled.

This branch does not register that worker. Its failure paths cannot cancel the lock/thermostat scheduler because it has no scheduled job. The two pricing GETs are awaited asynchronously; existing credentials and Hostaway API quota are shared, so manual reads are serialized and limited to one attempt per 30 seconds. Errors show generic read/validation messages without response bodies/secrets. Pricing audit tables are additive and do not change device or reservation tables.
