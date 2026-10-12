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

When enabled, **Admin → Infrastructure → Settings** selects the existing profile,
VLESS inbound on port 443, panel server IP and tg squad. The panel IP must be its
actual outbound address toward nodes, not necessarily its reverse-proxy address.
Debian 12/13 and Ubuntu 24.04 are supported. Root SSH password access is retained.
Adding a public administrator key queues durable propagation to managed servers,
including servers currently busy with another job; existing
panel nodes first need their SSH credentials attached. Other authorized keys are
preserved.

The two editable SNI pools are independent of the common profile. A chosen SNI
must be allowed by the selected REALITY inbound; unsupported values fail before
installation instead of modifying every server's profile. Pool edits never
silently rotate existing host selections.

## Jobs, replacement and billing

Jobs survive bot restarts. A partial interrupted job is marked interrupted for
manual retry, not blindly restarted. One active job per node is enforced by a
PostgreSQL partial unique index; an advisory lock elects one infrastructure worker.

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

## Diagnostics

- Small: container, listening ports, panel connection and external TCP 443. A TCP
  check does not assert a complete authenticated VPN session.
- Medium: the full one-pass diagnostic catalog, image pages sent through Telegram.
- Large: the medium catalog plus stability/UDP/IPv6/DNS/route/CDN/memory checks,
  repeated measurements and a PDF. Default repetitions: three.

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

- 89 local unit/database tests pass. Alembic upgrade and Ansible syntax check pass.
- Actual panel 2.8.1 scenarios passed: base/add-on purchase and extension, status,
  traffic reset, Telegram lookup, accessible nodes and device listing.
- The same panel database was restored and migrated to 3.4.5. The same scenarios
  passed with preserved bot references, plus new numeric-ID user creation,
  device creation/removal and idempotent user deletion.
- Clean Debian 13 VPS installation passed with node 2.8.0, root password and key,
  BBR/FQ and restricted management port. Actual Infra Billing extension, optional
  expense, expiry exclusion and restoration passed on panel 2.8.1.
- Nine medium diagnostic steps completed on the real VPS; their checkpoint is
  retained. Image layouts were reviewed against actual output.
- A clean replacement panel host received Docker and the same restored database.
  Its 19 pending panel migrations were applied explicitly, the API harness passed
  against panel 3.4.5, and the migrated node reported `isConnected=true`.
- The existing real node was upgraded to 3.4.2. Its upgrade job now waits for
  container startup, management-port readiness, panel connection and external
  TCP 443 before marking verification complete.

## Pending acceptance

- Replace the VPS while preserving panel node and host UUIDs; check Ubuntu 24.04
  provisioning and key propagation.
- Run repeated large diagnostics and inspect the saved PDF.
- An authenticated VPN session from outside the test server (currently failing).
- Telegram interaction with the complete updated test bot.
