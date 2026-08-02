#!/usr/bin/env python3
"""
Maps Claude Code token usage to the actual task/prompt that caused it,
across ALL local sessions in ~/.claude/projects.

Usage:
    python3 token_task_map.py
    python3 token_task_map.py --min-cost 1.0     # only show tasks costing $1+
    python3 token_task_map.py --project MemoryWeaver  # filter by project name
"""

import json
import re
import os
import sys
import argparse
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

def parse_date_arg(s):
    """Accepts YYYYMMDD or YYYY-MM-DD, returns a timezone-aware datetime."""
    if not s:
        return None
    s = s.replace("-", "")
    dt = datetime.strptime(s, "%Y%m%d")
    return dt.replace(tzinfo=timezone.utc)


def clean_task_label(text):
    """Strips harness-injected tags so a task is labelled by what was asked.

    IDE auto-context, system reminders and command wrappers get prepended to the
    user's message. Without this the most expensive task of the day is labelled
    with whatever file was open in the editor.
    """
    for pattern in (r"<ide_opened_file>.*?</ide_opened_file>",
                    r"<ide_selection>.*?</ide_selection>",
                    r"<system-reminder>.*?</system-reminder>",
                    r"<command-name>.*?</command-name>",
                    r"<command-message>.*?</command-message>",
                    r"<local-command-stdout>.*?</local-command-stdout>"):
        text = re.sub(pattern, " ", text, flags=re.S)
    return re.sub(r"\s+", " ", text).strip()

def parse_ts(ts):
    """Parse an ISO timestamp like 2026-07-31T06:29:39.911Z into aware datetime."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None

# Verified Anthropic list pricing as of Aug 2026, $ per million tokens:
# (input, output, cache_write [5-min TTL, 1.25x input], cache_read [10% of input])
# Source: Anthropic pricing docs / Opus 5 announcement (Jul 24, 2026)
PRICING = {
    "claude-opus-5":             (5.0, 25.0, 6.25, 0.50),
    "claude-opus-4-8":           (5.0, 25.0, 6.25, 0.50),
    "claude-opus-4-7":           (5.0, 25.0, 6.25, 0.50),
    "claude-sonnet-5":           (2.0, 10.0, 2.50, 0.20),   # introductory rate thru Aug 31 2026
    "claude-sonnet-4-6":         (3.0, 15.0, 3.75, 0.30),
    "claude-haiku-4-5-20251001": (1.0, 5.0, 1.25, 0.10),
    "claude-fable-5":            (10.0, 50.0, 12.50, 1.00),
    "claude-mythos-5":           (10.0, 50.0, 12.50, 1.00),
}
DEFAULT_PRICE = (5.0, 25.0, 6.25, 0.50)  # fallback ~ opus-tier

def get_price(model):
    for key, price in PRICING.items():
        if key in (model or ""):
            return price
    return DEFAULT_PRICE

def cost_for(usage, model):
    inp, out, cw, cr = get_price(model)
    u = usage or {}
    return (
        u.get("input_tokens", 0) * inp / 1_000_000
        + u.get("output_tokens", 0) * out / 1_000_000
        + u.get("cache_creation_input_tokens", 0) * cw / 1_000_000
        + u.get("cache_read_input_tokens", 0) * cr / 1_000_000
    )

def extract_tool_calls(content):
    """Return list of (tool_name, target_summary) for tool_use blocks in assistant content."""
    calls = []
    if not isinstance(content, list):
        return calls
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = block.get("name", "unknown")
        inp = block.get("input", {}) or {}
        target = ""
        if name in ("Read", "Edit", "Write", "NotebookEdit"):
            target = inp.get("file_path", "") or inp.get("path", "")
        elif name == "Bash":
            target = (inp.get("command", "") or "")[:80]
        elif name in ("Grep", "Glob"):
            target = inp.get("pattern", "") or inp.get("glob", "")
        elif name == "WebSearch":
            target = inp.get("query", "")
        elif name == "WebFetch":
            target = inp.get("url", "")
        elif name == "Task":
            target = (inp.get("description", "") or inp.get("prompt", ""))[:80]
        else:
            # generic fallback: first string value in input
            for v in inp.values():
                if isinstance(v, str):
                    target = v[:80]
                    break
        calls.append((name, target))
    return calls

def extract_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return " ".join(parts)
    return ""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-cost", type=float, default=0.0)
    ap.add_argument("--project", type=str, default=None)
    ap.add_argument("--root", type=str, default=str(Path.home() / ".claude" / "projects"))
    ap.add_argument("--since", type=str, default=None, help="YYYYMMDD or YYYY-MM-DD, inclusive")
    ap.add_argument("--until", type=str, default=None, help="YYYYMMDD or YYYY-MM-DD, inclusive")
    ap.add_argument("--technical", action="store_true",
                     help="Show tool-call breakdown (files touched, commands run, searches) per task")
    ap.add_argument("--max-targets", type=int, default=5,
                     help="Max example targets to show per tool type (default 5)")
    ap.add_argument("--show", action="store_true",
                     help="Show every task with no cost floor (same as --min-cost 0). "
                          "Mirrors daily_cost_log.py --show so the two scripts can be "
                          "smoke-tested with the same flag.")
    args = ap.parse_args()

    # --show means "hold nothing back", so it wins over --min-cost if both are
    # given. Everything else (--project, --since, --until) still applies.
    if args.show:
        args.min_cost = 0.0

    since_dt = parse_date_arg(args.since)
    until_dt = parse_date_arg(args.until)

    root = Path(args.root)
    if not root.exists():
        print(f"No such directory: {root}")
        sys.exit(1)

    # task_key -> aggregated stats
    tasks = defaultdict(lambda: {
        "cost": 0.0, "input": 0, "output": 0, "cache_create": 0, "cache_read": 0,
        "session": "", "project": "", "first_ts": None, "last_ts": None, "turns": 0,
        "tool_calls": defaultdict(list)  # tool_name -> list of targets
    })

    jsonl_files = []
    for proj_dir in root.iterdir():
        if not proj_dir.is_dir():
            continue
        if args.project and args.project not in proj_dir.name:
            continue
        jsonl_files.extend(proj_dir.glob("*.jsonl"))

    if not jsonl_files:
        print("No session files found (check --project filter or path).")
        sys.exit(0)

    for f in jsonl_files:
        session_id = f.stem
        project_name = f.parent.name
        current_task = "(no user message yet)"

        with open(f, "r", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg = d.get("message", {})
                role = msg.get("role") or d.get("type")

                # A new user text message starts a new "task"
                if role == "user":
                    content = msg.get("content")
                    text = clean_task_label(extract_text(content))
                    # Skip tool_result-only messages (they show up as role=user too)
                    if text and not text.startswith("<tool_result") and len(text) > 0:
                        # New task boundary
                        label = text[:100].replace("\n", " ")
                        current_task = f"{label}"

                # Assistant messages carry usage/cost info
                if role == "assistant":
                    usage = msg.get("usage")
                    model = msg.get("model", "")

                    # Apply date filter if provided
                    if usage and (since_dt or until_dt):
                        ts_dt = parse_ts(d.get("timestamp"))
                        if ts_dt:
                            if since_dt and ts_dt < since_dt:
                                continue
                            if until_dt and ts_dt > until_dt.replace(hour=23, minute=59, second=59):
                                continue

                    if usage:
                        key = (project_name, session_id, current_task)
                        t = tasks[key]
                        t["cost"] += cost_for(usage, model)
                        t["input"] += usage.get("input_tokens", 0)
                        t["output"] += usage.get("output_tokens", 0)
                        t["cache_create"] += usage.get("cache_creation_input_tokens", 0)
                        t["cache_read"] += usage.get("cache_read_input_tokens", 0)
                        t["session"] = session_id
                        t["project"] = project_name
                        t["turns"] += 1
                        ts = d.get("timestamp")
                        if ts:
                            if not t["first_ts"]:
                                t["first_ts"] = ts
                            t["last_ts"] = ts
                        # Extract tool calls from this assistant turn's content
                        for tool_name, target in extract_tool_calls(msg.get("content")):
                            t["tool_calls"][tool_name].append(target)

    # Sort by cost descending
    rows = sorted(tasks.items(), key=lambda kv: kv[1]["cost"], reverse=True)
    rows = [(k, v) for k, v in rows if v["cost"] >= args.min_cost]

    if not rows:
        print("No tasks found above the cost threshold.")
        return

    total_cost = sum(v["cost"] for _, v in rows)

    range_label = ""
    if args.since or args.until:
        range_label = f"  [{args.since or 'start'} -> {args.until or 'now'}]"

    print(f"\n{'='*100}")
    print(f"TOKEN → TASK MAP  ({len(rows)} tasks, ${total_cost:,.2f} total){range_label}")
    print(f"{'='*100}\n")

    for (project, session, task), v in rows:
        print(f"${v['cost']:>8.2f}  | {project} | session {session[:8]}")
        print(f"           task: \"{task}\"")
        print(f"           in={v['input']:,} out={v['output']:,} "
              f"cache_create={v['cache_create']:,} cache_read={v['cache_read']:,} "
              f"turns={v['turns']}")
        if v["first_ts"] and v["last_ts"]:
            print(f"           span: {v['first_ts']} -> {v['last_ts']}")

        if args.technical:
            tool_calls = v.get("tool_calls", {})
            if not tool_calls:
                print(f"           (no tool calls captured for this task)")
            else:
                # Sort tools by call count, descending
                sorted_tools = sorted(tool_calls.items(), key=lambda kv: len(kv[1]), reverse=True)
                print(f"           --- technical breakdown ---")
                for tool_name, targets in sorted_tools:
                    print(f"           {tool_name}: {len(targets)} call(s)")
                    # Show a few example targets, deduped, most-frequent-ish
                    seen = []
                    for tgt in targets:
                        if tgt and tgt not in seen:
                            seen.append(tgt)
                        if len(seen) >= args.max_targets:
                            break
                    for tgt in seen:
                        print(f"               - {tgt}")
                    if len(targets) > len(seen) and len(seen) >= args.max_targets:
                        print(f"               ... and {len(targets) - len(seen)} more call(s)")
        print()

if __name__ == "__main__":
    main()
