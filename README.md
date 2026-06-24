# session-synthesis

A session synthesis skill that turns an AI coding assistant session into a structured Markdown report.

It supports:

- **Live mode** for the current session
- **Retrospective mode** for past sessions reconstructed from session store/export data
- **Client auto-detection**: OpenCode, Warp, Copilot CLI (reported in the `Client` field)
- Token and cost reporting with bundled scripts
- OpenCode export telemetry with per-model and per-mode breakdowns
- Warp token usage from `warp.sqlite` with OpenRouter pricing for custom-inference models
- Optional manual cost estimation when fallback models match the bundled pricing table
- Session metadata capture such as repository, branch, duration, sub-agents, and enabled skills

## What it produces

The skill renders a Markdown summary with:

- **client** (auto-detected: OpenCode, Warp, Copilot CLI)
- goal
- approach
- key decisions
- outcome
- self-rating
- token usage
- notes
- session metadata

The default output format is driven by `assets/template.md`.

## Files

| Path | Purpose |
|------|---------|
| `SKILL.md` | Main skill instructions |
| `assets/template.md` | Markdown template used for the final report |
| `assets/model-pricing.md` | Pricing reference for supported models |
| `scripts/estimate_tokens.py` | Orchestrator-aware dispatcher |
| `scripts/estimate_tokens_opencode.py` | OpenCode export telemetry parser |
| `scripts/estimate_tokens_copilot_cli.py` | Copilot CLI-specific token estimator |
| `scripts/estimate_tokens_warp.py` | Warp terminal token reporter + OpenRouter pricing |
| `scripts/openrouter_pricing.py` | Shared OpenRouter API pricing helpers |

## How it works

1. Identify the target session.
2. Gather checkpoints and/or turns.
3. Draft a synthesis of the session.
4. Detect the orchestrator and model.
5. Estimate token usage.
6. Ask for any missing fields such as outcome, rating, notes, and output path.
7. Render the final Markdown report.

## Token estimation

For OpenCode sessions, the bundled backend runs `opencode export <session_id>` with
stdout redirected to a temporary JSON file, then parses the export. This avoids the
OpenCode export pipe bug where `opencode export <session_id> | jq` can fail.

The OpenCode backend reports:

- input tokens
- output tokens
- reasoning tokens
- cache read/write tokens
- recorded cost
- model/provider breakdown
- mode breakdown such as `plan` and `build`

For Copilot CLI sessions, the bundled backend estimates:

- base input tokens
- base output tokens
- context growth across turns
- file overhead from tracked edited/created files
- fixed system prompt overhead

The Copilot CLI estimate is still a **lower bound**. Tool outputs such as `web_fetch`, `bash`, `glob`, and `view` are not fully captured from the session store.

For Warp sessions, the bundled backend reports:

- per-model token totals from `conversation_usage_metadata.token_usage`
- tokens by source: `custom_endpoint_tokens` (OpenRouter), `warp_tokens` (Oz), `byok_tokens`
- category breakdown (e.g. `primary_agent`, `tool_summarization`)
- approximate OpenRouter cost via display-name → slug mapping + assumed input/output split

Warp cost is **approximate** because the DB stores display names (not slugs) and single token totals (no input/output split). The report always states the assumptions.

## Cost estimation

OpenCode cost is read from export metadata and grouped per model/mode. Manual cost
estimation is only used for fallback paths when the model name matches an entry in
`assets/model-pricing.md`.

If the model is unknown, custom, local, or no cost is present:

- token reporting still runs
- manual cost is skipped
- the rendered report should use a value like `N/A (unknown or custom model)` or the recorded OpenCode value

## Requirements

- OpenCode, Warp, or GitHub Copilot CLI for session data
- Access to the relevant session store/export data for retrospective mode
- Python 3 for the bundled estimation scripts
- `OPENROUTER_API_KEY` env var (or `--openrouter-api-key` flag) for Warp OpenRouter pricing

## Example prompts

- `synthesize this session`
- `save session notes`
- `wrap up session`
- `log last session`
- `synthesize session from 2026-05-10`
- `retrospective on auth refactor`

## Script usage

List recent sessions:

```bash
python3 scripts/estimate_tokens.py --list
```

Estimate the latest session:

```bash
python3 scripts/estimate_tokens.py latest --model gpt-5.4
```

Estimate a specific session as JSON:

```bash
python3 scripts/estimate_tokens.py <session-id> --model claude-sonnet-4.6 --json
```

Run the OpenCode backend directly:

```bash
python3 scripts/estimate_tokens_opencode.py latest --json
python3 scripts/estimate_tokens_opencode.py <session-id> --json
python3 scripts/estimate_tokens_opencode.py --list
```

Run the Warp backend directly:

```bash
python3 scripts/estimate_tokens_warp.py --list
python3 scripts/estimate_tokens_warp.py latest --json
python3 scripts/estimate_tokens_warp.py latest --json --openrouter-api-key "$OPENROUTER_API_KEY"
```

For OpenCode, model and cost metadata comes from the export. If the model is omitted
or unknown in fallback paths, pricing is skipped automatically.

## Installation

Place this skill in your assistant skills directory, for example:

```text
~/.agents/skills/session-synthesis/
~/.copilot/skills/session-synthesis/
```

The directory should contain `SKILL.md`, `assets/`, and `scripts/`.

## Limitations

- Retrospective synthesis quality depends on what was stored in the session store.
- OpenCode cost figures are the values recorded in export metadata, not independently verified provider invoices.
- Copilot CLI and fallback token/cost figures are estimates, not provider-billed ground truth.
- Warp cost is approximate: display-name → slug mapping is best-effort, and the assumed input/output split defaults to 80/20.
- Warp-managed (Oz) tokens are reported but never priced.

## Extending

To support another orchestrator:

1. Add a new `scripts/estimate_tokens_<orchestrator>.py` backend.
2. Register it in `ORCHESTRATORS` inside `scripts/estimate_tokens.py`.
3. Keep the same CLI interface so the dispatcher can route to it.
