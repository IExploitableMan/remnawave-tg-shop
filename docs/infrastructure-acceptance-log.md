# Infrastructure acceptance log

Recorded on 2026-10-13. Times in this log use Europe/Moscow.
Implementation: `feat/panel-v3-infrastructure`, based on `main` (`f40aa04`).
Feature commit: `f56aca3`; implementation fixes: `16d168b`.
Final live checks also found and fixed the 2.8.1 restart request body described below.
Only dedicated test infrastructure was used. No real payments were processed.

## Stand

| Host | Role | Operating system |
| --- | --- | --- |
| 95.182.88.225 | Original panel 2.8.1 and test bot; bot subsequently stopped | Debian 13 |
| 95.182.88.203 | Original node; later replacement/second-node fixtures | Debian 13 |
| 104.252.89.173 | Active replacement node, 1 CPU / 1 GB RAM / 10 GB disk | Ubuntu 24.04 |
| 154.86.119.144 | Replacement panel 3.4.5 and updated test bot | Ubuntu 24.04 |

Node versions tested: 2.8.0 and 3.4.2. Panel and node release versions are independent.

## Earlier saved acceptance

| Check | Execution and observed result | Status |
| --- | --- | --- |
| Panel 2.8.1 API | Actual base/add-on activation and extension, enable/disable, traffic reset, Telegram lookup, accessible nodes and devices | PASS |
| Database migration to panel 3.4.5 | Restored the same panel database, applied pending migrations, retained existing bot user references while API users changed to numeric IDs | PASS |
| Panel 3.4.5 API | Repeated subscription/user scenarios; new numeric-ID user, HWID creation/removal, idempotent deletion | PASS |
| Fresh Debian node | Ansible installation of node 2.8.0; panel connection, password/key root access, UFW, BBR/FQ | PASS |
| Node upgrade | Existing node upgraded to 3.4.2; container/listeners/panel readiness verified | PASS |
| Successful physical replacement | Installed Ubuntu VPS 104.252.89.173; retained panel node, host and billing UUIDs; switched address and active encrypted credentials | PASS |
| Actual VPN session | External VLESS/REALITY connection returned the node's exit IP | PASS |
| REALITY target control | Original Microsoft test target failed; changing only the acceptance profile target/SNI to Cloudflare made the session work | PASS after test configuration correction |
| Management firewall | External TLS handshake to port 2222 blocked; panel could reach it. TCP-connect alone was insufficient due to workstation proxy behavior | PASS |
| Billing | Extension, optional bookkeeping expense, expiry exclusion from tg and access restoration on actual panels | PASS |
| Manual deletion | Disposable panel node/host/billing removed; unrelated node preserved | PASS |
| Telegram handlers | 29 synthetic updates through actual dispatcher/FSM; settings, wizard, providers, SNI, SSH keys, reports, jobs, credentials authorization and archive | PASS |
| Automated checks | 106 unit/database tests; Python/locale syntax, Alembic migration and Ansible syntax checks | PASS |
| Test bot transfer | Restored bot database, automation key and reports on new panel host; confirmed Telegram polling; old test poller stopped with restart disabled | PASS |

These API subscription checks exercised entitlement delivery through bot services,
not real payment-provider transactions. The Telegram UI checks were programmatic,
not manual tapping of every button in a Telegram client.

## Saved diagnostics — not rerun during final server audit

- Medium: all 11 steps recorded, image output reviewed; interrupted execution resumed saved checks.
- Large: two passes, 38 recorded outcomes; 29 complete, six partial and three failed.
- Saved PDF: 48 pages, 2,760,027 bytes, delivered through Telegram.
- Partial outcomes included unavailable Russian endpoints, empty Geekbench scores
  and failed bench.sh destinations. Failed outcomes included sustained-speed
  endpoints and one UDP attempt. They remain explicit in the report.
- Default large-test repetition count changed to one; configurable repetitions retained.
- Report-only recovery uses saved data without contacting the VPS or fetching scripts again.

## Additional real-server audit, 2026-10-13, approximately 05:00–05:18

| Check | Execution and observed result | Status |
| --- | --- | --- |
| Failed replacement compensation | Real Ansible install/reconnect on 95.182.88.203; verified host had switched; injected failure at second health check; actual panel node/host addresses rolled back | PASS |
| Credential/deployment preservation | After failed replacement, original active deployment, encrypted credentials and node/host/billing UUIDs remained unchanged; old node passed operational check | PASS |
| Global numbering | Created panel `node75 (FI)` outside bot; bot allocated `node76 (FI)` for second actual node | PASS |
| Second actual node | Country FI, individual 15-day period, selected profile/inbound, enabled automatic host and Infra Billing entry verified | PASS |
| Multiple-node SSH propagation | Added temporary global key; real root authentication with it worked on both managed VPSs | PASS |
| Actual-node deletion | Removed temporary second node from panel/hosts/billing; active first node preserved | PASS |
| Node OS reboot | Boot ID changed; Docker/node started automatically; root password, UFW and BBR/FQ persisted | PASS |
| Panel host OS reboot | Boot ID changed; panel, both PostgreSQL instances, Redis and bot started automatically | PASS |
| Post-reboot operation | Panel connection, root password and independent automation-key login verified | PASS |
| Post-reboot VPN/firewall | Tiny external authenticated VPN request confirmed exit IP; external management TLS remained blocked | PASS |
| Unmanaged SSH key | Manually installed key outside bot configuration still authenticated after key synchronization; cleanup restored original authorized-key set | PASS |
| Bot polling after reboot | Actual polling log confirmed; no Telegram polling conflict | PASS |

No speed, quality, region or benchmark suites were rerun for this audit. The only
VPN traffic check was a small HTTP request to confirm authenticated routing.
Temporary nodes/keys were removed; failed replacement remains as an expected failed
job for audit. Test numbering high-water mark remains 76. Test polling was restored.

## Limits and follow-up checks

- Actual provider-VPS installations covered Debian 13 and Ubuntu 24.04; clean Debian
  12 installation was subsequently verified in a full KVM guest, as detailed below.
- Current SSH-key UI supports addition and propagation, not deletion.
- Original old-panel host stopped answering on its published API port; direct
  container-network check also refused the connection. Earlier real 2.8.1 acceptance
  remains valid evidence, but is not a fresh rerun of the final code.
- Fresh legacy acceptance on an isolated restored 2.8.1 copy passed; see below.

## Final-code follow-up, 2026-10-13, from approximately 10:40

The old database was restored into a separate panel 2.8.1 stack on the healthy
panel host, bound only to `127.0.0.1:18083`. This did not replace the running 3.4.5
panel. The legacy copy uses a separate API identity scope; initially reusing the
3.x test scope correctly triggered the UUID/ID relinking protection.

| Check | Observed result | Status |
| --- | --- | --- |
| Final code against restored 2.8.1 API | Base/add-on activation and extension, enable/disable, reset, Telegram lookup, accessible nodes, device create/remove, user create/idempotent delete | PASS |
| Actual 2.8.1 restart contract | Restart without body returned 400: required boolean `forceRestart`; earlier downloaded specification omitted it | FOUND AND FIXED |
| Real legacy node | Actual node 2.8.0 on Debian VPS connected to restored panel 2.8.1; restart with both `false` and `true`, operational readiness, root password/key access | PASS after fix |
| Legacy authenticated VPN | External VLESS/REALITY request returned 95.182.88.203; management TLS blocked outside panel | PASS |
| Panel API outage | Stopped actual 3.4.5 panel; queued operational `small` job stayed pending; restarted panel; same job completed automatically | PASS |
| SSH-only adoption | Actual credential handler verified root password over SSH and stored encrypted deployment without billing; paid-until remained NULL | PASS |
| Later billing link | Added billing record manually through panel API; bot discovered provider/billing binding and extended deadline, updating local deployment | PASS |
| Automatic tests after restart fix | 106 unit/database tests passed, including required boolean restart payload for both API versions | PASS |

One initial adoption fixture assertion incorrectly expected Telegram input deletion;
the credential operation itself succeeded. The fixture expectation was corrected,
and the complete adoption/billing-link case passed. Its temporary panel objects
were removed. No quality/benchmark suites were executed during these follow-ups.

| Check | Observed result | Status |
| --- | --- | --- |
| Final 3.4.5 restart | Actual restart with both `forceRestart=false` and `true`, followed by operational checks | PASS |
| Mixed-version connection | Panel 3.4.5 connected to actual node 2.8.0; listeners and external authenticated VPN verified | PASS |
| Mixed-version traffic accounting | User's panel traffic counter increased after a small real VPN request through node 2.8.0 | PASS |

The mixed-version temporary node/host were removed. The isolated legacy panel was
started again for final stand restoration.

### Clean Debian 12 guest

A separate clean Debian 12 KVM VM was booted on the Ubuntu test host, with 1 vCPU,
1 GB RAM and a 12 GB virtual disk. This is a full guest OS, not a Debian container
and not a reinstallation of a provider VPS. The official Debian cloud image was used.

- Initial password bootstrap required recovery through the injected automation key;
  a private fixture password was set before Ansible execution.
- Actual Ansible package installation, Docker and node 3.4.2 installation completed.
- Initial panel readiness failed because the fixture supplied NAT gateway `10.0.2.2`
  as panel source, while guest UFW observed Docker panel source `172.18.0.3`.
  The firewall correctly refused it. The fixture source was corrected; no product
  change was required for this test-network configuration.
- After using the actual observed panel source, operational readiness, root password
  authentication, UFW and BBR/FQ checks passed. This also exercised a repeated
  Ansible run against the same installation.
- External authenticated VPN and user traffic accounting passed; management TLS
  was blocked outside the panel. The temporary management forward was separately
  restricted on the parent host to the panel container.
- Actual guest OS reboot changed boot ID; password SSH, Docker/node, UFW and BBR/FQ
  recovered automatically. Post-reboot authenticated VPN and external management
  firewall verification also passed (`debian12-vpn-after-reboot.log`).
- Work paused at the user request on 2026-10-13: only the owned Debian 12 QEMU
  guest was suspended with SIGSTOP. Its disk/RAM and temporary panel fixture
  remain available for resumption. Main/legacy panels, bot and ordinary nodes
  remained running during the pause. Work resumed at the user request at
  approximately 15:00 Moscow; final cleanup is recorded below.

## Final cleanup and retained stand, approximately 15:03 Moscow

- Deleted only the Debian 12 temporary panel node and host, and removed its exact
  parent-host firewall rule. Resumed the suspended guest only to terminate its
  owned QEMU process; ports 443, 10022 and 10023 were verified released.
- Removed redundant plaintext credential/host-key fixture copies from the remote
  bot artifact folder. Master private acceptance files, encrypted bot credentials
  and saved diagnostic reports remain available.
- Main 3.4.5 panel, bot, bot database, panel database and Redis remain running.
  Isolated legacy 2.8.1 panel/database/Redis remain running as well.
- Ordinary node containers on 104.252.89.173 and 95.182.88.203 remain running.
  No additional OS reboot, provisioning, speed or quality suite was run on resume.
- VM disk/boot artifacts remain on the dedicated test host for reference; the VM
  consumes no CPU/RAM now. No production deployment was performed.

## Local evidence

Reusable helpers and private raw output are retained under `/tmp/infra-acceptance`:

- `SESSION.md`: detailed execution journal.
- `final-server-sequence.log`: additional live rollback/provision/key/delete/reboot checks.
- `vpn-after-reboot.log`: authenticated post-reboot VPN/firewall verification.
- `large-completed/results.json` and `large-completed/report.pdf`: saved diagnostics.
- `legacy-restore.log`: isolated legacy panel restoration.
- `legacy-api-final.log`, `legacy-node-final.log`, `legacy-vpn-final.log`: fresh old-version checks.
- `panel-outage-final.log`, `adoption-final.log`: recovery and billing-free credential adoption.
- `last-version-checks.log`: restart and mixed-version follow-up.
- `mixed-vpn-final.log`, `mixed-traffic-final.log`: mixed-version VPN/accounting checks.
- `debian12-vm-prepare.log`, `debian12-node-final.log`: Debian 12 VM bootstrap/provisioning.
- `debian12-vpn-final.log`, `debian12-reboot-final.log`: Debian 12 VPN/accounting and reboot.
- `debian12-vpn-after-reboot.log`: authenticated guest VPN and firewall after reboot.
- `stand-final-state.log`: final cleanup and running-service/node inventory.

Credentials, API tokens and private SSH keys are excluded from this committed log.

## Queue/profile/upgrade follow-up, 2026-10-13, approximately 20:45 Moscow

Only the authorized test bot on 154.86.119.144 was updated after confirming an
empty queue. Code hashes, migration `0016_infrastructure_job_messages`, polling
and the running-only DB constraint were checked. Production was untouched.

| Check | Execution and observed result | Status |
| --- | --- | --- |
| Local regressions | 137 Python 3.12 tests, actual PostgreSQL migrations, persistent operation messages, waiting order, profile selection/inbound ambiguity and upgrade confirmation guards; 13 handler checks repeated after navigation polish | PASS |
| Installed/latest node version | Real panel and GitHub reported 3.4.2; actual upgrade handler rendered Up to date with no upgrade action | PASS |
| Wizard profile | Real configured profile name shown without UUID; default selection resolved its VLESS/443 inbound and advanced to providers, without Ansible or a panel mutation | PASS |
| Add node placement | Actual Nodes handler contained Add node; main infrastructure menu no longer contains it | PASS |
| Live operational queue | Two small checks, audit IDs 30 and 31, ran on 104.252.89.173 with first finished_at <= second started_at | PASS |
| Telegram operation lifecycle | Both checks persisted distinct editable progress and separate completion message IDs, plus report history records | PASS |
| Completed queue entries | Actual queue handler excluded both completed checks | PASS |

These were lightweight container/listener/panel/TCP checks, not server quality
benchmarks. No speed/CPU/IP-quality suite was rerun. No physical VPS replacement
or OS reinstallation occurred in this follow-up. Historical API/provisioning
acceptance above remains separately attributed.

Evidence and reusable helpers: `/tmp/infra-queue-followup/`. Node 104 remains
active, 154 hosts the test panel/bot, 95.182.88.203 remains released for the owner's
manual test (old node container stopped, not a clean OS), and 95.182.88.225 remains
an unused old stand with its containers preserved. WARP/Psiphon is deferred.

Final checkpoint-count polish: 56 focused infrastructure tests passed, including legacy extra-result handling. The diagnostic catalog is version 3. This changes rendering/progress bookkeeping only; no remote benchmark was run.

## Remaining localization correction, 2026-10-13

Billing reminders and their node button, empty SSH snapshots and obsolete
snapshot messages use each recipient's language, with the bot default as fallback.
58 focused infrastructure tests passed; new DB-backed tests exercised mixed
English/Russian recipients, upcoming and overdue payment dates, HTML escaping
and snapshot list contraction. Syntax/locale/diff checks passed. No remote node
operation or server-quality benchmark was run.

The owner considers automatic snapshot refresh after panel name/country edits
low priority; that optional synchronization was not added in this correction.
Evidence: `/tmp/infra-localization-tail/`.

The correction was deployed to the authorized @cridpugbot on 154.86.119.144 after an empty-queue check. Polling startup and exact deployed source/locale hashes were verified; production was untouched.

## WARP pilot, 2026-10-13

Manual setup only; bot/Ansible integration remains a later step. Node
104.252.89.173 now has a native WARP tunnel, watchdog and persistent marked
IPv4/IPv6 direct-fallback guards. A separate Acceptance WARP Pilot profile
protects node77 at 95.182.88.203 from changes to the shared original profile.
The original node/host IDs and SSH access were preserved. Private backups and
an unexecuted rollback helper are available.

Real external VLESS requests confirmed Cloudflare exit 104.28.211.192 for a
temporarily selected trace destination, and direct exit 104.252.89.173 for
ordinary traffic. With WARP stopped, selected traffic failed while ordinary
traffic kept working; starting WARP recovered the selected route. The temporary
trace selector was removed. Tunnel/watchdog/guard/node running state was checked.

Gemini's country-rejection phrase was absent from both direct and WARP HTML.
Direct and WARP-only browser sessions reached the anonymous prompt interface,
so the original country failure could not be reproduced on this French node.
The SOCKS/VLESS browser consent flow did not consistently dismiss its dialog;
this is an unresolved UI-level verification limitation, not a proven Gemini
account unlock. The owner was given a private 24-hour test link for checking
their actual account. No benchmark or production change was performed.

See infrastructure-warp-pilot.md and `/tmp/infra-warp-pilot/` for evidence,
private-backup locations, isolated routing scope and rollback instructions.

## Accessible WARP ingress, 2026-10-13

At the owner's request, repeated the pilot on 95.182.88.203 instead of the
client-inaccessible 104.* server. Dynamic побрили.рф checks showed TLS filtering
on 104.* and no block on 95.203. node77 received a separate Acceptance WARP 95
profile with learn.microsoft.com REALITY target/SNI and Firefox fingerprint.
Real external client, direct/selected exits, fail-closed stop/recovery and IPv6
guard passed. Gemini HTML did not contain the country-refusal phrase; actual
account testing remains the owner's check. A fresh private 24-hour link was
prepared. Backups/rollback and full evidence are in the WARP pilot document and
/tmp/infra-warp-95. Production and server-quality benchmarks were untouched.
