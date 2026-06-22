#!/usr/bin/env python3
"""
estimate_tokens.py — Orchestrator-aware token usage estimator for Copilot sessions.

Detects the active orchestrator and delegates to the appropriate backend script,
or falls back to a base-only estimate when no specific backend is available.

Usage:
    python3 estimate_tokens.py <session_id_or_spec> [--model <model>] [--json] [--list]

Supported orchestrators (auto-detected):
    opencode      — OpenCode AI assistant (uses opencode export telemetry)
    copilot-cli   — GitHub Copilot CLI (session-store.db present + recent session)
    [future]      — Add new backends in scripts/estimate_tokens_<orchestrator>.py

Fallback:
    When the orchestrator is unknown or no backend script exists, falls back to
    a base estimate: stored turn text only, without context growth, file overhead,
    or system prompt corrections. Result is marked as "base only (no session store)".
"""

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Optional

# ── Constants ─────────────────────────────────────────────────────────────────

SKILL_ROOT = Path(__file__).resolve().parent.parent
SESSION_STORE_DB = Path.home() / ".copilot" / "session-store.db"
OPENCODE_DB = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
CHARS_PER_TOKEN = 4


# ── Orchestrator detection helpers ─────────────────────────────────────────────

def _opencode_detected() -> bool:
    """Detect OpenCode by a recent matching session or config files."""
    if OPENCODE_DB.exists():
        try:
            conn = sqlite3.connect(OPENCODE_DB)
            row = conn.execute(
                "SELECT id FROM session "
                "WHERE time_created > (unixepoch('now') - 3600) * 1000 "
                "AND directory = ? "
                "ORDER BY time_updated DESC LIMIT 1",
                (str(Path.cwd()),),
            ).fetchone()
            conn.close()
            if row is not None:
                return True
        except Exception:
            pass
    xdg_data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    if (xdg_data / "opencode" / "sessions").exists():
        return True
    xdg_config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    config_locations = [
        Path.cwd() / "opencode.json",
        Path.cwd() / "opencode.jsonc",
        xdg_config / "opencode" / "opencode.json",
        xdg_config / "opencode" / "opencode.jsonc",
    ]
    return any(p.exists() for p in config_locations)


def _copilot_cli_is_active() -> bool:
    """
    Detect Copilot CLI only if the session-store.db exists AND contains a recent
    session (< 1 hour old) with a cwd matching the current working directory.

    This prevents stale DBs from past usage from incorrectly selecting the
    copilot-cli backend when the user is actually in a different orchestrator.
    """
    if not SESSION_STORE_DB.exists():
        return False
    try:
        conn = sqlite3.connect(SESSION_STORE_DB)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id FROM sessions "
            "WHERE datetime(created_at) > datetime('now', '-1 hour') "
            "AND cwd = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (str(Path.cwd()),),
        ).fetchone()
        conn.close()
        return row is not None
    except Exception:
        return False


# ── Orchestrator registry ──────────────────────────────────────────────────────

ORCHESTRATORS = [
    {
        "name": "opencode",
        "description": "OpenCode AI assistant",
        "detect": _opencode_detected,
        "script": SKILL_ROOT / "scripts" / "estimate_tokens_opencode.py",
        "db": OPENCODE_DB,
    },
    {
        "name": "copilot-cli",
        "description": "GitHub Copilot CLI",
        "detect": _copilot_cli_is_active,
        "script": SKILL_ROOT / "scripts" / "estimate_tokens_copilot_cli.py",
        "db": SESSION_STORE_DB,
    },
]


def detect_orchestrator() -> dict | None:
    for orch in ORCHESTRATORS:
        try:
            if orch["detect"]():
                return orch
        except Exception:
            continue
    return None


# ── Fallback base estimator ────────────────────────────────────────────────────

def _pricing_status(model: Optional[str], priced: bool) -> str:
    if priced:
        return f"priced using {model}"
    if model:
        return f"unpriced: unknown model '{model}'"
    return "unpriced: model not provided"


def no_session_store_report(orchestrator_name: str, model: Optional[str]) -> dict:
    """
    Return a report indicating no session store is available.
    Used for orchestrators like OpenCode that have no local DB yet.
    """
    return {
        "session": {
            "id": "N/A",
            "summary": "N/A (no session store)",
            "repository": None,
            "branch": None,
            "created_at": None,
            "updated_at": None,
            "turn_count": None,
        },
        "model": model,
        "orchestrator": orchestrator_name,
        "estimate_type": "base only (no session store) — manual estimation required",
        "breakdown": {
            "base_input_tokens": 0,
            "base_output_tokens": 0,
        },
        "totals": {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "estimated_cost_usd": None,
        },
        "pricing_status": "no tokens to price (no session store available)",
        "untracked_overhead": [
            "ALL tokens (no session store available for this orchestrator)",
            "context growth (conversation history re-sent each turn)",
            "file overhead (created/edited files)",
            "system prompt (orchestrator fixed overhead)",
            "web_fetch results",
            "bash / grep / glob stdout",
            "view / read tool outputs",
            "injected skill context",
        ],
    }


def fallback_estimate(session_spec: str, db_path: Optional[Path], model: Optional[str]) -> dict:
    """
    Base-only estimate: stored turn text divided by CHARS_PER_TOKEN.
    No context growth, file overhead, or system prompt corrections.
    Used when no orchestrator-specific backend is available.
    """
    if db_path is None or not db_path.exists():
        return {"error": f"No session database found at {db_path}"}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    session_id = resolve_session_id(conn, session_spec, db_path)
    if not session_id:
        return {"error": f"Session not found: {session_spec}"}

    session = conn.execute(
        "SELECT id, summary, repository, branch, created_at, updated_at FROM sessions WHERE id = ?",
        (session_id,),
    ).fetchone()

    turns = conn.execute(
        "SELECT user_message, assistant_response FROM turns WHERE session_id = ? ORDER BY turn_index",
        (session_id,),
    ).fetchall()

    input_chars = sum(len(t["user_message"] or "") for t in turns)
    output_chars = sum(len(t["assistant_response"] or "") for t in turns)
    input_tokens = input_chars // CHARS_PER_TOKEN
    output_tokens = output_chars // CHARS_PER_TOKEN
    total_tokens = input_tokens + output_tokens

    cost = compute_cost(input_tokens, output_tokens, model)

    return {
        "session": {
            "id": session["id"],
            "summary": session["summary"],
            "repository": session["repository"],
            "branch": session["branch"],
            "created_at": session["created_at"],
            "updated_at": session["updated_at"],
            "turn_count": len(turns),
        },
        "model": model,
        "orchestrator": "unknown (fallback)",
        "estimate_type": "base only — stored turn text only, no tool/context overhead",
        "breakdown": {
            "base_input_tokens":  input_tokens,
            "base_output_tokens": output_tokens,
        },
        "totals": {
            "input_tokens":       input_tokens,
            "output_tokens":      output_tokens,
            "total_tokens":       total_tokens,
            "estimated_cost_usd": round(cost, 4) if cost is not None else None,
        },
        "pricing_status": _pricing_status(model, cost is not None),
        "untracked_overhead": [
            "context growth (conversation history re-sent each turn)",
            "file overhead (created/edited files)",
            "system prompt (orchestrator fixed overhead)",
            "web_fetch results",
            "bash / grep / glob stdout",
            "view / read tool outputs",
            "injected skill context",
        ],
    }


def resolve_session_id(conn, spec: str, db_path: Path) -> str | None:
    if spec == "latest":
        row = conn.execute(
            "SELECT id FROM sessions ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        return row["id"] if row else None
    if len(spec) == 10 and spec[4] == "-" and spec[7] == "-":
        row = conn.execute(
            "SELECT id FROM sessions WHERE date(created_at) = ? ORDER BY created_at DESC LIMIT 1",
            (spec,),
        ).fetchone()
        return row["id"] if row else None
    row = conn.execute("SELECT id FROM sessions WHERE id = ?", (spec,)).fetchone()
    return row["id"] if row else None


def list_sessions(db_path: Path) -> None:
    if not db_path.exists():
        sys.exit(f"No session database at {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, summary, repository, created_at FROM sessions ORDER BY created_at DESC LIMIT 10"
    ).fetchall()
    if not rows:
        print("No sessions found.")
        return
    print(f"{'ID':<38}  {'Date':<20}  {'Repository':<30}  Summary")
    print("-" * 120)
    for r in rows:
        date = r["created_at"][:19]
        repo = (r["repository"] or "—")[:30]
        summary = (r["summary"] or "—")[:60]
        print(f"{r['id']:<38}  {date:<20}  {repo:<30}  {summary}")


def _get_pricing(model: Optional[str]) -> Optional[tuple[float, float]]:
    if not model:
        return None
    MODEL_PRICING = {
        "claude-opus-4.6":   (15.00, 75.00),
        "claude-opus-4.5":   (15.00, 75.00),
        "claude-sonnet-4.6": (3.00,  15.00),
        "claude-sonnet-4.5": (3.00,  15.00),
        "claude-haiku-4.5":  (0.80,  4.00),
        "gpt-5.4":           (2.00,  8.00),
        "gpt-5.2":           (1.50,  6.00),
        "gpt-5.3-codex":     (2.00,  8.00),
        "gpt-5.2-codex":     (1.50,  6.00),
        "gpt-5.4-mini":      (0.15,  0.60),
        "gpt-5-mini":        (0.15,  0.60),
        "gpt-4.1":           (2.00,  8.00),
    }
    return MODEL_PRICING.get(model)


def compute_cost(input_tokens: int, output_tokens: int, model: Optional[str]) -> Optional[float]:
    pricing = _get_pricing(model)
    if pricing is None:
        return None
    return (input_tokens / 1_000_000 * pricing[0]) + (output_tokens / 1_000_000 * pricing[1])


# ── Delegate to backend script ─────────────────────────────────────────────────

def delegate_to_backend(script_path: Path, session_spec: Optional[str], model: Optional[str], as_json: bool, include_subagents: bool = False, list_only: bool = False, openrouter_api_key: Optional[str] = None) -> None:
    """Load and run an orchestrator-specific backend as a subprocess."""
    import subprocess
    cmd = [sys.executable, str(script_path)]
    if list_only:
        cmd.append("--list")
    elif session_spec:
        cmd.append(session_spec)
    if model:
        cmd.extend(["--model", model])
    if as_json:
        cmd.append("--json")
    if include_subagents:
        cmd.append("--include-subagents")
    if openrouter_api_key:
        cmd.extend(["--openrouter-api-key", openrouter_api_key])
    result = subprocess.run(cmd)
    sys.exit(result.returncode)


def print_fallback_report(report: dict) -> None:
    if "error" in report:
        print(f"Error: {report['error']}", file=sys.stderr)
        sys.exit(1)
    s = report["session"]
    t = report["totals"]
    b = report["breakdown"]
    print()
    print(f"{'─' * 60}")
    print(f"  Session: {s['summary'] or s['id'] or '—'}")
    print(f"  Repo:    {s['repository'] or '—'}  |  Turns: {'—' if s['turn_count'] is None else s['turn_count']}")
    print(f"  Date:    {(s['created_at'] or '—')[:19]}")
    print(f"  Model:   {report['model'] or 'unknown'}")
    print(f"  Orchestrator: {report['orchestrator']}")
    print(f"{'─' * 60}")
    print(f"  Token Breakdown  [{report['estimate_type']}]")
    print(f"    Base input (turns)   {b['base_input_tokens']:>10,}")
    print(f"    Base output (turns)  {b['base_output_tokens']:>10,}")
    print(f"    ─────────────────────────────────")
    print(f"    Total input          {t['input_tokens']:>10,}")
    print(f"    Total output         {t['output_tokens']:>10,}")
    print(f"    Total                {t['total_tokens']:>10,}")
    print(f"{'─' * 60}")
    if t["estimated_cost_usd"] is None:
        print(f"  Estimated cost:  skipped ({report['pricing_status']})")
    else:
        print(f"  Estimated cost:  ~${t['estimated_cost_usd']:.4f}")
    print(f"{'─' * 60}")
    print(f"  ⚠️  Untracked (actual cost is higher):")
    for item in report["untracked_overhead"]:
        print(f"      • {item}")
    print()


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Orchestrator-aware Copilot session token estimator."
    )
    parser.add_argument("session", nargs="?", help="Session ID, 'latest', or YYYY-MM-DD")
    parser.add_argument("--model",
                        help="Model name for cost calculation; omitted or unknown models skip pricing")
    parser.add_argument("--list", action="store_true", help="List recent sessions")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--orchestrator", help="Force a specific orchestrator name")
    parser.add_argument("--include-subagents", action="store_true",
                        help="Include related sessions (same project, overlapping time) in cost rollup (OpenCode only)")
    parser.add_argument("--openrouter-api-key",
                        help="OpenRouter API key for per-model pricing from GET /api/v1/models")
    args = parser.parse_args()

    orch = detect_orchestrator()
    if args.orchestrator:
        orch = next((o for o in ORCHESTRATORS if o["name"] == args.orchestrator), None)

    db_path = orch["db"] if orch and orch.get("db") else (SESSION_STORE_DB if orch is None else None)

    if args.list:
        if orch and orch.get("script") and orch["script"].exists():
            print(f"[session-synthesis] Using backend: {orch['name']}", file=sys.stderr)
            delegate_to_backend(orch["script"], None, None, args.json, list_only=True, include_subagents=args.include_subagents, openrouter_api_key=args.openrouter_api_key)
        if db_path and db_path.exists():
            list_sessions(db_path)
        elif SESSION_STORE_DB.exists():
            orch_name = orch["name"] if orch else "unknown"
            print(f"[session-synthesis] Orchestrator '{orch_name}' has no session DB; "
                  f"listing from Copilot CLI store instead.", file=sys.stderr)
            print(f"  Tip: use --orchestrator copilot-cli to force.", file=sys.stderr)
            list_sessions(SESSION_STORE_DB)
        else:
            orch_name = orch["name"] if orch else "unknown"
            print(f"No session database available for orchestrator '{orch_name}'.", file=sys.stderr)
            sys.exit(1)
        return

    if not args.session:
        # Print detected orchestrator and exit
        print(f"Detected orchestrator: {orch['name'] if orch else 'unknown (will use fallback)'}")
        available = [o["name"] for o in ORCHESTRATORS if o.get("script") and o["script"].exists()]
        print(f"Available backends:    {', '.join(available) or 'none'}")
        parser.print_help()
        sys.exit(1)

    if orch and orch.get("script") and orch["script"].exists():
        print(f"[session-synthesis] Using backend: {orch['name']}", file=sys.stderr)
        delegate_to_backend(orch["script"], args.session, args.model, args.json, include_subagents=args.include_subagents, openrouter_api_key=args.openrouter_api_key)
    elif orch and not orch.get("db"):
        # Orchestrator detected but has no session store (e.g. OpenCode)
        name = orch["name"]
        print(f"[session-synthesis] Orchestrator '{name}' detected — no session store available, using base-only report.", file=sys.stderr)
        report = no_session_store_report(name, args.model)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print_fallback_report(report)
    else:
        name = orch["name"] if orch else "unknown"
        print(f"[session-synthesis] No backend for orchestrator '{name}', using fallback estimator.", file=sys.stderr)
        report = fallback_estimate(args.session, db_path, args.model)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print_fallback_report(report)


if __name__ == "__main__":
    main()
