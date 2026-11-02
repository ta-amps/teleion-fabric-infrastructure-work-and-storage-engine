"""Print the BANNED_TEXT var for `make deploy`: the shared list's always and visible-text patterns as compact JSON.

The list is read from BANNED_WORDS_FILE, outside this repository (rule 16). Private names are hashes and are not
passed. Pages that render stored data mask these patterns at display time (deploy/cloudflare/display_mask.js).
"""

import json
import os
import sys
from pathlib import Path

data = json.loads(Path(os.environ["BANNED_WORDS_FILE"]).read_text(encoding="utf-8"))
json.dump(
    {"always": data["always"]["patterns"], "visible": data["visible_text"]["patterns"]},
    sys.stdout,
    separators=(",", ":"),
)
