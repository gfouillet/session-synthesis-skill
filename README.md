# session-synthesis

A GitHub Copilot CLI skill that turns a Copilot session into a structured Markdown report.

It supports:

- **Live mode** for the current session
- **Retrospective mode** for past sessions reconstructed from the session store
- Token estimation with bundled scripts
- Optional cost estimation when the model matches the bundled pricing table
- Session metadata capture such as repository, branch, duration, sub-agents, and enabled skills

## What it produces

The skill renders a Markdown summary with:

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
| `scripts/estimate_tokens_copilot_cli.py` | Copilot CLI-specific token estimator |

## How it works

1. Identify the target session.
2. Gather checkpoints and/or turns.
3. Draft a synthesis of the session.
4. Detect the orchestrator and model.
5. Estimate token usage.
6. Ask for any missing fields such as outcome, rating, notes, and output path.
7. Render the final Markdown report.

## Token estimation

For Copilot CLI sessions, the bundled backend estimates:

- base input tokens
- base output tokens
- context growth across turns
- file overhead from tracked edited/created files
- fixed system prompt overhead

This estimate is still a **lower bound**. Tool outputs such as `web_fetch`, `bash`, `glob`, and `view` are not fully captured from the session store.

## Cost estimation

Cost is only computed when the model name matches an entry in `assets/model-pricing.md`.

If the model is unknown, custom, or local:

- token estimation still runs
- cost is skipped
- the rendered report should use a value like `N/A (unknown or custom model)`

## Requirements

- GitHub Copilot CLI with skill support
- Access to the Copilot session store for retrospective mode
- Python 3 for the bundled estimation scripts

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

If the model is omitted or unknown, pricing is skipped automatically.

## Installation

Place this skill in your Copilot skills directory as:

```text
~/.copilot/skills/session-synthesis/
```

The directory should contain `SKILL.md`, `assets/`, and `scripts/`.

## Limitations

- Retrospective synthesis quality depends on what was stored in the session store.
- Token and cost figures are estimates, not provider-billed ground truth.
- Only Copilot CLI currently has a dedicated estimator backend.

## Extending

To support another orchestrator:

1. Add a new `scripts/estimate_tokens_<orchestrator>.py` backend.
2. Register it in `ORCHESTRATORS` inside `scripts/estimate_tokens.py`.
3. Keep the same CLI interface so the dispatcher can route to it.
