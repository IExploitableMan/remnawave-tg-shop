# Infrastructure corrections

Requested on 2026-10-13. Keep this checklist with the implementation and verification.

- [x] Clarify the client host address in the node wizard and keep the server IP as default.
- [x] Recover from provider-selection/creation SSH failures without losing wizard data; show progress and errors.
- [x] Back returns to the previous wizard step; provider creation returns to provider selection. Preserve entered values.
- [x] Normalize menu navigation: bottom Back button, parent submenu targets, navigation emojis.
- [x] Localize infrastructure menus, forms and errors in Russian and English.
- [x] Queue new diagnostics durably while another test runs; retain serial execution and block replacement during queued/running jobs. Include a DB migration.
- [x] Small diagnostics: formatted Telegram text, never a JSON attachment as the default report.
- [x] Medium diagnostics: retain Telegram images.
- [x] Large diagnostics: selectable console text in a dark PDF, ANSI colors/backgrounds, aligned columns; preserve failed/busy/N/A/rate-limit results.
- [x] Put the system and network overview first; remove advertising and author credits only.
- [x] Save ANSI output for later rendering; regenerate old reports from saved results without rerunning tests.
- [x] Refresh related panel metadata (name/country/provider/billing/host/SNI/inbound). Detect manual server-IP changes without reusing SSH credentials silently.
- [x] Regression tests: wizard/provider failures and Back, localization, queue/DB constraint, panel edits, short text and PDF content/colors/order.
- [x] Run Python 3.12/Docker tests, migrations, syntax/locale checks; review an exported PDF preview. Do not rerun server benchmarks.

Production and remote test deployments are separate from local verification. Record
any remaining limitation and whether the updated code has actually been deployed.

## Verification

- Python 3.12 test Compose: 119 tests passed, including PostgreSQL queue/index
  migration, actual handler/FSM regressions and offline report reconstruction.
- Alembic revision `0014_infrastructure_job_queue` is the head; bot import smoke
  and Python/locale syntax checks passed.
- Offline PDF preview: selectable text, no embedded images, retained ANSI colors
  and background highlights, readable columns, failures/busy/rate limits preserved.
- Quality/speed benchmarks were not run. Live SSH/provider steps were tested with
  controlled SSH responses/timeouts; this does not claim a new real installation.

Preview and reusable helpers are in `/tmp/infra-ui-fixes`. The plan is complete
for implementation/local verification; production deployment remains separate.

## Dedicated test deployment

Updated `@cridpugbot` on the already authorized stand at 154.86.119.144 only
after confirming there were no pending/running infrastructure jobs. Preserved a
source backup, applied migration 0014 and restarted this bot only. Startup polling
was confirmed, and read-only verification matched all deployed source hashes,
English labels and the running-only PostgreSQL constraint. No node/container
installation, benchmark or production deployment was performed.

Local evidence: `/tmp/infra-ui-fixes/final-all-tests.log`,
`stand-verify.log`, `stand-code-check.log`, and `preview/report.pdf`.
The initial six-second polling-log check was too early; follow-up verification
confirmed successful startup.

## Approved follow-up: existing access and concise reports

- [x] Separate SSH-only capture from explicit client-host editing. Existing nodes
  can have multiple hosts/inbounds; saving credentials does not patch the panel.
- [x] Keep imported nodes out of automatic host exclusion; preserve existing bot
  provisioning behavior. Add migration 0015 and record panel address independently
  of SSH address for safe change detection.
- [x] Keep upload/download independent, including partial failures and busy peers.
- [x] Add local flag icons without shifting console columns.
- [x] Replace UDP/sustained JSON dumps with concise summaries and interval tables;
  summarize DNS/CDN while keeping their errors. Retain raw data separately.
- [x] Capture IP/region/service facts once per job; repeat performance measurements.
  Remove the duplicate IP Check invocation that already includes IP quality.
- [x] Verify with 128 Python 3.12 tests on a fresh dedicated PostgreSQL database,
  migrations, syntax checks and an offline PDF preview. No live benchmarks run.

The follow-up was deployed to the authorized `@cridpugbot` stand after another
idle-job check. Migration 0015, polling and exact source hashes were confirmed.
Evidence: `/tmp/infra-report-polish/final-tests.log`, `stand-deploy.log`,
`stand-code-check.log`, and `preview/report.pdf`. The flag/PDF render checks
passed again after the final image-layout adjustment. Production was untouched.
