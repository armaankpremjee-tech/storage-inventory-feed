# storage-inventory-feed

Public fallback copy of the live unit inventory for Cypress Mini Storage and
Lifetime Climate Controlled Storage, refreshed every 5 minutes from Monument's
shopping API by the `refresh` workflow.

The websites (cypresslouisiana.com, lifetimeccs.com) read Monument directly and
only fall back to these files when Monument's API refuses the call, as it did on
19 Oct 2026 when Monument put a CAPTCHA (AWS WAF) in front of the public
storefront key and both sites lost their unit lists.

Files are named by Monument facility UUID:

- `29c2b9eb-699f-11f1-989e-e99e7c079122.json` Cypress Mini Storage
- `29c2b9e2-699f-11f1-989e-e99e7c079122.json` Lifetime Climate Controlled Storage
- `status.json` last successful refresh (heartbeat, committed at least hourly)

Everything here is already public on the two websites and on Monument's
storefront: sizes, rates, availability counts, fees, facility contact details.
No credentials are stored in this repository.

## Site check

`.github/workflows/site-units-feed.yml` loads `/api/monument/unit-groups` and the
homepage of both sites every 5 minutes and opens an issue here (labels
`site-units-down`, `site-units-degraded`) with an @mention when a site cannot show
its units or the safety net is at risk; it closes the issue on recovery. The M4
runs the same scripts every 5 minutes (launchd `com.armaan.site-units-watch`) so
an alert does not depend on GitHub's cron.
