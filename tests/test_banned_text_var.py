"""`make deploy` sets the BANNED_TEXT var from the one shared word list (rule 16). The list stays outside this repository,
so this test builds a made-up one: the var carries the always and visible-text patterns and never a private-name hash."""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_the_var_carries_the_patterns_and_no_private_name(tmp_path: Path) -> None:
    listing = {
        "always": {"patterns": [r"\bzzneverword\b"]},
        "visible_text": {"patterns": [r"\bzzhiddenword\b"]},
        "private_names": {"sha256": ["0" * 64]},
    }
    path = tmp_path / "list.json"
    path.write_text(json.dumps(listing), encoding="utf-8")
    out = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "banned_text_var.py")],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "BANNED_WORDS_FILE": str(path)},
    ).stdout
    assert json.loads(out) == {"always": [r"\bzzneverword\b"], "visible": [r"\bzzhiddenword\b"]}
    assert "0" * 64 not in out
