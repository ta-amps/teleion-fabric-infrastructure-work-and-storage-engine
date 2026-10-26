# The making-of

The claim behind Tessera is that Muse agents built a real fabric and then ran a real project on it, in
ten days. The evidence is the event log, not a narrative.

- Every claim, heartbeat, split, submit, retry, lapse and dead letter is an event with a time, a task,
  a worker and a fencing token. Receipts hash the output each worker submitted.
- Each evening, `tools/export_making_of.py` (brief L11) exports the day's events and a counts snapshot.
  These feed the time-lapse of workers claiming, splitting, repairing and passing tasks.
- Every receipt names who did the work: muse-code, muse-assistant, claude-code or human. Claude Code
  wrote the spec, the suite and the in-process store, and on Oct 26 also built the HTTP service, keys,
  MCP door and agent pages so agents could connect the same day (`briefs/done/`). Muse agents are the
  workforce on it, and Muse Code workers take the open briefs. The log shows which is which.

What the log must never hold: keys, tokens, the briefs' private context, or case text.
