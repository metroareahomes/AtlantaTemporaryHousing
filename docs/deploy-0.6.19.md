# 0.6.19 rollout: sticky menu

Owner explicitly approved deployment of PR #7. The prior confirmed working 0.6.18 source is
preserved at stable-0.6.18-before-sticky-menu (f40c6c5dec5601e62deee77969b0476e765496a0).
Existing Home Assistant backup copies were previously confirmed; a GitHub branch is a source
rollback, not a data backup.

Update the EXISTING Stay Automation app/add-on to 0.6.19 in Home Assistant Settings > Apps
(or Add-ons on older installations). Refresh the app store if the update has not appeared.
Do not uninstall or install a second copy. The app restarts during installation.

After updating, scroll down Properties: Status, Properties, Staff & access, System and the sync
controls should remain at the top. Column labels should stay immediately below them. Confirm
that System opens over the column labels and bottom Save buttons remain available. No settings
need to be re-entered or re-enabled. Existing occupied-reset enablement, temperatures, pacing,
NTC pauses, lock operations and backup messaging retain their saved values and behavior.

This is a CSS/scrolling-only interface change; it adds no database migrations or automation rules.
Separate reliability PR #2 and check-in scheduling PR #4 remain excluded.
