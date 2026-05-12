#!/usr/bin/env python3
"""
estimate_tokens_copilot_cli.py — Token usage estimator for Copilot CLI sessions.

Usage:
    python3 estimate_tokens_copilot_cli.py <session_id>
    python3 estimate_tokens_copilot_cli.py latest
    python3 estimate_tokens_copilot_cli.py YYYY-MM-DD          # most recent session on that date
    python3 estimate_tokens_copilot_cli.py --list              # show 10 most recent sessions
    python3 estimate_tokens_copilot_cli.py <id> --model claude-opus-4.6
    python3 estimate_tokens_copilot_cli.py <id> --json         # machine-readable output

What is estimated:
    1. Base turns     — stored user_message + assistant_response text
    2. Context growth — conversation history is re-sent each turn (O(n²) growth)
    3. File overhead  — created/edited files contribute to context via tool output
    4. System prompt  — fixed CLI overhead (~3,000 tokens)

What is NOT captured (flagged as untracked):
    - web_fetch results
    - bash / grep / glob stdout
    - view / read tool outputs
    - Injected skill context at session start

These can dominate in research-heavy sessions. Always treat the total as a lower bound.
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Optional

# --- Constants ---

SESSION_STORE_DB = Path.home() / ".copilot" / "session-store.db"
CHARS_PER_TOKEN = 4
SYSTEM_PROMPT_TOKENS = 3_000  # Fixed Copilot CLI system prompt overhead

# Pricing table: (input $/1M, output $/1M)
MODEL_PRICING = {
    "claude-opus-4.6":     (15.00, 75.00),
    "claude-opus-4.5":     (15.00, 75.00),
    "claude-sonnet-4.6":   (3.00,  15.00),
    "claude-sonnet-4.5":   (3.00,  15.00),
    "claude-haiku-4.5":    (0.80,  4.00),
    "gpt-5.4":             (2.00,  8.00),
    "gpt-5.2":             (1.50,  6.00),
    "gpt-5.3-codex":       (2.00,  8.00),
    "gpt-5.2-codex":       (1.50,  6.00),
    "gpt-5.4-mini":        (0.15,  0.60),
    "gpt-5-mini":          (0.15,  0.60),
    "gpt-4.1":             (2.00,  8.00),
}


# --- DB helpers ---

def connect(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        sys.exit(f"Error: session store not found at {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def resolve_session_id(conn: sqlite3.Connection, spec: str) -> str:
    """Resolve 'latest', a date string, or a raw session id."""
    if spec == "latest":
        row = conn.execute(
            "SELECT id FROM sessions ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            sys.exit("Error: no sessions found in session store.")
        return row["id"]

    # Date: YYYY-MM-DD
    if len(spec) == 10 and spec[4] == "-" and spec[7] == "-":
        row = conn.execute(
            "SELECT id FROM sessions WHERE date(created_at) = ? ORDER BY created_at DESC LIMIT 1",
            (spec,),
        ).fetchone()
        if not row:
            sys.exit(f"Error: no session found on date {spec}.")
        return row["id"]

    # Assume raw session id
    row = conn.execute("SELECT id FROM sessions WHERE id = ?", (spec,)).fetchone()
    if not row:
        sys.exit(f"Error: session '{spec}' not found.")
    return row["id"]


def list_sessions(conn: sqlite3.Connection) -> None:
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


# --- Estimation ---

def pricing_status(model: Optional[str], priced: bool) -> str:
    if priced:
        return f"priced using {model}"
    if model:
        return f"unpriced: unknown model '{model}'"
    return "unpriced: model not provided"


def estimate_base(turns: list) -> dict:
    """Estimate tokens from raw stored turn text."""
    input_chars = sum(len(t["user_message"] or "") for t in turns)
    output_chars = sum(len(t["assistant_response"] or "") for t in turns)
    return {
        "input_chars": input_chars,
        "output_chars": output_chars,
        "input_tokens": input_chars // CHARS_PER_TOKEN,
        "output_tokens": output_chars // CHARS_PER_TOKEN,
    }


def estimate_context_growth(turns: list) -> int:
    """
    Each turn re-sends the full accumulated conversation history as input.
    For turn N the model receives turns 0..N-1 as context, on top of the new message.
    This computes the cumulative extra input tokens from context re-sending.
    """
    cumulative_chars = 0
    growth_tokens = 0
    for turn in turns:
        # Prior context sent as input for this turn
        growth_tokens += cumulative_chars // CHARS_PER_TOKEN
        # Accumulate this turn's content for future turns
        cumulative_chars += len(turn["user_message"] or "")
        cumulative_chars += len(turn["assistant_response"] or "")
    return growth_tokens


def estimate_file_overhead(session_id: str, conn: sqlite3.Connection) -> dict:
    """
    Files created or edited during the session were sent as tool output to the model.
    Use current disk size as a proxy for their content size.
    Returns per-file breakdown and total tokens.
    """
    rows = conn.execute(
        "SELECT file_path, tool_name FROM session_files WHERE session_id = ?",
        (session_id,),
    ).fetchall()

    files = []
    total_chars = 0
    for row in rows:
        path = Path(row["file_path"])
        try:
            size = path.stat().st_size
        except (FileNotFoundError, PermissionError):
            size = 0
        total_chars += size
        files.append({
            "path": str(path),
            "tool": row["tool_name"],
            "size_bytes": size,
            "est_tokens": size // CHARS_PER_TOKEN,
        })
    return {
        "files": files,
        "total_chars": total_chars,
        "total_tokens": total_chars // CHARS_PER_TOKEN,
    }


def compute_cost(input_tokens: int, output_tokens: int, model: Optional[str]) -> Optional[float]:
    pricing = MODEL_PRICING.get(model) if model else None
    if pricing is None:
        return None
    return (input_tokens / 1_000_000 * pricing[0]) + (output_tokens / 1_000_000 * pricing[1])


# --- Report ---

def build_report(session_id: str, conn: sqlite3.Connection, model: Optional[str]) -> dict:
    session = conn.execute(
        "SELECT id, summary, repository, branch, created_at, updated_at FROM sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    if not session:
        sys.exit(f"Error: session '{session_id}' not found.")

    turns = conn.execute(
        "SELECT turn_index, user_message, assistant_response FROM turns WHERE session_id = ? ORDER BY turn_index",
        (session_id,),
    ).fetchall()

    base = estimate_base(turns)
    context_growth_tokens = estimate_context_growth(turns)
    file_overhead = estimate_file_overhead(session_id, conn)

    total_input = (
        base["input_tokens"]
        + context_growth_tokens
        + file_overhead["total_tokens"]
        + SYSTEM_PROMPT_TOKENS
    )
    total_output = base["output_tokens"]
    total_tokens = total_input + total_output
    cost = compute_cost(total_input, total_output, model)

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
        "estimate_type": "enhanced (turns + context growth + file overhead + system prompt)",
        "breakdown": {
            "base_input_tokens":      base["input_tokens"],
            "base_output_tokens":     base["output_tokens"],
            "context_growth_tokens":  context_growth_tokens,
            "file_overhead_tokens":   file_overhead["total_tokens"],
            "system_prompt_tokens":   SYSTEM_PROMPT_TOKENS,
        },
        "totals": {
            "input_tokens":  total_input,
            "output_tokens": total_output,
            "total_tokens":  total_tokens,
            "estimated_cost_usd": round(cost, 4) if cost is not None else None,
        },
        "pricing_status": pricing_status(model, cost is not None),
        "file_overhead_detail": file_overhead["files"],
        "untracked_overhead": [
            "web_fetch results",
            "bash / grep / glob stdout",
            "view / read tool outputs",
            "injected skill context",
        ],
    }


def print_report(report: dict) -> None:
    s = report["session"]
    t = report["totals"]
    b = report["breakdown"]

    print()
    print(f"{'─' * 60}")
    print(f"  Session: {s['summary'] or s['id']}")
    print(f"  Repo:    {s['repository'] or '—'}  |  Turns: {s['turn_count']}")
    print(f"  Date:    {s['created_at'][:19]}")
    print(f"  Model:   {report['model'] or 'unknown'}")
    print(f"{'─' * 60}")
    print(f"  Token Breakdown")
    print(f"    Base input (turns)        {b['base_input_tokens']:>10,}")
    print(f"    Context growth (re-send)  {b['context_growth_tokens']:>10,}")
    print(f"    File overhead             {b['file_overhead_tokens']:>10,}")
    print(f"    System prompt (fixed)     {b['system_prompt_tokens']:>10,}")
    print(f"    ─────────────────────────────────────")
    print(f"    Total input               {t['input_tokens']:>10,}")
    print(f"    Total output              {t['output_tokens']:>10,}")
    print(f"    Total                     {t['total_tokens']:>10,}")
    print(f"{'─' * 60}")
    if t["estimated_cost_usd"] is None:
        print(f"  Estimated cost:  skipped ({report['pricing_status']})")
    else:
        print(f"  Estimated cost:  ~${t['estimated_cost_usd']:.4f}")
    print(f"{'─' * 60}")
    if report["file_overhead_detail"]:
        print(f"  Files tracked:")
        for f in report["file_overhead_detail"]:
            print(f"    [{f['tool']:6}] {f['est_tokens']:>6,} tok  {f['path']}")
        print()
    print(f"  ⚠️  Untracked (actual cost likely higher):")
    for item in report["untracked_overhead"]:
        print(f"      • {item}")
    print()


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description="Estimate token usage for a Copilot CLI session."
    )
    parser.add_argument("session", nargs="?", help="Session ID, 'latest', or YYYY-MM-DD")
    parser.add_argument("--model",
                        help="Model name for cost calculation; omitted or unknown models skip pricing")
    parser.add_argument("--list", action="store_true", help="List recent sessions")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--db", default=str(SESSION_STORE_DB),
                        help="Path to session store DB")
    args = parser.parse_args()

    conn = connect(Path(args.db))

    if args.list:
        list_sessions(conn)
        return

    if not args.session:
        parser.print_help()
        sys.exit(1)

    session_id = resolve_session_id(conn, args.session)
    report = build_report(session_id, conn, args.model)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_report(report)


if __name__ == "__main__":
    main()
