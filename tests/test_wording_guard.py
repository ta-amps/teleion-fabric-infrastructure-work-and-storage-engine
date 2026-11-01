"""No tracked file uses the word the owner ruled out, apart from listed compatibility aliases.

The word is held ROT13-encoded, so this file does not contain it. A line may use it only when the
same line carries a "compat-alias: <id>" comment whose id is listed in ALIASES with its reason, and
every listed alias must still be present exactly once, so a stale entry fails too. File paths are
checked as well."""

from __future__ import annotations

import codecs
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WORD = re.compile(codecs.decode("flagurgvp", "rot13"), re.IGNORECASE)
MARKER = re.compile(r"compat-alias: ([a-z0-9-]+)")
# Each alias keeps an older client, server or file working through the transition.
ALIASES = {
    "tessera-error-code": "src/tessera/errors.py: the refusal code example_only replaced, still mapped to InvalidRequest",
}


def test_no_tracked_file_uses_the_retired_word() -> None:
    files = subprocess.check_output(["git", "-C", str(REPO), "ls-files"], text=True).splitlines()
    seen: dict[str, int] = {}
    offending = [f"{path} (path)" for path in files if WORD.search(path)]
    for path in files:
        try:
            text = (REPO / path).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if not WORD.search(text):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if not WORD.search(line):
                continue
            marker = MARKER.search(line)
            if marker and marker.group(1) in ALIASES:
                seen[marker.group(1)] = seen.get(marker.group(1), 0) + 1
            else:
                offending.append(f"{path}:{number}")
    assert offending == [], f"{len(offending)} lines use the retired word: {offending[:20]}"
    assert seen == dict.fromkeys(ALIASES, 1), f"each listed alias appears exactly once: {seen}"
