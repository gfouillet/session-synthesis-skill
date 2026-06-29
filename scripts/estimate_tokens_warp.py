#!/usr/bin/env python3
"""
estimate_tokens_warp.py — Token usage reporter for Warp terminal sessions.

Reads token usage from Warp's local SQLite database
(~/.local/state/warp-terminal/warp.sqlite) and optionally computes approximate
OpenRouter costs for custom-inference models.

Warp stores per-model token totals (no input/output split) using display names
rather than OpenRouter slugs.  This backend:
  - Reports exact token counts per model per token-source bucket.
  - Uses a configurable display-name → OpenRouter slug mapping (--model-map)
    plus an assumed input/output ratio (--input-output-split) to estimate cost.
  - Skips cost when no slug mapping is available and records the model as unpriced.

Usage:
    python3 estimate_tokens_warp.py <conversation_id|latest|YYYY-MM-DD> [--json]
    python3 estimate_tokens_warp.py latest --json
    python3 estimate_tokens_warp.py latest --json --openrouter-api-key <KEY>
    python3 estimate_tokens_warp.py latest --json --model-map /path/to/map.json
    python3 estimate_tokens_warp.py --list
"""

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ── Shared OpenRouter helpers ─────────────────────────────────────────────────
# The openrouter_pricing module sits next to this script.
_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))
from openrouter_pricing import (  # noqa: E402
    compute_openrouter_cost_single_total,
    fetch_openrouter_pricing,
    resolve_openrouter_api_key,
)

# ── Constants ─────────────────────────────────────────────────────────────────

WARP_DB = Path.home() / ".local" / "state" / "warp-terminal" / "warp.sqlite"

# Best-effort display-name → OpenRouter slug mapping.
# Users are encouraged to supply their own via --model-map.
BUILTIN_MODEL_MAP: dict[str, str] = {
    # Anthropic
    "Claude Opus 4.8":     "anthropic/claude-opus-4",
    "Claude Opus 4.7":     "anthropic/claude-opus-4",
    "Claude Opus 4.6":     "anthropic/claude-opus-4",
    "Claude Sonnet 4.6":   "anthropic/claude-sonnet-4",
    "Claude Sonnet 4.5":   "anthropic/claude-sonnet-4",
    "Claude Haiku 4.5":    "anthropic/claude-haiku-4",
    # DeepSeek
    "DeepSeek V4 Flash":       "deepseek/deepseek-chat",
    "DeepSeek V4 Flash Free":  "deepseek/deepseek-chat:free",
    # OpenAI
    "GPT-4o":          "openai/gpt-4o",
    "GPT-4o mini":     "openai/gpt-4o-mini",
    "GPT-5 Nano":      "openai/gpt-4.1-nano",    # nano → closest on OR
    # Google
    "Gemini 3 Flash":  "google/gemini-2.5-flash-preview",
}


# ── DB helpers ────────────────────────────────────────────────────────────────

def connect_ro(db_path: Path) -> sqlite3.Connection:
    """Open the Warp SQLite DB in read-only mode.

    Copies the WAL to a temp dir first so we never contend with a running Warp
    process on WAL checkpoints.
    """
    if not db_path.exists():
        sys.exit(f"Error: Warp database not found at {db_path}")

    tmpdir = tempfile.mkdtemp(prefix="session-synthesis-warp-")
    tmp_db = Path(tmpdir) / "warp.sqlite"
    # Copy main db + WAL + SHM so we have a consistent snapshot.
    shutil.copy2(db_path, tmp_db)
    wal = db_path.with_suffix(".sqlite-wal")
    shm = db_path.with_suffix(".sqlite-shm")
    if wal.exists():
        shutil.copy2(wal, tmp_db.with_suffix(".sqlite-wal"))
    if shm.exists():
        shutil.copy2(shm, tmp_db.with_suffix(".sqlite-shm"))

    conn = sqlite3.connect(f"file:{tmp_db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# ── Session listing ───────────────────────────────────────────────────────────

def list_sessions(conn: sqlite3.Connection) -> None:
    """List recent Warp agent conversations with timestamps and working dirs."""
    rows = conn.execute(
        """
        SELECT
            ac.conversation_id,
            ac.last_modified_at,
            MIN(aq.start_ts) AS first_ts,
            COUNT(aq.exchange_id) AS query_count,
            GROUP_CONCAT(DISTINCT aq.working_directory) AS cwds,
            SUBSTR(MIN(aq.input), 1, 80) AS first_input
        FROM agent_conversations ac
        LEFT JOIN ai_queries aq ON aq.conversation_id = ac.conversation_id
        GROUP BY ac.conversation_id
        ORDER BY ac.last_modified_at DESC
        LIMIT 10
        """
    ).fetchall()
    if not rows:
        print("No Warp agent conversations found.")
        return
    print(f"{'Conversation ID':<38}  {'Modified':<22}  {'Queries':>7}  Working directory / Title")
    print("-" * 120)
    for r in rows:
        mod = (r["last_modified_at"] or "—")[:19]
        cwd = (r["cwds"] or "—")[:50]
        # Use first query input as a rough title
        title = (r["first_input"] or "").replace("\n", " ")[:50] or "—"
        print(f"{r['conversation_id']:<38}  {mod:<22}  {r['query_count'] or 0:>7}  {cwd} | {title}")


# ── Session resolution ────────────────────────────────────────────────────────

def resolve_conversation_id(conn: sqlite3.Connection, spec: str, cwd: Optional[str] = None) -> str:
    """Resolve 'latest', a date string, or a raw conversation_id."""
    if spec == "latest":
        if cwd:
            row = conn.execute(
                """
                SELECT ac.conversation_id
                FROM agent_conversations ac
                JOIN ai_queries aq ON aq.conversation_id = ac.conversation_id
                WHERE aq.working_directory = ?
                ORDER BY ac.last_modified_at DESC
                LIMIT 1
                """,
                (cwd,),
            ).fetchone()
            if row:
                return row["conversation_id"]
        row = conn.execute(
            "SELECT conversation_id FROM agent_conversations ORDER BY last_modified_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            sys.exit("Error: no Warp agent conversations found.")
        return row["conversation_id"]

    # Date: YYYY-MM-DD
    if len(spec) == 10 and spec[4] == "-" and spec[7] == "-":
        row = conn.execute(
            """
            SELECT conversation_id
            FROM ai_queries
            WHERE date(start_ts) = ?
            ORDER BY start_ts DESC
            LIMIT 1
            """,
            (spec,),
        ).fetchone()
        if not row:
            sys.exit(f"Error: no Warp conversation found on date {spec}.")
        return row["conversation_id"]

    # Assume raw conversation_id — verify it exists
    row = conn.execute(
        "SELECT conversation_id FROM agent_conversations WHERE conversation_id = ?",
        (spec,),
    ).fetchone()
    if not row:
        sys.exit(f"Error: conversation '{spec}' not found in Warp database.")
    return row["conversation_id"]


# ── Token usage extraction ────────────────────────────────────────────────────

def extract_token_usage(conn: sqlite3.Connection, conversation_id: str) -> dict:
    """Parse conversation_usage_metadata.token_usage from agent_conversations.

    Returns the parsed conversation_data dict (or exits on error).
    """
    row = conn.execute(
        "SELECT conversation_data FROM agent_conversations WHERE conversation_id = ?",
        (conversation_id,),
    ).fetchone()
    if not row:
        sys.exit(f"Error: conversation '{conversation_id}' not found in agent_conversations.")

    try:
        data = json.loads(row["conversation_data"])
    except (json.JSONDecodeError, TypeError):
        sys.exit(f"Error: could not parse conversation_data for '{conversation_id}'.")

    return data


def extract_metadata(conn: sqlite3.Connection, conversation_id: str) -> dict:
    """Gather conversation metadata: timestamps, cwd, query count, query inputs."""
    rows = conn.execute(
        """
        SELECT start_ts, working_directory, input, output_status
        FROM ai_queries
        WHERE conversation_id = ?
        ORDER BY start_ts ASC
        """,
        (conversation_id,),
    ).fetchall()

    if not rows:
        return {
            "first_ts": None,
            "last_ts": None,
            "cwds": [],
            "query_count": 0,
            "title": None,
            "statuses": [],
        }

    cwds = list({r["working_directory"] for r in rows if r["working_directory"]})
    statuses = [r["output_status"] for r in rows if r["output_status"]]

    # Use first query input as title (truncated)
    first_input_raw = rows[0]["input"] or ""
    try:
        parsed_input = json.loads(first_input_raw)
        if isinstance(parsed_input, list):
            # Find first Query text
            for part in parsed_input:
                if isinstance(part, dict) and "Query" in part:
                    title = (part["Query"].get("text") or "")[:120]
                    break
            else:
                title = first_input_raw[:120]
        else:
            title = first_input_raw[:120]
    except (json.JSONDecodeError, TypeError):
        title = first_input_raw[:120]

    return {
        "first_ts": rows[0]["start_ts"],
        "last_ts": rows[-1]["start_ts"],
        "cwds": cwds,
        "query_count": len(rows),
        "title": title.strip() or None,
        "statuses": statuses,
    }


# ── Model map loading ─────────────────────────────────────────────────────────

def load_model_map(custom_path: Optional[str]) -> dict[str, str]:
    """Load the display-name → OpenRouter slug mapping.

    Merges the builtin table with any user-supplied JSON file (--model-map).
    User entries override builtins.
    """
    merged = dict(BUILTIN_MODEL_MAP)
    if custom_path:
        p = Path(custom_path)
        if not p.exists():
            print(f"Warning: --model-map file not found: {p}, using builtins only.", file=sys.stderr)
        else:
            try:
                user_map = json.loads(p.read_text())
                if isinstance(user_map, dict):
                    merged.update(user_map)
                else:
                    print("Warning: --model-map JSON must be an object, ignoring.", file=sys.stderr)
            except (json.JSONDecodeError, OSError) as e:
                print(f"Warning: could not parse --model-map file: {e}, using builtins only.", file=sys.stderr)
    return merged


# ── Report building ───────────────────────────────────────────────────────────

def empty_model_usage() -> dict:
    return {
        "warp_tokens": 0,
        "byok_tokens": 0,
        "custom_endpoint_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
        "assistant_turns": 0,
    }


def build_report(
    conversation_id: str,
    conn: sqlite3.Connection,
    pricing: Optional[dict],
    model_map: dict[str, str],
    input_output_split: float,
) -> dict:
    """Build a session report in the same shape as the OpenCode backend."""
    data = extract_token_usage(conn, conversation_id)
    meta = extract_metadata(conn, conversation_id)
    usage_meta = data.get("conversation_usage_metadata", {}) or {}
    token_usage = usage_meta.get("token_usage", []) or []

    # ── Aggregate per-model ───────────────────────────────────────────────
    totals = empty_model_usage()
    by_model: dict[str, dict] = {}
    model_order: list[str] = []
    unpriced_models: list[str] = []
    priced_models: list[str] = []
    any_openrouter_priced = False

    # Category breakdown (used as "mode" equivalent)
    by_category: dict[str, int] = defaultdict(int)

    for entry in token_usage:
        display_name = entry.get("model_id") or "unknown"
        warp_tok = int(entry.get("warp_tokens") or 0)
        byok_tok = int(entry.get("byok_tokens") or 0)
        ce_tok = int(entry.get("custom_endpoint_tokens") or 0)
        total = warp_tok + byok_tok + ce_tok

        if display_name not in by_model:
            model_order.append(display_name)
            by_model[display_name] = empty_model_usage()

        m = by_model[display_name]
        m["warp_tokens"] += warp_tok
        m["byok_tokens"] += byok_tok
        m["custom_endpoint_tokens"] += ce_tok
        m["total_tokens"] += total
        m["assistant_turns"] += 1

        totals["warp_tokens"] += warp_tok
        totals["byok_tokens"] += byok_tok
        totals["custom_endpoint_tokens"] += ce_tok
        totals["total_tokens"] += total
        totals["assistant_turns"] += 1

        # Category breakdown from custom_endpoint_token_usage_by_category
        ce_cats = entry.get("custom_endpoint_token_usage_by_category") or {}
        for cat, count in ce_cats.items():
            by_category[cat] += int(count)
        # Also warp and byok categories
        warp_cats = entry.get("warp_token_usage_by_category") or {}
        for cat, count in warp_cats.items():
            by_category[f"warp:{cat}"] += int(count)
        byok_cats = entry.get("byok_token_usage_by_category") or {}
        for cat, count in byok_cats.items():
            by_category[f"byok:{cat}"] += int(count)

        # ── OpenRouter pricing for custom_endpoint_tokens ─────────────────
        if ce_tok > 0 and pricing:
            slug = model_map.get(display_name)
            if slug and slug in pricing:
                cost = compute_openrouter_cost_single_total(
                    ce_tok, input_output_split, pricing[slug]
                )
                m["cost_usd"] += cost
                totals["cost_usd"] += cost
                any_openrouter_priced = True
                if display_name not in priced_models:
                    priced_models.append(display_name)
            elif slug:
                # Slug mapped but not found in pricing API
                unpriced_models.append(f"{display_name} → {slug} (not in pricing API)")
            else:
                unpriced_models.append(display_name)

    # ── Model breakdown ───────────────────────────────────────────────────
    model_breakdown = []
    for name in model_order:
        m = by_model[name]
        pct = (m["total_tokens"] / totals["total_tokens"] * 100) if totals["total_tokens"] else 0
        model_breakdown.append({
            "model": name,
            "share_pct": round(pct, 1),
            "input_tokens": 0,  # Warp has no input/output split
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "warp_tokens": m["warp_tokens"],
            "byok_tokens": m["byok_tokens"],
            "custom_endpoint_tokens": m["custom_endpoint_tokens"],
            "total_tokens": m["total_tokens"],
            "cost_usd": round(m["cost_usd"], 6),
            "assistant_turns": m["assistant_turns"],
        })

    # ── Category (mode) breakdown ─────────────────────────────────────────
    category_breakdown = []
    for cat, count in sorted(by_category.items(), key=lambda x: -x[1]):
        pct = (count / totals["total_tokens"] * 100) if totals["total_tokens"] else 0
        category_breakdown.append({
            "mode": cat,
            "share_pct": round(pct, 1),
            "total_tokens": count,
            "cost_usd": 0.0,
        })

    # ── Estimate type / pricing status ────────────────────────────────────
    if any_openrouter_priced:
        estimate_type = (
            f"Warp usage metadata (single-total tokens; no input/output split); "
            f"OpenRouter models priced via API with {input_output_split:.0%}/{1 - input_output_split:.0%} "
            f"input/output split assumption"
        )
        pricing_parts = [
            f"OpenRouter models priced via GET /api/v1/models with assumed "
            f"{input_output_split:.0%} input / {1 - input_output_split:.0%} output split",
        ]
        if unpriced_models:
            pricing_parts.append(
                f"unpriced models (no slug mapping or not in API): {', '.join(unpriced_models)}"
            )
        pricing_parts.append("Warp/Oz tokens reported but not priced")
        pricing_status = "; ".join(pricing_parts)
    else:
        estimate_type = "Warp usage metadata (single-total tokens; no input/output split)"
        if unpriced_models:
            pricing_status = (
                f"no OpenRouter pricing applied; unmapped models: {', '.join(unpriced_models)}"
            )
        else:
            pricing_status = "no OpenRouter pricing key provided or no models to price"

    # ── Determine source class for this conversation ──────────────────────
    has_custom = totals["custom_endpoint_tokens"] > 0
    has_warp = totals["warp_tokens"] > 0
    has_byok = totals["byok_tokens"] > 0
    sources = []
    if has_custom:
        sources.append("custom_endpoint")
    if has_warp:
        sources.append("warp")
    if has_byok:
        sources.append("byok")

    # ── Compute duration ──────────────────────────────────────────────────
    duration_str = None
    if meta["first_ts"] and meta["last_ts"]:
        try:
            fmt = "%Y-%m-%d %H:%M:%S"
            first = datetime.strptime(meta["first_ts"][:19], fmt)
            last = datetime.strptime(meta["last_ts"][:19], fmt)
            delta = last - first
            mins = int(delta.total_seconds() // 60)
            if mins < 60:
                duration_str = f"{mins}m"
            else:
                h, m = divmod(mins, 60)
                duration_str = f"{h}h {m:02d}m"
        except (ValueError, TypeError):
            pass

    # ── Build report dict ─────────────────────────────────────────────────
    model_names = ", ".join(name for name in model_order if by_model[name]["total_tokens"])
    return {
        "client": "Warp",
        "session": {
            "id": conversation_id,
            "summary": meta["title"],
            "repository": meta["cwds"][0] if meta["cwds"] else None,
            "branch": None,
            "created_at": meta["first_ts"],
            "updated_at": meta["last_ts"],
            "turn_count": meta["query_count"],
            "duration": duration_str,
        },
        "model": model_names or "unknown",
        "orchestrator": "warp",
        "estimate_type": estimate_type,
        "breakdown": {
            "input_tokens": 0,   # Warp does not split input/output
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "warp_tokens": totals["warp_tokens"],
            "byok_tokens": totals["byok_tokens"],
            "custom_endpoint_tokens": totals["custom_endpoint_tokens"],
        },
        "totals": {
            "input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "total_tokens": totals["total_tokens"],
            "estimated_cost_usd": round(totals["cost_usd"], 6) if totals["cost_usd"] else 0.0,
        },
        "pricing_status": pricing_status,
        "model_breakdown": model_breakdown,
        "mode_breakdown": category_breakdown,
        "untracked_overhead": [],
        "_warp_meta": {
            "token_sources": sources,
            "input_output_split": input_output_split,
            "priced_models": priced_models,
            "unpriced_models": unpriced_models,
            "credits_spent": usage_meta.get("credits_spent"),
            "context_window_usage": usage_meta.get("context_window_usage"),
            "tool_usage_metadata": usage_meta.get("tool_usage_metadata"),
        },
    }


# ── Pretty printer ────────────────────────────────────────────────────────────

def print_report(report: dict) -> None:
    s = report["session"]
    t = report["totals"]
    b = report["breakdown"]
    wm = report.get("_warp_meta", {})

    print()
    print("─" * 72)
    print(f"  Client:   Warp")
    print(f"  Session:  {s['summary'] or s['id']}")
    print(f"  Dir:      {s['repository'] or '—'}")
    dur = s.get("duration") or "—"
    print(f"  Date:     {(s['created_at'] or '—')[:19]}  |  Queries: {s['turn_count']}  |  Duration: {dur}")
    print(f"  Model:    {report['model'] or 'unknown'}")
    print("─" * 72)
    print(f"  Token Breakdown  [{report['estimate_type']}]")
    print(f"    Custom endpoint (OpenRouter)  {b['custom_endpoint_tokens']:>12,}")
    print(f"    Warp-managed (Oz)             {b['warp_tokens']:>12,}")
    print(f"    BYOK                          {b['byok_tokens']:>12,}")
    print(f"    ──────────────────────────────────")
    print(f"    Total                         {t['total_tokens']:>12,}")
    print("─" * 72)
    if t["estimated_cost_usd"]:
        print(f"  Estimated cost (approx):  ~${t['estimated_cost_usd']:.6f}")
        print(f"  Split assumption: {wm.get('input_output_split', 0.8):.0%} input / {1 - wm.get('input_output_split', 0.8):.0%} output")
    else:
        print(f"  Estimated cost:  N/A ({report['pricing_status']})")
    print("─" * 72)
    if report["model_breakdown"]:
        print("  By model:")
        for item in report["model_breakdown"]:
            cost = f"${item['cost_usd']:.6f}" if item["cost_usd"] else "N/A"
            print(
                f"    {item['share_pct']:>5.1f}%  {item['total_tokens']:>12,} tok  "
                f"{cost:<12}  {item['model']}"
            )
    if report["mode_breakdown"]:
        print("  By category:")
        for item in report["mode_breakdown"][:10]:  # Top 10
            print(f"    {item['share_pct']:>5.1f}%  {item['total_tokens']:>12,} tok  {item['mode']}")
    print()
    if wm.get("tool_usage_metadata"):
        print("  Tool usage:")
        tum = wm["tool_usage_metadata"]
        for tool_name, tool_data in tum.items():
            if isinstance(tool_data, dict) and any(v for v in tool_data.values()):
                parts = [f"{k}={v}" for k, v in tool_data.items() if v]
                print(f"    {tool_name}: {', '.join(parts)}")
        print()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Report token usage from Warp terminal sessions."
    )
    parser.add_argument(
        "session", nargs="?",
        help="Conversation ID, 'latest', or YYYY-MM-DD",
    )
    parser.add_argument("--list", action="store_true", help="List recent Warp conversations")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--model", help="Ignored; Warp provides per-model usage automatically")
    parser.add_argument("--db", default=str(WARP_DB), help="Path to Warp SQLite database")
    parser.add_argument(
        "--include-subagents", action="store_true",
        help="Accepted for interface compatibility; Warp sub-agent categories are included by default",
    )
    parser.add_argument(
        "--openrouter-api-key",
        help="OpenRouter API key for per-model pricing. "
        "If omitted, checks OPENROUTER_API_KEY env var, then auth.json.",
    )
    parser.add_argument(
        "--model-map",
        help="Path to a JSON file mapping display names to OpenRouter slugs. "
        'Format: {"Claude Opus 4.8": "anthropic/claude-opus-4", ...}',
    )
    parser.add_argument(
        "--input-output-split", type=float, default=0.8,
        help="Assumed fraction of tokens that are input (0.0-1.0). Default: 0.8 (80%% input / 20%% output).",
    )
    args = parser.parse_args()

    conn = connect_ro(Path(args.db))

    if args.list:
        list_sessions(conn)
        return

    if not args.session:
        parser.print_help()
        sys.exit(1)

    conversation_id = resolve_conversation_id(conn, args.session, cwd=str(Path.cwd()))

    # Load pricing
    pricing = None
    or_key = resolve_openrouter_api_key(args.openrouter_api_key)
    if or_key:
        pricing = fetch_openrouter_pricing(or_key)
        if not pricing:
            print("Warning: OpenRouter pricing fetch returned empty; costs will be skipped.", file=sys.stderr)

    model_map = load_model_map(args.model_map)

    report = build_report(
        conversation_id, conn, pricing, model_map, args.input_output_split,
    )

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_report(report)


if __name__ == "__main__":
    main()
