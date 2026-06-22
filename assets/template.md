# Session: {{DATE}} — {{TITLE}}

## Goal
{{GOAL}}

## Approach
{{APPROACH}}

## Key Decisions
{{KEY_DECISIONS}}

## Outcome
{{OUTCOME_EMOJI}} {{OUTCOME_LABEL}}

## Self-Rating
{{RATING_EMOJI}} {{RATING}}/5 — {{RATING_NOTE}}

## Token Usage
- **Model(s)**: {{MODELS}}
  <!-- Format: "<model> (<purpose>[, ~<pct>%])" e.g. "claude-opus-4.5 (implementation, ~90%), claude-sonnet-4.6 (synthesis)" -->
- **Estimate type**: {{ESTIMATE_TYPE}}
  <!-- "OpenCode export telemetry — exact assistant-turn token/cost metadata" -->
  <!-- "Enhanced Copilot CLI estimate — turns + context growth + file overhead + system prompt" -->
  <!-- "Base only — manual estimation, actual cost unknown" -->
- **Input tokens**: {{INPUT_TOKENS}}
- **Output tokens**: {{OUTPUT_TOKENS}}
- **Reasoning tokens**: {{REASONING_TOKENS}}
- **Cache tokens**: {{CACHE_TOKENS}}
- **Total tokens**: {{TOTAL_TOKENS}}
- **Estimated/recorded cost**: {{ESTIMATED_COST}}
- **Model breakdown**: {{MODEL_BREAKDOWN}}
  <!-- OpenCode example: "openrouter/auto: 851,230 tokens, $0.000000, 100.0%" -->
- **Mode breakdown**: {{MODE_BREAKDOWN}}
  <!-- OpenCode example: "plan: 508,967 tokens, 59.8%; build: 342,263 tokens, 40.2%" -->
- **Sub-agents**: {{SUBAGENT_COST}}
  <!-- Omit this line if there are no sub-agents.
       OpenCode example: "4 sub-agent sessions (+$2.59)" -->

## Notes
{{NOTES}}

## Session Context
- **Model(s)**: {{MODELS}}
- **Sub-agents**: {{SUB_AGENTS}}
  <!-- e.g. "rubber-duck (x2), task/explore (x3)" or "none" -->
- **Enabled skills**: {{ENABLED_SKILLS}}
  <!-- e.g. "juju-qa, jdb" or "none" — always exclude session-synthesis itself -->

## Session Metadata
- **Session ID**: {{SESSION_ID}}
- **Repository**: {{REPOSITORY}}
- **Branch**: {{BRANCH}}
- **Duration**: {{DURATION}}
- **Turns**: {{TURN_COUNT}}

---
