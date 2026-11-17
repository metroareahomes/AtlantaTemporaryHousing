# 0.6.16 deployment preparation

Status: release candidate on PR #3. Not merged or installed.

## Candidate
- Production source before this candidate: 774f4b93bd421cfcb249c2b7dab3c2a9dce0bfdf (0.6.15).
- Rollback source branch: stable-0.6.15-before-ui.
- UI work only; reliability PR #2 remains separate.
- Existing add-on slug, options, architecture and data path are preserved.
- No database migrations, dependency changes or control-loop changes are included.
- Only web.py's display context import and Setup help data were added outside templates/static assets.

## Before installation
1. Create a fresh Home Assistant backup that includes the installed Stay Automation add-on and its data.
   Record the backup name/time and confirm it completed. The GitHub rollback branch is not a data backup.
2. Record the installed version (expected 0.6.15) and the installed repository source.
3. Obtain explicit approval to merge/publish this candidate and install it on Home Assistant.
4. Run the final full test suite and browser job on the candidate before merging.
5. Merge only PR #3. Keep PR #2 unmerged.

## Installation after approval
- Refresh the Home Assistant add-on store and confirm the existing Stay Automation installation offers 0.6.16.
- Update the existing add-on; do not uninstall it or install a second copy.
- Plan for the update to restart the add-on. It will temporarily pause its reconciliation loops;
  existing codes remain in the locks, but this does not eliminate all operational risk.
- Avoid the period immediately around a scheduled code installation or guest arrival.
- Do not change code schedules, property automation switches, backup codes or thermostat temperatures
  merely to test the interface.

## Post-update checks
- Confirm version 0.6.16 and normal Hostaway synchronization.
- Confirm existing property/device assignments and saved settings are present.
- Scroll Properties vertically and sideways: headings should remain aligned and Save should stay visible.
- Check Setup explanations and question-mark controls on desktop and phone.
- Save an unchanged form to check submission; do not toggle automation or edit guest access for this check.
- Confirm normal lock and thermostat activity in the log without adding new errors.
- If a phone test is wanted, get approval to send it; test delivery goes to configured recipients.

## Rollback
If the update causes operational trouble, stop further changes and restore the fresh pre-update
Home Assistant backup. The GitHub branch preserves the previous source but is not itself a one-click
Home Assistant downgrade. Do not uninstall the add-on as a rollback method.

## PR #2 afterwards
After 0.6.16 is stable, update PR #2 against the new main branch and run the combined Python suite,
browser checks and a separate release review. Do not deploy the retry changes with this interface update.
