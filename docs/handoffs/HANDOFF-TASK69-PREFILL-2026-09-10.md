# Handoff — why LH-harness runs on `ornith-1.5:pool` burn their whole budget

**For the expert taking this on. Measured by the overseer session 2026-09-10 23:45 – 2026-09-11 00:01 UTC
(16:45–17:01 PT). Every number below was read off the live system, not taken from notes.**

---

## The problem in one paragraph

Harness task 69 has been attempted three times and has produced **zero commits** every time. It does not
crash and it is not deadlocked. It runs, does real work slowly, and hits its 3600-second executor budget
before finishing. The two earlier attempts died at *exactly* `cli_executor = 3600`. The third attempt is
running right now and is on course to do the same.

**The question is not "why does it hang" — it does not hang.** The question is why a single executor
episode cannot finish inside an hour on this model, and what to change: capacity, scheduling, budget, or
task size.

---

## ⏱ There is a live reproduction, and it expires shortly

```
run        20260910T230152Z_70f383f9     (task 69, workspace cognizioware-qa)
process    pid 3038350  claude --print ... --model ornith-1.5:pool
           LH_HARNESS_CLAUDE_ROLE=cli_executor
elapsed    2514 s of the 3600 s budget as of 00:01:35 UTC   →  ~18 minutes left
ceiling    ~00:19–00:20 UTC
```

After that the process is gone. Log files survive; live state (sockets, in-flight requests, CPU
behaviour) does not. `controls` reports `can_abort: true`; `/api/runs/{id}/abort`, `/stop`,
`/instructions`, `/resume` all exist. **It was deliberately left running as your reproduction.**

---

## Two competing explanations. I could not separate them. This is the real handoff.

### Hypothesis A — context size drives prefill cost

```
executor_raw_trajectory.jsonl   13,443,528 bytes and growing
this task's PREVIOUS death also reached ~13.7 MB
```

Every turn re-sends a colossal prompt; prefill time grows with it until an episode cannot fit in an hour.

### Hypothesis B — two runs contend for one model slot

`ornith-1.5:9b` is a hybrid SSM (`arch qwen35`). **Ollama refuses parallel requests for it**
(`n_seq_max = 1`), so concurrency comes only from more instances. Two runs were sharing the pool
throughout:

```
pid 3038350   task 69   cli_executor   41:54 elapsed
pid 3039795   task 136  cli_auditor    15:12 elapsed
```

### Why I could not call it — and this is the part worth your attention

Latency on `:11441` collapsed from minutes to seconds **while both runs remained active**:

```
23:43:41   3m44s        23:59:46    9.79s
23:44:19   3m30s        00:00:12   26.06s
23:44:22   3m9s         00:00:15    2.98s
23:44:22  40.16s        00:00:18    2.84s
                        00:00:35   16.31s
                        00:00:40    4.99s
```

If contention alone explained it, the recovery should have coincided with one run stopping. It did not.
So **B does not fully explain the data either**, and I am not handing you a confident answer I cannot
support. The GIN log records only the completion and the duration — it does not say *which run* each
request belonged to, so these two hypotheses are not separable from it.

### The experiment that does separate them

Every episode is already tagged on the LLM path:

```
x-litellm-tags: lh-run/20260910T230152Z_70f383f9,round_002,cli_executor
                lh-session/20260910T230152Z_70f383f9.round_002.cli_executor
```

So per-request latency **can** be attributed to a specific run and role via LiteLLM / Langfuse. Plot
latency per request for `cli_executor` of run `70f383f9` against prompt size and against whether another
run's request overlapped it. That answers A vs B directly. I did not do this — it is yours, and I stopped
rather than duplicate your work.

---

## Already established — please don't re-derive these

**The backend is healthy. This is not an outage.**

```
:11441  cognizioware-ollama-gpu1    38 /api/chat arrivals, all from 192.168.21.161
:11438  cognizioware-ollama-cloud   43 /api/chat arrivals, all from 192.168.21.161
        ornith-1.5:9b-256k  11,323,446,394 bytes  context_length 262144  resident in VRAM on BOTH
router  /health/liveliness = 200
```

**It is blocked on I/O, not spinning and not stuck on a local tool.**

```
state S   wchan do_epoll_wait   child processes NONE
utime accumulating: 2290 → 2317 → 2598
ESTABLISHED socket to the router throughout (socket identity changes as requests complete)
```

**Long requests do return.** One ran ~14 minutes and completed; the trajectory then advanced
(414,615 → 422,565 → 424,714 bytes). Slow, not stuck.

**Round 1 produced no work product at all.**

```
reflog  fix/hydra-e2e-repo-dir@{0}: branch: Created from origin/master
HEAD    e92b19b   = the branch point, ZERO commits ahead
git status --porcelain   EMPTY
files modified in the workspace in the last hour   NONE
```

Round 1 ran its executor (9.5 min) **and** its auditor (4.2 min) and recorded the round — leaving nothing
on disk.

**Prior history of this same task**

```
attempt A   r1 executor  60.0 min → agent.runtime.failed   (exactly cli_executor = 3600)
attempt B   r3 executor  60.0 min → agent.runtime.failed   (exactly cli_executor = 3600)
attempt C   r1 executor completed in 9.5 min; r2 executor now 42+ min and counting
```

---

## ⚠ Two reader traps I fell into. Please don't repeat them.

1. **The GIN access log writes only on COMPLETION.** A request still prefilling is invisible in it. I
   concluded "no GPU is working for this run" from the absence of a completion line. That was wrong. The
   socket table and the trajectory mtime are the reliable live signals.
2. **A snapshot is not a steady state.** I described the executor as "blocked on one giant in-flight
   request" — true at 23:45–23:56, false as a general description, because that request later returned.

Also already in memory: the GIN line is `POST     "/api/chat"` — quoted, multi-space, so a naive grep
matches nothing; and `docker logs --since` parses a bare timestamp as **host local** time, not UTC.

---

## Open questions for you

1. How much of a slow request is **queueing behind another run** versus **prefill of a 13 MB context**?
   (The tag-attribution experiment above answers this.)
2. Does the `cli_executor = 3600 s` budget count time spent **queued** as well as computing? A run can
   burn its entire budget waiting for a slot it never gets — if so, the budget is measuring the wrong
   thing.
3. Is there a context size past which prefill on this model is simply not worth paying?
4. Should two runs ever share a single-slot model, or should the launcher treat `ornith-1.5:pool` as
   capacity 1 rather than 2?

---

## What the overseer has already done about it (so you don't duplicate)

- **Task 69 has been cut into three smaller slices** (`711-69a` read-only diagnosis, `711-69b` the
  HYDRA_REPO_DIR fix, `711-69c` the FACTORY_REPO_DIR fix), each instructed to keep its episode small.
  Smaller episodes reduce exposure to both hypotheses.
- **`711-69c` is held for a human expert** at Paxton's direction and is out of the launcher's reach.
- Capacity is currently degraded to 2 concurrent runs because Synthetic is at **0 tokens for the week**
  and **0 of 7** Ollama Cloud keys are healthy — so the local pool is carrying everything.

---

## Constraints that still apply

- **Never the bare gateway `/health`** — it fans a completion out to every configured model, including
  paid and GPU ones. Use `/health/liveliness`.
- **Any edit to `infrastructure/litellm-config.yaml` force-recreates the CT202 prod router** and kills
  every in-flight run — including the reproduction above.
- **`router_settings` and four other sections are DB-shadowed**; the DB row wins over the YAML, so a file
  edit alone may change nothing. Use `POST /config/update`, then read the row back and diff it.
- Unrelated noise seen in passing, **not** this run's cause: `cognizioware-mcp-tools-litellm` logs
  repeated `_fetch_tools_with_timeout` recursion errors in `mcp_server_manager.py` (lines 4655/4669).
  Pre-existing; belongs to task 122.
