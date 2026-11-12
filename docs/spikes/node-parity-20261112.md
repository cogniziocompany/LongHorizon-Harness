# Node parity spike: can the CT110 deploy be reused for CT111, and are the
# registered lh-harness instances on the same branch?

Task: easybutt0n-131 (dedup tag `yt:easybutt0n-131:node-parity-spike`).
Docs only: this file is the whole change. No deploy was run, no workflow was
dispatched, no secret value was read, and no host was contacted from this
work.

## Answers first

**Paxton Q1 - "should the same UAT of ct110 code be reused in ct111 minus the
name change?"**

Yes for the code, no for the configuration, and the pipeline already says so.
The deploy workflow is one pipeline with a `target` input
(`.github/workflows/deploy-ct110.yml:73-87`), and the built artifact is the
same for both nodes: same commit resolved from `target_ref`, same build steps,
same wheel, same bundled Web UI. What differs is exactly one GitHub
environment, its two secrets and its web URL, plus per-node config that never
travels through the workflow at all (node `config.toml`, the settings DB seed,
the secrets env file). The "name change" is therefore smaller than it looks
inside the job: the job-level variables are still called `CT110_*` for both
targets (`deploy-ct110.yml:240-251`, explained at `:69-71`); only the
environment name and the GitHub secret/variable NAMES change. The pipeline is
nonetheless not usable on CT111 today, because the CT111 environment has never
been populated: both CT111 dispatches that ever ran failed at "Require target
configuration" (section 1.2 and section 6).

**Paxton Q2 - "The registered instances of lh-harness need to be on same
branch! Please confirm"**

Not confirmed, and the phrasing hides the real requirement. At the time of
this spike they are on the same *repository* and the same *branch name*
(`main`), but they are not on the same *commit*: CT110 was deployed by this
pipeline to `3f858204083543cd9e5721c56320feb6b4fe71ba` at 2026-11-12T19:50Z
(run 43294233236, success), while CT111 still runs
`b4e86bf9bec4930207efe5730107e116c5dceb81` - two main commits behind, because
nothing has ever installed anything on CT111 except a hand-run bootstrap
(section 2). "Same branch" cannot be verified from a branch name: `main` is a
moving ref and both nodes were "on main" while running different code. The
checkable property is **same full commit plus same artifact hash** (section 3),
and right now that property is false on the CT111 side.

---

## 1. Is the CT111 deploy byte-identical to CT110's except node identity?

Identical pipeline, identical build, identical artifact; different
configuration, and *no* hash comparison between nodes. Details below.

### 1.1 What is identical

| Step | Where | Identical for both targets? |
| --- | --- | --- |
| Trigger and inputs | `deploy-ct110.yml:73-105` | yes; `target` selects the node, `target_ref` selects the commit |
| Ref resolution and main-green gate | `deploy-ct110.yml:130-136`, `scripts/deploy/ct110/preflight.py:74-102` (ref to full sha), `:104-139` (default branch green) | yes |
| Web bundle build | `deploy-ct110.yml:159-166` (`npm ci`, `npm run build`), `:168-169` (bundle must exist) | yes |
| Commit stamp into the package | `deploy-ct110.yml:175-186` writes `src/lh_harness/_build_info.json` | yes |
| Wheel build | `deploy-ct110.yml:188-190` (`uv build --wheel`) | yes |
| Wheel self-check | `deploy-ct110.yml:192-214` (version, Web bundle present, build commit) | yes |
| Transfer + install + verify + rollback scripts | `scripts/deploy/ct110/run_on_ct110.sh`, `scripts/deploy/ct110/ct110_deploy.sh` | yes, the same files; the scripts are parameterised by `CT_ID`, `PVE_HOST`, `PVE_USER`, `--url` (`scripts/deploy/ct110/README.md:9-14`) |

The build is per dispatch, not per node: each run builds its own wheel and
uploads it under a target-named artifact (`deploy-ct110.yml:216-221`,
`:271-275`). Two independent builds of the same commit are observed to be
byte-identical - see 1.3 - but nothing in the workflow enforces that.

### 1.2 Every per-node difference

| Item | CT110 | CT111 | Citation |
| --- | --- | --- | --- |
| `target` input value | `ct110` (default) | `ct111` | `deploy-ct110.yml:76-83` |
| GitHub environment | `ct110-prod` | `ct111-prod` | `deploy-ct110.yml:227` |
| API token secret NAME | `CT110_API_TOKEN` | `CT111_API_TOKEN` | `deploy-ct110.yml:240`; `scripts/deploy/nodes.json:22-25,48-51` |
| SSH key secret NAME | `CT110_PVE_SSH_KEY` | `CT111_PVE_SSH_KEY` | `deploy-ct110.yml:241`; `nodes.json:22-25,48-51` |
| Web URL variable | `CT110_WEB_URL`, default `http://192.168.21.168:8799` | `CT111_WEB_URL`, **no default** (empty fails the run) | `deploy-ct110.yml:248`; `nodes.json:27,33,53,59` |
| PVE host variable | `CT110_PVE_HOST`, default `corsairai300` | `CT111_PVE_HOST`, default `ptait07` | `deploy-ct110.yml:249`; `nodes.json:17,43,58-61` |
| PVE ssh user | `CT110_PVE_SSH_USER`, default `root` | `CT111_PVE_SSH_USER`, default `root` | `deploy-ct110.yml:250`; `nodes.json:29,55` |
| Container id | `CT110_CT_ID`, default `110` | `CT111_CT_ID`, default `111` | `deploy-ct110.yml:251`; `nodes.json:15,41` |
| Concurrency group | `deploy-ct110` | `deploy-ct111` (nodes deploy in parallel, never queue behind each other) | `deploy-ct110.yml:116-118`; `nodes.json:19,45` |
| Wheel artifact name | `ct110-wheel` | `ct111-wheel` | `deploy-ct110.yml:219,274`; `nodes.json:20,46` |
| Overseer-sweep units job | runs | skipped (`overseerUnits: false`) | `deploy-ct110.yml:564-569`; `nodes.json:21,47` |
| Configuration gate | none | "Require target configuration" fails before anything is downloaded, drained or pushed | `deploy-ct110.yml:260-269` |

Per-node settings that do **not** pass through the workflow:

| Item | CT110 | CT111 | Citation |
| --- | --- | --- | --- |
| Node config file | live `config.toml` on the node | `templates/config.ct111.toml` (finance profile, auditor binding, trios) | `scripts/deploy/node/templates/config.ct111.toml:9-48`; `scripts/deploy/node/bootstrap-node.sh:67,93` |
| Capacity: per-trio caps | harness defaults `kimi_max 3`, `qwen_max 1` | `kimi_max 2`, `qwen_max 1` | `scripts/deploy/node/templates/config.ct111.toml:51-53`; defaults at `src/lh_harness/queue.py:1211-1213`; asserted by `tests/test_deploy_node_targets.py:311` |
| Capacity: node-wide ceiling | default `[queue] node_max_runs` = 5 | **not set** in the template, so also 5, although the fleet registry advertises 2 | `src/lh_harness/config.py:266`; `CHANGELOG.md:173-189`; `docs/nodes/ct111-cfo1.md:51-54` |
| Settings DB seed | operator-run import | `templates/settings.ct111.txt`, imported once | `bootstrap-node.sh:73,283`; `templates/settings.ct111.txt:1-29` |
| Settings DB identity | its own DB and key | its own DB and key (a fresh `LH_HARNESS_SETTINGS_KEY` is generated per node) | `bootstrap-node.sh:236-250`; `docs/settings-store.md:23-32` |
| Secrets env file | `/home/harness/.lh-harness-secrets.env` | same path, different node, different values | `packaging/lh-harness.service:59`; `bootstrap-node.sh:62,210-251` |
| Env-file credential NAMES | own gateway key, own `GH_TOKEN`, own fleet key | same NAMES, node-scoped values | `scripts/deploy/node/README.md:23-34,111-123` |
| Workspace root | `/home/harness/work` | `/home/harness/work-cfo` | `scripts/deploy/node/README.md:26` |
| Fleet identity | `LH_HARNESS_FLEET_NODE` | `LH_HARNESS_FLEET_NODE=ct111`, labels `kind=ct111,host=<ct111 hostname>` | `bootstrap-node.sh:227-234` |
| Rollback hold file | repo checkout path then `/home/harness/DEPLOY-HOLD` | `/home/harness/DEPLOY-HOLD` (no repo checkout on CT111) | `scripts/deploy/ct110/ct110_deploy.sh:65-69`; `scripts/deploy/node/README.md:202-203` |

Two capacity facts deserve to be called out because they are the only
"capacity settings" in scope and neither is a workflow input:

1. CT111's template sets the per-trio caps to 2/1 but never sets
   `node_max_runs`, while the Hydra registry entry advertises
   `maxConcurrentRuns: 2` (`docs/nodes/ct111-cfo1.md:51-54`). The node-level
   ceiling key exists precisely so a node can state the number it advertises
   (`src/lh_harness/config.py:260-266`, `CHANGELOG.md:173-189`). CT111
   currently relies on per-trio arithmetic, not on the node ceiling.
2. `templates/settings.ct111.txt` seeds `LH_HARNESS_RETENTION_*` NAMES
   (`templates/settings.ct111.txt:12-29`) but the disk-retention loader reads
   `LH_HARNESS_DISK_*` NAMES (`src/lh_harness/disk_retention/config.py:37-55`).
   The import still succeeds - unknown NAMES are accepted as custom settings
   (`src/lh_harness/settings_store.py:243-247`) - but the seeded rows are not
   the names the sweep consults. This is a template-level difference between
   the two nodes' seeding, not a workflow difference; it is recorded here
   because the spike asked for the settings store, and it is flagged in the
   repo already at `scripts/deploy/node/VERIFICATION-ct111.md:33`.

### 1.3 How the artifact and its hash are verified (and where they are not)

What the pipeline proves, in order:

1. **At build time** the wheel's own contents are asserted: file name carries
   the expected version, the Web bundle `_frontend/web/dist/index.html` is in
   the zip, `lh_harness/_build_info.json` is in the zip, and its `commit`
   equals the preflight sha (`deploy-ct110.yml:192-214`). The bundle is packed
   by hatchling from the build tree (`pyproject.toml:44-48,60-62`) and its
   presence was already required before packaging
   (`deploy-ct110.yml:168-169`).
2. **At each transfer hop** the wheel and the inner script are sha256-compared
   against the local file - runner to PVE host, then PVE host to container -
   and execution is refused on mismatch (`scripts/deploy/ct110/run_on_ct110.sh:120-147`).
   The run log of 43294233236 shows both hops passing on
   `lh_harness-0.1.7-py3-none-any.whl` with
   `sha256=3ba4637b3c14b82dd65c91bff522000fd70caa70dcae3d51fc63756571e2b850`.
3. **After install** the wheel pip recorded is compared to the staged wheel by
   sha256 (`scripts/deploy/ct110/ct110_deploy.sh:230-243`), the installed build
   commit is compared to the expected commit (`:208-216`), the same commit is
   re-read from the installed package (`deploy-ct110.yml:348-363`), and
   `/api/meta`'s `build.commit` is polled until it equals the target
   (`deploy-ct110.yml:370-378`, `scripts/deploy/ct110/verify_meta.py:97-113`).
4. **Rollback** reinstalls the pre-deploy wheel located BY SHA256, never by
   version (`ct110_deploy.sh:284-309`), which is required because every build
   carries the same `0.1.7` version.

What the pipeline does **not** do: it never compares its artifact against an
expected hash, and it never compares two nodes' artifacts. The wheel artifact
is downloaded by name with no digest pin
(`deploy-ct110.yml:271-275`). The two artifacts produced today demonstrate the
gap: run 43294233236's `ct110-wheel` and run 43294789110's `ct111-wheel` have
different artifact-level digests (`sha256:ea9242b7...8583` and
`sha256:334432a0...52c87`) while the wheel bytes inside them are identical
(`sha256:3ba4637b...b850` both, verified by downloading both artifacts in this
spike). The difference is the zip entry timestamp (19:51:20Z vs 19:55:44Z) and
nothing else, so the build is reproducible at wheel-byte level for one commit
- but a parity rule cannot rest on that observation unless the pipeline
asserts it (section 3).

---

## 2. How did CT111 get `b4e86bf9`, and what does that mean for "same branch"?

### 2.1 The pipeline never deployed CT111

Every dispatch of `deploy-ct110.yml` was enumerated (52 runs, all
`workflow_dispatch`) and each run's job list was read. Exactly two runs ever
contained the job "Deploy to CT111", and both failed at its first step:

| Run | Created | `target_ref` | Outcome |
| --- | --- | --- | --- |
| 43294789110 | 2026-11-12T19:53:38Z | `3f858204083543cd9e5721c56320feb6b4fe71ba` | failed at "Require target configuration (ct111 has no defaults)" |
| 42874760937 | 2026-11-10T18:23:49Z | `b0942d1984f67f87384de5b7de7e3878507c2b38` | failed at the same step |

Both runs' logs print the three missing NAMES and exit 1; 43294789110 is
quoted in section 6. Because the gate is the first step of the deploy job and
every later step in that job is recorded as skipped, neither run downloaded a
wheel, set a drain, suspended a run, or pushed anything to CT111. The run-level
job list also shows the `overseer-units` job skipped, as designed
(`deploy-ct110.yml:564-569`).

### 2.2 Where `b4e86bf9` came from

CT111 was installed by hand, inside the container, outside this pipeline. The
install receipts live in the `cogniziocompany/claude-code-teams` repo (the
overseer apparatus repo, not this one):

- `agents/infraops1-op/docs/ovf-lhh198-ct110-install-20261112.md:15` - the
  CT110 install at 02:38Z was "exact CT111 install198.sh, unmodified", i.e. the
  same script CT111 had already been installed with at 02:16Z.
- Same file `:6` - a clean GitHub clone at the FULL
  `b4e86bf9bec4930207efe5730107e116c5dceb81`, npm Web bundle built,
  `_build_info.json` stamped, `python -m build wheel`; "same recipe as CT111
  02:16Z".
- Same file `:11` - the resulting wheel sha256
  `5c7f76069fefaa054c6b119234401453df7779c82d268aa7f09400a39d061b51` was
  "BYTE-IDENTICAL to the CT111 wheel that canaried GREEN (same deterministic
  build)".
- `agents/infraops1-op/docs/ovf-lhh196-install-20261111.md:11-13,18` - the
  same manual pattern one day earlier (CT111 force-reinstall at 08:29:15Z for
  `9a01b5e4`, then CT110 at 08:50:49Z), so manual in-container installs are
  the established practice, not a one-off.
- `agents/infraops1-op/docs/ovf-harness-node-parity-guard-20261112.md:50-56`
  (merged as cct#246, 2026-11-12T07:41:57Z) - the read-only baseline: CT110
  active since 02:38:32Z and CT111 since 02:16:36Z, both wheel 0.1.7,
  `_build_info` and `/api/meta` both reporting
  `b4e86bf9bec4930207efe5730107e116c5dceb81`.
- The LHH-side corroboration for that build being *main*: `b4e86bf9` is the
  merge of PR #198 into `main` (2026-11-11T17:54:14-08:00 = 2026-11-12T01:54Z,
  `git log -1 b4e86bf9`), and the CT110 run of 19:50Z records it as CT110's
  previous build (`[ct110-deploy] previous build recorded: ... commit=b4e86bf9...`),
  consistent with the receipts.

So: CT111's current code was built from a clone, inside the container, by an
operator-driven script, at 02:16Z on 2026-11-12. It is genuinely `main` code;
it simply was not produced or verified by the deploy pipeline, and it predates
`3f85820` by two commits (`821740a`, `83352d1`, plus the merge `3f85820`).

Limitation, stated plainly: this spike cannot read either node. The read-only
boundary in this task forbids host access, so CT111's *current* running commit
is taken from the 07:25Z read-only baseline above plus the R8 dual-node report
(`claude-code-teams` PR #241, `docs/v15/r8-131-dual-node-qa-20261112.md:15-27`:
wheel sha `5c7f7606...`, `_build_info` = `b4e86bf9` on both nodes). Nothing in
this spike re-read CT111 after that.

### 2.3 Branch vs exact commit vs artifact identity

| Level | What it means | What it costs | How to check it |
| --- | --- | --- | --- |
| Branch | `main` is the same ref name on both nodes | nothing - it is a moving pointer and says nothing about installed code | impossible to check from a node; a node has no branch |
| Exact commit | both nodes run code built from the same full sha | one deploy per node per commit | `meta.build.commit` (`src/lh_harness/build_info.py:1-11`), `deploy-ct110.yml:370-378` |
| Artifact identity | both nodes installed the same wheel bytes | one build per commit, distributed to every node | wheel sha256 at install (`ct110_deploy.sh:230-243`) and per-hop (`run_on_ct110.sh:126-147`); recorded per node at `.../deploy/state/deployed_commit` (`ct110_deploy.sh:280`) |

Paxton's "same branch" is only answerable at the second and third levels. At
the commit level the answer today is *no* (CT110 `3f85820`, CT111 `b4e86bf9`).
At the artifact level the two nodes were at hash `5c7f7606...` for
`b4e86bf9` (receipts above) and CT110 has since moved to `3ba4637b...`; a
matching hash on both nodes has not existed since the 19:50Z CT110 deploy.

---

## 3. The parity rule to adopt

**Rule.** Every registered harness instance runs branch `main`, at the SAME
full commit, with the SAME artifact hash. Deploys roll node by node inside a
bounded window. Drift is detected continuously and reported by name. A lagging
node gets an automatic follow-up deploy from the same pipeline dispatch that
deployed its peer, not a hand install.

Concretely:

1. **One commit per wave.** A wave is a single dispatch with one `target_ref`
   that resolves to one full sha (`preflight.py:74-85`). Every node in the wave
   receives that sha. The input is documented as "Tag (vX.Y.Z) or full commit
   sha" (`deploy-ct110.yml:84-87`); the guard that makes this safe is not the
   input label but the resolution: the resolved sha is what gets checked out
   (`deploy-ct110.yml:143-146`), stamped (`:175-186`) and verified after
   restart (`:370-378`), so a mutable ref name cannot silently stand in for a
   commit. Dispatching the same `target_ref` for two nodes at different times
   is the failure mode this forbids; a *branch* name as `target_ref` would
   reintroduce it, because `preflight.py` resolves any ref GitHub recognizes.
2. **One build per wave, one hash per wave.** Build once, then require the
   installed wheel sha256 on each node to equal the built wheel's sha256. The
   pipeline already has all the primitives: per-hop sha256
   (`run_on_ct110.sh:126-147`), installed-vs-staged sha256
   (`ct110_deploy.sh:230-243`), and a per-build archive keyed by commit
   (`ct110_deploy.sh:201-206`). What is missing is the wave-level comparison:
   today each dispatch rebuilds, and the two artifacts of 2026-11-12 differed
   at artifact-zip level (section 1.3). The rule should therefore compare
   *wheel* hashes, not artifact-zip digests.
3. **Rolling, bounded window.** Nodes deploy one at a time, CT110 first and
   CT111 only on CT110 success, with a stop-the-train rule and a rollback to
   the previous build on failure - the sequence already written for this exact
   pair in `claude-code-teams`
   `agents/orchestrator/plans/lhh199-node-deploy-d1-retest-2026-11-12.md:6-11`
   ("Per node, one at a time", "Order: CT110 first. Only on CT110 success:
   CT111", "Failure: stop the train; roll that node back"). The window is
   bounded by the deploy job's own `timeout-minutes: 150`
   (`deploy-ct110.yml:228-230`) plus the concurrency group per node
   (`deploy-ct110.yml:116-118`), and each node's rollback is automatic and
   verified (`deploy-ct110.yml:416-476`).
4. **Drift detection = the cct#246 parity guard, as built.** The guard is a
   read-only 3-way comparison, once per Infraops1-op 15-minute tick: CT110
   `build.commit`, CT111 `build.commit`, and `git ls-remote` of LHH `main`
   (`agents/infraops1-op/docs/ovf-harness-node-parity-guard-20261112.md:9-21`).
   How it reports, exactly:
   - PARITY: prints one `PARITY ...` line, writes nothing. Silence is healthy
     (`lhh-parity-check.sh:38-42`).
   - New drift: exit 4, `DRIFT-NEW`, ledger line suppressed - one tick of
     grace for a rolling deploy in flight (`lhh-parity-check.sh:44-49`;
     spec `:26-28`).
   - Persisted drift: exit 3, prints a ready-made ledger row
     `parity-check: DRIFT ct110=<sha8> ct111=<sha8> main=<sha8> since=<tick>`,
     plus one Fleet Chat escalation line for Overseer1
     (`lhh-parity-check.sh:50-53`; spec `:29-34`).
   - Probe failure (node down, `meta` 401 twice, `ls-remote` fail): same
     two-tick rule, reported as `PROBE-FAIL <node> <reason>` (spec `:35-38`).
   - Matching is prefix-tolerant at the shortest common length, so an 8-char
     `/api/meta` value legitimately equals a 40-char `ls-remote` head
     (`lhh-parity-check.sh:25-31`).
   The guard is deliberately read-only and does not install anything - installs
   stay with Overseer1-sequenced leases (spec `:40-43`).
5. **Automatic follow-up deploy for a lagging node.** Today the guard's exit 3
   ends at a ledger row and a chat line; the deploy itself is a manual
   `workflow_dispatch` (`deploy-ct110.yml:73-87`, `scripts/deploy/ct110/README.md:196-205`).
   The rule closes that gap: when the guard reports persisted drift where one
   node equals `main` and the other lags, the follow-up is a dispatch of the
   same workflow with `target=<lagging node>` and
   `target_ref=<the leading node's full commit>`, which is exactly the input
   shape the workflow already takes. Two constraints keep it safe: the
   dispatch must reuse the *leading node's* commit (so the artifact hash can be
   compared, section 3.2) and it must go through the existing gates
   (preflight, main-green, configuration gate, drain/suspend, verify,
   auto-rollback) rather than bypass them - which is why the trigger belongs in
   the guard's escalation path, not inside the deploy workflow.
6. **What must never be the parity signal.** Service health, version string,
   or "both say `main`". Every build carries version `0.1.7`
   (`pyproject.toml:7`, `src/lh_harness/build_info.py:1-8`), which is why the
   pipeline stopped trusting it (`deploy-ct110.yml:171-174`).

---

## 4. A data-driven node registry for the deploy workflow

### 4.1 What exists

`scripts/deploy/nodes.json` is an inventory with one entry per node
(`nodes.json:12-64`): `ctId`, `hostname`, `pveHost`, `githubEnvironment`,
`concurrencyGroup`, `wheelArtifact`, `overseerUnits`, the secret NAMES
(`apiToken`, `pveSshKey`), the variable NAMES (`webUrl`, `pveHost`,
`pveSshUser`, `ctId`) and `workflowDefaults`. It is deliberately **not** read
at run time - the workflow's per-target selection is static expressions, so a
bad edit there cannot change a production deploy - and a test keeps the two in
agreement (`nodes.json:2-9`; `tests/test_deploy_node_targets.py:16,146-150`).

The cost of that choice is visible: adding a node means three edits (a
`nodes.json` entry, the `target` choice list at `deploy-ct110.yml:80-83`, and
the selection expressions at `:232-251`), and the per-target expression
`inputs.target == 'ct111' && ... || ...` is repeated twelve times across the
workflow (grep count over `.github/workflows/deploy-ct110.yml`). That is the thing to retire.

### 4.2 Design

One entry per node, NAMES only, and the workflow reads it:

```json
{
  "nodes": [
    {
      "id": "ct110",
      "ct_id": 110,
      "pve_host": "corsairai300",
      "url": "http://192.168.21.168:8799",
      "environment": "ct110-prod",
      "secrets": { "apiToken": "CT110_API_TOKEN", "pveSshKey": "CT110_PVE_SSH_KEY" },
      "vars": { "webUrl": "CT110_WEB_URL", "pveHost": "CT110_PVE_HOST",
                "pveSshUser": "CT110_PVE_SSH_USER", "ctId": "CT110_CT_ID" },
      "capacity": { "node_max_runs": 5 }
    },
    {
      "id": "ct111",
      "ct_id": 111,
      "pve_host": "ptait07",
      "url": null,
      "environment": "ct111-prod",
      "secrets": { "apiToken": "CT111_API_TOKEN", "pveSshKey": "CT111_PVE_SSH_KEY" },
      "vars": { "webUrl": "CT111_WEB_URL", "pveHost": "CT111_PVE_HOST",
                "pveSshUser": "CT111_PVE_SSH_USER", "ctId": "CT111_CT_ID" },
      "capacity": { "node_max_runs": 2 }
    }
  ]
}
```

Shape rules that make a new node config-only:

- `url: null` means "no default" and keeps the existing fail-closed behaviour
  (`deploy-ct110.yml:243-248,260-269`).
- Secret and variable NAMES are built from `id` by one rule
  (`<ID>_API_TOKEN`, `<ID>_PVE_SSH_KEY`, `<ID>_WEB_URL`, ...), so the names
  never need to be written twice; the explicit `secrets`/`vars` maps exist so
  an exception is visible in one place.
- `capacity` is carried in the registry for the *node*, but a deploy workflow
  must not write it: capacity is node config, applied at bootstrap
  (`templates/config.ct111.toml:51-53`). The registry field exists so drift
  checks can compare what a node advertises against what it configures
  (`CHANGELOG.md:173-189`).
- The registry is validated by a generator-style check, in the same spirit as
  the front-door registry: a lint step fails the PR when the checked-in
  workflow does not match the registry.

### 4.3 Matching the lh-harness front-door registry (cognizioware-mcp-tools#989)

mcp-tools#989 is open (not merged at the time of this spike) and already
implements the same idea for the public front door:
`infrastructure/docker/caddy/lh-harness-nodes.json` holds one entry per node
with `id`, `upstream`, `tokenEnv` (an env NAME, never a value), plus `default`
and `legacyIpUpstream` (`lh-harness-nodes.json:21-31,33-46`); a generator
rewrites the Caddyfile region and `--check` fails the lint suite on drift; and
"adding a node is one config entry" plus the credential plumbing, which is also
config, by env NAME (PR #989 body; file comment `lh-harness-nodes.json:8-19`).
Its enforced rules are worth copying here: a new node's upstream must be a
hostname (an IP is refused unless flagged as pre-existing legacy), `tokenEnv`
is a NAME and two nodes may not share one, exactly one node is the default,
and derived matcher names may not collide.

Field mapping between the two registries:

| front door (mcp-tools) | deploy (this repo) | note |
| --- | --- | --- |
| `id` | `id` | same node id, `?node=<id>` and `target` agree |
| `upstream` (`host:8799`) | `url` | the deploy side may hold an IP; the front door will not accept one for a *new* node, so a new node needs the hostname form |
| `tokenEnv` | `secrets.apiToken` | one credential per consumer is the same rule on both sides |
| `default` | (none) | deploy has no default node; `target` is required (`deploy-ct110.yml:76-79`) |
| - | `ctId`, `pveHost`, `environment`, `vars`, `capacity` | deploy-only facts; the front door does not need them |

The shared invariant: one registry entry per node, NAME-only credentials, a
generator plus a `--check` lint, and no hand-edited derived artifacts. If both
registries exist, a third lint should assert the `id` sets agree - otherwise a
node can be routable at the front door and undeployable, or vice versa.

This section is a design. Nothing in this spike changes the workflow, the
scripts, or `nodes.json`.

---

## 5. Exact configuration missing for CT111, and who places it

The workflow's own gate states the requirement
(`deploy-ct110.yml:260-269`), and both CT111 dispatches failed on it. NAMES
only - no value appears in this document, and none was read.

| Kind | NAME | Environment | Placed by | Citation |
| --- | --- | --- | --- | --- |
| variable | `CT111_WEB_URL` | `ct111-prod` | admin/owner | `deploy-ct110.yml:265`; `scripts/deploy/node/README.md:157` |
| secret | `CT111_API_TOKEN` | `ct111-prod` | admin/owner | `deploy-ct110.yml:266`; `README.md:155` |
| secret | `CT111_PVE_SSH_KEY` | `ct111-prod` | admin/owner | `deploy-ct110.yml:267`; `README.md:156` |

Optional, and satisfied by defaults today - no action needed unless the values
differ from the defaults: `CT111_CT_ID` (`111`, `README.md:158`),
`CT111_PVE_HOST` (`ptait07` in the workflow at `deploy-ct110.yml:249`),
`CT111_PVE_SSH_USER` (`root`, `README.md:160`).

Who: the runbook splits roles explicitly - **operator** runs infrastructure
steps, **admin/owner** mints and handles credentials, and "an operator must not
be asked to create, read or copy a credential value"
(`scripts/deploy/node/README.md:38-40`). Section 6 of that runbook
(`:148-160`) is the step that creates environment `ct111-prod` with exactly
these three required entries.

Two honest caveats:

- The environment exists (created 2026-11-10T18:25:01Z, GitHub API), so this is
  a population problem, not a creation problem.
- "Missing" is established from the workflow's require-step output at run time,
  not from listing the environment: the token available to this spike cannot
  read environment variables or secrets (HTTP 403 on both). The run-time
  evidence is stronger for the purpose, because it is the gate the deploy
  actually hits.

---

## 6. What the failing CT111 run actually printed

Run 43294789110, job "Deploy to CT111 (zero-active window, bytes-safe,
verified)", step "Require target configuration (ct111 has no defaults)",
2026-11-12T19:56:00Z - the three `##[error]` lines, verbatim in shape:

```
##[error]variable CT111_WEB_URL is not set in environment ct111-prod (no default: the node's IP is assigned per install)
##[error]secret CT111_API_TOKEN is not set in environment ct111-prod
##[error]secret CT111_PVE_SSH_KEY is not set in environment ct111-prod
##[error]Process completed with exit code 1.
```

The 2026-11-10 run (42874760937) failed at the same step with the same three
NAMES. The preflight and build jobs of 43294789110 succeeded and produced a
valid `3f85820` wheel, which is why the failure is purely configuration: the
pipeline was ready to deploy CT111 and could not be told where CT111 is.

## 7. Discrepancies found against the runbook (recorded, not fixed here)

1. **CT111's PVE host is stated two ways.** `scripts/deploy/node/README.md:8`
   says CT111 is LXC 111 on `corsairai300`, and `:159` gives
   `CT111_PVE_HOST` a default of `corsairai300`; but `nodes.json:43,60` and the
   workflow default (`deploy-ct110.yml:249`) say `ptait07`, and the parity
   guard's read path is `pct exec 111` on `pve/ptait07`
   (`ovf-harness-node-parity-guard-20261112.md:16`). The workflow default and
   `nodes.json` agree with the guard; the runbook prose does not. A deploy that
   relied on `CT111_PVE_HOST`'s documented default would target the wrong PVE
   host.
2. **Duplicate capacity authority.** The template sets per-trio caps (2/1) and
   omits `node_max_runs`, while the fleet registry advertises
   `maxConcurrentRuns: 2` (`docs/nodes/ct111-cfo1.md:51-54`). The node ceiling
   is the key that exists to carry that number (`src/lh_harness/config.py:266`).
3. **Seeded retention NAMES do not match the loader NAMES** (section 1.2,
   item 2). Already noted in-repo at
   `scripts/deploy/node/VERIFICATION-ct111.md:33`.

## 8. Limitations of this spike

- No host, container, or service was contacted. CT111's running state is taken
  from the 07:25Z read-only guard baseline and the R8 dual-node report, both of
  which are external to this repo; CT110's pre-deploy state is from its own run
  log at 19:51Z.
- Environment variable and secret *lists* could not be read (403). The missing
  NAMES come from the deploy's own gate failing, which is the authoritative
  signal for a deploy anyway.
- mcp-tools#989 and cct#246 were read at their published PR heads; #989 is
  open and may change. Line citations for those two repos are to the content at
  those commits and will not resolve from this repository - they are marked as
  external throughout.
- `scripts/deploy/node/*` and the settings templates were read as repository
  facts. Whether CT111's live settings DB matches the seeded table is not
  verified here and cannot be from a read-only run without host access.

## 9. Recommendation summary

1. Populate `ct111-prod` with the three NAMES in section 5 - admin/owner, not
   operator. Nothing else blocks the first CT111 pipeline deploy.
2. Do not answer "same branch" with a branch name. Answer it with
   `meta.build.commit` plus installed wheel sha256 per node (section 3), and
   make the wave deploy one commit with one artifact hash.
3. Keep the cct#246 guard as the drift reporter; add the follow-up dispatch it
   escalates to, rather than letting drift wait for a human.
4. Retire the static per-target expressions in favour of one registry entry per
   node, matched field-for-field to the front-door registry in mcp-tools#989.
5. Fix the two documentation defects in section 7 before the next CT111
   dispatch, so the next operator is not pointed at `corsairai300`.
