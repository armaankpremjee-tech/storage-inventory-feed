# storage-inventory-feed

Public fallback copy of the live unit inventory for Cypress Mini Storage and
Lifetime Climate Controlled Storage, refreshed from Monument's shopping API by
the `refresh` workflow. Its schedule says every 5 minutes, but GitHub runs it
every 2 to 7 hours in practice, so the M4 dispatches it whenever the snapshot is
more than an hour old.

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
homepage of both sites, probes Monument's storefront captcha and reads recent
storefront leads, and keeps 1 issue per alert here (labels `site-units-down`,
`site-units-degraded`, `bot-leads`, `storefront-captcha-off`), closing each on
recovery. The issues are state only and mention nobody. The M4 runs the same
scripts every 5 minutes (launchd `com.armaan.site-units-watch`) and sends the
owner 1 email per open alert issue, so an alert does not depend on GitHub's cron.
