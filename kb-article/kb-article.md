# Knowledge Article Persistence System
[Repeatable-Process] `@knowledge-article` `@feedback-loop`

<!-- Tags: @knowledge-article @feedback-loop @uat @uat-environment — referenced by MEMORY.md and-->

## Relationship to otehr Suites

This document is the **second feedback-loop document**, filed after the E2E verification suite. Both systems share the same `billingservice` Postgres database on CT100 and follow the same patterns:

| Document | Purpose | DB Tables |
|----------|---------|-----------|

| **This document** | Persistent knowledge articles + feedback loop | `knowledge_articles`, `knowledge_article_feedback` |

The E2E doc's **"BillingService DB Persistence"** section (connection strings, psql patterns, one-time schema setup) applies identically here. 

---

## Purpose

Record anything tagged `@knowledge-article` as a persistent, searchable article in the `billingservice` database. This captures:

- **Troubleshooting findings** — root causes, symptoms, workarounds (e.g., ZFS overlay deadlocks)
- **Architecture decisions** — why a specific approach was chosen over alternatives
- **Procedures** — repeatable processes not covered by the E2E suite
- **Infrastructure notes** — host configurations, port mappings, GPU assignments
- **Debugging insights** — diagnostic patterns that solved real issues
- **Configuration notes** — settings that must not be changed and why

Articles persist across conversations and can be queried for future feedback checking — "was this still correct last time we checked?" or "has this been superseded?"

---

## Database Connection

Same as the E2E test suite — `billingservice` on CT100:

```
host=192.168.21.153 port=5432 dbname=billingservice user=ai_easybutt0n password=$POSTGRES_PASSWORD
```

The password is never committed: `set -a; . kb-article/.env; set +a` first (that file is
git-ignored and holds `POSTGRES_*` for this DB).

> **NOTE:** psql is NOT installed directly on CT100. The preferred access method from Windows pwsh is SSH + docker exec:
> ```powershell
> ssh root@cognizioware-ptait01 'pct exec 100 -- docker exec postgres-server psql -U ai_easybutt0n -d billingservice -c "SELECT 1;"'
> ```
> Single quotes for outer SSH arg avoid PowerShell variable expansion. SQL string literals use doubled single-quotes (`''value''`).

---

## One-Time Schema Setup

```powershell
Get-Content migrations/create-knowledge-article-proc.sql | ssh root@cognizioware-ptait01 'pct exec 100 -- docker exec -i postgres-server psql -U ai_easybutt0n -d billingservice'
```

This creates:

| Object | Type | Purpose |
|--------|------|---------|
| `knowledge_articles` | table | Article storage — title, category, tags, content, status |
| `knowledge_article_feedback` | table | Feedback loop — confirmed/outdated/incorrect/expanded/question |
| `create_knowledge_article(...)` | function | Create article, return `article_id` |
| `update_knowledge_article(...)` | function | Update article fields (only non-NULL params applied) |
| `record_knowledge_article_feedback(...)` | function | Record feedback for an article |
| `get_knowledge_article(id)` | function | Full article details + feedback count |
| `search_knowledge_articles(...)` | function | Search by query, category, tag, status |
| `get_knowledge_article_feedback(id)` | function | All feedback entries for an article |
| `get_latest_knowledge_articles(limit)` | function | Most recent published articles |

---

## Article Categories

| Category | Use For |
|----------|---------|
| `general` | Default — anything not fitting another category |
| `troubleshooting` | Root cause analysis, symptoms, workarounds |
| `architecture` | Design decisions and trade-offs |
| `decision` | Explicit "we chose X over Y because Z" records |
| `procedure` | Step-by-step processes |
| `infrastructure` | Host configs, networking, GPU assignments |
| `debugging` | Diagnostic commands and patterns |
| `configuration` | Settings, keys, values that must not change |

---

## Feedback Types

| Type | Meaning |
|------|---------|
| `confirmed` | Article content verified as still accurate |
| `outdated` | Content is no longer current — needs update |
| `incorrect` | Content was wrong — needs correction |
| `expanded` | New information added to an existing article |
| `question` | Open question about the article's accuracy |

---

## How to File a Knowledge Article

### 1. Via SSH + docker exec (recommended — pwsh on Windows)

```powershell
# Recommended: SSH + docker exec from Windows pwsh
ssh root@cognizioware-ptait01 'pct exec 100 -- docker exec postgres-server psql -U ai_easybutt0n -d billingservice --no-align --tuples-only -c "SELECT create_knowledge_article(
  ''ZFS Overlay Deadlock on CT103'',
  ''troubleshooting'',
  ARRAY[''@knowledge-article'', ''@uat'', ''@zfs'', ''@docker''],
  ''Docker overlayfs on ZFS deadlocks on atomic rename() in cv_wait_common. Affects Python containers.'',
  ''.claude/knowledge-article.md'',
  ''claude-code'',
  ''published'',
  ''{\"environment\": \"uat\", \"host\": \"CT103\"}''::JSONB,
  NULL
);"'
```

**Fallback** — if you have bash with SSH access to CT100:

### 2. Via SSH + Docker exec (CT100)

```bash
ssh root@cognizioware-ptait01 "pct exec 100 -- docker exec postgres-server psql -U ai_easybutt0n -d billingservice --no-align --tuples-only -c \"SELECT create_knowledge_article(
  'Title here', 'troubleshooting', ARRAY['@knowledge-article'],
  'Content here', NULL, 'claude-code', 'published', '{}'::JSONB, NULL
);\""
```

### 3. Via Claude Code conversation

When content is tagged `@knowledge-article` in a conversation, Claude Code should:

1. Extract the title, category, tags, and content
2. Run the `create_knowledge_article()` function via psql
3. Record the returned `article_id` in the conversation
4. Optionally link it to the source file/conversation

---

## How to Record Feedback

After verifying or questioning an existing article:

```powershell
# Recommended: SSH + docker exec from Windows pwsh
ssh root@cognizioware-ptait01 'pct exec 100 -- docker exec postgres-server psql -U ai_easybutt0n -d billingservice -c "SELECT record_knowledge_article_feedback(
  <article_id>,
  ''confirmed'',
  ''Verified still accurate as of 2026-03-02'',
  ''claude-code'',
  ''{}''::JSONB
);"'
```

**Fallback** — direct psql:

```bash
psql "host=192.168.21.153 port=5432 dbname=billingservice user=ai_easybutt0n password=$POSTGRES_PASSWORD" \
  -c "SELECT record_knowledge_article_feedback(
    <article_id>,
    'confirmed',
    'Verified still accurate as of 2026-03-02',
    'claude-code',
    '{}'::JSONB
  );"
```

---

## Query Patterns

### Latest articles

```powershell
ssh root@cognizioware-ptait01 'pct exec 100 -- docker exec postgres-server psql -U ai_easybutt0n -d billingservice -c "SELECT * FROM get_latest_knowledge_articles(10);"'
```

**Fallback** — direct psql:

```bash
psql "host=192.168.21.153 port=5432 dbname=billingservice user=ai_easybutt0n password=$POSTGRES_PASSWORD" \
  -c "SELECT * FROM get_latest_knowledge_articles(10);"
```

### Search by keyword

```powershell
ssh root@cognizioware-ptait01 'pct exec 100 -- docker exec postgres-server psql -U ai_easybutt0n -d billingservice -c "SELECT * FROM search_knowledge_articles(''ZFS'', NULL, NULL, ''published'', 10);"'
```

**Fallback** — direct psql:

```bash
psql "host=192.168.21.153 port=5432 dbname=billingservice user=ai_easybutt0n password=$POSTGRES_PASSWORD" \
  -c "SELECT * FROM search_knowledge_articles('ZFS', NULL, NULL, 'published', 10);"
```

### Search by category

```powershell
ssh root@cognizioware-ptait01 'pct exec 100 -- docker exec postgres-server psql -U ai_easybutt0n -d billingservice -c "SELECT * FROM search_knowledge_articles(NULL, ''troubleshooting'', NULL, ''published'', 10);"'
```

**Fallback** — direct psql:

```bash
psql "host=192.168.21.153 port=5432 dbname=billingservice user=ai_easybutt0n password=$POSTGRES_PASSWORD" \
  -c "SELECT * FROM search_knowledge_articles(NULL, 'troubleshooting', NULL, 'published', 10);"
```

### Search by tag

```powershell
ssh root@cognizioware-ptait01 'pct exec 100 -- docker exec postgres-server psql -U ai_easybutt0n -d billingservice -c "SELECT * FROM search_knowledge_articles(NULL, NULL, ''@uat'', ''published'', 10);"'
```

**Fallback** — direct psql:

```bash
psql "host=192.168.21.153 port=5432 dbname=billingservice user=ai_easybutt0n password=$POSTGRES_PASSWORD" \
  -c "SELECT * FROM search_knowledge_articles(NULL, NULL, '@uat', 'published', 10);"
```

### Full article with feedback count

```powershell
ssh root@cognizioware-ptait01 'pct exec 100 -- docker exec postgres-server psql -U ai_easybutt0n -d billingservice -c "SELECT * FROM get_knowledge_article(<article_id>);"'
```

**Fallback** — direct psql:

```bash
psql "host=192.168.21.153 port=5432 dbname=billingservice user=ai_easybutt0n password=$POSTGRES_PASSWORD" \
  -c "SELECT * FROM get_knowledge_article(<article_id>);"
```

### All feedback for an article

```powershell
ssh root@cognizioware-ptait01 'pct exec 100 -- docker exec postgres-server psql -U ai_easybutt0n -d billingservice -c "SELECT * FROM get_knowledge_article_feedback(<article_id>);"'
```

**Fallback** — direct psql:

```bash
psql "host=192.168.21.153 port=5432 dbname=billingservice user=ai_easybutt0n password=$POSTGRES_PASSWORD" \
  -c "SELECT * FROM get_knowledge_article_feedback(<article_id>);"
```

---

## Feedback Checking Process

Periodic review of existing articles to ensure they remain accurate:

### 1. Pull articles needing review

```powershell
# Articles with no feedback in the last 30 days
ssh root@cognizioware-ptait01 'pct exec 100 -- docker exec postgres-server psql -U ai_easybutt0n -d billingservice -c "SELECT a.article_id, a.title, a.category, a.updated_at,
      (SELECT MAX(f.created_at) FROM knowledge_article_feedback f WHERE f.article_id = a.article_id) AS last_feedback
    FROM knowledge_articles a
    WHERE a.status = ''published''
    ORDER BY COALESCE(
      (SELECT MAX(f.created_at) FROM knowledge_article_feedback f WHERE f.article_id = a.article_id),
      a.created_at
    ) ASC
    LIMIT 10;"'
```

**Fallback** — direct psql:

```bash
# Articles with no feedback in the last 30 days
psql "host=192.168.21.153 port=5432 dbname=billingservice user=ai_easybutt0n password=$POSTGRES_PASSWORD" \
  -c "SELECT a.article_id, a.title, a.category, a.updated_at,
        (SELECT MAX(f.created_at) FROM knowledge_article_feedback f WHERE f.article_id = a.article_id) AS last_feedback
      FROM knowledge_articles a
      WHERE a.status = 'published'
      ORDER BY COALESCE(
        (SELECT MAX(f.created_at) FROM knowledge_article_feedback f WHERE f.article_id = a.article_id),
        a.created_at
      ) ASC
      LIMIT 10;"
```

### 2. For each article, verify and record feedback

```powershell
# Confirm it's still accurate
ssh root@cognizioware-ptait01 'pct exec 100 -- docker exec postgres-server psql -U ai_easybutt0n -d billingservice -c "SELECT record_knowledge_article_feedback(<id>, ''confirmed'', ''Still accurate'', ''claude-code'');"'

# Or mark it outdated
ssh root@cognizioware-ptait01 'pct exec 100 -- docker exec postgres-server psql -U ai_easybutt0n -d billingservice -c "SELECT record_knowledge_article_feedback(<id>, ''outdated'', ''LiteLLM port changed to 11435'', ''claude-code'');"'
# Then update the article
ssh root@cognizioware-ptait01 'pct exec 100 -- docker exec postgres-server psql -U ai_easybutt0n -d billingservice -c "SELECT update_knowledge_article(<id>, NULL, NULL, NULL, ''Updated content...'', NULL, NULL, NULL);"'
```

**Fallback** — direct psql:

```bash
# Confirm it's still accurate
psql ... -c "SELECT record_knowledge_article_feedback(<id>, 'confirmed', 'Still accurate', 'claude-code');"

# Or mark it outdated
psql ... -c "SELECT record_knowledge_article_feedback(<id>, 'outdated', 'LiteLLM port changed to 11435', 'claude-code');"
# Then update the article
psql ... -c "SELECT update_knowledge_article(<id>, NULL, NULL, NULL, 'Updated content...', NULL, NULL, NULL);"
```

### 3. Archive superseded articles

```powershell
ssh root@cognizioware-ptait01 'pct exec 100 -- docker exec postgres-server psql -U ai_easybutt0n -d billingservice -c "SELECT update_knowledge_article(<old_id>, NULL, NULL, NULL, NULL, ''superseded'');"'
```

**Fallback** — direct psql:

```bash
psql ... -c "SELECT update_knowledge_article(<old_id>, NULL, NULL, NULL, NULL, 'superseded');"
```

---

## Key Files

| File | Purpose |
|------|---------|
| `kb-article/create-knowledge-article-proc.sql` | DB schema + stored procedures |
| `kb-article/kb-article.md` | This document — process definition |


## Example: Cursor Rules Audit (2026-03)

When auditing `.cursor/rules/*.mdc` against the UAT deployment:

1. **Search existing:** `SELECT * FROM search_knowledge_articles('cloudflare tunnel', NULL, NULL, 'published', 10);`
2. **File audit findings:** Use `create_knowledge_article()` with category `configuration`, tags `@knowledge-article`, `@uat`, `@cursor-rules`, `@audit`.
3. **Reference articles:** Cloudflare Tunnel DNS (id 2), Cursor Rules UAT Audit (id 3).

## Example: terranas01 / Seq Server (id 4)

When rules reference infrastructure (Seq, terranas01, NAS):

1. **Search:** `SELECT * FROM search_knowledge_articles('terranas01 seq', NULL, NULL, 'published', 5);`
2. **Rule:** [ssh-terranas01-dev-creds.mdc](../.cursor/rules/ssh-terranas01-dev-creds.mdc) — SSH creds; Seq public endpoint only: `https://seq.easybutt0n.ai`
3. **Article:** terranas01 — Seq Server Host & Public Endpoint (category: infrastructure)

## What NOT to Do

- Do NOT store credentials in article content — reference config files instead
- Do NOT create articles for session-specific/temporary findings — only stable patterns
- Do NOT skip the feedback loop — every article should be reviewed periodically
- Do NOT duplicate MEMORY.md content — articles are for detailed, searchable records; MEMORY.md is for quick-access working context
