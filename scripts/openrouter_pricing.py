#!/usr/bin/env python3
"""
Shared OpenRouter pricing helpers used by both the OpenCode and Warp backends.

Provides:
    - resolve_openrouter_api_key   Resolve API key from flag / env / auth.json
    - fetch_openrouter_pricing     Fetch + cache per-model pricing from /api/v1/models
    - compute_openrouter_cost      Compute cost from token counts and pricing dict
"""

import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

# ── Constants ─────────────────────────────────────────────────────────────────

PRICING_CACHE_PATH = Path(tempfile.gettempdir()) / "openrouter_pricing_cache.json"
PRICING_CACHE_TTL = 3600  # 1 hour


# ── API key resolution ────────────────────────────────────────────────────────

def resolve_openrouter_api_key(provided: Optional[str]) -> Optional[str]:
    """Resolve the OpenRouter API key from CLI arg, env var, or auth.json."""
    if provided:
        return provided
    env_key = os.environ.get("OPENROUTER_API_KEY")
    if env_key:
        return env_key
    auth_paths = [
        Path.home() / ".local" / "share" / "opencode" / "auth.json",
        Path.home() / ".config" / "opencode" / "auth.json",
    ]
    for p in auth_paths:
        if p.exists():
            try:
                auth = json.loads(p.read_text())
                key = auth.get("openrouter", {}).get("key")
                if key:
                    return key
            except (json.JSONDecodeError, OSError, KeyError):
                pass
    return None


# ── Pricing fetch + cache ─────────────────────────────────────────────────────

def fetch_openrouter_pricing(api_key: str) -> dict[str, dict[str, float]]:
    """Fetch per-token pricing from OpenRouter API with disk caching.

    Returns ``{model_id: {prompt, completion, input_cache_read, input_cache_write}}``.
    All prices are per-token (e.g. ``0.0000005`` = $0.50 / 1M tokens).
    """
    if PRICING_CACHE_PATH.exists():
        age = time.time() - PRICING_CACHE_PATH.stat().st_mtime
        if age < PRICING_CACHE_TTL:
            try:
                return json.loads(PRICING_CACHE_PATH.read_text())
            except (json.JSONDecodeError, OSError):
                pass

    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/models",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = json.loads(resp.read())
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, json.JSONDecodeError):
        # If stale cache exists, use it as fallback
        if PRICING_CACHE_PATH.exists():
            try:
                return json.loads(PRICING_CACHE_PATH.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    pricing = {}
    for m in raw.get("data", []):
        mid = m.get("id")
        if not mid:
            continue
        p = m.get("pricing") or {}
        pricing[mid] = {
            "prompt": float(p.get("prompt", 0)),
            "completion": float(p.get("completion", 0)),
            "input_cache_read": float(p.get("input_cache_read", 0)),
            "input_cache_write": float(p.get("input_cache_write", 0)),
        }

    try:
        PRICING_CACHE_PATH.write_text(json.dumps(pricing))
    except OSError:
        pass

    return pricing


# ── Cost computation ──────────────────────────────────────────────────────────

def compute_openrouter_cost(
    tokens_input: int,
    tokens_output: int,
    tokens_cache_read: int,
    tokens_cache_write: int,
    model_pricing: dict[str, float],
) -> float:
    """Compute USD cost from token counts and per-token pricing dict."""
    return (
        tokens_input * model_pricing.get("prompt", 0)
        + tokens_output * model_pricing.get("completion", 0)
        + tokens_cache_read * model_pricing.get("input_cache_read", 0)
        + tokens_cache_write * model_pricing.get("input_cache_write", 0)
    )


def compute_openrouter_cost_single_total(
    total_tokens: int,
    input_output_split: float,
    model_pricing: dict[str, float],
) -> float:
    """Compute USD cost from a single token total with an assumed input/output split.

    Args:
        total_tokens: Combined token count.
        input_output_split: Fraction of tokens assumed to be input (0.0-1.0).
        model_pricing: Per-token rates dict with keys prompt/completion/etc.
    """
    input_tokens = int(total_tokens * input_output_split)
    output_tokens = total_tokens - input_tokens
    return (
        input_tokens * model_pricing.get("prompt", 0)
        + output_tokens * model_pricing.get("completion", 0)
    )
