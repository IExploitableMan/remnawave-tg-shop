# WARP pilot on the dedicated test node

Configured on 2026-10-13, before any bot/Ansible integration.

## Scope and state

- Node: 104.252.89.173, Remnawave node 3.4.2, host-network Docker.
- Test panel: 154.86.119.144, panel 3.4.5.
- Separate panel profile: `Acceptance WARP Pilot`. The shared `Acceptance Default`
  remains unchanged, including node77 at 95.182.88.203.
- Existing node/host identities remain intact. The host inbound now references
  the cloned profile, and its inbound is added to the relevant existing squads.
- Selected domains: gemini.google.com, aistudio.google.com,
  generativelanguage.googleapis.com, alkalimakersuite-pa.clients6.google.com,
  alkali-pa.clients6.google.com, accounts.google.com, consent.google.com and
  waa-pa.clients6.google.com. Authentication/consent/anti-abuse dependencies use
  the same pilot exit. Other traffic retains the ordinary node exit.
- Xray outbound: freedom + ForceIPv4 + socket mark 255. VLESS inbound sniffing
  recognizes HTTP/TLS/QUIC destinations. Container explicitly has NET_ADMIN.
- Native WireGuard interface `warp`, separate routing table 51820, mark 255.
  The host's main default route and SSH access remain unchanged.
- `warp-watchdog.timer` maintains the tunnel; no automated speed benchmark.
- `warp-pilot-guard.service` persists IPv4 rejection of marked traffic outside
  the warp interface and IPv6 rejection of marked traffic. It prevents direct
  fallback when the tunnel/routing rule disappears.

The reviewed installer is https://github.com/tagashi666/vps-warp,
script v3.4, SHA-256
`e37751c8b15cee5bfa58f44cb8a1f72abdef749c7b62835f26b811108cd4a8a2`.
Installation was from the saved file, not a fresh curl-to-shell pipeline.

## Verification and limits

- Direct trace: 104.252.89.173, FR, warp=off.
- WARP trace: 104.28.211.192, FR, warp=on (observed value, not a fixed-IP promise).
- A real external VLESS/REALITY client confirmed both exits independently:
  a temporarily selected Cloudflare trace request used WARP; api.ipify.org used
  the ordinary node IP. The temporary Cloudflare selector was removed afterwards.
- With WARP stopped, the selected VLESS request failed (curl exit 35), while
  ordinary VLESS traffic still returned 104.252.89.173. WARP was restarted in a
  finally block and the selected exit recovered. The IPv6 guard was checked.
- Gemini HTML returned 200 without the country-rejection phrase on both direct
  and WARP exits. Separate browser sessions through direct/WARP proxy routes
  reached the anonymous Gemini prompt interface after Google consent.
- External VLESS fetched Gemini successfully. Its browser consent flow was also
  inspected, but its Google consent dialog did not consistently dismiss in the
  automated SOCKS/VLESS browser despite HTTP requests succeeding. That UI flow
  is not an acceptance pass; a manual account-level check was requested.
  No Google account was supplied, so authenticated Gemini operation
  and resolution of the owner's original country error are not proven.
  The original error was not reproducible on this French node even without WARP.
- No server quality/speed/CPU benchmark was run. Production was not changed.

A dedicated `warp_pilot_client` expires after 24 hours and has a private import
link in `/tmp/infra-warp-pilot/test-link.txt` for the owner's account-level check.
Temporary browser proxies are loopback-only and stopped after verification;
local browser/SSH/client test processes are closed at the end of the task.

## Private backups and rollback

Never commit the JSON backups or WireGuard/client keys.

- Node: `/root/warp-pilot-backup/` contains original Compose, routes, firewall
  rules, rp_filter values and the installer log.
- Panel: `/opt/remnawave-panel-acceptance/infra-data/warp-pilot/` contains the
  original node/profile/hosts/squads and the pilot state, with private permissions.
- Panel rollback helper: `/root/warp-pilot-rollback.py` on 154.86.119.144.
  It restores the original node profile and host inbound bindings, removes only
  pilot-added squad inbounds, and removes the pilot profile/test user. It refuses
  to proceed if the node address has changed; inspect that case manually.
  Run inside the authorized test bot container using stdin:

```bash
cd /opt/remnawave-panel-acceptance
docker compose -f compose.yml -f bot-compose.yml exec -T bot python - < /root/warp-pilot-rollback.py
```

After panel rollback, on the node restore `/opt/remnanode/compose.yml` from its
private backup and run Compose up. Remove WARP using `vps-warp uninstall`; disable
`warp-pilot-guard.service` and remove its two exact OUTPUT rules and files.
Do not restore a complete old firewall dump over unrelated later changes.
The rollback helper is prepared but has not been executed against the completed
pilot, because that would undo the requested setup.

## Before integrating into the bot

Preserve the tunnel, routing guard and container capabilities through install,
upgrade and replacement. Validate domain rules, DNS/IPv6 behavior and service-level
availability. Maintain per-node routing/profile isolation and a reversible switch.
The current bot provisioning roles do not install or manage WARP; an upgrade or
reprovision can rewrite the manually edited Compose. Keep this pilot separate
until those lifecycle operations are integrated and checked.

Reusable helpers and raw evidence: `/tmp/infra-warp-pilot/`. No credentials are
embedded in this document. Country/region decisions remain Google's, and a
healthy WireGuard handshake alone is not proof that Gemini accepts an exit.
