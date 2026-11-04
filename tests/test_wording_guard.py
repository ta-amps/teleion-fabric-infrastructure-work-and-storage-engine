"""No tracked file uses the words the owner ruled out, apart from listed compatibility aliases.

Each word is held ROT13-encoded, so this file does not contain it. A line may use one only when the
same line carries a "compat-alias: <id>" comment whose id is listed in ALIASES for that word, with its
reason, and every listed alias must still be present exactly once, so a stale entry fails too. The
retired model family has no aliases at all. File paths are checked as well."""

from __future__ import annotations

import codecs
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# The retired wording for example content (2026-10-31) and the retired model family (2026-11-01).
WORDS = {
    name: re.compile(codecs.decode(rot13, "rot13"), re.IGNORECASE)
    for name, rot13 in {"example-wording": "flagurgvp", "model-family": "yynzn"}.items()
}
MARKER = re.compile(r"compat-alias: ([a-z0-9-]+)")
# Each alias keeps an older client, server or file working through the transition: id: (word, why).
ALIASES = {
    "tessera-error-code": (
        "example-wording",
        "src/tessera/errors.py: the refusal code example_only replaced, still mapped to InvalidRequest",
    ),
}


def test_no_tracked_file_uses_a_retired_word() -> None:
    files = subprocess.check_output(["git", "-C", str(REPO), "ls-files"], text=True).splitlines()
    seen: dict[str, int] = {}
    offending = [f"{path} (path)" for path in files for word in WORDS.values() if word.search(path)]
    for path in files:
        if (
            path == "NOTICE-META-MODELS.txt"
        ):  # the one file that holds license wording (owner, Nov 4)
            continue
        try:
            text = (REPO / path).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if not any(word.search(text) for word in WORDS.values()):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            found = [name for name, word in WORDS.items() if word.search(line)]
            if not found:
                continue
            marker = MARKER.search(line)
            alias = ALIASES.get(marker.group(1)) if marker else None
            if marker and alias and found == [alias[0]]:
                seen[marker.group(1)] = seen.get(marker.group(1), 0) + 1
            else:
                offending.append(f"{path}:{number}")
    assert offending == [], f"{len(offending)} lines use a retired word: {offending[:20]}"
    assert seen == dict.fromkeys(ALIASES, 1), f"each listed alias appears exactly once: {seen}"
