#!/usr/bin/env python3
"""OpenCode token and cost report using `opencode export`."""

import argparse
import json
import sqlite3
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

OPENCODE_DB = Path.home() / ".local" / "share" / "opencode" / "opencode.db"


def connect(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        sys.exit(f"Error: OpenCode database not found at {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def ms_to_iso(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def resolve_session_id(conn: sqlite3.Connection, spec: str) -> str:
    if spec == "latest":
        row = conn.execute("SELECT id FROM session ORDER BY time_updated DESC LIMIT 1").fetchone()
        if not row:
            sys.exit("Error: no OpenCode sessions found.")
        return row["id"]
    if len(spec) == 10 and spec[4] == "-" and spec[7] == "-":
        start = int(datetime.fromisoformat(spec).replace(tzinfo=timezone.utc).timestamp() * 1000)
        end = start + 86_400_000
        row = conn.execute(
            "SELECT id FROM session WHERE time_created >= ? AND time_created < ? ORDER BY time_updated DESC LIMIT 1",
            (start, end),
        ).fetchone()
        if not row:
            sys.exit(f"Error: no OpenCode session found on date {spec}.")
        return row["id"]
    row = conn.execute("SELECT id FROM session WHERE id = ?", (spec,)).fetchone()
    if not row:
        sys.exit(f"Error: OpenCode session '{spec}' not found.")
    return row["id"]


def list_sessions(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT id, title, directory, model, cost, tokens_input, tokens_output, tokens_reasoning, "
        "tokens_cache_read, tokens_cache_write, time_updated FROM session ORDER BY time_updated DESC LIMIT 10"
    ).fetchall()
    if not rows:
        print("No OpenCode sessions found.")
        return
    print(f"{'ID':<34}  {'Updated':<20}  {'Cost':>10}  {'Tokens':>12}  Title")
    print("-" * 110)
    for row in rows:
        total = sum(int(row[k] or 0) for k in (
            "tokens_input",
            "tokens_output",
            "tokens_reasoning",
            "tokens_cache_read",
            "tokens_cache_write",
        ))
        updated = (ms_to_iso(row["time_updated"]) or "—")[:19]
        cost = f"${float(row['cost'] or 0):.4f}"
        title = (row["title"] or row["directory"] or "—")[:45]
        print(f"{row['id']:<34}  {updated:<20}  {cost:>10}  {total:>12,}  {title}")


def export_session(session_id: str, export_file: Optional[Path], keep_export: bool) -> dict:
    if export_file:
        with export_file.open() as fh:
            return json.load(fh)
    with tempfile.TemporaryDirectory(prefix="session-synthesis-opencode-") as tmp:
        path = Path(tmp) / f"{session_id}.json"
        with path.open("w") as fh:
            result = subprocess.run(["opencode", "export", session_id], stdout=fh)
        if result.returncode != 0:
            sys.exit(result.returncode)
        with path.open() as fh:
            data = json.load(fh)
        if keep_export:
            target = Path.cwd() / f"opencode-export-{session_id}.json"
            target.write_text(json.dumps(data, indent=2))
            print(f"[session-synthesis] kept export at {target}", file=sys.stderr)
        return data


def empty_usage() -> dict[str, float]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
        "assistant_turns": 0,
    }


def add_usage(target: dict, tokens: dict, cost: Any) -> None:
    cache = tokens.get("cache") or {}
    input_tokens = int(tokens.get("input") or 0)
    output_tokens = int(tokens.get("output") or 0)
    reasoning_tokens = int(tokens.get("reasoning") or 0)
    cache_read_tokens = int(cache.get("read") or tokens.get("cache_read") or 0)
    cache_write_tokens = int(cache.get("write") or tokens.get("cache_write") or 0)
    total_tokens = int(tokens.get("total") or (input_tokens + output_tokens + reasoning_tokens + cache_read_tokens + cache_write_tokens))
    target["input_tokens"] += input_tokens
    target["output_tokens"] += output_tokens
    target["reasoning_tokens"] += reasoning_tokens
    target["cache_read_tokens"] += cache_read_tokens
    target["cache_write_tokens"] += cache_write_tokens
    target["total_tokens"] += total_tokens
    target["cost_usd"] += float(cost or 0)
    target["assistant_turns"] += 1


def model_label(provider: Optional[str], model: Optional[str]) -> str:
    if provider and model and not model.startswith(f"{provider}/"):
        return f"{provider}/{model}"
    return model or provider or "unknown"


def build_report(data: dict) -> dict:
    info = data.get("info") or {}
    session_tokens = info.get("tokens") or {}
    totals = empty_usage()
    by_model = defaultdict(empty_usage)
    by_mode = defaultdict(empty_usage)
    model_order = []
    mode_order = []
    user_turns = 0

    for message in data.get("messages") or []:
        msg = message.get("info") or message
        role = msg.get("role")
        if role == "user":
            user_turns += 1
            continue
        if role != "assistant":
            continue
        tokens = msg.get("tokens") or {}
        if not tokens:
            continue
        provider = msg.get("providerID") or (msg.get("model") or {}).get("providerID")
        model = msg.get("modelID") or (msg.get("model") or {}).get("modelID") or (msg.get("model") or {}).get("id")
        mode = msg.get("mode") or msg.get("agent") or "unknown"
        label = model_label(provider, model)
        if label not in by_model:
            model_order.append(label)
        if mode not in by_mode:
            mode_order.append(mode)
        cost = msg.get("cost")
        add_usage(totals, tokens, cost)
        add_usage(by_model[label], tokens, cost)
        add_usage(by_mode[mode], tokens, cost)

    session_cost = float(info.get("cost") or 0)
    if totals["total_tokens"] == 0 and session_tokens:
        add_usage(totals, session_tokens, session_cost)
    elif totals["cost_usd"] == 0 and session_cost:
        totals["cost_usd"] = session_cost

    model_breakdown = []
    for label in model_order or [model_label((info.get("model") or {}).get("providerID"), (info.get("model") or {}).get("id"))]:
        usage = by_model.get(label) or totals
        if len(model_order) <= 1 and usage["cost_usd"] == 0 and totals["cost_usd"]:
            usage = {**usage, "cost_usd": totals["cost_usd"]}
        pct = (usage["total_tokens"] / totals["total_tokens"] * 100) if totals["total_tokens"] else 0
        model_breakdown.append({"model": label, "share_pct": round(pct, 1), **usage})

    mode_breakdown = []
    for mode in mode_order:
        usage = by_mode[mode]
        pct = (usage["total_tokens"] / totals["total_tokens"] * 100) if totals["total_tokens"] else 0
        mode_breakdown.append({"mode": mode, "share_pct": round(pct, 1), **usage})

    created = (info.get("time") or {}).get("created") or info.get("time_created")
    updated = (info.get("time") or {}).get("updated") or info.get("time_updated")
    return {
        "session": {
            "id": info.get("id"),
            "summary": info.get("title") or info.get("slug"),
            "repository": info.get("directory"),
            "branch": None,
            "created_at": ms_to_iso(created),
            "updated_at": ms_to_iso(updated),
            "turn_count": user_turns + totals["assistant_turns"],
            "opencode_version": info.get("version"),
            "agent": info.get("agent"),
        },
        "model": ", ".join(item["model"] for item in model_breakdown if item["assistant_turns"] or len(model_breakdown) == 1),
        "orchestrator": "opencode",
        "estimate_type": "exact telemetry (opencode export; assistant turns grouped by model/mode)",
        "breakdown": {
            "input_tokens": totals["input_tokens"],
            "output_tokens": totals["output_tokens"],
            "reasoning_tokens": totals["reasoning_tokens"],
            "cache_read_tokens": totals["cache_read_tokens"],
            "cache_write_tokens": totals["cache_write_tokens"],
        },
        "totals": {
            "input_tokens": totals["input_tokens"],
            "output_tokens": totals["output_tokens"],
            "reasoning_tokens": totals["reasoning_tokens"],
            "cache_read_tokens": totals["cache_read_tokens"],
            "cache_write_tokens": totals["cache_write_tokens"],
            "total_tokens": totals["total_tokens"],
            "estimated_cost_usd": round(totals["cost_usd"], 6),
        },
        "pricing_status": "cost read from OpenCode export metadata; no manual pricing table used",
        "model_breakdown": model_breakdown,
        "mode_breakdown": mode_breakdown,
        "untracked_overhead": [],
    }


def print_report(report: dict) -> None:
    s = report["session"]
    t = report["totals"]
    b = report["breakdown"]
    print()
    print("─" * 72)
    print(f"  Session: {s['summary'] or s['id']}")
    print(f"  Dir:     {s['repository'] or '—'}")
    print(f"  Date:    {(s['created_at'] or '—')[:19]}  |  Turns: {s['turn_count']}")
    print(f"  Model:   {report['model'] or 'unknown'}")
    print("─" * 72)
    print(f"  Token Breakdown  [{report['estimate_type']}]")
    print(f"    Input              {b['input_tokens']:>12,}")
    print(f"    Output             {b['output_tokens']:>12,}")
    print(f"    Reasoning          {b['reasoning_tokens']:>12,}")
    print(f"    Cache read         {b['cache_read_tokens']:>12,}")
    print(f"    Cache write        {b['cache_write_tokens']:>12,}")
    print(f"    ─────────────────────────────────")
    print(f"    Total              {t['total_tokens']:>12,}")
    print("─" * 72)
    print(f"  Recorded cost:       ${t['estimated_cost_usd']:.6f}")
    print("─" * 72)
    if report["model_breakdown"]:
        print("  By model:")
        for item in report["model_breakdown"]:
            print(
                f"    {item['share_pct']:>5.1f}%  {item['total_tokens']:>12,} tok  "
                f"${item['cost_usd']:.6f}  {item['model']}"
            )
    if report["mode_breakdown"]:
        print("  By mode:")
        for item in report["mode_breakdown"]:
            print(
                f"    {item['share_pct']:>5.1f}%  {item['total_tokens']:>12,} tok  "
                f"${item['cost_usd']:.6f}  {item['mode']}"
            )
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize token usage from OpenCode export telemetry.")
    parser.add_argument("session", nargs="?", help="Session ID, 'latest', or YYYY-MM-DD")
    parser.add_argument("--list", action="store_true", help="List recent OpenCode sessions")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--model", help="Ignored; OpenCode export provides per-turn models")
    parser.add_argument("--db", default=str(OPENCODE_DB), help="Path to OpenCode database")
    parser.add_argument("--export-file", help="Parse an existing OpenCode export JSON file")
    parser.add_argument("--keep-export", action="store_true", help="Keep a copy of the exported JSON in cwd")
    args = parser.parse_args()

    export_file = Path(args.export_file) if args.export_file else None
    conn = None if export_file else connect(Path(args.db))

    if args.list:
        if conn is None:
            sys.exit("Error: --list requires an OpenCode database")
        list_sessions(conn)
        return

    if export_file:
        data = export_session("export-file", export_file, False)
    else:
        if not args.session:
            parser.print_help()
            sys.exit(1)
        session_id = resolve_session_id(conn, args.session)
        data = export_session(session_id, None, args.keep_export)

    report = build_report(data)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_report(report)


if __name__ == "__main__":
    main()
