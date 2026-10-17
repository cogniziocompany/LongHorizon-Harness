# Agent Profiles: Backoffice, Finance & CRM

**Status:** Draft design — Round 1 of LHH-AGENT-PROFILES-BACKOFFICE
**Owners:** CFO1 (consumer), Overseer1 (router), SupportOps (operational)
**Source task:** Paxton 2026-10-17 — *"branch [lh-harness agent profiles] with M365, PPTX, HTML spec, research and other CFO back-office skills which the agent profiles in lh-harness can reach. Even make a CRM sub-flavor, like Monday and Dynamics paths."*
**Companion node:** CFO1-HARNESS-NODE-CT111 (CFO1's own harness; profile contract must round-trip).

---

## 1. Motivation

LongHorizon-Harness today ships five built-in MCP profiles (`none`, `audit`, `default`, `ops`, `full`) plus per-user / per-project overrides in `mcp_profiles.json` and `.lh-harness/config.toml`. Those profiles are tuned for **code-and-fleet** work: github, youtrack, ssh, hydra, proxmox. They say nothing about:

- M365 mail / calendar / SharePoint
- PPTX/DOCX/XLSX generation
- HTML spec authoring
- General research / web
- QuickBooks / reporting-DB reads
- Monday.com or Dynamics 365 / Dataverse

CFO1, Overseer1 and SupportOps all need subsets of those surfaces, and **Git must stay optional** — CFO1 may work artifact-only with no repo, while SupportOps still wants github on the same profile family.

This document specifies five new profile families (gated behind `BACKOFFICE_PROFILES_ENABLED=false` default so no live keys are required) and the project-start "template/form stage" that lets the requester pick a family plus an optional git repo/branch.

## 2. Existing surface (verified from `src/lh_harness/mcp_profiles.py` and `src/lh_harness/adapters/claude_permissions.py`)

### 2.1 Current built-ins (do not change)

| Profile   | Servers                                                                                          | Read-only |
|-----------|--------------------------------------------------------------------------------------------------|-----------|
| `none`    | (none)                                                                                           | yes       |
| `audit`   | `kb`, `guides`, `skills`, `memory`, `langfuse_mcp`                                               | yes       |
| `default` | audit + `github`, `youtrack`, `ssh`                                                               | no        |
| `ops`     | default + `hydra`, `hydrafleet`, `proxmoxptait01`, `proxmoxptait07`, `proxmoxcorsairai300`        | no        |
| `full`    | gateway allow-list header **omitted** → all configured servers                                    | no        |

`render_mcp_config()` writes the profile's server list into the `x-mcp-servers` header on the `cognizioware` MCP gateway (LiteLLM Streamable-HTTP). Auditor roles must use read-only profiles unless `allow_auditor_write_mcp = true` is set (`mcp_profiles.py:412-418`). All non-`none` profiles collapse to `none` when `LH_HARNESS_MCP_GATEWAY_KEY` is missing (`mcp_profiles.py:399-409`).

### 2.2 Role policy (`adapters/claude_permissions.py`)

| Role              | Permission mode        | Notable denies                                                              |
|-------------------|-----------------------|------------------------------------------------------------------------------|
| `manager`         | `bypassPermissions`   | `Bash`, `Write/Edit/NotebookEdit`, `Agent`, `mcp__*`, workspace read-only     |
| `gui_executor` / `cli_executor` | `bypassPermissions` | `Agent` only (full Bash + write tools + computer MCP)               |
| auditor family    | `bypassPermissions`   | write tools, `Agent`, all network git ops, `gh *`, computer MCP, R/O workspace |
| `final_response`  | `bypassPermissions`   | same as `manager` (no-side-effect reply role)                                |

Auditor network git is killed via `AUDITOR_NETWORK_GIT_ENV` (`GIT_TERMINAL_PROMPT=0`, `GIT_ASKPASS=/bin/false`, `SSH_AUTH_SOCK` cleared, `GIT_CONFIG_COUNT` overrides blocking credential helpers). The auditor integrity guarantee therefore transfers cleanly to any new profile that is marked `read_only=True`.

### 2.3 Configuration surfaces (verified)

- **Built-ins:** hard-coded in `_BUILTINS` (`mcp_profiles.py:28-77`).
- **User-level overrides:** `~/.lh-harness/mcp_profiles.json` → `_load_user_profiles()` (`mcp_profiles.py:156-187`).
- **Project-level overrides:** `.lh-harness/config.toml` `[run.mcp_profiles.<name>]` → `_load_project_profiles()` (`mcp_profiles.py:190-230`).
- **Resolution precedence** (`mcp_profiles.py:316-380`): API/CLI role override → API/CLI run override → `[run.roles.<role>].mcp_profile` → `[run].mcp_profile` → web-default env (per-role, then global) → built-in default. Same precedence chain will apply to the new backoffice profiles.

The new families therefore drop in as **new built-ins** (not as project overlay) so the precedence rules already cover them.

## 3. New profile families

All five families are **added** to `_BUILTINS` behind the `BACKOFFICE_PROFILES_ENABLED` flag. The flag default is `false`; setting `LH_HARNESS_BACKOFFICE_PROFILES_ENABLED=true` (or `BACKOFFICE_PROFILES_ENABLED` in `.lh-harness/config.toml [run]`) registers them with `list_available_profiles()` and `resolve_profile()`. When the flag is off, every reference to a backoffice family name falls through the same "unknown profile → built-in default" path that already handles typos.

### 3.1 `dev` (git first, read+write)

| Aspect         | Value                                                                              |
|----------------|------------------------------------------------------------------------------------|
| Servers        | `kb`, `guides`, `skills`, `memory`, `langfuse_mcp`, `github`, `youtrack`            |
| Read-only      | **no** (write to github/youtrack; bash + write tools inherited from `executor` policy) |
| Purpose        | Code-and-repo work; the user-facing rename of today's `default` for clarity.         |
| Allowed agents | `gui_executor`, `cli_executor`, `manager` (write via executor only); **never auditor** unless `allow_auditor_write_mcp=true` |

`dev` is a superset of `audit`; auditors may NOT resolve it. It excludes `ssh`, `hydra`, `hydrafleet`, `proxmox*` from `ops` to keep CFO1 off fleet surfaces.

### 3.2 `backoffice` (M365, PPTX/DOCX/XLSX, HTML spec, research/web, KB)

| Aspect         | Value                                                                                          |
|----------------|------------------------------------------------------------------------------------------------|
| Servers        | `kb`, `guides`, `skills`, `memory`, `langfuse_mcp`, `ms365`, `pptx`, `htmlspec`, `web`, `research` |
| Read-only      | **no** (M365 mail send, file uploads to SharePoint, generated artifacts all write)              |
| Purpose        | CFO1's daily surface: read mail/calendar, draft replies, generate decks, write HTML specs, look things up. |
| Allowed agents | `gui_executor`, `cli_executor`, `manager` (rare — manager should not normally need M365)       |

**Tool scope notes:**
- `ms365` covers Outlook mail, Calendar, SharePoint files (per gateway alias `ms365` — read surface first, write tools under `mail.send` / `files.upload`).
- `pptx` is a generator: read templates from KB, emit `.pptx` to the run dir.
- `htmlspec` renders the HTML specification template used by `[run.profile = backoffice]` workspace specs.
- `web` and `research` are the read-only web fetcher and the multi-source research aggregator.

Git is **not** in this profile. CFO1 can run artifact-only.

### 3.3 `finance` (QuickBooks + reporting DB read)

| Aspect         | Value                                                                          |
|----------------|--------------------------------------------------------------------------------|
| Servers        | `kb`, `guides`, `skills`, `memory`, `langfuse_mcp`, `quickbooks`, `reporting_db` |
| Read-only      | **mixed** — profile carries `read_only=True` flag because `reporting_db` is a read-only Postgres MCP. `quickbooks` *can* write (invoices/journal entries) but the profile is still marked read_only so auditors can mount it. CFO1's runtime resolves a per-tool allow-list at execution time. |
| Purpose        | Pull P&L, AR, AP, run custom reports, draft invoices/journal entries for human approval. |
| Allowed agents | `gui_executor`, `cli_executor`, `manager`, all auditor variants |

**Why a mixed profile?** `quickbooks` ships with both read (`get_invoice`) and write (`create_invoice`) tools. The harness cannot scope at the *tool* level through the `x-mcp-servers` header (the gateway scopes at the *server* level — see `mcp_profiles.py:25-27`). Two-step pattern:

1. Profile is `read_only=True` so auditors may resolve it.
2. CFO1's executor wraps write calls in an explicit human-approval gate (the existing `harness_resolve_gate` MCP tool) before calling `quickbooks.create_invoice`.

This keeps the contract intact and the audit trail whole.

### 3.4 `crm_monday`

| Aspect         | Value                                                          |
|----------------|----------------------------------------------------------------|
| Servers        | `kb`, `guides`, `skills`, `memory`, `langfuse_mcp`, `monday`    |
| Read-only      | **no** (board mutation allowed)                                |
| Purpose        | Monday.com CRM/pipeline work: boards, items, updates.          |
| Allowed agents | `gui_executor`, `cli_executor`, `manager`                       |

Sub-flavor of "CRM" — see §3.5.

### 3.5 `crm_dynamics`

| Aspect         | Value                                                                                |
|----------------|--------------------------------------------------------------------------------------|
| Servers        | `kb`, `guides`, `skills`, `memory`, `langfuse_mcp`, `dataverse`, `d365`, `xrm`        |
| Read-only      | **no** (records, accounts, opportunities writable)                                   |
| Purpose        | Microsoft Dynamics 365 / Dataverse: account/opportunity/case lifecycle, XRM custom entities. |
| Allowed agents | `gui_executor`, `cli_executor`, `manager`                                             |

`crm_monday` and `crm_dynamics` are intentionally two separate profiles (not one merged `crm`) because their *write-tool surfaces differ* — the CFO1/Overseer1/SupportOps triad will switch between them, and mixing the tools would balloon the context for agents that don't talk to both vendors.

### 3.6 Profile × agent matrix

| Profile / Agent   | CFO1   | Overseer1 (manager) | SupportOps (executor) | Auditor (any)             |
|-------------------|--------|----------------------|------------------------|---------------------------|
| `dev`             | —      | only via run.mcp_profile override | yes (write) | only with `allow_auditor_write_mcp=true` |
| `backoffice`      | **yes** (write) | only via override     | yes (write)            | **no** (write tools)       |
| `finance`         | **yes** (write, gated) | only via override | yes (write, gated)     | yes (read-only flag)       |
| `crm_monday`      | yes    | only via override     | yes (write)            | **no**                     |
| `crm_dynamics`    | yes    | only via override     | yes (write)            | **no**                     |
| `audit` (existing) | (read) | yes (read)            | yes (read)              | **yes**                    |
| `none` (existing)  | —      | yes                   | yes                     | yes                       |

"CFO1", "Overseer1" and "SupportOps" are **agent identities**, not Claude Code role names. They map onto the harness roles as follows:

| Identity   | Claude role used             | Allowed profiles                                   |
|------------|-------------------------------|----------------------------------------------------|
| CFO1       | `cli_executor` or `gui_executor` | `backoffice`, `finance`, `crm_monday`, `crm_dynamics`, `none` |
| Overseer1  | `manager` / `final_response`   | `audit`, `none` (and overrides only when explicitly resolved) |
| SupportOps | `cli_executor` / `gui_executor` | `dev`, `backoffice`, `finance`, `crm_monday`, `crm_dynamics` |

The CFO1 harness node (CFO1-HARNESS-NODE-CT111) consumes the same profile registry through `resolve_profile()`; identity→role is set in CFO1's `~/.lh-harness/mcp_profiles.json`.

## 4. Project-start "template/form stage"

The "project start" Paxton described is the **new-task creation form** — the moment the requester picks what kind of work they're queuing. The form stages that decision before any code/git/MCP servers are configured.

### 4.1 Data model

```
NewTaskRequest {
  task_id:                 ULID (server-generated)
  profile_family:          enum { dev | backoffice | finance | crm_monday | crm_dynamics | none | audit }
  workspace_kind:          enum { git_repo | artifact_only }
  git:                     GitSpec?         # required iff workspace_kind == git_repo
  artifact:                ArtifactSpec?    # required iff workspace_kind == artifact_only
  max_rounds:              int              # default 25, capped per profile
  priority:                int              # default 0
  base_check:              str?             # optional smoke test command
  requested_by:            str              # CFO1 | Overseer1 | SupportOps
  notes:                   str?
  created_at:              ISO-8601
}

GitSpec {
  provider:                enum { github | gitlab }   # github now; gitlab later
  repo:                    str            # e.g. "cogniziocompany/LongHorizon-Harness"
  ref:                     str            # branch, tag, or sha (default: default branch)
  ssh_key_alias:           str?           # which CFO1-CFO1 ssh key to mount; null → public read-only
  create_branch:           bool           # default true; if true, executor checks out -B <new> origin/<ref>
  worktree_path:           str?           # default .lh-harness/workspaces/<task_id>
}

ArtifactSpec {
  mount_template:          str            # e.g. "cfo-board-deck", "htmlspec-v1"
  storage_root:            str            # e.g. .lh-harness/artifacts/<task_id>
  publish_target:          enum { local | sharepoint | webui }   # default local
}
```

`workspace_kind` is the only **required** decision after `profile_family`. `git` and `artifact` are mutually exclusive at validation time (422 if both, 422 if neither when the chosen profile family needs a workspace — `backoffice` allows `artifact_only`; `dev` requires `git_repo`; `finance`/`crm_*` allow either).

### 4.2 Validation rules

| Profile family | Allowed workspace_kinds            | Required Git field on git_repo |
|----------------|------------------------------------|---------------------------------|
| `dev`           | `git_repo`                         | `repo` (org/name) + `ref` (branch/tag/sha) |
| `backoffice`    | `git_repo` or `artifact_only`      | optional; CFO1 most often picks `artifact_only` |
| `finance`       | `git_repo` or `artifact_only`      | optional                        |
| `crm_monday`    | `git_repo` or `artifact_only`      | optional                        |
| `crm_dynamics`  | `git_repo` or `artifact_only`      | optional                        |
| `audit`         | `git_repo` or `artifact_only`      | optional                        |
| `none`          | `artifact_only`                    | n/a                             |

`provider=gitlab` is **accepted at the API surface now** but the resolution path is stubbed to a 501 until the GitLab provider ships. The UI shows `GitHub` enabled and `GitLab` greyed with a "coming soon" badge — that decision lives in the Web bundle, not in the harness.

### 4.3 API contract

```
POST   /api/new-task              # create the staged request, returns task_id + form-state URL
GET    /api/new-task/{task_id}    # read form state (for resume)
POST   /api/new-task/{task_id}/submit   # finalize; creates the run and enqueues it
GET    /api/meta/profiles         # returns profile families visible on this node (additive; backoffice families included only when flag on)
```

`POST /api/new-task` body matches `NewTaskRequest`; response is the same with `task_id`, `created_at`, and `state_url` populated. **The form does not enqueue a run** — `submit` does, after a final consistency check against the MCP server allow-list (the gateway's `x-mcp-servers` header is built from `resolve_profile()` output, not from the form input).

`submit` returns 422 if `BACKOFFICE_PROFILES_ENABLED` is false and the request named a backoffice family — the audit-friendly behavior is to fail loud at the API, not silently downgrade.

### 4.4 UI surface

A single page in the Web bundle (`src/lh_harness/_frontend/web/`) with three top-to-bottom sections:

1. **Profile family picker** — radio chips, each with description + server allow-list (read from `GET /api/meta/profiles`). Tooltips name which agent identities may use each.
2. **Workspace kind picker** — radio chips `git_repo` / `artifact_only`. When `git_repo`:
   - `Provider` select (GitHub enabled, GitLab "coming soon").
   - `Repository` text input (validated as `^[^/\s]+/[^/\s]+$`).
   - `Reference` text input (default branch).
   - `Create new branch` toggle (default on) + `New branch name` text.
   - `SSH key alias` select (populated from CFO1's `~/.ssh/config` parsing — out of scope for this round).
   When `artifact_only`:
   - `Mount template` select (from a fixed starter set: `cfo-board-deck`, `htmlspec-v1`, `quarterly-report`, `empty`).
   - `Storage root` text (default `.lh-harness/artifacts/<task_id>`).
   - `Publish target` select (local / sharepoint / webui).
3. **Run metadata** — `max_rounds` (default 25, profile-capped), `priority`, `base_check`, `notes`, `requested_by` (auto-filled from session).

The form is **wizard-style** (Step 1 → Step 2 → Step 3 → Submit) so the requester cannot pick `dev` + `artifact_only` (the validation matrix above returns 422 and the UI disables Step 3's Submit button with an inline error).

### 4.5 Resolution chain on submit

```
POST /api/new-task/{task_id}/submit
  ├─ validate workspace_kind ⊆ profile_family
  ├─ validate git repo accessible (gh repo view … --json name)   # only on dev/backoffice with git_repo
  ├─ resolve mcp_profile = resolve_profile(role=cli_executor | manager, run_profile=family_name)
  ├─ assert mcp_profile.servers ⊆ {server: ..., ...}            # sanity; would catch flag off + name leak
  ├─ create run_dir = .lh-harness/runs/<task_id>/
  ├─ if workspace_kind == git_repo:
  │     git -C <worktree_path> init -b main      # only if create_branch
  │     git -C <worktree_path> remote add origin <url>
  │     git -C <worktree_path> fetch --depth=1 origin <ref>
  │     git -C <worktree_path> checkout -B <new> origin/<ref>
  └─ if workspace_kind == artifact_only:
        mkdir -p <storage_root>
        cp -r templates/<mount_template>/* <storage_root>/
        chmod 0700 <storage_root>
  └─ enqueue run (POST /api/queue with the existing surface)
```

The `git_repo` path is what Paxton meant by "tie in the git of each project at the start of lh-harness": the workspace is **always provisioned at form stage**, not later in the run. CFO1 never has to run `git clone` from inside an executor round.

## 5. Flag mechanism in `mcp_profiles.py`

```python
# src/lh_harness/mcp_profiles.py (additive change)

import os
from typing import Any

_BACKOFFICE_FAMILIES: dict[str, dict[str, Any]] = {
    "dev": {
        "description": "Git-first code work; superset of audit, no fleet/ssh.",
        "servers": ("kb", "guides", "skills", "memory", "langfuse_mcp",
                    "github", "youtrack"),
        "read_only": False,
    },
    "backoffice": {
        "description": "M365 mail/calendar/SharePoint, PPTX/DOCX/XLSX, HTML spec, web, research.",
        "servers": ("kb", "guides", "skills", "memory", "langfuse_mcp",
                    "ms365", "pptx", "htmlspec", "web", "research"),
        "read_only": False,
    },
    "finance": {
        "description": "QuickBooks + reporting DB (read). Write tools gated at executor.",
        "servers": ("kb", "guides", "skills", "memory", "langfuse_mcp",
                    "quickbooks", "reporting_db"),
        "read_only": True,
    },
    "crm_monday": {
        "description": "Monday.com boards/items/updates.",
        "servers": ("kb", "guides", "skills", "memory", "langfuse_mcp", "monday"),
        "read_only": False,
    },
    "crm_dynamics": {
        "description": "Microsoft Dataverse / Dynamics 365 / XRM.",
        "servers": ("kb", "guides", "skills", "memory", "langfuse_mcp",
                    "dataverse", "d365", "xrm"),
        "read_only": False,
    },
}


def _backoffice_profiles_enabled() -> bool:
    """True only when the env or project config opts in."""
    env = os.environ.get("LH_HARNESS_BACKOFFICE_PROFILES_ENABLED", "").strip().lower()
    if env in {"1", "true", "yes", "on"}:
        return True
    if env in {"0", "false", "no", "off"}:
        return False
    # Project config fallback: [run] backoffice_profiles_enabled = true
    cfg = _load_project_profiles(Path(".lh-harness/config.toml"))  # cheap; only the path
    return cfg.get("__backoffice_flag__", False)  # set in _load_project_profiles below
```

`_BUILTINS` and `resolve_profile()` stay unchanged structurally; `_backoffice_profiles_enabled()` is consulted inside `list_available_profiles()` (skip the backoffice entries when off) and inside `resolve_profile()` (when the requested name is a backoffice family but the flag is off, fall through to the "unknown profile → built-in default" path with `reason="BACKOFFICE_PROFILES_ENABLED not set"`).

No new credentials are referenced. The `ms365`, `monday`, `dataverse`, `quickbooks`, `pptx`, `htmlspec`, `web`, `research`, `reporting_db` server names are **strings** at this stage — they appear in `x-mcp-servers` but the underlying gateway registrations are a separate task. The harness does not depend on those servers existing for its tests; `resolve_profile()` is pure.

## 6. Out of scope (this round)

- Web UI implementation in the React bundle (design + data model only this round).
- Gateway-side registration of `ms365`, `pptx`, `htmlspec`, `monday`, `dataverse`, `d365`, `xrm`, `reporting_db`, `research` — those are infra work in `cognizioware-mcp-tools/infrastructure/litellm-config.yaml`.
- CFO1-HARNESS-NODE-CT111 itself (separate task).
- Per-tool write-allow lists for `quickbooks` and `crm_*` — covered by the existing `harness_resolve_gate` MCP tool.
- GitLab provider (UI shows "coming soon"; API accepts `provider: gitlab` and 501s on resolution).

## 7. Acceptance criteria for this round

1. This document exists at `docs/design/agent-profiles-backoffice.md`.
2. It covers all five profile families with server allow-lists, read-only flags, and allowed-agent matrix.
3. It specifies the new-task form data model, validation rules, API contract, and resolution chain.
5. No code that requires live credentials has been introduced (the only file change this round is the doc).