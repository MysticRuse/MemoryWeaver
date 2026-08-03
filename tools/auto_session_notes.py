#!/usr/bin/env python3
"""
Fires from the Claude Code SessionEnd hook. Appends a timestamped, purely
mechanical snapshot of git state to SESSION_NOTES.md in the current project
directory (creating the file if it doesn't exist yet).

This costs zero tokens — it's plain git commands, not a model call — so it's
guaranteed to run every time a session ends, regardless of whether Claude
remembered to write a prose handoff note in that session.

It is deliberately NOT a substitute for Claude's own narrative SESSION_NOTES.md
entries (the "what we decided and why") — it's a safety net under them, so the
raw facts (what changed, what's still uncommitted, last commit) are never lost
even if the prose summary gets skipped.

Usage (called by the hook, not typically run by hand):
    python3 ~/auto_session_notes.py
"""

import subprocess
import sys
from pathlib import Path
from datetime import datetime, timezone

NOTES_FILE = "SESSION_NOTES.md"


def run(cmd):
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=15
        )
        return result.stdout.strip() or "(none)"
    except Exception as e:
        return f"(error running command: {e})"


def main():
    cwd = Path.cwd()

    # Only act if this looks like a git repo — otherwise there's nothing
    # mechanical to capture, and we don't want to create noise elsewhere.
    is_git = subprocess.run(
        "git rev-parse --is-inside-work-tree",
        shell=True, capture_output=True, text=True
    ).returncode == 0
    if not is_git:
        return

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    branch = run("git branch --show-current")
    last_commit = run("git log -1 --oneline")
    status_short = run("git status --short")
    diff_stat = run("git diff --stat")
    staged_stat = run("git diff --cached --stat")

    # Cap each section's length so a huge diff doesn't itself become a
    # multi-hundred-KB file that costs money to re-read next session.
    def cap(text, n=1500):
        return text if len(text) <= n else text[:n] + f"\n... (truncated, {len(text)} chars total)"

    block = f"""
## Auto-captured state — {timestamp}
_(mechanical snapshot, zero-cost, written by the SessionEnd hook — not a substitute for a real handoff summary)_

- Branch: `{branch}`
- Last commit: `{last_commit}`

**Uncommitted changes (git status --short):**
```
{cap(status_short)}
```

**Unstaged diff summary:**
```
{cap(diff_stat)}
```

**Staged diff summary:**
```
{cap(staged_stat)}
```
"""

    notes_path = cwd / NOTES_FILE
    with open(notes_path, "a") as f:
        f.write(block)


if __name__ == "__main__":
    main()
