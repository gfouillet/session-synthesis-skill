---
name: session-synthesis
description: 'Synthesize and save a Copilot session as a structured Markdown file.
  Use when asked to "synthesize this session", "save session notes", "log this session",
  "summarize what we did", "wrap up session", "track session cost", "record session
  outcome", "log token usage", "retrospective on <topic>", "log last session", or
  "synthesize session from <date>". Operates in two modes: live (current session,
  full context available) or retrospective (past session reconstructed from session
  store). Estimates token usage and cost, prompts for self-rating and notes, and
  writes a structured MD report to a configurable output directory.'
---

# Session Synthesis

Captures a structured summary of a Copilot session — goal, approach, outcome, token
estimate, and personal notes — and appends it to a Markdown log file for later
efficiency analysis.

## Modes

### 🟢 Live Mode
Triggered during or at the end of the **current active session**. Full conversation
context is already in memory. Token estimation is based on current turn content.

**Trigger phrases:** "synthesize this session", "save session", "wrap up", "log session"

### 🔵 Retrospective Mode
Triggered on a **past session** after it has closed. Reconstructs the session from
the session store using session id, date, repository, or keyword search.

**Trigger phrases:** "synthesize session from 2026-05-10", "log last session",
"retrospective on auth refactor", "log session <id>"

---

## Workflow

### Step 1: Identify the Session

**Live mode:**
- The current session is already in context. Skip to Step 2.

**Retrospective mode:**
- If the user provided a session id, date, or keyword, query the session store:

```sql
-- By date
SELECT id, summary, cwd, repository, branch, created_at
FROM sessions
WHERE date(created_at) = '<date>'
ORDER BY created_at DESC;

-- By keyword (full-text search)
SELECT s.id, s.summary, s.repository, s.created_at
FROM sessions s
JOIN search_index si ON si.session_id = s.id
WHERE si.search_index MATCH '<keyword>'
ORDER BY s.created_at DESC
LIMIT 10;

-- Most recent session (excluding current)
SELECT id, summary, cwd, repository, branch, created_at
FROM sessions
ORDER BY created_at DESC
LIMIT 5;
```

- Present matching sessions to the user and ask them to confirm which one to synthesize.

### Step 2: Gather Session Content

**From session store (retrospective) or current context (live):**

```sql
-- Get checkpoints (structured summaries already exist)
SELECT title, overview, work_done, next_steps, technical_details
FROM checkpoints
WHERE session_id = '<session_id>'
ORDER BY checkpoint_number;

-- Get full turn history if no checkpoints
SELECT turn_index, user_message, assistant_response
FROM turns
WHERE session_id = '<session_id>'
ORDER BY turn_index;
```

### Step 3: Generate Summary

Using the gathered content, produce:

- **Goal**: What the user was trying to accomplish (infer from first user messages and checkpoints)
- **Approach**: How it was tackled (plan mode? direct implementation? debugging loop?)
- **Key decisions**: Important choices made during the session
- **Outcome**: What was achieved

Present the draft summary to the user for confirmation or edits before proceeding.

### Step 3b: Detect Orchestrator & Extract Session Metadata

**Detect orchestrator** before running the estimation script. The dispatcher
`scripts/estimate_tokens.py` does this automatically, but you should also resolve
these fields for the report:

| Signal | Orchestrator |
|--------|-------------|
| `~/.copilot/session-store.db` exists | **copilot-cli** |
| `~/.claude/projects/` exists | claude-code (no backend yet) |
| Otherwise | unknown — use fallback estimator |

**Extract model name:**
- Live mode: check session metadata or the `model_information` block in system context.
- Retrospective: query `turns` or `checkpoints`; model may appear in assistant responses
  or session summary. Fall back to asking the user if ambiguous.

**Extract sub-agents invoked:**

```sql
-- Sub-agent IDs appear as "agent_id" references in assistant responses (Copilot CLI task tool)
SELECT turn_index, substr(assistant_response, 1, 300) AS snippet
FROM turns
WHERE session_id = '<session_id>'
  AND assistant_response LIKE '%agent_id%'
ORDER BY turn_index;
```

Also scan for agent type names in assistant responses: `explore`, `rubber-duck`,
`task`, `general-purpose`, `code-review`, `research`. Summarize as
`"<type> (×<count>)"` per type, e.g. `"rubber-duck (×2), task/explore (×3)"`.

**Extract enabled skills:**

```sql
-- Skills injected at runtime appear as "skill-context" sections in turn content,
-- or as SKILL.md entries in session_files
SELECT DISTINCT file_path
FROM session_files
WHERE session_id = '<session_id>'
  AND file_path LIKE '%SKILL.md%';
```

Also check the first assistant response or system turns for `<invoked_skills>` blocks.
List skill names, e.g. `"session-synthesis, juju-qa"`. Use `"none"` if none found.

### Step 4: Estimate Token Usage

Run the bundled dispatcher — it auto-detects the orchestrator and delegates to the
appropriate backend, or falls back to a base-only estimate:

```bash
python3 "$SKILL_ROOT/scripts/estimate_tokens.py" <session_id> --model <model>
# or for the current (latest) session:
python3 "$SKILL_ROOT/scripts/estimate_tokens.py" latest --model <model>
```

Where `$SKILL_ROOT` is the absolute path to the skill folder
(`~/.copilot/skills/session-synthesis`).

The dispatcher:
1. Detects orchestrator (Copilot CLI → uses `estimate_tokens_copilot_cli.py`)
2. Falls back to base-only estimate if no backend exists for the detected orchestrator
3. Reports which backend was used and flags untracked overhead

For **Copilot CLI**, the backend computes five components:

| Component              | Source                                                     |
|------------------------|------------------------------------------------------------|
| Base input tokens      | Stored `user_message` text in `turns` table                |
| Base output tokens     | Stored `assistant_response` text in `turns` table          |
| Context growth         | Conversation history re-sent each turn (cumulative sum)    |
| File overhead          | Files created/edited tracked in `session_files` table      |
| System prompt          | Fixed CLI overhead constant (~3,000 tokens)                |

Use `--json` flag to get machine-readable output for embedding in the MD template.

> ⚠️ **Still untracked** (flagged in script output):
> `web_fetch` results, `bash`/`grep`/`glob` stdout, `view` outputs, injected skill
> context. These are not stored in the session store. The script total is a lower
> bound — actual cost is higher in research-heavy sessions.

If the model is unknown or not listed in `assets/model-pricing.md`, skip cost estimation and
mark it as unavailable rather than guessing. This covers local or custom models.

### Step 5: Prompt User for Missing Fields

Ask the user (use ask_user tool when available):

1. **Outcome**: ✅ Done / ⚠️ Partial / ❌ Failed
2. **Self-rating**: 1–5 (quality of the session / was it efficient?)
3. **Notes**: Any lessons learned, things to do differently, or follow-up actions
4. **Output path**: Where to save (default: `~/copilot-sessions/`)
5. **Model** (if not already resolved in Step 3b)

If the user cannot identify the model, continue without pricing. Keep the token estimate and
render the cost field as `N/A (unknown or custom model)`.

### Step 6: Write the Markdown File

Use the template in `assets/template.md` to render the synthesis.

Populate `{{DURATION}}` from the session timestamps:
- Live mode: compute `updated_at - created_at` for the current session.
- Retrospective mode: compute `updated_at - created_at` from the `sessions` row.
- Format as a short human-readable span such as `12m`, `1h 08m`, or `2h 34m`.

**File naming:**
- One file per session: `<output_path>/YYYY-MM-DD-<slug>.md`
  where `<slug>` is a 3–5 word kebab-case summary of the goal.
- Or append to a monthly file: `<output_path>/YYYY-MM.md` with `---` separator.

Ask the user which format they prefer if not specified.

Ensure the output directory exists before writing:
```bash
mkdir -p <output_path>
```

Then write or append the rendered file.

### Step 7: Confirm

Report the file path written and show a brief preview of the synthesis header.

---

## Token Estimation Details

| Component              | Formula / Source                                           |
|------------------------|------------------------------------------------------------|
| Base input tokens      | `sum(len(user_message chars)) / 4`                         |
| Base output tokens     | `sum(len(assistant_response chars)) / 4`                   |
| Context growth         | Cumulative re-send of prior turns per input call           |
| File overhead          | `session_files` disk sizes / 4                             |
| System prompt          | ~3,000 tokens fixed for Copilot CLI                        |
| Estimated cost         | Computed only when the model matches `assets/model-pricing.md` |

Always mark estimates with `~` prefix (e.g. `~26,000 input tokens`).
Always note whether the estimate is **script-computed (copilot-cli)**, **script-computed (fallback)**, or **base only**.

## Bundled Assets

| Asset | When to load |
|-------|-------------|
| `assets/template.md` | Step 6 — use as the output MD template |
| `assets/model-pricing.md` | Reference — script uses this table for cost calculation |
| `scripts/estimate_tokens.py` | Step 4 — orchestrator dispatcher; run to compute token/cost estimate |
| `scripts/estimate_tokens_copilot_cli.py` | Called automatically by dispatcher for Copilot CLI sessions |

## Extending to New Orchestrators

To add support for a new orchestrator (e.g. Claude Code, VS Code Copilot):

1. Create `scripts/estimate_tokens_<orchestrator>.py` with the same CLI interface:
   `<script> <session_id> [--model <model>] [--json] [--list]`
2. Add a detection entry to the `ORCHESTRATORS` list in `scripts/estimate_tokens.py`
3. The dispatcher will automatically route to it when the orchestrator is detected
