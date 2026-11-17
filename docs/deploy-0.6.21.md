# Deploy 0.6.21

User authorized release on November 17, 2026. Source rollback: stable-0.6.20-before-verification-fix.

338 local automated tests passed, including delayed target readback and missing/invalid recipient validation. Require release-head CI and browser checks before merging.

Install in Home Assistant: Settings → Apps → Stay Automation → Update. After startup confirm version 0.6.21. Watch the next ordinary thermostat adjustment: an old target gets one additional local-state read after 40 seconds without another temperature command. Confirm recipient test selection shows a friendly message if absent. No live-device test was run during development.

Known issue: integration reauthorization monitor response may exceed 1 MB; separate fix pending. SMTP delivery still depends on saved enabled server configuration.

For rollback use the existing app/data backup as needed and the preserved 0.6.20 source branch. Source rollback alone is not a data backup.
