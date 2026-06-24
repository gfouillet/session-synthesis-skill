#!/usr/bin/env python3
"""OpenCode token and cost report using `opencode export`."""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ── Shared OpenRouter helpers ─────────────────────────────────────────────────
_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))
from openrouter_pricing import (  # noqa: E402
    PRICING_CACHE_PATH,
    PRICING_CACHE_TTL,
    compute_openrouter_cost,
    fetch_openrouter_pricing,
    resolve_openrouter_api_key,
)

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


def find_related_sessions(conn: sqlite3.Connection, session_id: str) -> list[dict]:
    """Query DB for sub-agent sessions spawned by the given session.

    Matches sessions whose ``parent_id`` points to the given session (direct
    children in the session hierarchy).  No agent-type filter is applied —
    any session with the matching parent_id is included.
    """
    rows = conn.execute(
        """
        SELECT id, title, agent, model, cost,
               tokens_input, tokens_output, tokens_reasoning,
               tokens_cache_read, tokens_cache_write
        FROM session
        WHERE parent_id = ?
        ORDER BY time_created
        """,
        (session_id,),
    )
    return [dict(r) for r in rows]




def add_subagent_data(report: dict, subagents: list[dict], pricing: Optional[dict] = None) -> None:
    """Merge sub-agent costs and tokens into the main report.

    Adds a ``subagent_sessions`` list to the report and updates totals,
    model_breakdown, and pricing_status.

    ``subagents`` should come from :func:`find_related_sessions` — sessions
    whose ``parent_id`` points to the main session (direct children).

    If ``pricing`` (from :func:`fetch_openrouter_pricing`) is provided,
    costs for OpenRouter sub-agents are recomputed from token counts × API pricing.
    """
    entries = []
    sa_total_cost = 0.0
    sa_total_tokens = 0

    for sa in subagents:
        inp = int(sa.get("tokens_input") or 0)
        out = int(sa.get("tokens_output") or 0)
        reas = int(sa.get("tokens_reasoning") or 0)
        cr = int(sa.get("tokens_cache_read") or 0)
        cw = int(sa.get("tokens_cache_write") or 0)
        total = inp + out + reas + cr + cw

        model_raw = sa.get("model")
        model_id = None
        provider_id = None
        if model_raw:
            try:
                parsed = json.loads(model_raw) if isinstance(model_raw, str) else model_raw
                model_id = parsed.get("id")
                provider_id = parsed.get("providerID")
            except (json.JSONDecodeError, AttributeError):
                pass

        cost = float(sa.get("cost") or 0)
        if pricing and provider_id == "openrouter" and model_id in pricing:
            cost = compute_openrouter_cost(inp, out, cr, cw, pricing[model_id])

        sa_total_cost += cost
        sa_total_tokens += total

        entries.append({
            "id": sa["id"],
            "title": sa.get("title"),
            "agent": sa.get("agent"),
            "model_id": model_id,
            "provider_id": provider_id,
            "cost_usd": round(cost, 6),
            "input_tokens": inp,
            "output_tokens": out,
            "reasoning_tokens": reas,
            "cache_read_tokens": cr,
            "cache_write_tokens": cw,
            "total_tokens": total,
        })

    report["subagent_sessions"] = entries

    # Roll up into totals
    t = report["totals"]
    b = report["breakdown"]
    for k in ("input_tokens", "output_tokens", "reasoning_tokens",
              "cache_read_tokens", "cache_write_tokens"):
        sa_sum = sum(e[k] for e in entries)
        t[k] += sa_sum
        b[k] += sa_sum
    t["total_tokens"] += sa_total_tokens
    t["estimated_cost_usd"] = round(t["estimated_cost_usd"] + sa_total_cost, 6)

    # Roll up into model_breakdown
    for e in entries:
        if e["model_id"]:
            label = f"{e['provider_id']}/{e['model_id']}" if e["provider_id"] and not e["model_id"].startswith(f"{e['provider_id']}/") else e["model_id"]
            existing = [m for m in report["model_breakdown"] if m["model"] == label]
            if existing:
                m = existing[0]
                for k in ("input_tokens", "output_tokens", "reasoning_tokens",
                          "cache_read_tokens", "cache_write_tokens"):
                    m[k] += e[k]
                m["total_tokens"] += e["total_tokens"]
                m["cost_usd"] += e["cost_usd"]
                m["assistant_turns"] += 1
            else:
                report["model_breakdown"].append({
                    "model": label,
                    "share_pct": 0.0,
                    "input_tokens": e["input_tokens"],
                    "output_tokens": e["output_tokens"],
                    "reasoning_tokens": e["reasoning_tokens"],
                    "cache_read_tokens": e["cache_read_tokens"],
                    "cache_write_tokens": e["cache_write_tokens"],
                    "total_tokens": e["total_tokens"],
                    "cost_usd": e["cost_usd"],
                    "assistant_turns": 1,
                })

    # Recalculate percentages
    for m in report["model_breakdown"]:
        m["share_pct"] = round(m["total_tokens"] / t["total_tokens"] * 100, 1) if t["total_tokens"] else 0.0

    or_priced = pricing and any(
        e.get("provider_id") == "openrouter" and e.get("model_id") in pricing
        for e in entries
    )
    if or_priced:
        report["estimate_type"] = (
            "exact telemetry (opencode export + DB sub-agent rollup; "
            "main OpenRouter session priced via API, sub-agents from API pricing)"
        )
        report["pricing_status"] = (
            "OpenRouter models priced via GET /api/v1/models per-token rates; "
            "non-OpenRouter sub-agents use DB session.cost"
        )
    else:
        report["estimate_type"] = (
            "exact telemetry (opencode export + DB sub-agent rollup; "
            "main session from export, sub-agents from session.cost in DB)"
        )
        report["pricing_status"] = (
            "cost read from OpenCode export metadata and DB session.cost "
            "(includes OpenRouter usage.cost telemetry for sub-agents)"
        )


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


def build_report(data: dict, pricing: Optional[dict] = None) -> dict:
    info = data.get("info") or {}
    session_tokens = info.get("tokens") or {}
    totals = empty_usage()
    by_model = defaultdict(empty_usage)
    by_mode = defaultdict(empty_usage)
    model_order = []
    mode_order = []
    user_turns = 0
    flag_or_priced = False

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
        if pricing and provider == "openrouter" and model in pricing:
            cache = tokens.get("cache") or {}
            t_input = int(tokens.get("input") or 0)
            t_output = int(tokens.get("output") or 0)
            t_cache_read = int(cache.get("read") or tokens.get("cache_read") or 0)
            t_cache_write = int(cache.get("write") or tokens.get("cache_write") or 0)
            cost = compute_openrouter_cost(t_input, t_output, t_cache_read, t_cache_write, pricing[model])
            flag_or_priced = True

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

    if flag_or_priced:
        estimate_type = "exact telemetry (OpenCode export, OpenRouter models priced via GET /api/v1/models)"
        pricing_status = "OpenRouter models priced via per-token rates from GET /api/v1/models; non-OpenRouter models use export cost"
    else:
        estimate_type = "exact telemetry (opencode export; assistant turns grouped by model/mode)"
        pricing_status = "cost read from OpenCode export metadata; no manual pricing table used"

    return {
        "client": "OpenCode",
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
        "estimate_type": estimate_type,
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
        "pricing_status": pricing_status,
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
    if report.get("subagent_sessions"):
        print(f"  Includes {len(report['subagent_sessions'])} sub-agent sessions:")
        for sa in report["subagent_sessions"]:
            model = sa["model_id"] or "?"
            print(f"    {sa['agent']:<10} {model:<30} ${sa['cost_usd']:<8.6f}  {sa['title'][:40]}")
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
    parser.add_argument(
        "--include-subagents",
        action="store_true",
        help="Query DB for sub-agent sessions (sessions whose parent_id points to this session) and roll up their costs and tokens",
    )
    parser.add_argument(
        "--openrouter-api-key",
        help="OpenRouter API key for fetching per-model pricing from GET /api/v1/models. "
        "If omitted, checks OPENROUTER_API_KEY env var, then auth.json.",
    )
    args = parser.parse_args()

    export_file = Path(args.export_file) if args.export_file else None
    conn = None if export_file else connect(Path(args.db))

    if args.list:
        if conn is None:
            sys.exit("Error: --list requires an OpenCode database")
        list_sessions(conn)
        return

    pricing = None
    or_key = resolve_openrouter_api_key(args.openrouter_api_key)
    if or_key:
        pricing = fetch_openrouter_pricing(or_key)

    if export_file:
        data = export_session("export-file", export_file, False)
    else:
        if not args.session:
            parser.print_help()
            sys.exit(1)
        session_id = resolve_session_id(conn, args.session)
        data = export_session(session_id, None, args.keep_export)

    report = build_report(data, pricing)

    if args.include_subagents:
        if conn is None:
            print("Warning: --include-subagents requires a DB connection; skipping sub-agent rollup", file=sys.stderr)
        elif not args.export_file and args.session:
            subagents = find_related_sessions(conn, session_id)
            if subagents:
                add_subagent_data(report, subagents, pricing)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_report(report)


if __name__ == "__main__":
    main()
