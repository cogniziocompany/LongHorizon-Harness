"""Launch queued harness tasks on CT110 as capacity allows. Queue = C:/tmp/queue/*.json, each
{"name":..., "task_file":..., "workspace":..., "max_rounds":N, "trio":"kimi"|"qwen", "base_check": optional}.
Rules: kimi trio needs >=2 healthy Ollama keys and < CAP kimi runs active; qwen trio needs no other qwen run active.
409/busy workspace -> skip this cycle. Launched entries move to C:/tmp/queue/done/ with the run id."""
import json, glob, os, time, datetime, urllib.request, urllib.error, subprocess, shutil, uuid
BASE="http://192.168.21.168:8799"; TOKEN="<CT110_BEARER_TOKEN_REDACTED_SEE_docs/SECRETS.md>"
# 2026-09-09 THE ROUTE SPLIT (Paxton's direction: separate models for the anthropic and the
# non-anthropic route; qwen3.8 gets two sub-types). PROVEN today, not assumed:
#   Every :synthetic model was registered with the `openai/` prefix, which sends /v1/messages
#   through the OpenAI *Responses* API -> 404. That is why "Synthetic is OpenAI compatible" and
#   "Synthetic 404s for the harness" were BOTH true: different routes.
#   Re-registered the same upstreams under `custom_openai/` as :synthetic-anthropic twins and
#   verified each on /v1/messages, streaming AND non-streaming:
#     qwen3.8-27b:synthetic-anthropic    200, 784ms, content=[text "READY"], no thinking block
#     glm-5.3-flash:synthetic-anthropic  200 stream clean
#     kimi-k3:synthetic-anthropic        200 stream clean (non-stream emits a thinking block
#                                        with signature=null; STREAMING does not, and Claude
#                                        Code streams - this is the bug that killed 4 runs)
#     glm-5.2:synthetic-anthropic        200 stream clean
#   The `openai/`-prefixed :synthetic entries STAY for non-anthropic callers. Two models, two
#   routes, each verified on the route it serves. NEVER put an `openai/`-prefixed model in a
#   harness role.
# qwen3.8 SUB-TYPES: local span (:11442, shared 2 slots, GPU-bound) vs Synthetic cloud (no GPU
# contention). Same weights, different backend. The executor now prefers the cloud sub-type.
CAP=4  # was 2 (local span slots). Executors no longer sit on the span, so the cap is Synthetic's
       # per-model parallelism (~2/model), which we spread by rotating the pools below.
# Pools are ROTATED BY ACTIVE-RUN INDEX so two concurrent runs never share a backend for the
# same role - that collision is what 429'd runs into 300s cooldowns on 2026-09-08.
# MEASURED time-to-first-token on /v1/messages, 2026-09-09 (emit probe, streaming):
#   qwen3.8-27b:synthetic-anthropic 1.0s | glm-5.3-flash:cloud 1.2s | kimi-k3:cloud 1.6s
#   glm-5.3-flash:synthetic-anthropic 2.0s | kimi-k3:synthetic-anthropic 2.2s
#   glm-5.2:synthetic-anthropic 12.0s TIMEOUT  -> registered but NOT pooled (too slow to lead)
#   qwen3.8-nothink (local span)    12.0s TIMEOUT -> DELIBERATELY NOT POOLED. Measured the same
#     hour: one 8-token request to :11442 took 240s while runs were active. The span is fine
#     when idle and useless under load; keeping it out of the pools is the whole point of the
#     route split. It stays registered for chat/eval use.
# ALIAS != BACKEND. Verified 2026-09-09 by reading the model name the router reports back:
#   kimi-k3:cloud  -->  SERVED BY glm-5.3-flash:cloud  (a fallback chain silently redirects it)
# The first pools built today put kimi-k3:cloud and glm-5.3-flash:cloud in different roles of the
# same trio, believing they were two backends. They are one. Every entry below was confirmed to
# report a DISTINCT served model; check_pools() re-asserts it at startup so this cannot rot.
# 2026-09-09 21:05 ALL FOUR OLLAMA-CLOUD MODELS ARE 429 AND ONE IS 403 (6 of 7 keys at LIMIT),
# so all three roles run on Synthetic. The 6 backends below were each re-probed on the HARNESS key
# and each returned real text within 3s: syn:large:vision 0.7s | glm-5.3-flash 1.1s |
# qwen3.8-27b 1.1s | nemotron-3-super 1.1s | glm-5.2 1.3s | kimi-k3 1.5s.
# NOT pooled on purpose: gpt-oss-120b, glm-4.7-flash, syn:small:text, syn:large:text - all answer
# fast but return an EMPTY text block in a 48-token budget. They pass a liveness probe and would
# then fail a real role.
#
# 2026-09-09 21:25 REAL SUBSCRIPTION LIMITS, from Paxton off the Synthetic billing page:
#   $150/month pack, SAME account (capacity added to the existing pack, not a second key)
#   2500 requests / 5 hours      (the config note had said 1000 - wrong)
#   5 concurrent requests PER MODEL   (the config note had said 2 - wrong, and CAP was built on it)
# Provider-side ceiling is therefore floor(6 backends * 5 concurrent / 3 roles) = 10 runs.
# WE DO NOT TAKE 10. The binding constraint is CT110: 4 vCPU / 6 GB, ~2.5 agent processes per run.
# Measured at 4 runs: 10 processes, load 1.31-1.45 on 4 cores (~35%), 1 GB of 6 used.
# Extrapolated: 6 runs ~15 processes / load ~2.1 (~52%); 8 runs ~20 processes / load ~2.8 (~70%).
# CAP=6 is a measured step, not a jump to the provider ceiling. Raise only after watching load.
# WATCH THE BUDGET TOO: 2500 per 5h is pack-wide (~8 rpm in aggregate) while each deployment
# declares rpm 15. Tool-heavy runs can exhaust the 5-hour budget without ever hitting concurrency;
# 429s while only a few runs are active mean the BUDGET, not the parallelism.
CAP=6
# ══════════════════════════════════════════════════════════════════════════════
# MODEL DOCTRINE - Paxton, 2026-09-09 21:35. This replaces the earlier pools.
# ══════════════════════════════════════════════════════════════════════════════
# THE ECONOMIC POINT THAT DRIVES ALL OF IT: Synthetic bills a REQUEST, not a model.
# 2500 requests / 5 hours is consumed identically whether the request goes to a flagship or to a
# mid-tier model. So spending a Synthetic request on a cheap model is pure waste - if we are paying
# for the slot either way, the slot must carry the STRONGEST model available. Cheap and mid-tier
# work belongs on the local GPUs, where it is free. The previous pools had glm-5.2, nemotron-3-super
# and syn:large:vision in them; all three are removed - they burned a full-price request each.
#
#   EXECUTOR  -> LOCAL qwen3.8 (64k) first. Highest-volume role by far; running it on Synthetic
#                would consume the request budget fastest for the least benefit. Falls through to a
#                FLASH cloud model when local cannot answer - Paxton: "if there is no local
#                available models, then we should use the flash models from the cloud for the
#                executor on the long harness tasks that are queued".
#   MANAGER   -> FLASH (glm-5.3-flash). Planning role, low volume; a flash model is the right tool.
#   AUDITOR   -> FRONTIER (kimi-k3). The role where judgement quality actually matters, and the one
#                worth a full-price Synthetic request.
#
# LOCAL CAPACITY IS REAL AND SMALL. The span (:11442) serves ONE qwen3.8 spread across both RTX
# 3090s and the architecture is single-stream (~45 tok/s), so it is 1-2 concurrent executors, not
# more. It is ALSO where the eval gates run by settled doctrine - observed this minute generating
# at 45 tok/s on an eval task while answering none of my probes. So only the first two trios take
# the local executor; the rest use flash cloud, which is exactly the fallback Paxton authorised.
# The idle :11441 span cannot absorb the overflow: it was RETIRED 2026-08-29 because at num_ctx
# 16384 Ollama truncates the head of a Claude Code prompt and the model then hallucinates its tool
# inventory, and GPU1 has ~12 GB free against the ~20 GB a second 64k instance would need.
# 2026-09-09 21:40 CORRECTION TO MY OWN EXCLUSIONS. I dropped gpt-oss-120b, syn:small:text and
# syn:large:text for "returning an empty text block" - measured with a 48-token budget that the
# model's thinking block consumed entirely. Re-tested at 300 tokens: all three answer correctly
# ("2, 3, 5 DONE") in 0.8-3.6s. That is the SAME calibration error made earlier with
# glm-5.3-flash:cloud, made twice now.  Only glm-4.7-flash truly yields no text (300 tokens, all
# thinking); gemini-3-flash-preview:cloud is HTTP 410 Gone.
# Recovering syn:large:text matters: it is Synthetic's 512k flagship, a SECOND frontier-class
# auditor. With the auditor spread over two frontier backends neither exceeds Synthetic's ceiling
# of 5 per model, so CAP goes to 6 without self-inflicted 429s - and the auditor keeps frontier
# quality rather than being diluted to buy throughput.
CAP=6
# 2026-09-14 PAXTON: SYNTHETIC CAP = 3. At most three LIVE runs may hold any :synthetic-anthropic role.
# The recovery ramp only limited launches to one per CYCLE; nothing capped how many were live, and on
# 2026-09-12 00:00-03:34Z six runs died on one Synthetic 429 burst. active() counts them into _SYN_LIVE.
SYN_CAP=3
_SYN_LIVE=[0]
# 2026-09-09 23:00 FULL POOL RE-MEASUREMENT. Every alias probed on /v1/messages with a 300-token
# budget and a nonce, reading BOTH the served model name and x-litellm-model-id. The earlier note
# below this one said the Ollama quota was "partial" and named three cloud models as still usable.
# That is no longer true, and the way it stopped being true is the point:
#
#   OLLAMA CLOUD IS EXHAUSTED ACROSS EVERY ACCOUNT, NOT ONE KEY.
#     prax211 weekly | aidev01 weekly | aidev03 MONTHLY | ai-dev0x  -> four distinct accounts.
#     Directly 429: kimi-k3:cloud, kimi-k2.7-code:cloud, deepseek-v4-pro:cloud, minimax-m3:cloud,
#                   nemotron-3-ultra:cloud, gpt-oss:120b-cloud, mistral-large-3:675b, qwen3.5:397b.
#     RETIRED UPSTREAM (not a quota problem, the model is gone): qwen3-coder-next, devstral-2:123b.
#     Still 200 - but ONLY as fallbacks, and this is the trap: glm-5.3:cloud, glm-5.3-flash:cloud,
#     glm-5.2:cloud and kimi-k2.6:cloud all answer, and all four are served by something ELSE.
#     glm-5.3-flash:cloud is served by kimi-k2.7-code, whose OWN alias returns 429 in the same
#     sweep, and glm-5.3:cloud was served by kimi-k2.7-code at 22:48 and by glm-5.3-flash at 23:02.
#     A 200 from a flapping fallback is not capacity you can bind three roles to. ALL :cloud MODELS
#     ARE THEREFORE OUT OF THE TRIO TABLE - not because they all fail, but because the ones that
#     succeed are lying about who answered.
#
#   THE LOCAL SPAN IS HEALTHY AND BUSY - do not read the timeout as death. qwen3.8/-nothink/--128k
#     all timed out at 150s, but ptait01 shows qwen3.8:27b resident at 20.5 GB across BOTH 3090s at
#     context_length 65536, GPU0 100% / GPU1 50%, load average 38.9 falling to 3.4. It is generating
#     for the CI eval gate, which owns this span by settled doctrine. A non-streaming probe DID
#     return the correct answer, in 119s: queue wait, not a broken backend. So the span stays in the
#     table as the preferred executor and the launch probe decides per launch - which is exactly
#     Paxton's rule ("if there is no local available models, then use the flash models from the
#     cloud for the executor"), with Synthetic standing in for a cloud tier that no longer exists.
#     DO NOT retune the 64k throughput config to chase this - Paxton, 2026-09-09: already tested.
#
#   SYNTHETIC IS THE ONLY RELIABLE CAPACITY: 9 of 9 models answered correctly in 0.8-3.2s, each
#     served by ITSELF (fallbacks=0, served name == alias). It is the whole table below.
#
# HOW THE TABLE IS BUILT. Synthetic bills per REQUEST regardless of which model serves it
# (Paxton: "why would you be using the cheaper models?"), so model choice is free and the only real
# constraint is the ceiling of 5 CONCURRENT PER MODEL. Spread so that at CAP=6 no model is bound
# more than 3 times across the six trios, leaving headroom rather than sitting on the limit - the
# previous table had glm-5.3-flash:synthetic-anthropic as executor on 5 of 6 live runs, exactly AT
# the ceiling, on the highest-volume role. Auditor keeps the three frontier backends
# (kimi-k3, syn:large:text, syn:large:vision); manager keeps the flash tier; every trio holds three
# distinct backends, which check_pools() re-asserts against the SERVED name at startup.
# 2026-09-09 23:58 CORRECTING MY OWN REBUILD. Two launches from the 23:00 table died at round 1 with
#   400 Custom_openaiException - "System message must be at the beginning."
# on manager syn:small:vision:synthetic-anthropic. Same fault class that killed qwen3.8-27b:synthetic-anthropic
# three times earlier tonight. THE EMIT PROBE CANNOT SEE IT: a single user message returns 200 in 1.3s from the
# exact model that then rejects a real agent payload. So "9 of 9 models answered the probe" was never evidence
# that all nine could run a round, and I built a table on it anyway. That is the same shape of error as measuring
# an empty text block with a 48-token budget - a probe that does not resemble the real workload.
# THE RULE NOW: only models PROVEN IN A REAL MULTI-ROUND RUN go in the table. Proven tonight, by role, from live
# snapshots: kimi-k3 (manager+auditor), glm-5.2 (executor, 6 rounds), syn:large:vision (manager+auditor),
# glm-5.3-flash (executor, many rounds), nemotron-3-super (auditor), syn:large:text (auditor). That is six
# backends plus the free local span - enough for six trios of three distinct backends with none above 4
# concurrent, against Synthetic's ceiling of 5.
# REMOVED as unproven, whatever the probe says: syn:small:vision (proven BROKEN), syn:small:text (same family,
# same route, untested in a round), gpt-oss-120b (untested in a round). They may be fine; they have not earned a
# 30-minute episode. All :cloud models stay out - Ollama is quota-dead until the weekly accounts roll (~4 days).
# 2026-09-10 02:37 LOCAL EXECUTOR TRIOS CUT FROM 4 TO 2 - the watchdog proved the span's real capacity
# by killing a run, and the number it produced is the reason for this edit.
# WHAT HAPPENED. The spin watchdog fired for the first time and aborted+requeued run ba2ca222 (task 16a). Its
# trail is exactly the design working, not a hair trigger:
#   02:34:51  cold probe failed, WARM RETRY also 12.0s -> FAIL   strike 1/2, no action
#   02:36:20  89 seconds later, probed again -> FAIL 12.0s       strike 2 -> ABORTED + REQUEUED
# Four probe attempts across two rounds, 89s apart, all at the ceiling. Then I checked whether it was RIGHT rather
# than assuming: a direct 300-token call to qwen3.8-nothink returned 200 in **111.5 SECONDS**. The span was
# genuinely starved, the abort was correct, and the task went back to its own queue slot with nothing lost.
# THE MEASUREMENT THAT MATTERS: two harness executors on the span is ALREADY too many. It is effectively a
# ONE-executor resource while it is also serving the CI eval gate. The launch probe gates the moment of launch,
# but it cannot stop a SECOND run landing on the span afterwards - which is exactly how this run starved.
# So the table now asks for the local executor in TWO trios, not four. Fewer starvations to catch beats a
# watchdog catching more of them. This is a reduction in ambition against a measured number, not a retreat from
# Paxton's "local qwen3.8 for ALL executors" - the span physically serves about one, and asking for four
# guaranteed the failure the watchdog then had to clean up.
# CONSTRAINTS HELD, all re-verified after the edit: every trio 3-distinct; glm-5.2 NEVER an executor (demoted
# 02:10 after an 1800s timeout); max Synthetic concurrency 4 against the ceiling of 5; and all six trios are
# DISTINCT configurations, so a probe failure rotating to the next trio actually changes something - the naive
# balanced table had identical pairs, where a rotation would have retried the same three backends.
_TRIOS=[
    # manager (FLASH)                       executor                               auditor (FRONTIER)
    # 2026-09-10 00:42: the local executor slot was `qwen3.8-nothink`, which has **0 completions
    # in 6 episodes** across two unrelated tasks (task 82 timed out on 5 of 7 rounds at 1800s,
    # task 88 the same). ornith-1.5:pool has actually COMPLETED an executor turn tonight -
    # 9,554 chars on run 115 - so the local slot now goes to the backend with evidence.
    ("syn:large:vision:synthetic-anthropic","ornith-1.5:pool",                     "kimi-k3:synthetic-anthropic"),   # 2026-09-14: glm-5.2 RETIRED upstream (404 "no longer supported"); manager swapped, 3 distinct backends kept
    ("kimi-k3:synthetic-anthropic",         "ornith-1.5:pool",                     "syn:large:vision:synthetic-anthropic"),   # 2026-09-14: glm-5.2 retired; manager swapped
    ("kimi-k3:synthetic-anthropic",         "nemotron-3-super:synthetic-anthropic","syn:large:vision:synthetic-anthropic"),   # 2026-09-14: glm-5.2 retired; manager swapped
    ("syn:large:vision:synthetic-anthropic","nemotron-3-super:synthetic-anthropic","kimi-k3:synthetic-anthropic"),   # 2026-09-14: glm-5.2 retired; manager swapped
    ("nemotron-3-super:synthetic-anthropic","kimi-k3:synthetic-anthropic",         "syn:large:vision:synthetic-anthropic"),
    ("nemotron-3-super:synthetic-anthropic","syn:large:vision:synthetic-anthropic","kimi-k3:synthetic-anthropic"),
]
_SYN_DOWN=[False]  # set once per cycle by synthetic_down()
_DQ=[None]  # doctor's quota verdict: ADVISORY ONLY since 2026-09-10 19:12 PT. Never gates.
_PREV_SYN_DOWN=[True]   # last cycle's synthetic state, for recovery ramp-up.
# 2026-09-10 00:42 SEEDED **True**, not False. It was False, and that silently DEFEATED the ramp:
# I restarted the launcher at 00:38 while Synthetic was down, which reset this to False, so on
# the next cycle _RAMP = (False and not False) = False and **three runs launched at once**
# instead of the intended one. Synthetic happened to be healthy so nothing burned, but the herd
# guard was bypassed by my own restart - the exact failure the ramp exists to prevent (six runs
# launched into a brief recovery earlier tonight and five died). Seeding True means a restart
# assumes the worst: the first cycle after any restart ramps rather than stampedes.
# TICK #1559 (2026-09-22 18:0x PT): _SEQ WAS IN-MEMORY ONLY, SO EVERY LAUNCHER RESTART RESET THE
# ROTATION TO 0 AND RE-SEATED THE FIRST SYNTHETIC LAUNCH ON SYN_ONLY[0] = trio 2 = the
# nemotron-3-super executor. That is not a theory: task 204's executor died on nemotron
# ("Prompt is too long - automatic compaction failed"), #1558 restarted the launcher to requeue
# it, and the "divert" drew nemotron AGAIN because the counter had gone back to zero. A rotation
# that resets on restart cannot rotate away from a failing backend - which is exactly what the
# three-strikes discriminator asks it to do. Persist it. Fail-open: any read error starts at 0.
_SEQ_FILE=r'C:/tmp/launch_seq.txt'
def _seq_load():
    try:
        return int(open(_SEQ_FILE,encoding='utf-8').read().strip())
    except Exception:
        return 0
def _seq_save(v):
    try:
        open(_SEQ_FILE,'w',encoding='utf-8').write(str(v))
    except Exception:
        pass   # never let bookkeeping break a launch
_SEQ=[_seq_load()]   # monotonic launch counter, now durable - see the 04:05 note at the call site
def _trio(i):
    mgr,ex,aud=_TRIOS[i%len(_TRIOS)]
    return {"manager":{"agent":"claude_code","model":mgr},
            "executor":{"agent":"claude_code","model":ex},
            "auditor":{"agent":"claude_code","model":aud}}
# 2026-09-09 16:20 PT  PAXTON: "have a fallback kimi trio that works with synthetic models!"
# Trios 0 and 1 seat the EXECUTOR on the local span. The span is effectively a ONE-executor
# resource while it is also serving the CI eval gate (see the LOCAL_CAP note above), and task 67
# died on it THREE times - five zero-output rounds on run 6c0a3e45, then two zero-output rounds
# plus two timeouts on run 2e58b480 - while Synthetic sat at 48.97% weekly credit and answered
# every probe. The old degraded-mode fallback made this WORSE: _SYN_DOWN sent everything to the
# all-local "qwen" trio, i.e. it fell back ONTO the backend that was already failing.
# So: when the Ollama keys are short and Synthetic is UP, draw only from the trios whose three
# roles are ALL :synthetic-anthropic - no local span, no :cloud. That is the fallback Paxton asked
# for. The local-only fallback still exists for the opposite case (Synthetic down, keys fine).
SYN_ONLY=[i for i,(m,e,a) in enumerate(_TRIOS)
          if all(':synthetic-anthropic' in x for x in (m,e,a))]
def _local_exec(roles):
    """True if this trio's EXECUTOR sits on the local Ollama span. The span is the
    contended resource; a synthetic or :cloud executor costs it nothing."""
    m=((roles or {}).get("executor") or {}).get("model","")
    return (":synthetic" not in m) and (":cloud" not in m)
def _trio_syn(i):
    return _trio(SYN_ONLY[i%len(SYN_ONLY)]) if SYN_ONLY else _trio(i)
# TASK 196 (2026-09-18): the span is a ONE-EXECUTOR resource, and it is still the CI eval gate's
# backend. Measurement over all 20 runs of 2026-09-17: ornith-1.5:pool executors lost 3 of 4 runs
# to the 1800s executor budget (6 rounds), while nemotron-3-super - the largest sample in the
# fleet, 9 runs - lost none. When Synthetic is HEALTHY there is no reason to pay that tax: the
# executor seat is diverted to the all-Synthetic trios unconditionally. This SUBSUMES the two
# earlier guards below (ok<2 and qw>=LOCAL_CAP): they only diverted in their own corner cases,
# so a healthy-keys, half-empty span still seated an executor on the span - which is how task
# 180 drew trio 0 and lost three rounds on 2026-09-17. Manager/auditor seats on the span are
# UNCHANGED - the span proved it can serve those roles (418s/15009 chars on task 88 r1). The
# 2026-09-09 bug is NOT reintroduced: when Synthetic is DOWN the diversion does not fire, and
# degraded mode below still never falls back onto the failing backend for the executor.
_TRIOS_LOCAL_EXEC=[i for i,(m,e,a) in enumerate(_TRIOS) if (":synthetic" not in e) and (":cloud" not in e)]
# NOTE: this is documentation only - the actual divert keys off _local_exec(roles) on the DRAWN
# trio (trios 0 and 1 today), so it stays correct if the table is re-ordered or re-annotated.
# 2026-09-09 22:45 PT  PAXTON: "update lh-harness template to use whatever is available when llm
# provider is down." DEGRADED MODE. Every trio above is :synthetic-anthropic plus a local qwen
# executor, so when Synthetic is exhausted there was NOTHING in the pool that could run - the queue
# just held. MEASURED 2026-09-09 22:45 on /v1/messages with the HARNESS key (not the master key,
# and not /v1/chat/completions - the agent speaks ONLY the Anthropic route):
#     glm-5.3:cloud   200 in 0.76s   glm-5.3:pool 200 in 0.44s   <- USE THESE
#     kimi-k2.6:cloud 429 (ai-dev01 weekly)   kimi-k2.7-code:pool 429 (ai-dev02 weekly)
#     kimi-k3:pool    429 (aidev01cogniziocompany MONTHLY)   glm-5.2:cloud 429 (prax211 weekly)
#     nemotron/qwen/glm :cloud  HTTP 403  not on the harness key allow-list
# 22:55 PAXTON: "stop using k2.7 switch to glm5.3". Correct call and it caught a live fault:
# kimi-k2.6 answered 200 at 22:45 and 429 at 22:55. Quota is PER OLLAMA ACCOUNT, and the
# accounts differ per alias (ai-dev01, ai-dev02, aidev01cogniziocompany, prax211) - so an
# alias that probed healthy ten minutes ago is NOT evidence it is healthy now. The emit
# probe below is what actually gates the launch; this table only picks the candidates.
DEGRADED_TRIOS=[
    # 2026-09-10 02:27 SPLIT ACROSS THE TWO LOCAL DAEMONS. Removing the second ornith slot at 02:15
    # left ONE slot, and all-ornith trios then serialise every role of every run through it. But
    # there is a second local daemon ALREADY LOADED and costing no extra host RAM: the qwen3.8:27b
    # span on :11442 (1.7 GB resident). Using it for the manager splits the load across two slots.
    #
    # EVIDENCE PER ROLE, and I refused to make this change until the missing half was measured:
    #   ornith EXECUTOR  9,554 / 9,212 / 6,255 chars   - three completions, the role it does
    #   span   EXECUTOR  **0 completions in 6 episodes** - NEVER put the executor here
    #   span   MANAGER   418s / 15,009 chars (task 88 r1), and measured again 02:26:
    #                    200 in 24.8s, stop=end_turn, 526 chars of a concrete plan naming a
    #                    migration file and real SQL. TWO successes now, not one.
    #   span   AUDITOR   **UNMEASURED** - inferred from the manager result, same model, similar
    #                    read-and-judge shape. That inference is HEDGED by the rotation: if the
    #                    span auditor fails its probe, trio 2 and then the all-ornith fallback
    #                    take over. No single unmeasured assumption can stall the queue.
    # 2026-09-10 09:50 qwen3.8 RETIRED from the local trios (Paxton: "all qwen models will
    # forward to ornith1.5"). Two rows used to seat qwen3.8 as manager/auditor. Both are gone,
    # because CALLING qwen3.8 RELOADS THE SPAN: qwen3.8 resolves to :11442, which is
    # CUDA_VISIBLE_DEVICES=0,1 + OLLAMA_SCHED_SPREAD=1, so it re-takes ~10 GB of VRAM across BOTH
    # cards and ~5 GB of host RAM. Measured today: after unloading it, two new runs whose manager
    # was qwen3.8 pulled it straight back (GPU0 12,262 -> 22,905 MiB, RAM 15.8 -> 10.6 GB).
    # ornith can take those seats now in a way it could not this morning: it is loaded on BOTH
    # GPUs (:11441 CUDA1 + :11438 CUDA0), one instance per card, at 262144 context, and the pool
    # round-robins across them - proven by traffic, 3 requests to :11438 vs 2 to :11441, with
    # latency down from 36.086s to 0.25-0.41s.
    # NOTE ornith is a hybrid SSM (arch qwen35) and Ollama REFUSES parallel requests on it
    # ("model architecture does not currently support parallel requests", n_seq_max=1), so
    # concurrency comes ONLY from having two instances. Do not "fix" this with NUM_PARALLEL.
    ("ornith-1.5:pool", "ornith-1.5:pool", "ornith-1.5:pool"),  # both GPUs, 256K, fully local
    ("ornith-1.5:pool", "ornith-1.5:pool", "glm-5.3:cloud"),    # if the cloud alias recovers
]
# NOT glm-5.3-flash:cloud: it answers, but at 3.9s against the 3s emit budget it would be BLOCKED
# by the probe on every attempt. A backend that is up but slower than the gate is not a candidate.
# ONE key of capacity is a SLIVER, and tonight proved what a herd does to a sliver: a momentary
# Synthetic recovery let six runs launch at once, they collectively burned it, and five died on 429
# - including both top-priority powerplatform tasks. So degraded mode runs at a LOW cap. Two, not six.
# 2026-09-10 00:02 RAISED 2 -> 3. The cap existed because ONE Ollama cloud key of capacity is a
# sliver and a herd burns it. ornith changes that: it is local, has NO quota, and the trio table
# rotates so only trio 0 puts an EXECUTOR on its single slot - a third concurrent run lands on
# the all-cloud trio and contends with nothing. Still bounded, because the cloud aliases remain
# quota-limited and ornith is one slot; this is not a licence to stampede.
# 2026-09-10 02:15 LOWERED 3 -> 2. ptait01 OOM-killed llama-server TWICE (02:04 and 02:09,
# anon-rss ~9 GB each), which surfaced as `unexpected EOF` and failed run 115. Host RAM is
# 42 GB with SEVEN LXC guests plus docker, so it is oversubscribed by design - ornith is not the
# sole cause, but its two daemons were 11.3 GB of it and the :11438 one was the single largest
# process on the box at 7.7 GB. I removed the :11438 ornith deployment from the router, so there
# is now ONE ornith slot; a cap of 3 against one slot recreates exactly the single-slot
# contention that produced 61-char stubs on the qwen span earlier tonight.
DEGRADED_CAP=2
def _degraded_trio(i):
    mgr,ex,aud=DEGRADED_TRIOS[i%len(DEGRADED_TRIOS)]
    return {"manager":{"agent":"claude_code","model":mgr},
            "executor":{"agent":"claude_code","model":ex},
            "auditor":{"agent":"claude_code","model":aud}}
EXEC_POOL=_TRIOS   # the launch loop iterates len(EXEC_POOL) trios before holding
MGR_POOL=_TRIOS
AUD_POOL=_TRIOS
KIMI=_trio(0)
def served_model(alias):
    """Ask the router which model actually answers for this alias (message_start reports it)."""
    # 2026-09-10 04:40: was a FIXED "hi" prompt with no cache bypass, which made this
    # diagnostic unreliable in exactly the case it exists to detect. The router has a
    # response cache, so an identical prompt is served from it and message_start then
    # reports the model that answered LAST time, not the one answering now - and spotting
    # "an alias silently serves another model" is this function's whole purpose. Measured
    # the hazard: repeated identical /v1/messages probes returned 200 in 0.013s / 0.013s /
    # 0.015s (impossible for real inference) while the SAME model with unique prompts took
    # 1.31s / 1.31s / 2.00s. Nonce + no-cache, matching emit_ok().
    nonce=uuid.uuid4().hex[:8]
    body=json.dumps({"model":alias,"max_tokens":8,"stream":True,"cache":{"no-cache":True},
                     "messages":[{"role":"user","content":"hi (%s)"%nonce}]}).encode()
    req=urllib.request.Request(ROUTER+"/v1/messages",data=body,headers={
        "x-api-key":router_key(),"anthropic-version":"2023-06-01","Content-Type":"application/json"})
    try:
        r=urllib.request.urlopen(req,timeout=45)
        for raw in r:
            line=raw.decode("utf-8","replace").strip()
            if line.startswith("data:"):
                ev=json.loads(line[5:].strip())
                if ev.get("type")=="message_start":
                    r.close(); return ev["message"].get("model")
    except Exception as e: return "ERR:"+str(e)[:40]
    return "?"
def check_pools():
    """Assert every trio uses three DIFFERENT backends. Warn loudly rather than exit: a wrong
    warning must not stop the queue draining, but a silent collapse must never happen again."""
    cache={}
    for i in range(max(len(EXEC_POOL),len(MGR_POOL),len(AUD_POOL))):
        t=_trio(i); seen={}
        for role,cfg in t.items():
            al=cfg["model"]
            if al not in cache: cache[al]=served_model(al)
            seen.setdefault(cache[al],[]).append(role+"="+al)
        dupes={k:v for k,v in seen.items() if len(v)>1}
        # A 429 from an exhausted provider makes every model report the SAME error string, which
        # reads as a collapse but is not one - it is one outage seen three times. Say so rather
        # than printing a misleading COLLAPSED line (2026-09-10 05:37).
        tag="OK" if not dupes else ("all backends 429 - provider exhausted, not a pool collapse"
             if all("429" in str(k) for k in dupes) else "COLLAPSED -> "+str(dupes))
        print("  trio %d %s"%(i,tag),flush=True)
# 2026-09-16 21:26 PT TICK #362: WAS 2, AND THAT LET TWO RUNS ONTO A ONE-SLOT BACKEND.
# LOCAL_CAP was sized to OLLAMA_NUM_PARALLEL=2 on the OLD span. The local executor today is
# `ornith-1.5:pool`, which is a hybrid SSM (arch qwen35) that Ollama REFUSES parallel requests
# on (see the DEGRADED_TRIOS note ~line 268), and the :11438 ornith deployment was removed from
# the router - so the real local capacity is ONE executor, not two. With CAP=2 the `SPAN FULL`
# guard at ~line 731 only fired on the THIRD local run, so a second run bound ornith and starved.
# MEASURED: run 20260917T024751Z_d8a4d5ad (task 100) failed three consecutive rounds with its
# executor timing out at EXACTLY 1800s (1800134/1800157/1800216 ms) while 20260917T023220Z_75700773
# (task 107b) held the same pool as its executor; manager+auditor on Synthetic finished in 23-137s.
# CONTROL (tick #337): the one executor episode that COMPLETED did so in 334s, in a window when the
# other run was between roles. Same role, same backend, repeatedly = backend fault, not scope -
# and tick #337 diagnosed that correctly but requeued without changing this cap, so it recurred.
LOCAL_CAP=1   # ornith-1.5:pool serves ONE executor; do NOT raise without re-measuring parallelism
QWEN={"manager":{"agent":"claude_code","model":"qwen3.8"},"executor":{"agent":"claude_code","model":"qwen3.8-nothink"},"auditor":{"agent":"claude_code","model":"qwen3.8"}}
def synthetic_down():
    """Is Synthetic refusing everything right now? 2026-09-10 05:29 it began returning
    429 "You've exceeded your subscription rate limits" on EVERY model - the pack-level
    2500-requests-per-5-hours budget, not the 5-concurrent-per-model ceiling. Four runs died
    and the fleet went to zero with 16 tasks queued.
    One probe of one model is enough to answer this: the limit is per PACK, so if one model
    429s on a fresh request they all do. Cheap, and it costs one request rather than three."""
    r=emit_ok("nemotron-3-super:synthetic-anthropic")
    return (not r[0]) and "429" in str(r[2])
H={"Authorization":"Bearer "+TOKEN,"Content-Type":"application/json"}

# ---- 1-3 SECOND EMIT PROBE (Paxton, 2026-09-09) ------------------------------------------
# WHY: on 2026-09-08 ten runs each burned a full 1800s episode budget and returned NOTHING,
# because nine executors were queued behind a 2-slot local span. The harness could not tell
# "working" from "queued": both look like silence. A saturated backend cannot answer even a
# 4-token request quickly, so ask it to emit one BEFORE committing a 30-minute episode.
# A run is launched only if every role's model emits inside EMIT_BUDGET seconds.
EMIT_BUDGET=12.0
# 2026-09-16 16:40 PT tick #326 RAISED 3.0 -> 12.0 FOR HOSTED. MEASURED, one probe cycle
# (16:12, isolated lines 3609-EOF): 40 trios / 106 :synthetic-anthropic measurements,
# min 5.2s median 6.2s max 11.8s, ZERO under 3s, while synthetic read `ok` and the router
# answered 200. The 3s budget therefore blocked 100% of hosted candidates on a HEALTHY
# backend: 0 LAUNCHED in a cycle that ran 26+ minutes, and each blocked alias is probed
# TWICE (the retry below), so the probe burned ~200 Synthetic requests against a 500/5h
# bucket to launch nothing - plausibly feeding the very 429s that killed 86/99/100.
# 106/106 of those measurements pass at 12s. Second consecutive `ok` cycle, which is the
# trigger tick #325 recorded in task 172's note.
# 2026-09-10 01:55 MODEL-AWARE BUDGET. A hosted alias that is slow to start emitting is
# genuinely unhealthy, so 3s is right for :synthetic and :cloud. A LOCAL alias is different:
# it queues behind whatever is already executing on the same GPU, so time-to-first-emit is
# dominated by CONTENTION, not health. MEASURED tonight on ornith-1.5:pool -
#     idle:                    0.3s / 0.5s / 0.7s / 3.4s   -> passes
#     one run executing on it: 10.1s / 12.1s / 12.3s       -> "read operation timed out"
# The 3s budget therefore reported a HEALTHY local backend as dead and capped the queue at ONE
# run, while ornith was the only backend answering at all. That is the probe failing the fleet,
# not the fleet failing the probe. Local aliases get 6x; hosted ones keep the tight budget.
def emit_budget(model):
    local = (":synthetic" not in model) and (":cloud" not in model)
    # tick #326: local is PINNED at 18s, the value the 6x multiplier gave when the hosted
    # budget was 3s. It must not follow the hosted raise - 12*6=72s, and this cycle measured
    # ornith-1.5:pool timing out at exactly 72.0s, i.e. a 72s budget would stop catching a
    # genuinely starved local span. Local contention behaviour is unchanged by this tick.
    return 18.0 if local else EMIT_BUDGET
ROUTER="https://litellm.easybutt0n.ai"
_RK=[None]
def router_key():
    """The HARNESS's virtual key, not the master key.
    2026-09-09: the first version of this probe used LITELLM_MASTER_KEY, so every model passed -
    and then two runs died instantly with 403 'key not allowed to access model'. The harness key
    carries an allow-list (33 models then, 37 now); a probe on the master key proves nothing about
    what the harness can actually reach. Probe with the credential the agent itself will use."""
    if _RK[0] is None:
        _RK[0]=subprocess.run(["ssh","-o","StrictHostKeyChecking=no","root@192.168.21.151",
            "pct exec 110 -- bash -lc 'grep -hoE \"^ANTHROPIC_AUTH_TOKEN=.*\" "
            "/home/harness/.lh-harness-secrets.env | cut -d= -f2-'"],
            capture_output=True,text=True,timeout=60).stdout.strip()
    return _RK[0]
def emit_ok(model):
    """Time to the FIRST content event on the streaming path - Paxton's 1-3 second emit.
    Total completion time is the wrong metric: a healthy cloud model can spend 14s finishing a
    sentence while answering instantly, and a starved local span spends 1800s producing nothing.
    What separates them is how fast the backend STARTS emitting.
    Two calibration mistakes made while building this, both recorded so they are not repeated:
      - requiring a text delta with max_tokens=24 failed every thinking-capable model, because
        the thinking block ate the whole budget. glm-5.3-flash:cloud looked dead and was fine.
      - requiring a delta at all fails a model whose first block is empty. We accept
        content_block_start: the question is whether the backend is SERVING, not what it says.
    Measured 2026-09-09: synthetic/cloud 0.6-2.2s; local span 12s timeout (240s under load)."""
    # NONCE: LiteLLM caches responses. Two probes returned in 0.0s on first run - a cached hit,
    # which would make a fully saturated backend look instant and defeat the entire check.
    # A unique prompt per probe guarantees the request actually reaches the backend.
    nonce=uuid.uuid4().hex[:8]
    body=json.dumps({"model":model,"max_tokens":32,"stream":True,"cache":{"no-cache":True},
                     "messages":[{"role":"user","content":"Say READY (%s)"%nonce}]}).encode()
    req=urllib.request.Request(ROUTER+"/v1/messages",data=body,headers={
        "x-api-key":router_key(),"anthropic-version":"2023-06-01","Content-Type":"application/json"})
    t=time.time()
    try:
        r=urllib.request.urlopen(req,timeout=int(emit_budget(model)*4))
        for raw in r:
            line=raw.decode("utf-8","replace").strip()
            if not line.startswith("data:"): continue
            try: ev=json.loads(line[5:].strip())
            except Exception: continue
            if ev.get("type") in ("content_block_start","content_block_delta"):
                dt=time.time()-t; r.close()
                return dt<=emit_budget(model), dt, "emit"
            if ev.get("type")=="error":
                return False, time.time()-t, str(ev)[:70]
        return False, time.time()-t, "stream ended with no content event"
    except Exception as e:
        return False, time.time()-t, str(e)[:70]
def roles_ready(roles):
    """Probe each DISTINCT model in the trio. Returns (ok, report-string)."""
    seen={}; bad=[]
    for role,cfg in roles.items():
        m=cfg["model"]
        # ONE retry before holding. Healthy backends measure 0.6-2.4s, but a marginal 3.1s (seen
        # 19:27 on kimi-k3:synthetic-anthropic) would park a task for a whole 15-minute cycle over
        # a tenth of a second. A backend that is genuinely starved fails both attempts; a cold
        # start or a momentary blip passes the second. The budget itself stays at 3s.
        if m not in seen:
            r=emit_ok(m)
            if not r[0] and "403" not in r[2]: r=emit_ok(m)   # never retry an allow-list refusal
            seen[m]=r
        ok,dt,detail=seen[m]
        if not ok: bad.append("%s/%s %.1fs %s"%(role,m,dt,detail))
    rep=" ".join("%s=%.1fs"%(m,v[1]) for m,v in seen.items())
    return (not bad), (rep if not bad else rep+" || BLOCKED: "+"; ".join(bad))

# ---- SPEC GATE (2026-09-10) ---------------------------------------------------------------
# stage_specs.py writes spec_file into each queue entry, pointing at a BMAD-style spec the
# overseer distills from the raw task_file. The spec's frontmatter `status:` is the launch gate:
# only `ready-for-dev` launches; `draft` prints SPEC_PENDING and the entry waits in place, keeping
# its queue position. Entries with no spec_file keep the old behaviour (raw task_file) so nothing
# already queued stalls. The role receives the spec BODY only - frontmatter stripped - because the
# frontmatter is bookkeeping for this launcher and the app, not instructions for the model.
def _spec_status(text):
    if not text.startswith("---"): return None, text
    end=text.find("\n---",3)
    if end<0: return None, text
    st=None
    for l in text[3:end].splitlines():
        if l.strip().startswith("status:"): st=l.split(":",1)[1].split("#",1)[0].strip().strip("\"'")
    body=text[end+4:]
    return st, body[1:] if body.startswith("\n") else body
def _task_text(j):
    """Task text the run gets, or None (and a printed reason) when this entry must not launch."""
    now=datetime.datetime.now().strftime("%H:%M")
    if j.get("spec_file"):
        if not os.path.exists(j["spec_file"]): print(now,"  SPEC_MISSING",j["name"],flush=True); return None
        status,body=_spec_status(open(j["spec_file"],encoding="utf-8").read())
        if status!="ready-for-dev": print(now,"  SPEC_PENDING",j["name"],status,flush=True); return None
        return body.strip()
    try: return open(j["task_file"],encoding="utf-8").read().strip()
    except (OSError,KeyError): print(now,"  TASK_FILE_MISSING",j["name"],flush=True); return None

def keys_ok():
    q=open("C:/tmp/keys_status.txt",encoding="utf-8",errors="replace").read().strip().splitlines()
    for l in reversed(q):
        if "keys ok:" in l: return int(l.split("keys ok:")[1].split("|")[0])
    return 0
def active():
    _SYN_LIVE[0]=0
    d=json.load(urllib.request.urlopen(urllib.request.Request(BASE+"/api/runs",headers=H),timeout=180))
    rs=d if isinstance(d,list) else (d.get("runs") or d.get("items") or [])
    live=[r for r in rs if str(r.get("status")) in ("running","starting","waiting_approval")]
    kimi=qwen=0; ws=[]
    for r in live:
        rid=r.get("id") or r.get("run_id")
        try:
            snap=json.load(urllib.request.urlopen(urllib.request.Request(BASE+f"/api/runs/{rid}/snapshot",headers=H),timeout=120))
            blob=json.dumps(snap.get("run",{}).get("roles") or snap.get("run",{}).get("provenance") or snap.get("run",{}))
        except Exception: blob=json.dumps(r)
        # 2026-09-11 FIX: the local executor is `ornith-1.5:pool`, which CONTAINS ":pool".
        # The old test therefore counted every LOCAL run as kimi, left qwen permanently 0,
        # and made the `qw>=LOCAL_CAP` guard at ~line 684 DEAD CODE - so CAP=6 governed local
        # capacity instead of LOCAL_CAP=2, allowing six runs onto a backend with ONE slot per
        # instance. That is the starvation that killed tasks 67, 83 and 125. Test the LOCAL
        # backend name FIRST; ":pool" after it then means a cloud pool, as originally intended.
        if ":synthetic-anthropic" in blob: _SYN_LIVE[0]+=1   # SYN_CAP accounting (2026-09-14)
        if "ornith" in blob: qwen+=1
        elif ":pool" in blob: kimi+=1
        elif "qwen3.8" in blob: qwen+=1
        else: kimi+=1
        ws.append((snap.get("run",{}) if isinstance(snap,dict) else {}).get("workspace") or r.get("workspace"))
    return len(live),kimi,qwen,ws
# ---- AUTHORITATIVE QUOTA CHECK (2026-09-10 12:01) ----------------------------------------
# The failure-window gate below has a flaw I found an hour after writing it: it is SELF-
# DEFEATING. It fires because Synthetic runs died; firing stops Synthetic launches; no launches
# means no new failures; the window empties; the gate stops firing; runs launch on Synthetic
# again and die. It oscillates, paying ~2 dead episodes per cycle to re-learn what is already
# known elsewhere.
# The ops doctor on CT202 ALREADY asks Synthetic for its real quota (that container holds
# SYNTHETIC_API_KEY, so this is the provider's own number, not an estimate from spend logs).
# Measured 2026-09-10 18:56Z: "0 tokens remaining this week (625,900,000 used of 625,900,000)".
# ops.easybutt0n.ai/api/doctor is reachable from THIS PC and /api/ is allow-listed in
# ForwardedIdentityMiddleware, so no user credential and no auth bypass is involved - it is a
# service key doing the read it exists for. Key lives in a file, NOT in this script.
def synthetic_quota_exhausted():
    # ---- DISABLED 2026-09-10 18:55 PT BY PAXTON'S DECISION -------------------------------
    # "do NOT flip to fail-closed - stop the doctor gating launches until its signal is fixed"
    #
    # WHY: this gate read ops.easybutt0n.ai /api/doctor, whose check_synthetic_quota() DOES NOT
    # ASK SYNTHETIC ANYTHING. It runs a psql SUM(total_tokens) against OUR OWN LiteLLM_SpendLogs
    # and compares it to a HARDCODED WEEKLY_QUOTA_TOKENS = 625_900_000. Synthetic does not meter
    # in tokens at all: it meters 500 REQUESTS PER 5 HOURS (regenerating 25 every 15 min) and a
    # WEEKLY DOLLAR CREDIT (regenerating ~2% per ~2h). So the gate compared a local estimate to
    # an invented ceiling and reported "0 tokens remaining this week".
    #
    # MEASURED 2026-09-10 18:45 PT, the same moment the doctor said exhausted:
    #     dashboard: five-hour requests 100% UNUSED, weekly credits 3.28% and regenerating
    #     SpendLogs: 66 requests in the last 5h against a cap of 500 = 13%
    # Regeneration alone is +100 requests/hour. We were using about half of what refills, while
    # this gate pinned the launcher at DEGRADED_CAP=2 with 23 tasks queued for an entire evening.
    #
    # NOT FAIL-CLOSED, deliberately. A gate that cannot measure the real limit should not get a
    # vote at all - making it fail-closed would turn a false positive into a permanent stall.
    # The fail-open below is the only reason any Synthetic work ran tonight (task 69a launched
    # through it and completed in 8 minutes).
    #
    # RE-ENABLE ONLY WHEN check_synthetic_quota() measures a REAL meter - requests/5h, or the
    # weekly credit - and not a locally-summed token proxy. Tracked as task 146.
    # NOTE 2026-09-10 19:12 PT: the blanket `return False` that sat here is REMOVED.
    # Killing the call destroyed the signal as well as the vote. This function now runs
    # and reports normally; it is simply NOT in the _SYN_DOWN chain any more (see ~L543).
    # Its verdict is printed as doctor_quota=... on the status line and gates NOTHING.
    try:
        k=open("C:/tmp/.ops_ingest_key").read().strip()
        if not k: return False
        rq=urllib.request.Request("https://ops.easybutt0n.ai/api/doctor",
                                  headers={"Authorization":"Bearer "+k})
        d=json.load(urllib.request.urlopen(rq,timeout=45))
    except Exception:
        return False   # fail OPEN: never block launches because a monitoring surface hiccuped
    for c in d.get("checks") or []:
        if "Synthetic quota" in str(c.get("name","")) and c.get("ok") is False:
            det=str(c.get("detail",""))
            if "0 tokens remaining" in det or "CRITICAL" in det:
                return True
    return False

# ---- QUOTA GATE (2026-09-10 11:33) -------------------------------------------------------
# WHY: at 11:08 synthetic_down() probed nemotron, got an answer in 1.0s, and this launcher
# recorded synthetic=ok and launched THREE runs onto all-Synthetic trios. All three were dead
# within 15 minutes - tasks 126, 114 and 102 - every one abort_reason=provider_rate_limit, 429.
# The ops doctor already knew, at that same moment:
#     Synthetic quota: 0 tokens remaining this week (625,900,000 used of 625,900,000)
# A 4-token probe is served out of whatever headroom is left; an hour of real work is refused.
# Same lesson as 2026-09-09 ("a 200 on a tiny probe does not prove capacity"), in a new place.
# So do not ask a probe. Ask the runs that already died.
# SIGNAL: >=2 runs whose role_configs name a :synthetic model, FAILED in the last 30 minutes.
# The asymmetry is deliberate. A false positive routes work to ornith-1.5:pool - local, both
# GPUs since task 123, cap 2 - and costs a little throughput. A false negative costs three
# dead episodes, which is what actually happened.
def synthetic_failing_real_work(window_s=1800, threshold=2):
    try:
        d=json.load(urllib.request.urlopen(urllib.request.Request(BASE+"/api/runs",headers=H),timeout=180))
        rs=d if isinstance(d,list) else (d.get("runs") or d.get("items") or [])
    except Exception:
        return False   # never let this gate fail CLOSED on a transport error
    now=time.time(); n=0
    for r in rs:
        if str(r.get("status"))!="failed": continue
        try: ts=float(r.get("updated_at") or 0)
        except Exception: continue
        if ts<=0 or (now-ts)>window_s: continue
        if ":synthetic" in json.dumps(r.get("role_configs") or {}): n+=1
    return n>=threshold

# ---- SHADOW DUAL-FILING (task 168 blocker (a), added 2026-09-22 tick #1554) ---------------
# WHY: the CT110 queue runs in observe mode ([queue] observe=true, live since 16:54 PDT). Its
# shadow launcher logs the decision it WOULD have made for every entry in ITS store, and
# scripts/compare_shadow.py classifies that against this log. Until now this launcher never
# called POST /api/queue at all (its only write was POST /api/runs below), so the two streams
# covered DISJOINT entry sets and every row could only classify pc-only or shadow-only - the
# six promotion evidence items could never accrue no matter how long the shadow ran.
# SHAPE, bounded by the routes that actually exist on origin/main (POST /api/queue :1077,
# DELETE /api/queue/{id} :1117 - there is NO "mark launched" route, so DELETE is the only
# retirement mechanism):
#   file it   -> when this launcher first considers the entry launchable (task text resolved)
#   retire it -> the moment this launcher launches it, or the entry leaves the queue dir
# The DELETE is not optional. Observe mode NEVER marks an entry launched, so a filed entry
# stays pending and the shadow re-decides it every poll (~5.4 records/min, measured on the
# 16:55 probe). Dual-filing WITHOUT the DELETE reintroduces exactly the unbounded-log fault
# that was closed this morning.
# FAIL-OPEN, ALWAYS: every call here is wrapped. CT110 being unreachable must never stop a
# launch - this is instrumentation for a phase that is being measured, not a dependency.
# Set LH_DUALFILE=0 in the environment to disable.
_QIDS={}   # queue-dir basename -> CT110 queue_id, so the entry can be retired later
_RETIRE_AFTER=[]  # task 168 blocker (d), tick #1558: files launched this cycle whose shadow
# entry must OUTLIVE at least one shadow poll before it is deleted. Measured at tick #1555: a
# file-then-retire inside one launcher iteration lands both in the SAME SECOND, so the ~15 s
# shadow poll never sees the entry pending and compare_shadow.py gets ZERO rows to classify.
# Retiring immediately is therefore self-defeating: it is what made the evidence gate
# unaccruable. Delaying it FOREVER is the other failure - that is blocker (c), the unbounded
# re-decide log - so this is deliberately bounded to _RETIRE_DELAY seconds (~3 polls, so ~3
# records per entry: enough for the shadow to be observed deciding, few enough that duplicate
# records cannot inflate the evidence count).
_RETIRE_DELAY=45
def _dualfile_on(): return os.environ.get("LH_DUALFILE","1")!="0"
def _q_trio(trio):
    # CT110 accepts only {kimi,qwen} (queue.py _VALID_TRIOS). This launcher also uses
    # "degraded" and "default". Map anything else to kimi - and do NOT read agreement into
    # the trio field afterwards: compare_shadow.py already calls the PC trio family "not
    # determinable" (scripts/compare_shadow.py L150-159) and never classifies trio-differ.
    return trio if trio in ("kimi","qwen") else "kimi"
def dualfile(f,j,task,trio):
    """Mirror one pending entry into the CT110 store. Returns the queue_id or None."""
    if not _dualfile_on(): return None
    b=os.path.basename(f)
    if b in _QIDS: return _QIDS[b]
    body={"name":j.get("name") or b[:-5],"task":task,"workspace":j["workspace"],
          "max_rounds":j.get("max_rounds",8),"trio":_q_trio(trio),
          "requested_by":"launch_queue.py","dedup_key":"pc:"+b}
    # dedup_key makes this idempotent: a re-POST after a launcher restart returns the entry
    # that already exists rather than minting a second one (queue.py create(), docstring).
    try:
        r=json.load(urllib.request.urlopen(urllib.request.Request(
            BASE+"/api/queue",data=json.dumps(body).encode(),headers=H,method="POST"),timeout=60))
        qid=r.get("queue_id")
        if qid:
            _QIDS[b]=qid
            print(now,"  shadow-filed",b,qid,flush=True)
        return qid
    except Exception as e:
        print(now,"  shadow-file FAILED (ignored)",b,type(e).__name__,str(e)[:120],flush=True)
        return None
def dualfile_retire(f):
    """Delete the mirrored entry once this launcher has launched or dropped it."""
    if not _dualfile_on(): return
    b=os.path.basename(f); qid=_QIDS.pop(b,None)
    if not qid: return
    try:
        urllib.request.urlopen(urllib.request.Request(
            BASE+"/api/queue/"+qid,headers=H,method="DELETE"),timeout=60).read()
        print(now,"  shadow-retired",b,qid,flush=True)
    except Exception as e:
        # 404 = already gone, 409 = the shadow store moved it off pending. Both are fine and
        # neither is worth a retry; the entry is no longer this launcher's to hold.
        print(now,"  shadow-retire noted",b,qid,type(e).__name__,str(e)[:120],flush=True)

os.makedirs("C:/tmp/queue/done",exist_ok=True)
print("pool backend check:",flush=True); check_pools()
while True:
    now=datetime.datetime.now().strftime("%H:%M")
    # tick #356: an overseer scratch file (_t355_75700773.json) written into the queue dir and
    # deleted mid-cycle CRASHED this loop with FileNotFoundError at 20:33:56 and the launcher
    # stayed dead until the next tick noticed. Task files never start with "_"; scratch does.
    q=sorted(f for f in glob.glob("C:/tmp/queue/*.json")
             if not os.path.basename(f).startswith("_"))
    _live={os.path.basename(x) for x in q}
    for _gone in [b for b in list(_QIDS) if b not in _live]:
        dualfile_retire("C:/tmp/queue/"+_gone)   # task 168: left the queue dir by some other hand
    if not q: print(now,"queue empty; exiting"); break
    try: n,k,qw,ws=active(); ok=keys_ok(); _SYN_DOWN[0]=synthetic_down() or synthetic_failing_real_work()
    except Exception as e: print(now,"status err",str(e)[:80],flush=True); time.sleep(600); continue
    try: _DQ[0]=synthetic_quota_exhausted()
    except Exception: _DQ[0]=None
    print(now,f"active={n} kimi={k} qwen={qw} keys_ok={ok} queued={len(q)} synthetic={'DOWN' if _SYN_DOWN[0] else 'ok'} doctor_quota={'EXHAUSTED(advisory)' if _DQ[0] else ('unread' if _DQ[0] is None else 'ok')}",flush=True)
    print(now,f"  synthetic runs live={_SYN_LIVE[0]} SYN_CAP={SYN_CAP}",flush=True)
    # ---- DRAIN SWITCH (task 147, added 2026-09-10 20:30 PT) --------------------------------
    # A zero-active window NEVER occurs on its own: this loop refills slots every 900s. Task 144
    # (merge PR #140, then pg_repack the 26 GB SpendLogs table) needs ACTIVE=0, so the window has
    # to be MADE. Touch C:/tmp/queue/DRAIN and this launches nothing; delete it to resume.
    # Placed AFTER the status print on purpose, so active= keeps being logged during the drain -
    # that log line is how you watch the count fall to zero.
    # The flag has NO .json suffix, so the glob at the top of this loop can never read it as a task.
    # NOTE: this stops NEW launches only. Runs parked at waiting_approval never drain on their own;
    # their gates must be resolved or the count holds forever.
    if os.path.exists("C:/tmp/queue/DRAIN"):
        print(now,f"  DRAIN flag set - launching nothing (active={n}); remove C:/tmp/queue/DRAIN to resume",flush=True)
        time.sleep(900); continue
    # 2026-09-09 22:05 PT RECOVERY RAMP. MEASURED: at 21:30 the emit probe passed on a momentary
    # 5-hour request-budget sliver while the WEEKLY credit was still exhausted, the loop fired SIX
    # runs in one cycle, they collectively burned the sliver, and five died on 429 - including the
    # two top-priority powerplatform tasks (107, 108). The probe was not wrong at that instant; the
    # HERD was. On the first cycle after Synthetic recovers, launch AT MOST ONE run and let it prove
    # the capacity is real before the next cycle fires a batch. One burned task is recoverable; six
    # burned priority tasks is not.
    _RAMP = _PREV_SYN_DOWN[0] and not _SYN_DOWN[0]
    _PREV_SYN_DOWN[0] = _SYN_DOWN[0]
    if _RAMP: print(now,"  RECOVERY RAMP: Synthetic just returned - launching at most ONE run this cycle",flush=True)
    _LOCAL_SPAN_BUSY=[False]  # per-cycle circuit breaker; see the LOCAL-ONLY probe below
    for f in q:
        # tick #356: never let one unreadable/vanished queue file kill the whole launcher.
        try:
            j=json.load(open(f,encoding="utf-8"))
        except (FileNotFoundError,OSError,ValueError) as e:
            print(now,"  SKIP unreadable queue file",os.path.basename(f),"-",type(e).__name__,flush=True)
            continue
        if os.path.basename(str(j["workspace"]).rstrip("/")) in {os.path.basename(str(w or "").rstrip("/")) for w in ws}: continue  # one run per workspace; compare basenames on both sides
        trio=j.get("trio","kimi")
        # Ollama/kimi keys exhausted -> fall back to the local qwen3.8 trio, ONE at a time,
        # and only for a task text small enough for its 64k window (Paxton, 2026-09-08).
        # 2026-09-10 05:40 LOCAL-ONLY FALLBACK RE-ENABLED. Synthetic's PACK budget is exhausted
        # (429 on every model since 05:29) so every Synthetic trio fails its launch probe and the
        # queue stops dead - 16 tasks and zero runs. The local span is untouched and free:
        # qwen3.8-nothink 1.27s, qwen3.8 1.22s, measured this minute.
        # 2026-09-09 11:12 PT: RAISED 1 -> 2, and the old comment here was WRONG. It claimed
        # "the span is a single-slot backend". Measured on the container itself:
        #     cognizioware-ollama-span  OLLAMA_NUM_PARALLEL=2  OLLAMA_SCHED_SPREAD=1
        # The span serves TWO concurrent requests. Ollama pre-allocates KV cache for NUM_PARALLEL
        # slots at load time, so the second slot is ALREADY reserved whether or not we use it -
        # a second concurrent run costs no additional VRAM and no additional host RAM. That is
        # what makes this safe on ptait01, which has OOM-killed the span before and had only
        # ~10G RAM free when this was measured.
        # Do NOT raise above 2 without re-reading OLLAMA_NUM_PARALLEL: at 3+ the runs contend for
        # two slots and every episode slows toward its 600s timeout, which is how six runs died
        # at 17:30 today (that was a sustained provider 429, but the failure shape is identical).
        # TASK SIZE CAP KEPT at 24000 chars: qwen3.8 runs at 64k context, and a task text larger
        # than that leaves no room for the workspace it has to reason about.
        # 2026-09-09 20:50 PT LOCAL-ONLY FALLBACK DISABLED. MEASURED, not assumed:
        #   task 82 (20260909T231816Z_b8cd0dd8) executor qwen3.8-nothink: TIMEOUT at 1800s on
        #     r1,r3,r5,r6,r7 - five of seven rounds - each returning the same 61-char stub
        #     "(executor agent produced no readable natural-language output)"
        #   task 88 (20260910T031027Z_7734191d) same executor, task text only 5388 chars:
        #     TIMEOUT at 1800186ms on r1, same 61-char stub, then advanced to r2 REGARDLESS.
        # SIX executor episodes, ZERO completions, across two unrelated tasks. Task size was never
        # the discriminator - 88 was small and still failed - so the 24000-char cap below cannot
        # catch this.
        # The MANAGER role works locally: 418s / 15009 chars on task 88 r1. So the span is not
        # broken - it simply cannot finish the heavy role within an episode.
        # A queue that "runs" tasks which all time out is WORSE than an idle queue: it burns 30
        # minutes per round, holds a workspace, blocks every other local-only task behind it, and
        # produces a stub that reads like output. Hold instead, and let recovery restart the queue.
        # RE-ENABLE ONLY with a local floor for manager/auditor and a CLOUD executor.
        if _SYN_DOWN[0]:
            # DEGRADED MODE (Paxton 22:45): do not hold - fall back to whatever still answers.
            # The local span is NEVER an option for the executor: 0 completions in 6 episodes.
            if n>=DEGRADED_CAP:
                print(now,"  HOLD (degraded mode at cap %d - one Ollama key of capacity, not stampeding it)"%DEGRADED_CAP,j["name"],flush=True)
                continue
            trio="degraded"
        # CAP is on TOTAL live runs. Do not count by trio: the trio classifier keys off model
        # names, and the route split renamed every model, so it silently read kimi=0 and let
        # six runs through a cap of four (2026-09-09).
        if n>=CAP: break
        task=_task_text(j)   # 2026-09-10 spec gate: draft spec / missing file -> skip, keep position
        if task is None: continue
        dualfile(f,j,task,trio)   # task 168: mirror this pending entry into the CT110 shadow store
        j["task_source"]="spec" if j.get("spec_file") else "task_file"
        roles=KIMI if trio=="kimi" else QWEN
        _forced_syn=False   # tick #229: set by the two Synthetic fallbacks below
        # 2026-09-22 02:00 PT TICK #1400 (comment-only edit after the 02:01 restart): "default" is what the interactive overseer writes in new
        # entries (190, 217, 218 all carry it) and NOTHING handled it: it fell through to the
        # bare rotation below, skipping the task-196 SYNTHETIC HEALTHY executor divert, so two
        # local-executor runs shared the one-slot span and 217 lost r1+r2 to the 1800s budget.
        # Measured: zero "TRIO SELECT" lines in the log for any "default" launch. Route it with kimi.
        if trio in ("kimi","default"):
            # Synthetic allows ~3 rpm and 2 concurrent PER MODEL on one pack. Two runs that
            # both take glm-5.2 as manager 429 each other into a 300s cooldown (seen 19:13Z
            # 2026-09-08, two runs dead at round 0). Alternate the manager deployment.
            # 2026-09-10 04:05 FIXED: this used _trio(k), where k is the COUNT OF LIVE KIMI RUNS -
            # not a rotation index. Near CAP that count barely moves, so the index sticks and every
            # new launch lands on the SAME trio. Observed: 3 of 4 launches this session went to
            # trio 5, putting THREE concurrent executors on syn:large:vision (measured in-flight:
            # vision 3, nemotron 2, kimi-k3 1 - still under Synthetic's ceiling of 5, but the
            # concentration is the mechanism that would breach it, and the table's spread was
            # computed assuming trios are used evenly). _SEQ is a monotonic launch counter, so
            # consecutive launches genuinely walk the table.
            roles=_trio(_SEQ[0])  # rotate all three roles so concurrent runs never share a backend
            # KEYS SHORT -> all-Synthetic trio (Paxton 2026-09-09). A local-executor trio drawn
            # while the span is contended burns the launch; Synthetic is answering, so use it.
            # 2026-09-11 FIX (Paxton: "i want both GPUs earning their keep ... run slices
            # concurrently"). This used to rotate to all-Synthetic whenever ok<2, UNCONDITIONALLY -
            # including when the drawn trio had a LOCAL executor and the local lane was idle.
            # With keys_ok=0 (all 7 Ollama keys LIMIT) that meant EVERY launch went to Synthetic
            # and both 3090s sat at 0% while loaded. The local lane is free and available: keep a
            # local trio when local capacity allows, and only fall back to Synthetic when the local
            # lane is full or the drawn trio was not local anyway.
            # NOTE: this does not FORCE a local trio, it stops discarding one. Forcing selection is
            # a further change and was deliberately not made here.
            # 2026-09-18 TASK 196 NOTE: that note is now OBSOLETE. Forcing selection IS the change:
            # see the SYNTHETIC HEALTHY divert below, which seats the executor on Synthetic
            # unconditionally while Synthetic answers, regardless of key count or span load.
            # 2026-09-09 17:15 THE LOCAL-EXECUTOR CAP ONLY EXISTED IN FALLBACK MODE.
            # LOCAL_CAP is checked at line ~384, but that check sits INSIDE the
            # `if _SYN_DOWN[0]:` block - so it only ever gated launches while Synthetic was
            # down. In NORMAL mode trios 0 and 1 still seat qwen3.8-nothink as the executor,
            # and nothing capped those. Result observed live: qwen=3 with synthetic=ok, three
            # runs contending for a span that serves about ONE executor.
            # That is what killed task 67 three times (five zero-output rounds, then two more
            # plus two timeouts) and task 83 (3 rounds, nothing produced, executor emitted no
            # readable output). Same role, same backend, repeatedly - a backend fault, not scope.
            # So: if the drawn trio wants the local span and the span is already full, rotate to
            # an all-Synthetic trio rather than launching into starvation.
            # 2026-09-18 TASK 196: the SPAN FULL branch is folded into the SYNTHETIC HEALTHY
            # diversion above - with the executor never seated locally while Synthetic answers,
            # a full span is only one of the reasons that same divert fires. Its log text is
            # preserved in the divert line (span-full suffix) so old log greps keep working.
            if not _SYN_DOWN[0] and _local_exec(roles):
                roles=_trio_syn(_SEQ[0]); _forced_syn=True
                _si=SYN_ONLY[_SEQ[0]%len(SYN_ONLY)] if SYN_ONLY else _SEQ[0]%len(EXEC_POOL)
                print(now,"  TRIO SELECT %d -> %d (SYNTHETIC HEALTHY -> executor diverted to syn trio; span=one-executor resource%s; tick #446 measured 3 of 4 ornith-executor runs losing rounds to the 1800s executor budget)"%(_SEQ[0]%len(EXEC_POOL),_si,"; span also full (qwen=%d>=%d)"%(qw,LOCAL_CAP) if qw>=LOCAL_CAP else ""),j["name"],flush=True)
            elif ok<2 and not _SYN_DOWN[0] and not _local_exec(roles):
                roles=_trio_syn(_SEQ[0]); _forced_syn=True
                _si=SYN_ONLY[_SEQ[0]%len(SYN_ONLY)] if SYN_ONLY else _SEQ[0]%len(EXEC_POOL)
                print(now,"  TRIO SELECT %d -> %d (KEYS SHORT: %d ok, local %d/%d -> all-Synthetic trio)"%(_SEQ[0]%len(EXEC_POOL),_si,ok,qw,LOCAL_CAP),j["name"],flush=True)
            else:
                print(now,"  TRIO SELECT %d (no divert: executor already non-local%s)"%(_SEQ[0]%len(EXEC_POOL),"; keys short (%d ok)"%ok if ok<2 else ""),j["name"],flush=True)
        # TRY EVERY TRIO BEFORE HOLDING. 2026-09-09: task 63 - Paxton's TOP priority - was held
        # because kimi-k2.6:cloud answered in 3.4s against a 3.0s budget, while tasks 52 and 54
        # launched behind it on other backends. Holding the head of the queue over four tenths of
        # a second on ONE backend is an effective reorder of the priority, which is Paxton's to
        # set and not mine. So a failing probe now rotates to the next trio and tries again; the
        # task is only held when EVERY trio is unhealthy, which is a real capacity problem rather
        # than one slow deployment.
        # 2026-09-14 SYN_CAP PRE-PROBE CHECK. The first SYN_CAP patch held AFTER probing, so at the cap every
        # queued task still probed up to five all-Synthetic trios before being held - measured 42 Synthetic
        # requests on the first 3 tasks of one cycle, for zero launches. Every _TRIOS row carries a Synthetic
        # role (both non-Synthetic rows live in DEGRADED_TRIOS), so a kimi task at the cap cannot launch: hold it
        # BEFORE probing. "degraded" and "qwen" are excluded - they can bind all-local roles, and the post-probe
        # check below still decides for them from the roles actually bound.
        if trio not in ("qwen","degraded") and _SYN_LIVE[0]>=SYN_CAP:
            print(now,"  HOLD",j["name"],"- SYN_CAP (pre-probe): %d live Synthetic runs >= cap %d; no probe sent"%(_SYN_LIVE[0],SYN_CAP),flush=True)
            continue
        rdy=False; rep=""
        if trio=="degraded":
            # ROTATE. The first version probed ONE trio and held on a single 429, which is the
            # same single-sample error the main path was already fixed for: one backend refusing
            # is not proof the degraded pool is dead. Walk the table, launch on the first trio
            # whose three backends all emit, and hold only when every one of them fails.
            for _d in range(len(DEGRADED_TRIOS)):
                roles=_degraded_trio(_SEQ[0]+_d)
                rdy,rep=roles_ready(roles)
                print(now,"  emit-probe",j["name"],"[DEGRADED %d/%d]"%(_d+1,len(DEGRADED_TRIOS)),rep,flush=True)
                if rdy: break
        elif trio=="qwen":
            # LOCAL-ONLY MODE. There is exactly one trio here - the span - so there is nothing to
            # rotate to. Probing the Synthetic table would only burn requests against a provider
            # that is already refusing them. 2026-09-10 05:37: the first version of this fallback
            # selected QWEN and then the rotation loop overwrote `roles` with _trio(...) anyway,
            # so it probed Synthetic, was blocked, and launched nothing. Fixed by not entering the
            # rotation at all when the trio is local.
            # 2026-09-09 20:21 PT PER-CYCLE CIRCUIT BREAKER. MEASURED: with one local run holding
            # the span, EVERY subsequent probe timed out at 12.0s against a 3s budget - roughly 20
            # queued tasks x 3 models, so ONE cycle spent ~10 MINUTES hammering a backend that was
            # already saturated. Worse, that probe traffic contends with the executor of the run it
            # had just launched (OLLAMA_NUM_PARALLEL=2: one slot for the run, one for the probes),
            # so the launcher was starving its own work. The span cannot change state within one
            # pass, so a single failure is enough to know for the rest of the cycle.
            if _LOCAL_SPAN_BUSY[0]:
                print(now,"  SKIP (local span already failed a probe this cycle)",j["name"],flush=True)
                continue
            roles=QWEN
            rdy,rep=roles_ready(roles)
            print(now,"  emit-probe",j["name"],"[LOCAL-ONLY]",rep,flush=True)
            if not rdy: _LOCAL_SPAN_BUSY[0]=True
        else:
            for attempt in range(len(EXEC_POOL)):
                roles=(_trio_syn if _forced_syn else _trio)(_SEQ[0]+attempt)   # tick #229
                rdy,rep=roles_ready(roles)
                print(now,"  emit-probe",j["name"],"[trio %d]"%((_SEQ[0]+attempt)%len(EXEC_POOL)),rep,flush=True)
                if rdy: break
        if not rdy:
            print(now,"  HOLD",j["name"],"- no trio had all three backends emit within budget (%.0fs hosted / %.0fs local)"%(EMIT_BUDGET,EMIT_BUDGET*6),flush=True)
            continue
        if any(":synthetic-anthropic" in str(c.get("model","")) for c in roles.values()) and _SYN_LIVE[0]>=SYN_CAP:
            print(now,"  HOLD",j["name"],"- SYN_CAP: %d live Synthetic runs >= cap %d (Paxton 2026-09-14)"%(_SYN_LIVE[0],SYN_CAP),flush=True)
            continue
        body={"task":task,"workspace":j["workspace"],"max_rounds":j.get("max_rounds",8),"roles":roles}
        try:
            r=json.load(urllib.request.urlopen(urllib.request.Request(BASE+"/api/runs",data=json.dumps(body).encode(),headers=H,method="POST"),timeout=180))
            run=r.get("run",r); rid=run.get("id") or run.get("run_id")
            print(now,"LAUNCHED",j["name"],rid,flush=True)
            j["run_id"]=rid
            # Record WHICH backends this run got. Without this a watchdog cannot tell whether a
            # long-running round is a starved backend or a model genuinely chewing on hard work -
            # it would have to guess, and guessing is how a working run gets killed.
            j["roles_bound"]={r:c["model"] for r,c in roles.items()}
            _SEQ[0]+=1; _seq_save(_SEQ[0])   # advance the rotation ONLY on a real launch, so held tasks do not skip trios
            j["launched_at"]=datetime.datetime.now(datetime.timezone.utc).isoformat()
            json.dump(j,open(f,"w",encoding="utf-8"),indent=1); shutil.move(f,"C:/tmp/queue/done/"+os.path.basename(f))
            _RETIRE_AFTER.append(f)   # task 168 blocker (d): retire AFTER a poll, not now -
            # see _RETIRE_AFTER. The loop-head sweep is still the safety net if we never get there.
            n+=1   # count against CAP within this cycle, not just across cycles
            if any(":synthetic-anthropic" in str(c.get("model","")) for c in roles.values()): _SYN_LIVE[0]+=1   # SYN_CAP, same cycle
            k+=1   # rotation index, so the next launch this cycle takes a different backend
            if _RAMP: print(now,"  RAMP: one run launched; holding the rest of this cycle",flush=True); break
            if trio=="qwen": qw+=1   # 2026-09-10 05:41: WITHOUT THIS, local-only mode probes the span
            # for every remaining queued task in the same cycle - three 12s timeouts each - because qw
            # is read once at cycle start and the `qw>=1` guard therefore never trips. Those probes are
            # not merely wasted: they QUEUE ON THE SINGLE-SLOT SPAN that the run just launched is using,
            # so the launcher was actively slowing the one task it had managed to start.
            ws.append(j["workspace"])  # same-cycle guard
        except urllib.error.HTTPError as e:
            print(now,"launch failed",j["name"],e.code,e.read()[:100],flush=True)
    if _RETIRE_AFTER:
        # The launcher is about to idle for 900 s anyway, so this costs nothing but bounds the
        # window in which a launched entry is still pending in the CT110 shadow queue.
        time.sleep(_RETIRE_DELAY)
        for _f in _RETIRE_AFTER: dualfile_retire(_f)
        _RETIRE_AFTER.clear()
        time.sleep(900-_RETIRE_DELAY)
    else:
        time.sleep(900)
