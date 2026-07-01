---
name: session-synthesis
description: 'Synthesize and save an OpenCode or Copilot coding session as a structured
  Markdown file. Use when asked to "synthesize this session", "save session notes",
  "log this session", "summarize what we did", "wrap up session", "track session
  cost", "record session outcome", "log token usage", "retrospective on <topic>",
  "log last session", or "synthesize session from <date>". Operates in two modes:
  live (current session, full context available) or retrospective (past session
  reconstructed from session store/export data). Uses OpenCode export telemetry for
  precise multi-model token/cost reporting when available, prompts for self-rating
  and notes, and writes a structured MD report to a configurable output directory.'
---

# Session Synthesis

Captures a structured summary of an AI coding assistant session — goal, approach,
outcome, token usage, model cost, and personal notes — and appends it to a
Markdown log file for later efficiency analysis.

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
- If the user provided a session id, date, or keyword, query the relevant session store.

For OpenCode, prefer the bundled list command or query `~/.local/share/opencode/opencode.db`:

```bash
python3 "$SKILL_ROOT/scripts/estimate_tokens_opencode.py" --list
```

```sql
-- Most recent OpenCode sessions
SELECT id, title, directory, time_created, time_updated, model, cost,
       tokens_input, tokens_output, tokens_reasoning,
       tokens_cache_read, tokens_cache_write
FROM session
ORDER BY time_updated DESC
LIMIT 10;
```

For Copilot CLI:

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

**From session store/export (retrospective) or current context (live):**

For OpenCode, use `opencode export <session_id> > <json-file>` and read the exported
`messages` array. User text appears in user message parts; assistant responses,
reasoning, tool calls, token usage, mode, model, provider, and cost appear in
assistant message `info` and `parts`.

For Copilot CLI:

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

Note the branch name from the session metadata — it will be used in Step 4 to
infer whether the session is part of a larger feature.

Present the draft summary to the user for confirmation or edits before proceeding.

### Step 3b: Detect Orchestrator & Extract Session Metadata

**Detect orchestrator** before running the estimation script. The dispatcher
`scripts/estimate_tokens.py` auto-detects using the heuristics below, but its result
is a **suggestion only**. In interactive (live) mode, always confirm with the user
before proceeding (see below).

**Detection heuristics** (checked in this order — first match wins):

| Priority | Signal | Orchestrator |
|----------|--------|-------------|
| 1 | `~/.local/share/opencode/opencode.db` exists **with a recent session (<1h, matching cwd)** | **opencode** |
| 2 | `~/.local/share/opencode/sessions/` or `$XDG_DATA_HOME/opencode/sessions/` exists | **opencode** |
| 2 | `opencode.json` or `opencode.jsonc` in cwd or `~/.config/opencode/` | **opencode** (confirmation) |
| 3 | `~/.copilot/session-store.db` exists **with a recent session (<1h, matching cwd)** | **copilot-cli** |
| 4 | `~/.claude/projects/` exists | claude-code (planned — not yet registered in dispatcher) |
| — | Otherwise | unknown — use fallback estimator |

> **Note for OpenCode:** Token and cost reporting uses `opencode export`
> through `scripts/estimate_tokens_opencode.py`. The script redirects export output
> into a temporary JSON file before parsing because piping OpenCode export directly
> into another command can fail.

> **Staleness guard:** A stale `session-store.db` from past Copilot CLI usage does
> NOT qualify. The DB must contain a session created within the last hour whose `cwd`
> matches the current working directory. Otherwise, detection falls through.

> **Confirm with user (live mode):** Detection is heuristic-based and unreliable on
> machines with multiple tools installed. Present the detected orchestrator and ask
> the user to confirm or correct it:
>
> > Detected orchestrator: **<name>** (based on <signal>).
> > Is this correct, or are you running in a different tool?
>
> If the user corrects it, use their answer. Pass `--orchestrator <name>` to the
> script to override auto-detection.

**Extract model name:**
- Live mode: check session metadata or the `model_information` block in system context.
  Note: in OpenCode, the system prompt declares the *current* model which may differ
  from the model used for the bulk of the session (users switch models mid-session).
  Model resolution here is preliminary — Step 5 always confirms with the user.
- Retrospective: query `turns` or `checkpoints`; model may appear in assistant responses
  or session summary. Fall back to asking the user if ambiguous.

**Extract sub-agents invoked:**

For OpenCode, use `--include-subagents` in Step 5 to automatically detect
sub-agent sessions via the `parent_id` column in the session table
(direct children in the session hierarchy, any agent type).
Their costs and tokens are rolled up into the report and listed individually.
See Step 5 for details.

For Copilot CLI (fallback), use:

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
List skill names, e.g. `"juju-qa, jdb"`. Use `"none"` if none found.

> **Important:** Exclude `session-synthesis` itself from the listed skills. It is
> always implicitly active during synthesis and listing it adds no signal.

### Step 4: Prompt User for Missing Fields

Ask the user (use ask_user tool when available):

1. **Outcome**: ✅ Done / ⚠️ Partial / ❌ Failed
2. **Session context**: 🎯 One-shot (standalone task) or 🏗️ Part of a **larger feature**?
   - If it looks like a larger feature (infer from the branch name extracted in Step 3),
     ask: "Branch is `<branch>`. Is this session part of a larger feature? If so, do you have
     a reference? (JIRA ticket, GitHub issue/PR)"
   - Use `question` tool with a custom option for the user to provide the reference.
   - If no branch info is available, still ask: "Is this a one-shot or part of a larger
     feature? If larger, got a reference?"
3. **Task size**: Which bucket best fits this session?
   - 🪶 **XS** — minutes, trivial change (typo, one-line fix)
   - 🌱 **S** — <1 hour, small fix
   - 🌿 **M** — 1–3 hours, moderate feature
   - 🌳 **L** — 3–8 hours, significant feature
   - 🏔️ **XL** — 1–3 days, major feature
   - 🌌 **XXL** — multi-day, complex initiative
4. **Self-rating**: 1–5 (quality of the session / was it efficient?)
   - 5 → 🏆  |  4 → 🟢  |  3 → 🟡  |  2 → 🟠  |  1 → 🔴
5. **Notes**: Any lessons learned, things to do differently, or follow-up actions
6. **Output path**: Where to save (default: `~/copilot-sessions/`)
7. **Model(s)**:
   - For OpenCode sessions, do not ask the user to estimate model percentages before
     token reporting. `opencode export` records the model/provider, mode, tokens, cache,
     reasoning, and cost per assistant message. Use that telemetry as the source of truth.
   - For Copilot CLI or fallback sessions, ask for model(s) because the local store does
     not reliably preserve per-turn model telemetry. Pre-fill with the model from system
     context as default. Format the question as:

   > Model(s) used during this session. Default: `<detected model> (primary)`.
   > If you switched models, list each with its purpose, e.g.:
   > `claude-opus-4.5 (implementation), claude-sonnet-4.6 (synthesis)`
   >
   > Note: the default is the model currently active. If a different model was used
   > for the bulk of the work, specify that as primary.

   Each non-OpenCode model entry should follow the format: `<model-name> (<purpose>[, ~<percentage>%])`.

**Multi-model cost computation:**
- For OpenCode, use the per-message costs and grouped model breakdown from the export.
  Do not recompute costs from `assets/model-pricing.md` unless the export lacks costs
  and the user explicitly asks for a manual estimate.
- For Copilot CLI/fallback sessions, if the user specifies multiple models with
  percentages, compute a weighted cost using `assets/model-pricing.md`.
- If no percentages are given for non-OpenCode multi-model sessions, report the cost
  range or ask the user for an approximate split.
- If a model is unknown/custom, skip its cost portion and note it in the report.

If the model cannot be identified and no orchestrator telemetry is available, continue
without pricing. Keep the token estimate and render the cost field as
`N/A (unknown or custom model)`.

### Step 5: Estimate Token Usage

Run the bundled dispatcher after confirming the orchestrator. Use `--json` when you
need machine-readable output for the Markdown template.

```bash
python3 "$SKILL_ROOT/scripts/estimate_tokens.py" <session_id> --json
# or for the current/latest session:
python3 "$SKILL_ROOT/scripts/estimate_tokens.py" latest --json
```

Where `$SKILL_ROOT` is the absolute path to wherever this skill is installed
(e.g. `~/.agents/skills/session-synthesis` or `~/.copilot/skills/session-synthesis`).

**For OpenCode sessions:**

The dispatcher routes to `scripts/estimate_tokens_opencode.py`, which runs:

```bash
opencode export <session_id> > <temporary-json-file>
```

This redirection is intentional. Do not pipe `opencode export <session_id>` directly
into `jq`, Python, or another process; OpenCode export can fail unless stdout is a
regular file. The backend then parses that JSON file and reports:

- exact input, output, reasoning, cache-read, and cache-write tokens from assistant turns
- recorded OpenCode cost from per-message telemetry
- per-model/provider breakdown for multi-model sessions
- per-mode breakdown such as `plan` vs `build`
- session metadata from the export (`title`, directory, timestamps, OpenCode version)

**OpenRouter pricing via API (recommended):** Pass `--openrouter-api-key <KEY>` to
fetch live per-token pricing from `GET https://openrouter.ai/api/v1/models` and
recompute costs for any OpenRouter models found in the export. The key is
auto-detected from `OPENROUTER_API_KEY` env var or `auth.json` if available. The
pricing is cached on disk (`/tmp/openrouter_pricing_cache.json`, 1-hour TTL) to
avoid repeated API calls.

When OpenRouter pricing is active:
- Costs for `openrouter/` provider messages are computed as: `(input_tokens ×
  prompt_price) + (output_tokens × completion_price) + (cache_read ×
  cache_read_price) + (cache_write × cache_write_price)`
- Non-OpenRouter models (opencode free tier, github-copilot) keep their export cost
- The `estimate_type` and `pricing_status` fields note that OpenRouter models used
  API-based pricing

```bash
python3 "$SKILL_ROOT/scripts/estimate_tokens_opencode.py" <session_id> --json --openrouter-api-key "$OPENROUTER_API_KEY"
```

You can also pass via the dispatcher:

```bash
python3 "$SKILL_ROOT/scripts/estimate_tokens.py" <session_id> --json --openrouter-api-key "$OPENROUTER_API_KEY"
```

**Sub-agent rollup (OpenCode only):** Pass `--include-subagents`
to automatically detect sub-agent sessions and roll up their costs and tokens.
The script queries the DB for sessions whose ``parent_id`` points to the main
session (direct children in the session hierarchy), without restricting by
agent type. Each related session is listed individually in the report's
``subagent_sessions`` array. OpenRouter pricing applies to OpenRouter sub-agents
too when ``--openrouter-api-key`` is provided.

```bash
# Main session priced via OpenRouter API, sub-agents too:
python3 "$SKILL_ROOT/scripts/estimate_tokens_opencode.py" <session_id> --json --include-subagents --openrouter-api-key "$OPENROUTER_API_KEY"
```

This is important because the `opencode export` only covers the main session —
sub-agent costs (often significant, e.g. 4x $0.50-1.00 for PR reviews) are stored
as separate DB rows and would otherwise be missed.

For OpenCode multi-model sessions, prefer the script's `model_breakdown` and
`mode_breakdown` over user-estimated percentages. If `estimated_cost_usd` is `0`,
report it as the value recorded by OpenCode and note that some providers/routes may
not return priced cost metadata.

You can also run the OpenCode backend directly:

```bash
python3 "$SKILL_ROOT/scripts/estimate_tokens_opencode.py" <session_id> --json
python3 "$SKILL_ROOT/scripts/estimate_tokens_opencode.py" <session_id> --json --include-subagents
python3 "$SKILL_ROOT/scripts/estimate_tokens_opencode.py" latest --json
python3 "$SKILL_ROOT/scripts/estimate_tokens_opencode.py" --list
```

**For Copilot CLI sessions:**

Pass the confirmed model to `--model` for single-model pricing:

```bash
python3 "$SKILL_ROOT/scripts/estimate_tokens.py" <session_id> --model <confirmed_model> --json
```

For Copilot CLI multi-model sessions, omit `--model` to get token totals without
pricing. Then compute weighted cost manually using the rates from
`assets/model-pricing.md`, splitting according to the percentages confirmed in Step 4.

The dispatcher:
1. Detects orchestrator (OpenCode → `estimate_tokens_opencode.py`; Copilot CLI → `estimate_tokens_copilot_cli.py`)
2. Falls back to base-only estimate if no backend exists for the detected orchestrator
3. Reports which backend was used and flags untracked overhead where applicable

For **Copilot CLI**, the backend computes five components:

| Component              | Source                                                     |
|------------------------|------------------------------------------------------------|
| Base input tokens      | Stored `user_message` text in `turns` table                |
| Base output tokens     | Stored `assistant_response` text in `turns` table          |
| Context growth         | Conversation history re-sent each turn (cumulative sum)    |
| File overhead          | Files created/edited tracked in `session_files` table      |
| System prompt          | Fixed CLI overhead constant (~3,000 tokens)                |

> ⚠️ **Still untracked for Copilot CLI/fallback estimates** (flagged in script output):
> `web_fetch` results, `bash`/`grep`/`glob` stdout, `view` outputs, injected skill
> context. These are not stored in the Copilot session store. The script total is a
> lower bound — actual cost is higher in research-heavy sessions.

If the model is unknown or not listed in `assets/model-pricing.md`, skip manual cost
estimation and mark it as unavailable rather than guessing. This covers local or
custom models.

### Step 5b: Check for Tokenscope Analysis (Optional Cross-Check)

For OpenCode, `opencode export` is the primary source because it contains per-turn
model, token, cache, reasoning, and cost metadata. If the user has run `/tokenscope`,
use its output only as an optional cross-check or explanatory supplement for tool-heavy
sessions.

If Tokenscope output exists, parse it for:

- token breakdown by category (system/user/tools/assistant/reasoning)
- tool usage statistics and top token contributors
- cache efficiency metrics
- subagent breakdown (if `includeSubagents` was true)

Do not replace OpenCode export totals with Tokenscope totals unless the export is
missing telemetry or the user explicitly asks for the Tokenscope view.

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
| OpenCode input/output/reasoning/cache | `opencode export` assistant message telemetry |
| OpenCode model split   | Group exported assistant turns by `providerID` + `modelID` |
| OpenCode mode split    | Group exported assistant turns by `mode` / `agent`         |
| OpenCode cost          | Sum exported assistant message `cost` values (or recomputed from OpenRouter API pricing when `--openrouter-api-key` is used) |
| OpenCode sub-agent rollup | `session` DB rows where `parent_id` = main session ID (direct children, any agent type) — rolled up via `--include-subagents` |
| OpenRouter API pricing | `GET /api/v1/models` returns per-token `prompt`, `completion`, `input_cache_read`, `input_cache_write` rates. Cost = Σ(tokens × rate). Cached at `/tmp/openrouter_pricing_cache.json` (1-hour TTL). |
| Copilot base input     | `sum(len(user_message chars)) / 4`                         |
| Copilot base output    | `sum(len(assistant_response chars)) / 4`                   |
| Copilot context growth | Cumulative re-send of prior turns per input call           |
| Copilot file overhead  | `session_files` disk sizes / 4                             |
| Copilot system prompt  | ~3,000 tokens fixed for Copilot CLI                        |
| Manual estimated cost  | Computed only when the model matches `assets/model-pricing.md` |

For OpenCode, treat the export values as recorded telemetry, not estimates.
Without `--include-subagents`, the cost covers only the main session and
under-reports total spend when sub-agents were used (e.g. parallel PR review
lenses). With `--include-subagents`, the cost includes sub-agent sessions
queried from the DB and is much closer to the OpenRouter billing total.

With `--openrouter-api-key`, OpenRouter model costs are recomputed from
per-token API pricing instead of relying on the export/DB cost, giving an
independent and auditable cost estimate. Costs for non-OpenRouter providers
(github-copilot, opencode free tier) still use the export/DB values.

For Copilot CLI and fallback estimates, mark estimates with `~` prefix (e.g. `~26,000
input tokens`). Always note whether the estimate is **opencode export telemetry**,
**opencode export + DB sub-agent rollup**, **script-computed (copilot-cli)**,
**script-computed (fallback)**, or **base only**.

## Bundled Assets

| Asset | When to load |
|-------|-------------|
| `assets/template.md` | Step 6 — use as the output MD template |
| `assets/model-pricing.md` | Reference — script uses this table for cost calculation |
| `scripts/estimate_tokens.py` | Step 5 — orchestrator dispatcher; run to compute token/cost estimate |
| `scripts/estimate_tokens_opencode.py` | Called automatically by dispatcher for OpenCode export telemetry; pass `--include-subagents` to roll up costs from sub-agent sessions |
| `scripts/estimate_tokens_copilot_cli.py` | Called automatically by dispatcher for Copilot CLI sessions |

## Extending to New Orchestrators

To add support for a new orchestrator (e.g. Claude Code, VS Code Copilot):

1. Create `scripts/estimate_tokens_<orchestrator>.py` with the same CLI interface:
   `<script> <session_id> [--model <model>] [--json] [--list]`
2. Add a detection entry to the `ORCHESTRATORS` list in `scripts/estimate_tokens.py`
3. The dispatcher will automatically route to it when the orchestrator is detected
