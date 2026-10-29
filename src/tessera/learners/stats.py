"""Deterministic outcome statistics over execution observations, shared by learners.

Statistics describe recorded runs. Paths saw different inputs, workers and times, so
differences are associations, not causal effects, admission evidence or permission.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

_Z95 = 1.959963984540054
_SPACE = re.compile(r"[ \t\n\v\f\r]+")


def wilson_interval(successes: int, trials: int) -> list[float] | None:
    """95% Wilson score interval for a binomial proportion."""
    if trials <= 0:
        return None
    p = successes / trials
    z2 = _Z95 * _Z95
    denominator = 1 + z2 / trials
    centre = (p + z2 / (2 * trials)) / denominator
    margin = _Z95 * math.sqrt(p * (1 - p) / trials + z2 / (4 * trials * trials)) / denominator
    return [max(0.0, centre - margin), min(1.0, centre + margin)]


def median(values: list[Any]) -> Any:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def reason_text(text: str) -> str:
    """First line, ASCII-folded and whitespace-collapsed, at most 160 code points."""
    first = re.split(r"[\r\n]", text, maxsplit=1)[0]
    folded = "".join(chr(ord(c) + 32) if "A" <= c <= "Z" else c for c in first)
    line = _SPACE.sub(" ", folded).strip(" ")
    return line[:160] if line else "unspecified"


def _top(counter: Counter[str], top: int) -> list[dict[str, Any]]:
    ranked = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    return [{"reason": reason, "count": count} for reason, count in ranked[:top]]


def _number(value: Any) -> bool:
    return type(value) in (int, float)


def outcome_statistics(observations: list[dict[str, Any]], top: int = 5) -> dict[str, Any]:
    """Counts, Wilson interval and medians; medians use accepted runs with known values."""
    accepted = [item for item in observations if item["ok"]]
    learned = [item["report"].get("learning") or {} for item in observations]
    durations = [item["observed"].get("server_duration_seconds") for item in accepted]
    totals = [item["report"].get("total_tokens") for item in accepted]
    accepted_learning = [item["report"].get("learning") or {} for item in accepted]
    reported = [value.get("duration_seconds") for value in accepted_learning]
    costs = [value["cost"] for value in accepted_learning if value.get("cost")]
    currencies = sorted({cost["currency"] for cost in costs})
    outcomes = Counter(value.get("outcome") or "unreported" for value in learned)
    failures = Counter(
        reason_text(item.get("error") or "") for item in observations if not item["ok"]
    )
    problems: Counter[str] = Counter()
    for value in learned:
        problems.update({reason_text(text) for text in value.get("went_wrong", [])})
    known_tokens = [value for value in totals if type(value) is int]
    known_reported = [value for value in reported if _number(value)]
    single = len(currencies) == 1
    return {
        "attempts": len(observations),
        "accepted": len(accepted),
        "failed": len(observations) - len(accepted),
        "acceptance_rate": len(accepted) / len(observations) if observations else None,
        "acceptance_wilson_95": wilson_interval(len(accepted), len(observations)),
        "tasks": len({item["task_id"] for item in observations}),
        "tasks_accepted": len({item["task_id"] for item in accepted}),
        "retried_attempts": sum(1 for item in observations if item["observed"].get("retried")),
        "median_server_duration_seconds": median([v for v in durations if _number(v)]),
        "median_total_tokens": median(known_tokens),
        "tokens_known": len(known_tokens),
        "median_reported_duration_seconds": median(known_reported),
        "reported_duration_known": len(known_reported),
        "cost": {
            "known": len(costs),
            "currency": currencies[0] if single else "mixed" if currencies else None,
            "median": median([cost["amount"] for cost in costs]) if single else None,
        },
        "worker_outcomes": dict(sorted(outcomes.items())),
        "failure_reasons": _top(failures, top),
        "reported_problems": _top(problems, top),
    }
