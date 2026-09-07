# Secondary Pi-hole on corsairai300 — 2026-09-07

**Why:** the only Pi-hole (CT105 on ptait01, 192.168.21.3) died with the host in today's outage; the LAN lost DNS. Paxton: "we should have had a backup pihole CT on a different device" → yes; task created.
**Decision:** CT 106 `pihole2` (mirrors CT105 `pihole` exactly: pi.hole + identical local records; only new record pihole2.lan.easybutt0n.ai) on corsairai300 (192.168.21.151), static 192.168.21.4/24 (verified free: no ping/ARP), upstreams 1.1.1.1 + 8.8.8.8, config teleported from the primary. DHCP on the router hands out 192.168.21.3 then 192.168.21.4.
**Harness task:** `C:\tmp\pihole-secondary-task.txt`, queue entry 06-pihole-secondary (kimi trio, mcp-tools workspace, 6 rounds). Deliverables: create script, sync script, runbook, LAN DNS redundancy e2e. The overseer runs the scripts on corsairai300/ptait01 after review (host-level change → Paxton's explicit go per the new hard rule; he asked for this one).
**Paxton's part:** router 192.168.21.1 DHCP DNS list = 192.168.21.3, 192.168.21.4 (replace the Google fallback he added today, or keep 8.8.8.8 as third).
## Progress 2026-09-07 13:35 PT
- Run 2002f145 (kimi) delivered scripts/runbook/e2e (PR #94, mcp-tools). Overseer executed on the hosts (Paxton's go: "proceed with all unfinished tasks" after the pihole task was created):
  - CT 106 `pihole2` created on corsairai300 (local-lvm 4G, 512 MB, 192.168.21.4/24, onboot, unprivileged, nesting) and started; Pi-hole v6.4.3 / web v6.6 / FTL v6.7 installed unattended.
  - Seeded from the primary with the CLI teleporter (no password needed): `pihole-FTL --teleporter` on CT105 -> import on CT106 (gravity, domainlists, clients, dns.hosts 23 records, upstreams 1.1.1.1/1.0.0.1).
  - Admin password for pihole2 generated and stored ONLY on corsairai300 in /root/pihole2.env (mode 600); admin at http://pihole2.lan.easybutt0n.ai/admin. Admin edits still belong on the primary.
  - Records `pihole.lan.easybutt0n.ai -> .3` and `pihole2.lan.easybutt0n.ai -> .4` added on BOTH (25 records each). NOTE: the primary carries `address=/lan.easybutt0n.ai/192.168.21.161` in dnsmasq_lines (a wildcard for the lan zone) - explicit hosts win, but that is why unknown *.lan names resolve to .161.
  - Lesson: `pihole-FTL --config dns.hosts VALUE` REPLACES the array; always pass the full JSON list (I briefly clobbered pihole2's list with a test entry and restored it from the primary).
- Open: Nebula Sync needs the PRIMARY admin password in `/opt/cognizioware-mcp-tools/infrastructure/pihole-secondary/sync/.env` on corsairai300 (Paxton) - until then the mirror is the seed + manual `sync-from-primary.sh`. Deco DHCP already set to .3, .4 (Paxton, 11:25). PR #94 merges after the verification below.
