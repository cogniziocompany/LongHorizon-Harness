# Secondary Pi-hole on corsairai300 — 2026-09-07

**Why:** the only Pi-hole (CT105 on ptait01, 192.168.21.3) died with the host in today's outage; the LAN lost DNS. Paxton: "we should have had a backup pihole CT on a different device" → yes; task created.
**Decision:** CT 106 `pihole2` (pihole2.lan.easybutt0n.ai) on corsairai300 (192.168.21.151), static 192.168.21.4/24 (verified free: no ping/ARP), upstreams 1.1.1.1 + 8.8.8.8, config teleported from the primary. DHCP on the router hands out 192.168.21.3 then 192.168.21.4.
**Harness task:** `C:\tmp\pihole-secondary-task.txt`, queue entry 06-pihole-secondary (kimi trio, mcp-tools workspace, 6 rounds). Deliverables: create script, sync script, runbook, LAN DNS redundancy e2e. The overseer runs the scripts on corsairai300/ptait01 after review (host-level change → Paxton's explicit go per the new hard rule; he asked for this one).
**Paxton's part:** router 192.168.21.1 DHCP DNS list = 192.168.21.3, 192.168.21.4 (replace the Google fallback he added today, or keep 8.8.8.8 as third).
