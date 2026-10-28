@AGENTS.md

## Worker execution direction

Follow the [worker-first execution policy](docs/WORKER-FIRST-EXECUTION.md) when designing agentic work. Prefer claimed verbs and authorized workers, including private workers. Keep ordinary coordination deterministic; additional review is a verb when required. The policy does not widen effect, spending or publication authority.

# Claude Code notes

- The guard hook (`.claude/hooks/guard.py`, rules in `.claude/guard.json`) enforces the hard rules in
  every permission mode and fails closed. If it blocks something you need, stop and ask Taylor; don't
  route around it.
- The format hook runs ruff on every Python file you edit.
- Skills: `/gate <backend>` runs the conformance suite and reports which requirements pass;
  `/brief <task id>` writes a Muse Code brief (written, never sent).
- Subagent: `reviewer` reads the diff against AGENTS.md and the task's done-when before every commit.
- One task per session; `/clear` between tasks.
- Muse Code builds the networked service from `briefs/`. When a Muse task misses its day and you take it
  over, say so in the receipt.
