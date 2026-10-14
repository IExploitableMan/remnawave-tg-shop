# Infrastructure corrections

Requested on 2026-10-13. Keep this checklist with the implementation and verification.

## Region, credentials and diagnostic follow-up, 2026-10-14

- [x] Use Psiphon's automatic exit selection instead of a fixed DE region. The pinned installer describes auto as fastest available, not necessarily geographically closest. A rerun reconfigures an existing fixed-region installation.
- [x] Explicit All credentials requests send fresh HTML messages; background updates still edit the latest export. Delivery errors are shown, and credentials remain available before installation completes.
- [x] Calculate node numbers from current panel/local names, including bare `node2`, without the historical counter. Migration 0017 permits reuse of archived names without changing archived history.
- [x] Pin the requested IPRegion script, including Gemini Supported, for both medium and large diagnostics. Ignore proxy environment/curlrc for this check and disable debug uploads. Capture it once per job and report missing Gemini results explicitly.

The script's Gemini verdict compares Google's detected country with the supported
region list; it does not authenticate with a Google account or generate a response.
Verification: 86 focused infrastructure tests passed with a dedicated PostgreSQL
database; script hash, privacy/direct-request transforms, Bash and Ansible syntax
checks passed. No performance diagnostics were rerun. Evidence:
`/tmp/infra-followup-tests.log`, `/tmp/infra-followup-static.py`.
These changes were deployed to the authorized remote test stand on 2026-10-14;
production was untouched. Final verification: 161 tests and the live acceptance
cases documented in `infrastructure-acceptance-log.md`.

## Panel deletion follow-up, 2026-10-14

- [x] Reconcile managed nodes with a successfully validated panel node list when opening nodes/archive/cards and during the periodic billing check.
- [x] Archive missing registered nodes, delete their linked billing record (404 is already deleted), and stop billing reminders. Preserve installations not yet registered in the panel.
- [x] Retain SSH credentials, deployments, operation history and test reports. Archived cards offer credentials and both histories, without querying the removed panel node.
- [x] Cancel pending operations and request cancellation of running operations for archived nodes.
- [x] Reject failed or malformed panel responses without treating them as an empty node list. Retry unsuccessful billing cleanup on the next synchronization.

Verification: 80 focused infrastructure tests passed in Docker against a fresh,
dedicated PostgreSQL database, including report/access retention, queue cancellation,
404 handling, billing cleanup failure, and protection of existing/unregistered nodes.
Python syntax and locale JSON checks passed. The temporary database was removed.
Evidence: `/tmp/infra-archive-tests.log`; runner: `/tmp/infra-archive-test-runner.py`.
This follow-up was deployed and verified on the authorized remote test stand.
The real panel's cascade/500-A138 billing behavior is handled using the current
billing list; credentials, report/history fixtures and the archive card were verified.

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

## Agreed queue and node workflow follow-up

- [x] Show only pending/running operations in the queue, with relative positions and readable node/action labels.
- [x] Send one editable progress message per operation and a separate completion message with artifacts and contextual retry/report/node actions.
- [x] Keep readable operation history on the node card.
- [x] Add per-installation profile selection by name, defaulting to the configured profile; retain wizard data and resolve the selected VLESS inbound.
- [x] Open an upgrade details/confirmation screen with actual installed version and latest compatible release; omit the upgrade action when current. Revalidate before execution.
- [x] Move Add node into the Nodes section (verified on the test deployment).
- [x] Refresh documentation/navigation references and verify the approved follow-up on the test bot.

WARP/Psiphon egress options are under research only; installation or routing changes have not been requested yet.

Follow-up verification: 137 Python 3.12 tests passed against the dedicated test
PostgreSQL, followed by 13 focused handler tests after navigation polish. Syntax,
locale JSON and diff checks passed. Migration 0016 and exact deployed hashes were
verified on the test bot. Two live operational-only checks (audit IDs 30 and 31)
ran serially on the existing 104.252.89.173 node; distinct progress/completion
message IDs and report records persisted. Completed jobs disappeared from the
queue. The live upgrade screen retrieved node/GitHub version 3.4.2 and offered
no upgrade action. Default profile name/inbound and Add node placement were
verified against the real panel through the handlers without installing a VPS.
No quality/speed benchmarks or production deployment occurred. Evidence is in
`/tmp/infra-queue-followup/{tests-final,handler-tests,stand-deploy,stand-hashes,live-checks}.log`.

Latest release discovery is restricted to the configured node major/minor line;
this avoids assuming every newer node release works with an older panel. Failed
lookups or unknown installed versions do not expose an upgrade action.

Final checkpoint-count polish: 56 focused infrastructure tests passed, including legacy extra-result handling. The diagnostic catalog is version 3. This changes rendering/progress bookkeeping only; no remote benchmark was run.

## Completion audit gaps found on 2026-10-13

This audit initially found a required localization gap and a low-priority
snapshot synchronization improvement. The earlier completion declaration was
too broad. The owner subsequently noted that panel names/countries rarely change.

- [x] Localize background infrastructure billing reminders and the empty/obsolete
  SSH snapshot messages using each administrator's language.
- Optional improvement (the owner noted these fields rarely change): refresh every administrator's credential snapshot after a node name/country
  changes directly in the panel. Current metadata refresh updates local rows but
  does not trigger snapshot publication. Preserve verified SSH addresses and
  credentials; never infer new SSH access from a changed panel address.

Localization correction: billing reminders, their action button and empty/obsolete
SSH snapshot notices now resolve each recipient's DB language independently,
falling back to the bot default. Tests covered English and Russian recipients,
upcoming/overdue deadlines, HTML escaping and shrinking snapshot message lists.
All 58 focused infrastructure tests, syntax/JSON and diff checks passed.
No quality benchmarks or remote node operations were run for this correction.
Evidence: `/tmp/infra-localization-tail/`.

The correction was deployed to the authorized @cridpugbot on 154.86.119.144 after an empty-queue check. Polling startup and exact deployed source/locale hashes were verified; production was untouched.
