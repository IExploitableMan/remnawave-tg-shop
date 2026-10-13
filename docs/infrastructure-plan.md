# Infrastructure management planning notes

This document records agreed requirements. Implementation and dedicated server acceptance are recorded in infrastructure-acceptance-log.md; the branch is based on main. Production deployment remains separate.

## Compatibility

- Current production panel version clarified by the owner: 2.8.1.
- Local API snapshots describe 2.7.4 and 3.4.5.
- Support legacy UUID user operations and numeric-ID user operations through adapters.
- Preserve existing identifiers and backfill base/add-on panel IDs before upgrading.
- Validate on isolated 2.8.1 infrastructure, then upgrade the same test database and repeat scenarios.
- Test payments with sandbox providers or fixtures, never real charges.

## Provisioning and access

- Use Ansible; target Debian 12/13 and Ubuntu 24.04 with shared roles and OS-specific conditions.
- Keep root SSH login and password access available to administrators.
- Store retrievable node credentials encrypted in the bot database; exclude secrets from update logs, Ansible output, and reports.
- Expose credentials through an admin-only node-card action in private chat.
- Do not change passwords silently. Any changed password must be delivered to the initiating administrator.
- Keep a separate automation key and manage administrator public keys globally in root authorized_keys.
- Adding a public key creates propagation jobs for all existing reachable nodes; retain per-node failures for retry.
- Preserve unrelated authorized_keys entries. Record installation success per managed key and node.
- Provide an admin-only Telegram credentials snapshot on request, for one node or all nodes. Use a readable country/name heading and separate monospace lines for address, password and SSH command; include non-default ports/logins when applicable.
- A requested snapshot remains usable for manual access while the bot is unavailable. Do not add a custom backup/export subsystem for this feature.
- Refresh credentials snapshots for all administrators after relevant credential/address/name changes, regardless of prior snapshot requests or notification opt-out. Edit each administrator's latest tracked snapshot in their private chat where possible, otherwise send a replacement. Split long snapshots into messages; track delivery failures per administrator without failing the infrastructure operation.

## Node lifecycle

- The purchased duration is individual to each node; 30 days is an initial default only.
- Distinguish the duration in days (suggested extension amount) from the absolute nextBillingAt deadline.
- Generate node numbers from all panel node names, with locking and a persistent high-water mark.
- Assign countryCode and a name such as node26 (NL).
- Select a default existing profile and inbound through settings, without manual UUID entry.
- Provision base nodes only in the current UI; leave room for future product presets internally.
- Automatically create an IP-based host and update its IP when replacing the VPS.
- Address replacement uses one flow for IPs/domains: show the previous configuration during entry and let the administrator enter/copy the intended address. DNS updates are the administrator's responsibility; no DNS automation or separate "do not change IP" switch.
- Diversify SNI across hosts through the host SNI override and configured regional pools. Spread allocations rather than blindly repeating random values; preserve the chosen value until deliberately changed and validate server-side compatibility.
- Manage exactly two persistent SNI pools from the bot's admin settings: Russia (RU nodes) and non-Russia (other countries). Support listing, adding, editing and deleting entries. Pool edits do not silently rewrite existing host SNI selections.
- Keep the panel node UUID during replacement; retain deployment/IP history separately.
- Reuse Infra Billing providers and nextBillingAt. Price is optional.
- Restrict infrastructure actions to administrators; notifications go to subscribed administrators.
- All administrators are subscribed to infrastructure notifications by default and can opt out.
- Proposed expiry behavior: optionally remove the host from access for the tg squad, preserving the node for administration.
- Add explicit manual panel node/host deletion with a concrete confirmation describing affected objects; do not infer remote VPS deletion or server wiping.
- Confirm host/squad access semantics and SNI override behavior before implementation.

## Diagnostics

- Small: automatic operational checks after provisioning/replacement and configuration changes; no heavy benchmarks.
- Medium: manual full ServerGrade-equivalent suite in one pass, including region/services, censorship/DPI, Russian and international network tests, YABS, bench.sh, IP Check Place/IP quality and CPU checks; image reports organized by test as in the supplied examples.
- Large: manual expanded multitest-bot-equivalent suite, with repeated measurements and longer stability checks; PDF including per-run results and aggregate statistics for repeatable numeric measurements. Repetition count is configurable and defaults to one; IP/region/availability facts are captured once per job.
- Reuse medium results in the large report where applicable; do not redundantly launch equivalent heavy tests without a measurement purpose.
- Store structured measurements, raw output, report artifacts and test version against a deployment, including its historical IP.
- Never publish test reports to competing third-party reporting or image-upload services. The owner explicitly permits Geekbench and Ookla's normal measurement/result handling. Send requested artifacts directly through Telegram.
- External measurement endpoints are required for speed/region checks; review each dependency for telemetry/publication.
- ServerGrade offers SERVERGRADE_PUBLISH=0, but inspect its nested scripts and all upload paths before adoption.
- The owner reports permission from the ServerGrade author for any use. User-facing reports must have no author credits, advertising or sponsor blocks.
- Artifact retention remains to be settled. Keep results local and retrievable from the node's history even after VPS replacement.
- A separate external VPS is not a prerequisite for local API/migration validation. Real VPS installation and outside-network access checks are additional acceptance checks for provisioning, firewall and actual client connectivity.
- Run real panel migration acceptance checks on a dedicated server and provisioning checks on a separate clean node VPS. The owner will provide SSH access on request; access has been requested.

## Future TODO (not part of the current implementation)

- Monitor CPU steal over time.
- Monitor network throughput over time; distinguish passive interface traffic from active speed measurements.

## Agreed operation UI, profile and upgrade changes

- Show only active operations in the queue; use relative waiting positions.
- Send persistent editable progress and a separate completion message with
  attachments and contextual actions; keep operation history on each node card.
- Add node belongs in Nodes. Select the installation profile by name, initially
  from settings, without changing the global default.
- Upgrade first displays the reported current version and available stable
  release, then requires confirmation. Hide the action when already current.
- SSH access capture belongs to an existing node and enables its management; it
  does not create or install a new node.
- WARP/Psiphon egress is deferred; no installation/routing changes were made.
