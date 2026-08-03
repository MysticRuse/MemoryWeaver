#!/usr/bin/env python3
"""
Appends a daily summary row (tokens, cost, top tasks, per-project cost) to a
persistent CSV log, and can generate both daily and cumulative/trend reports.

Usage:
    python3 daily_cost_log.py                    # log + report today
    python3 daily_cost_log.py --date 2026-08-01  # log + report a specific day
    python3 daily_cost_log.py --show             # print the raw CSV log
    python3 daily_cost_log.py --cumulative       # full-history trend report
    python3 daily_cost_log.py --save             # also save report as markdown
    python3 daily_cost_log.py --cumulative --save
"""

import json
import csv
import sys
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import defaultdict

PRICING = {
    "claude-opus-5":             (5.0, 25.0, 6.25, 0.50),
    "claude-opus-4-8":           (5.0, 25.0, 6.25, 0.50),
    "claude-opus-4-7":           (5.0, 25.0, 6.25, 0.50),
    "claude-sonnet-5":           (2.0, 10.0, 2.50, 0.20),
    "claude-sonnet-4-6":         (3.0, 15.0, 3.75, 0.30),
    "claude-haiku-4-5-20251001": (1.0, 5.0, 1.25, 0.10),
    "claude-fable-5":            (10.0, 50.0, 12.50, 1.00),
    "claude-mythos-5":           (10.0, 50.0, 12.50, 1.00),
}
DEFAULT_PRICE = (5.0, 25.0, 6.25, 0.50)

LOG_FILE = Path(__file__).resolve().parent / "claude_daily_log.csv"
CSV_HEADER = ["date", "cost_usd", "input_tokens", "output_tokens",
              "cache_create_tokens", "cache_read_tokens", "turns",
              "tasks", "projects", "project_costs", "top_task", "top_task_cost",
              "models_used"]
REPORTS_DIR = Path(__file__).resolve().parent / "claude_reports"


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


def parse_ts(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", type=str, default=None,
                     help="YYYY-MM-DD (defaults to today, UTC)")
    ap.add_argument("--root", type=str, default=str(Path.home() / ".claude" / "projects"))
    ap.add_argument("--show", action="store_true", help="Print the log file and exit")
    ap.add_argument("--cumulative", action="store_true",
                     help="Print a cumulative/trend report from the full log history")
    ap.add_argument("--save", action="store_true",
                     help="Also write the report to a markdown file in ~/claude_reports/")
    args = ap.parse_args()

    if args.show:
        if not LOG_FILE.exists():
            print(f"No log yet at {LOG_FILE}")
            return
        with open(LOG_FILE) as f:
            print(f.read())
        return

    if args.cumulative:
        run_cumulative_report(save=args.save)
        return

    if args.date:
        target_date = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=timezone.utc).date()
    else:
        target_date = datetime.now(timezone.utc).date()

    root = Path(args.root)
    if not root.exists():
        print(f"No such directory: {root}")
        sys.exit(1)

    total_cost = 0.0
    total_in = total_out = total_cc = total_cr = total_turns = 0
    task_costs = defaultdict(float)   # task label -> cost, for that day
    project_costs = defaultdict(float)  # project -> cost, for that day
    projects_touched = set()
    models_used = set()

    for proj_dir in root.iterdir():
        if not proj_dir.is_dir():
            continue
        for f in proj_dir.glob("*.jsonl"):
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

                    if role == "user":
                        text = extract_text(msg.get("content")).strip()
                        if text and not text.startswith("<tool_result"):
                            current_task = text[:80].replace("\n", " ")

                    if role == "assistant":
                        usage = msg.get("usage")
                        if not usage:
                            continue
                        ts_dt = parse_ts(d.get("timestamp"))
                        if not ts_dt or ts_dt.date() != target_date:
                            continue

                        model = msg.get("model", "")
                        c = cost_for(usage, model)
                        total_cost += c
                        total_in += usage.get("input_tokens", 0)
                        total_out += usage.get("output_tokens", 0)
                        total_cc += usage.get("cache_creation_input_tokens", 0)
                        total_cr += usage.get("cache_read_input_tokens", 0)
                        total_turns += 1
                        task_costs[current_task] += c
                        project_costs[proj_dir.name] += c
                        projects_touched.add(proj_dir.name)
                        if model:
                            models_used.add(model)

    if total_turns == 0:
        print(f"No Claude Code activity found for {target_date}.")
        return

    top_task, top_cost = max(task_costs.items(), key=lambda kv: kv[1])
    project_costs_rounded = {k: round(v, 2) for k, v in project_costs.items()}

    row = {
        "date": str(target_date),
        "cost_usd": round(total_cost, 2),
        "input_tokens": total_in,
        "output_tokens": total_out,
        "cache_create_tokens": total_cc,
        "cache_read_tokens": total_cr,
        "turns": total_turns,
        "tasks": len(task_costs),
        "projects": "|".join(sorted(projects_touched)),
        "project_costs": json.dumps(project_costs_rounded),
        "top_task": top_task,
        "top_task_cost": round(top_cost, 2),
        "models_used": "|".join(sorted(models_used)),
    }

    # Print human-readable summary
    lines = []
    lines.append(f"\n{'='*80}")
    lines.append(f"DAILY SUMMARY — {target_date}")
    lines.append(f"{'='*80}")
    lines.append(f"Total cost:        ${total_cost:.2f}")
    lines.append(f"Tokens in/out:     {total_in:,} / {total_out:,}")
    lines.append(f"Cache create/read: {total_cc:,} / {total_cr:,}")
    lines.append(f"Turns:             {total_turns}  across {len(task_costs)} tasks")
    lines.append(f"Projects touched:  {', '.join(sorted(projects_touched))}")
    for proj, c in sorted(project_costs.items(), key=lambda kv: -kv[1]):
        lines.append(f"    {proj}: ${c:.2f}")
    lines.append(f"Models used:       {', '.join(sorted(models_used))}")
    lines.append(f"Most expensive task: \"{top_task}\" (${top_cost:.2f})")
    lines.append(f"{'='*80}\n")
    report_text = "\n".join(lines)
    print(report_text)

    # Append (or update) the CSV log
    existing_rows = []
    if LOG_FILE.exists():
        with open(LOG_FILE, newline="") as f:
            existing_rows = list(csv.DictReader(f))

    # Replace existing entry for this date if present, else append
    existing_rows = [r for r in existing_rows if r["date"] != str(target_date)]
    existing_rows.append(row)
    existing_rows.sort(key=lambda r: r["date"])

    with open(LOG_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADER)
        writer.writeheader()
        writer.writerows(existing_rows)

    print(f"Logged to {LOG_FILE}  ({len(existing_rows)} day(s) tracked total)")

    if args.save:
        REPORTS_DIR.mkdir(exist_ok=True)
        out_path = REPORTS_DIR / f"daily_{target_date}.md"
        with open(out_path, "w") as f:
            f.write(f"# Daily Claude Code Report — {target_date}\n\n```\n{report_text}\n```\n")
        print(f"Saved report to {out_path}")


def run_cumulative_report(save=False):
    if not LOG_FILE.exists():
        print(f"No log yet at {LOG_FILE} — run the daily logger at least once first.")
        return

    with open(LOG_FILE, newline="") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        print("Log file is empty.")
        return

    total_cost = sum(float(r["cost_usd"]) for r in rows)
    total_in = sum(int(r["input_tokens"]) for r in rows)
    total_out = sum(int(r["output_tokens"]) for r in rows)
    total_cc = sum(int(r["cache_create_tokens"]) for r in rows)
    total_cr = sum(int(r["cache_read_tokens"]) for r in rows)
    total_turns = sum(int(r["turns"]) for r in rows)
    num_days = len(rows)
    avg_daily_cost = total_cost / num_days if num_days else 0

    # Per-project cumulative totals
    project_totals = defaultdict(float)
    for r in rows:
        pc = r.get("project_costs") or "{}"
        try:
            pc_dict = json.loads(pc)
        except json.JSONDecodeError:
            pc_dict = {}
        for proj, c in pc_dict.items():
            project_totals[proj] += c

    # Most expensive day
    top_day = max(rows, key=lambda r: float(r["cost_usd"]))

    # Week-over-week trend (last 7 days vs the 7 before that), if enough data
    sorted_rows = sorted(rows, key=lambda r: r["date"])
    last7 = sorted_rows[-7:]
    prev7 = sorted_rows[-14:-7] if len(sorted_rows) >= 14 else []
    last7_cost = sum(float(r["cost_usd"]) for r in last7)
    prev7_cost = sum(float(r["cost_usd"]) for r in prev7) if prev7 else None

    lines = []
    lines.append(f"\n{'='*80}")
    lines.append(f"CUMULATIVE REPORT — {num_days} day(s) tracked "
                 f"({sorted_rows[0]['date']} -> {sorted_rows[-1]['date']})")
    lines.append(f"{'='*80}")
    lines.append(f"Total cost to date:  ${total_cost:.2f}")
    lines.append(f"Average per day:     ${avg_daily_cost:.2f}")
    lines.append(f"Total turns:         {total_turns:,}")
    lines.append(f"Tokens in/out:       {total_in:,} / {total_out:,}")
    lines.append(f"Cache create/read:   {total_cc:,} / {total_cr:,}")
    lines.append("")
    lines.append("Cost by project (all time):")
    for proj, c in sorted(project_totals.items(), key=lambda kv: -kv[1]):
        pct = (c / total_cost * 100) if total_cost else 0
        lines.append(f"    {proj:<40} ${c:>8.2f}  ({pct:.0f}%)")
    lines.append("")
    lines.append(f"Most expensive single day: {top_day['date']} (${float(top_day['cost_usd']):.2f})")
    lines.append(f"    top task that day: \"{top_day['top_task']}\" (${float(top_day['top_task_cost']):.2f})")
    lines.append("")
    if prev7_cost is not None:
        delta = last7_cost - prev7_cost
        pct_change = (delta / prev7_cost * 100) if prev7_cost else 0
        direction = "up" if delta > 0 else "down"
        lines.append(f"Last 7 days: ${last7_cost:.2f}  vs  previous 7 days: ${prev7_cost:.2f}"
                     f"   ({direction} {abs(pct_change):.0f}%)")
    else:
        lines.append(f"Last {len(last7)} day(s): ${last7_cost:.2f}  "
                     f"(not enough history yet for a week-over-week comparison)")
    lines.append(f"{'='*80}\n")

    report_text = "\n".join(lines)
    print(report_text)

    if save:
        REPORTS_DIR.mkdir(exist_ok=True)
        out_path = REPORTS_DIR / f"cumulative_{datetime.now(timezone.utc).date()}.md"
        with open(out_path, "w") as f:
            f.write(f"# Cumulative Claude Code Report\n\n```\n{report_text}\n```\n")
        print(f"Saved report to {out_path}")


if __name__ == "__main__":
    main()
