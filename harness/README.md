# Work harness

The harness is the layer above the queue that decides what work exists and in what order: tasks, facts,
sources, ratings, decisions and approvals, with playbooks for each kind of work. It is designed but not
built; it is workstream W (W1 to W20), and it must never crowd out the product.

## Rules that hold at every level

- Agents create only proposed tasks, at most 4 levels deep; the queue caps open tasks (see `limits.toml`).
- Nobody rates their own work; a refutation comes from a different model than the claim's author.
- Facts and decisions are append-only; a correction supersedes, it doesn't overwrite.
- Every send, publish, deploy and merge to main is Taylor's, at every autonomy level.

## Ordering

A different agent scores each task 1 to 5 on impact, urgency, unblocks, confidence and learning:

```
value    = 3 * impact + 2 * urgency + 2 * unblocks + learning
priority = value * (confidence / 5) / max(effort_minutes / 30, 0.5)
```

Blocked tasks never reach the queue; Taylor's pins and drops override the formula as recorded decisions.

## Autonomy

Level 2 is the default for the hackathon: agents create, rate and order tasks within the limits, and
Taylor reviews the top ten and the approvals twice a day. Level 3 (adopting improvements that win a test
Taylor approved in advance) is optional and comes last.
