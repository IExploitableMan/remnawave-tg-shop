# Infrastructure operations

Implementation branch: `feat/panel-v3-infrastructure`, based on `main`.
Server acceptance checks are required before production use.

## Configuration

- `PANEL_API_VERSION=auto` reads `/system/metadata`. Explicit overrides: `2` or `3`.
- The bot synchronizes durable panel-scoped UUID/ID mappings at startup. Existing
  UUID links remain intact. New v3 users have a local stable reference in the
  historical `*_uuid` columns; that local reference is never sent to the v3 API.
  Numeric IDs are stored independently. Changing the panel URL requires review
  of identity mappings, since it changes the scope.
- Start the compatible bot against panel 2.8.1 before upgrading if possible,
  so existing numeric IDs are captured before UUID removal.
- Panel 3.4.5 uses `APP_SECRET` instead of the two old JWT secrets. Preserve
  the old `JWT_AUTH_SECRET` as `APP_SECRET` so the administrator password remains
  valid; create a new API token after migration and update the bot configuration.
  The old API token must not be assumed valid.
- Infrastructure is disabled by default: `INFRA_ENABLED=False`.
- Generate `INFRA_ENCRYPTION_KEY` with:
  `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
  Keep this value stable across restarts; changing it prevents password decryption.
- Mount a persistent `INFRA_ARTIFACT_DIR` (default `/app/infra-data`). It contains
  reports and the automation SSH private key. Never commit this directory.
- Optionally mount your own automation key and set `INFRA_SSH_PRIVATE_KEY` to its
  path. Otherwise an Ed25519 key is created with private filesystem permissions.

When enabled, **Admin → Settings → Infrastructure → Settings** selects the existing profile,
VLESS inbound on port 443, panel server IP and tg squad. The panel IP must be its
actual outbound address toward nodes, not necessarily its reverse-proxy address.
Debian 12/13 and Ubuntu 24.04 are supported. Root SSH password access is retained.
Adding a public administrator key queues durable propagation to managed servers,
including servers currently busy with another job; existing
panel nodes first need their SSH credentials attached. Other authorized keys are
preserved.
Attaching SSH credentials does not require an Infra Billing record. An unknown
paid-until date remains unknown; a later panel billing link is discovered from
the panel node UUID. Migration `0013_infra_unknown_deadline` permits that state.

The two editable SNI pools are independent of the common profile. A chosen SNI
must be allowed by the selected REALITY inbound; unsupported values fail before
installation instead of modifying every server's profile. Pool edits never
silently rotate existing host selections.

## Jobs, replacement and billing

Jobs survive bot restarts. A partial interrupted job is marked interrupted for
manual retry, not blindly restarted. Multiple pending diagnostics can wait durably
in FIFO order. One running job per node is enforced by a PostgreSQL partial unique
index; an advisory lock elects one infrastructure worker.

Replacement installs the new VPS and retains the existing panel node UUID. Node
and host addresses are switched together with compensation on failed checks.
After successful switching, the active deployment and its credentials are persisted
before billing operations. Previous deployments and report history are retained.
No VPS deletion or server wiping is performed.

Provider records and billing deadlines are authoritative in panel Infra Billing.
The per-node duration suggests an extension amount. Renewal extends from the later
of the current deadline and now. Optional expenses are recorded through the provider
card or with an extension amount. Notifications default to on for all administrators.
Credential snapshots are delivered to every administrator independently of reminder
opt-out; inaccessible Telegram chats do not cause node installation to fail.

Automatic tg exclusion is optional. It changes only that host's access; it does
not stop the remote container or guarantee immediate disconnection of clients with
cached configurations. Restoring access uses the saved previous restrictions.
Manual delete removes selected panel objects, retaining local diagnostic history.
Archived node reports remain accessible through the read-only archive menu.

## Diagnostics

- Small: container, listening ports, panel connection and external TCP 443,
  delivered as a formatted Telegram message (including failures). JSON is kept
  internally for history. A TCP check does not assert a complete authenticated VPN session.
- Medium: the full one-pass diagnostic catalog, image pages sent through Telegram.
- Large: the medium catalog plus stability/UDP/IPv6/DNS/route/CDN/memory checks,
  a dark PDF with selectable monospaced console text, ANSI colors/backgrounds
  and configurable repeated measurements. Default repetitions: one.
  Extra passes compare variable network performance; they are optional.

Diagnostic sources are pinned by SHA-256 in `infra/diagnostics/scripts.json`.
Changed upstream content fails verification and must be reviewed before updating
its hash. IP Check report upload, YABS arbitrary JSON upload, run counters and RIPE
Atlas public measurement creation are disabled. Geekbench publication and Ookla's
normal measurement/result handling are explicitly allowed by the owner.
No external report/image hosting is used. Scripts still query
measurement endpoints, install tools and download benchmark binaries.

Results belong to the deployment/IP at test time. Reports and structured results
are local, persistent and retrievable through the node card. Interrupted partial
output stays local and manual retry resumes completed checks for the same deployment
and diagnostic level; completed reports have a database history entry. Automated
report retention and deletion of historical secrets are not yet implemented.
If all measurements are saved, retry only renders/delivers the report and does not
connect to the VPS or download scripts again.

## Local verification

The test Compose stack has its own network and temporary PostgreSQL. It does not
connect to the application's database or Telegram API.

```bash
docker compose -f docker-compose.test.yml run --build --rm test-runner python -m unittest discover -s tests
docker compose -f docker-compose.test.yml run --rm test-runner python -m alembic upgrade head
docker compose -f docker-compose.test.yml run --rm test-runner python -m ansible.cli.playbook --syntax-check -i node, infra/ansible/provision.yml
```

Do not apply production migrations or install remote servers as part of local tests.

## Acceptance evidence

- All 106 local unit/database tests, Alembic upgrade and Ansible syntax checks pass.
  Embedded migrations preserve application and aiogram logging configuration.
- Actual panel 2.8.1 scenarios passed: base/add-on purchase and extension, status,
  traffic reset, Telegram lookup, accessible nodes and device listing.
- The same panel database was restored and migrated to 3.4.5. The same scenarios
  passed with preserved bot references, plus new numeric-ID user creation,
  device creation/removal and idempotent user deletion.
- Clean Debian 13 VPS installation passed with node 2.8.0, root password and key,
  BBR/FQ and restricted management port. Actual Infra Billing extension, optional
  expense, expiry exclusion and restoration passed on panel 2.8.1.
- The medium diagnostic suite completed all eleven steps, reusing nine completed
  checks after interruption. Saved image layouts were reviewed against real output.
- A clean replacement panel host received Docker and the same restored database.
  Its 19 pending panel migrations were applied explicitly, the API harness passed
  against panel 3.4.5, and the migrated node reported `isConnected=true`.
- The existing real node was upgraded to 3.4.2. Its upgrade job now waits for
  container startup, management-port readiness, panel connection and external
  TCP 443 before marking verification complete.
- Replacement on Ubuntu 24.04 passed: the existing panel node, host and billing
  UUIDs remained unchanged. Root password, automation key, additional administrator
  key, BBR/FQ and management-port firewall checks passed.
- Actual v3 Infra Billing extension, optional expense, expiry exclusion and access
  restoration passed. Disposable manual deletion removed only the selected objects.
- An authenticated external VLESS/REALITY session confirmed the new VPS exit IP.
  The acceptance profile's original Microsoft target caused handshake failure;
  changing only the test target/SNI to Cloudflare fixed it. A related upstream
  report is [Xray-core issue 6356](https://github.com/XTLS/Xray-core/issues/6356).
  Application code never rewrites a shared profile to fix a chosen host SNI.
- Twenty-nine synthetic Telegram updates exercised the actual handlers/FSM against
  the remote panel, including settings, provider/SNI/key menus, encrypted wizard,
  credential authorization and archived-node history.
- Large diagnostics on the 1 CPU / 1 GB / 10 GB replacement VPS completed two
  passes, with 38 saved outcomes and a 48-page PDF delivered through Telegram.
  There were 29 complete, six partial and three failed outcomes; unavailable
  endpoints and missing Geekbench scores remain explicit instead of becoming
  zero-speed measurements. Default repetitions are now one.

- The updated test bot runs on the replacement panel server with the restored bot
  database, automation key and report history. Telegram polling is confirmed;
  the previous test poller is stopped with automatic restart disabled.
- Additional real-server checks passed without repeating quality benchmarks:
  an injected health-check failure after switching the host rolled the panel node
  and host back and preserved the original active deployment/credentials. A second
  real Debian node used `node76 (FI)` after a manually created panel `node75`, kept
  its individual 15-day period, and received its automatic host and billing entry.
  A new global key authenticated root access to both managed nodes. Deleting the
  temporary node removed its panel/host/billing objects without touching the first.
- Actual OS reboots on the Ubuntu node and replacement panel server preserved
  Docker startup, root password/key access, UFW and BBR/FQ. Panel, PostgreSQL, Redis
  and bot recovered automatically. A new authenticated external VPN session passed
  after both reboots; the management port remained blocked outside the panel.
- A manually installed SSH key outside the bot's key list remained usable after
  synchronization; the temporary key was removed and the original authorized keys
  were restored. The current administrator UI supports adding/synchronizing keys,
  not deleting them.

Final acceptance details, including the restored legacy 2.8.1 stack, required
`forceRestart` body in both APIs, API-outage recovery, SSH-only adoption, mixed
panel/node versions and a clean Debian 12 KVM guest, are recorded in
[infrastructure-acceptance-log.md](infrastructure-acceptance-log.md). Temporary
Debian guest panel objects/firewall rule were removed and its VM stopped; ordinary
test panels, bot and nodes remain running. No quality suites were repeated.

## Wizard and panel edits

The client address is the IP/domain placed in the subscription; it defaults to
the VPS address. Back returns through wizard steps without discarding entered
values; Back from provider creation returns to provider selection. SSH fingerprint
retrieval shows progress and handles timeouts while retaining the form. Menus and
forms use the Russian/English locale catalog and bottom parent-navigation buttons.

Node cards/queued commands refresh panel names, countries, providers, billing
bindings, host addresses, SNI and selected inbounds. Deleted billing bindings are
cleared; ambiguous host links are not guessed. If the panel's node IP changes,
commands require fresh SSH access. Saving access for that new panel IP creates a
new deployment and retains the previous deployment's credentials/report history.
Replacements wait for queued/running jobs, so they cannot move a measured VPS.

Migration `0014_infrastructure_job_queue` changes the unique constraint to running
jobs only; deploy it together with this code (normal bot startup applies it).
Old reports are rebuilt from saved results on opening; no remote diagnostic
commands are run. Old checkpoints without ANSI cannot recover original colors;
status highlighting is applied conservatively. System/network overview comes
first, and failures/busy/N/A/rate limits are retained.

The correction checklist and local validation are in
[infrastructure-fixes-plan.md](infrastructure-fixes-plan.md).

## Approved SSH-only import and report polish

An existing panel node can be attached and have its SSH address/port/root password
verified and stored separately from client-host editing. This does not install
software, create hosts, patch panel settings or queue a key update. Hosts/inbounds
may be ambiguous or absent; SSH-only records allow unknown inbound bindings.
Imported nodes are excluded from automatic host expiry changes, including when
the global expiry setting is enabled. Nodes created through provisioning retain
that behavior. Explicit host editing/renewal/replacement remain separate actions.
Migration 0015 identifies existing imported nodes by the absence of provisioning
job history and stores the captured panel address separately from SSH address.

Each job captures IP region, geoblocking, DPI, service availability, IP Check and
IPv6 availability once. Network/CPU/disk/memory performance, DNS/CDN delays, route,
UDP and sustained measurements repeat according to the configured pass count.
IP Check already contains service and IP-quality information, so its duplicate
invocation was removed. The catalog now has 10 unique medium checks and 18 large
checks: three large passes schedule 42 checks instead of repeating all facts.
Saved completed outcomes are reused when resuming; raw historical data are kept.

The PDF uses compact UDP summaries (sender/receiver, measured speed/time, packet
loss, jitter, errors), sustained interval tables, and DNS/CDN summaries. Plain
console tables retain their layout; upload/download metrics are independent.
Country icons are local assets placed in existing padding; no CDN is queried at
render time. Missing values and failures remain explicit. Renderer version 3
rebuilds older reports from saved output without re-running measurements.

## Operation queue and confirmed upgrades

The infrastructure menu shows only pending/running operations in FIFO order.
Waiting positions are relative; running checks show completed/total counts. Each
operation has a persistent editable Telegram progress message. Its outcome is a
separate message with node/action, elapsed time, diagnostic issues and relevant
report/retry/node buttons; report files follow it. Telegram delivery failures do
not roll back infrastructure work, and saved reports remain accessible separately.
Operation history lives in the node card. Retry of a completed diagnostic starts
a fresh measurement; retry of an interrupted/failed diagnostic reuses checkpoints.

Add node lives in the Nodes section. The wizard shows the configured profile's
name, supports selecting another profile for that installation and asks for a
VLESS/443 inbound when the choice is ambiguous. Global defaults remain unchanged.

The node upgrade button opens a read-only details screen first. It uses the node
version reported by the panel and stable GitHub releases in the configured
major/minor node release line. A current/newer node has no upgrade action. Unknown
versions or failed release lookup require a refresh, rather than an assumed
upgrade. Confirmation persists the target version in the job and execution
revalidates it. Opening the screen does not run Ansible.

Migration 0016 persists progress/completion message IDs, start time and confirmed
upgrade target. Verification: 137 local tests and two real queued operational
checks passed on the dedicated test bot on 2026-10-13. See the acceptance log.
WARP integration is deferred. Psiphon/Gemini routing is implemented for panel
2.x and 3.x and verified on the dedicated test stand; see the acceptance log.

Gemini enable uses the existing host's inbound, installs the loopback-only proxy,
and assigns a checked copy of the profile while retaining all active inbounds.
Missing SSH access opens the credential flow. The installer uses automatic exit
selection instead of a fixed country. Panel-side Gemini selection is checked
periodically; missing access triggers a notification rather than an installation.
Disabling restores the recorded original profile and host binding. A core reload
can interrupt connections briefly; the unchanged client was observed to recover
with an 8.05-second gap between successful HTTP samples on the test stand.

Node deletions in the panel archive the local node and retain access/tests/history.
Current billing is checked before deletion because some panels return 500 for a
record already removed by cascade. Migration 0017 makes node names unique only
among active records, so archived names do not reserve the next node number.

Final checkpoint-count polish: 56 focused infrastructure tests passed, including legacy extra-result handling. The diagnostic catalog is version 3. This changes rendering/progress bookkeeping only; no remote benchmark was run.

### Manual tg host visibility

The node card reads the current panel host restrictions and toggles only the configured tg squad. Other squad restrictions, inbound settings and host disabled state remain unchanged. Multiple linked hosts require explicit selection when there is no recorded primary host. Manual hiding is retained on renewal; renewal restores only restrictions saved by the expiry worker. The Excluded Internal Squads property belongs to hosts in Panel API 2.x, and is represented by internalSquads in API 3.x.
