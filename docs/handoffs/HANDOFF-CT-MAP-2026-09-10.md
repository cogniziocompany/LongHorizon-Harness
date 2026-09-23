# Handoff — CT / host map before the infra revision

**Originally measured live 2026-09-10 12:49 PT. RE-MEASURED AND REVISED 2026-09-10 15:20 PT.**
Not from notes. Full context if you need it: `C:/tmp/OVERSEER-HALT-2026-09-10.md`.

> ### THE INFRA REVISION HAS BEEN APPLIED — this doc now describes the NEW layout.
> The expert completed the three moves and the overseer re-verified each one by `pct list` /
> `pct exec hostname -I` on all three hosts at 15:20 PT (not from the expert's report):
>
> | move | before | after | verified |
> |---|---|---|---|
> | CT204 UAT router | ptait01 (.110) | **ptait07 (.138)**, CT keeps its own IP **.162** | `pct list` shows 204 running on ptait07, stopped on ptait01 |
> | billing prod | CT100 on ptait01 | **CT120 on corsairai300 (.151)**, IP **.153**, hostname still `ct-100` | `pct exec 120 -- hostname -I` → 192.168.21.153 |
> | prod YAML rows | 24 YAML rows in DB | **0 YAML rows, 24 DB rows**; md5 54ba112e → e9ba752b | reported by expert, not independently re-read |
>
> **`.162` vs `.138` is not a contradiction.** The expert reported CT204 "running at .162"; that is
> the CONTAINER's address, unchanged by the move. `.138` is the HOST it now sits on. Both are true.
>
> Ollama is **0.34.0 fleet-wide**. ptait01 went 3.7 GB → 19.0 GB available.
> PRs #137, #138, #139 merged.
>
> **Two stopped rollback copies are still holding ~137 GB** — CT100 on ptait01 and CT204's old
> disk. Destroy them after a week of clean running (i.e. on/after **2026-09-17**), not before.

The PC launcher was stopped during the halt and was **restarted at 15:15 PT** (PID 60276); it
immediately launched task 136. Capacity is degraded — see the bottom of this doc.

---

## The map as it stands

### ptait01 — `cognizioware-ptait01` — **192.168.21.110** — disk 31%
| CTID | name | what it really is |
|---|---|---|
| ~~100~~ | `ct-100` | ~~billing PROD~~ — **STOPPED / MOVED 2026-09-10** to **CT120 on corsairai300**. This is one of the two rollback copies holding ~137 GB. Do not start it. |
| **202** | `cognizioware-ops-it-services-uat-copy` | **PROD LiteLLM router**, 192.168.21.161:4000 — *the name says "uat-copy". It is PROD.* |
| ~~204~~ | `cognizioware-ops-it-services-uat` | ~~UAT router~~ — **STOPPED / MOVED 2026-09-10** to **ptait07**. Old disk is the second rollback copy. Do not start it. |
| 105 | `pihole` | **DNS primary** (.3) — all admin edits here |
| 101 `ruflo` · 103 `cognizioware-workstation-uat` · 104 `azure-mcp` · 203 `gh-runner-lan` · 207 `comfyui` · 208 `cognizioware-auth-broker` | | |

**The GPUs and Ollama are on the HOST, not in a CT:**
```
:11441  cognizioware-ollama-gpu1    CUDA_VISIBLE_DEVICES=1
:11438  cognizioware-ollama-cloud   CUDA_VISIBLE_DEVICES=0   (also cloud relay + embeddings)
:11442  cognizioware-ollama-span    CUDA_VISIBLE_DEVICES=0,1
2x RTX 3090, 24,576 MiB each
```

### ptait07 — `cognizioware-ptait07` — **192.168.21.138** — disk 50%
| CTID | name | notes |
|---|---|---|
| 100 | `pp-dev-uat` | **a SECOND CT100** — not the billing one. IP **192.168.21.163** (pp-dev-app-1 :3000, pp-uat-app-1 :3001). |
| **204** | `cognizioware-ops-it-services-uat` | **UAT router — MOVED HERE 2026-09-10** from ptait01. CT IP **192.168.21.162**. |
| **210** | `ci-runners` | `ct210-qa`, `ct210-pp`, `ct210-billing` |
| 101 `ruflo` · 201 `cognizioware-ops-it-services` | | |

### corsairai300 — `cognizio03-corsairai300` — **192.168.21.151** — disk 10%
| CTID | name | notes |
|---|---|---|
| **110** | `lh-harness` | **the harness**, 192.168.21.168:8799 — every run executes here |
| 105 | `cognizioware-powerplatform` | **stale decoy**, newest session 2026-06-09. Prod pp is the HOST docker stack. |
| 106 | `pihole2` | DNS mirror (.4) |
| **120** | `ct-100` | **billing PROD — MOVED HERE 2026-09-10** from CT100 on ptait01. IP **192.168.21.153**, `mcp-billing-service` :20251. *Hostname is still `ct-100` while the CTID is 120 — a fourth name/id trap.* |

---

## Three traps that have already cost time here

1. **CTIDs are reused across hosts.** 100, 101 and 105 each exist on more than one host. A
   `pct exec` against the wrong host answers about a different machine, silently. That produced a
   fabricated incident report in this session, and separately made a live app appear not to exist.
2. **Names lie.** CT202 is called `…-uat-copy` and is **production**. CT105 on corsairai300 is a
   decoy. Trust what a box serves, not what it is called.
3. **`pct list` hosts report a Tailscale IP first** on ptait07 (`100.82.36.56`) and corsairai300
   (`100.127.170.78`). The LAN addresses are **.138** and **.151**.

---

## What breaks if these move — the hard-coded list

| value | who depends on it |
|---|---|
| `192.168.21.161:4000` | LiteLLM prod router — all model traffic |
| `192.168.21.168:8799` | harness API — `launch_queue.py` line 6, every overseer probe |
| `192.168.21.153:20251` | billing **prod** origin — Cloudflare ingress rule 2, `cloudflare/prod.config.yaml`, `deployments/tier/README.md`, `validate_prod_tunnel_config.py`, `validate_canonical_host_config.py` |
| `192.168.21.153:20252` / `:20253` | billing **dev** / **uat** lane host ports (container port is always 20251; only the host port varies) |
| `192.168.21.110:11441` and `:11438` | **both ornith deployments, stored in the LiteLLM DB** — not in the YAML |
| `192.168.21.154:5432` | agent-db Postgres (CT103) — fleet-admin + hivemind |
| `192.168.21.163` | pp dev/uat (`pp-dev-app-1` :3000, `pp-uat-app-1` :3001) |

Also: `cognizioware-qa` repo variables `HYDRA_REPO_DIR` and `FACTORY_REPO_DIR` are **Windows paths
on a Linux runner** — already broken, already being worked (task 69). Re-check them first if the
runner moves.

---

## The one that will cost you a day if you miss it

**Five LiteLLM config sections live in Postgres and the DB row WINS over the YAML** —
`general_settings`, `guardrails`, `litellm_settings`, `mcp_servers`, `router_settings`.

- Editing `infrastructure/litellm-config.yaml` **may change nothing**. On 2026-09-09, nine merged
  alias edits were dead on the live router.
- Editing it **also force-recreates the CT202 prod router** (the deploy job hashes the deployed
  file), killing every in-flight harness run. Even a comment-only change.
- So after a host move, the ornith `api_base` values must be changed via `POST /model/update`
  (they are DB deployments, UUID ids), then **read back** — a 200 is not proof.

**And when you re-create those two deployments, set `rpm` on BOTH.** They are load-balanced by
`simple-shuffle`, which weights by `rpm`; a deployment without one gets weight **0** and is never
selected. That is exactly why one GPU sat idle today until it was fixed.

---

## Verify like this afterwards

```bash
for h in 192.168.21.110 192.168.21.138 192.168.21.151; do ssh root@$h "hostname; pct list"; done
curl -s -o /dev/null -w '%{http_code}\n' http://192.168.21.161:4000/health/liveliness   # 200
```
For ornith, do **not** accept a 200 on the alias — a pool of one passes that. Drive real traffic and
count arrivals at each daemon:
`docker logs --since 6m <container> | grep -c 'POST .*"/api/chat"'`
(the GIN line is `POST     "/api/chat"` — quoted, multi-space; a naive grep matches nothing, and
`docker logs --since` parses a bare timestamp as **host local** time, not UTC).


---

## POST-REVISION STATE — added by the overseer 2026-09-10 15:20 PT

### Capacity is the binding constraint right now, and it is NOT an infra fault

Read live this tick, both readers checked for freshness before being believed:

| gate | reading | source |
|---|---|---|
| Synthetic weekly credit | **0 tokens remaining** (625,900,000 of 625,900,000 used) | live `ops.easybutt0n.ai/api/doctor` |
| Ollama Cloud keys | **0 of 7 OK** — key1–key7 all `LIMIT` | `C:/tmp/keys_status.txt`, written 15:02, fresh |
| Local ornith pool | **healthy**, 6/6 trio backends OK, emit-probe **0.5s** | launcher pool check 15:15 |
| Both LAN Ollama boxes | HTTP 200 | ops doctor |

So the fleet is healthy and the *providers* are exhausted. The launcher self-gates to
**degraded mode, cap 2**, routing to `ornith-1.5:pool`. That is correct behaviour, not a fault.

**The expert's rebuild is measurably good:** the same emit-probe against `ornith-1.5:pool` read
**72.0 s / timeout** at 05:44 and reads **0.5 s** at 15:15. Whatever was done to those daemons
fixed a real latency problem, not just a version number.

Do not raise the cap to work around this. Synthetic's weekly credit does not reset until roughly
**2026-10-09**; the monthly manual reset is already spent.

### An open contradiction the next person should settle — billing tunnel

The expert reports, and it is NOT yet independently verified by the overseer:

> `billing.easybutt0n.ai` 502 is pre-existing — that hostname **is not in this tunnel's config at
> all**, and its `cloudflared` reports `Unauthorized: Tunnel not found`, i.e. deleted
> Cloudflare-side.

**This contradicts the premise of queued task 126** and of merged PR #105, whose operator steps
assume the hostname exists in `cloudflare/prod.config.yaml` and simply needs the cross-network
attach restored after a compose recreate (see memory `ct100-billing-tunnel-network`). If the
tunnel was deleted Cloudflare-side, reconnecting the docker network fixes nothing and task 126 is
scoped against a world that no longer exists.

Settle it by reading, in this order:
1. the running `cloudflared` config on **CT120** (billing moved hosts — check there, not CT100),
2. `cloudflare/prod.config.yaml` in `mcp-cognizioware` for a `billing.easybutt0n.ai` ingress rule,
3. the Cloudflare dashboard for whether the tunnel UUID still exists.

Do not re-scope task 126 until all three have been read. One of them alone will mislead.

### Also outstanding

- **QA Verify (prod) reports `infra: Request timeout`** — flagged by the expert, unresolved.
- The two rollback copies (~137 GB) are safe to destroy **on/after 2026-09-17**, not before.
