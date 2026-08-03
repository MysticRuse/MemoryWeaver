# Cost Tools — Cheat Sheet
Keep this file open in a text editor, or print it. Every command below is FREE —
run them in your terminal, never by asking Claude inside a session.

All three scripts now live inside MemoryWeaver/tools/. Run these commands from
inside the MemoryWeaver folder (cd /Users/hironmoy/MemoryWeaver first). If you're
working in a different project that day (Rewind, ClaimReady, etc.), use the full
path instead: python3 /Users/hironmoy/MemoryWeaver/tools/daily_cost_log.py ...

---

## "How much did today cost?"
```bash
python3 tools/daily_cost_log.py
```

## "How am I doing over time?" (trend, week-over-week)
```bash
python3 tools/daily_cost_log.py --cumulative
```

## "What exactly did the tokens do?" (which task, which files, which commands)
```bash
python3 tools/token_task_map.py --technical
```
Add `--min-cost 5` if the output is too long (only shows tasks over $5).

## "Save a snapshot before I clear" (git facts only, no Claude needed)
```bash
python3 tools/auto_session_notes.py
```

## "I'm done with this task, should I clear?"
Ask yourself just ONE question: **am I about to do something different from
what I just finished?**
- Yes → clear first.
- Unsure → clear anyway. Clearing too often costs nothing extra.

## The clear command itself
```
/clear
```
(typed into Claude, not your terminal)

---

## The only 2 rules worth memorizing (everything else is in this file)
1. **One task type per session.** Switch tasks → `/clear` first.
2. **Never ask Claude cost questions inside an active session.** Run the
   commands above in your terminal instead — same answer, zero cost.

## Note on location
This file, and the three scripts it refers to, now live inside the MemoryWeaver
project (tools/ folder) rather than the home folder. The cost tracking itself
still covers ALL your projects — only the script files and their saved data
moved, not what they track.
