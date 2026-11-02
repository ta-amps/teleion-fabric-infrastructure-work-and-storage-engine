"""The deploy guard's light, banned-word and ancestry checks (scripts/deploy_guard.py).

The real banned list lives outside this repository (rule 16), so these tests build a temporary one from
made-up patterns and the sha256 of a made-up name. No pattern from the real list appears here.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("deploy_guard", ROOT / "scripts" / "deploy_guard.py")
assert _spec and _spec.loader
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

LIGHT = (
    "<!doctype html><html><head><meta name='color-scheme' content='only light'><style>"
    ":root{color-scheme:only light;--bg:#F5F2EA}body{background:#F5F2EA}.btn{background:#2846B8}</style>"
)


def made_up_list(tmp_path: Path) -> dict[str, object]:
    path = tmp_path / "banned.json"
    path.write_text(
        json.dumps(
            {
                "case_insensitive": True,
                "always": {"patterns": [r"\bzzneverword\b"]},
                "visible_text": {"patterns": [r"\bzzproseword\b"]},
                "agent_apps_outside_models_page": {"patterns": [r"\bzzappname\b"]},
                "private_names": {"sha256": [hashlib.sha256(b"examplecorpzz").hexdigest()]},
            }
        )
    )
    banned: dict[str, object] = guard.load_banned(str(path))
    return banned


def test_the_worker_sources_are_light() -> None:
    problems: list[str] = []
    for path in guard.worker_sources():
        problems += guard.light_problems(path.name, path.read_text(encoding="utf-8"))
    assert problems == []


def test_dark_pages_fail() -> None:
    assert guard.light_problems("x", LIGHT) == []  # a dark button on a light page is fine
    assert guard.light_problems(
        "x", LIGHT.replace("body{background:#F5F2EA}", "body{background:#102332}")
    )
    assert guard.light_problems("x", LIGHT.replace("--bg:#F5F2EA", "--bg:#091923"))
    assert guard.light_problems("x", LIGHT + "@media (prefers-color-scheme: dark){}")
    assert guard.light_problems("x", "<!doctype html><html><body>no scheme pinned</body></html>")


def test_banned_words_come_from_the_shared_list(tmp_path: Path) -> None:
    banned = made_up_list(tmp_path)
    clean = guard.banned_findings("x", 'const a = "an ordinary page of prose";', banned)
    assert not any(clean.values())
    found = guard.banned_findings(
        "x", 'const id = 1; // zzneverword\nconst b = "a zzproseword in prose";', banned
    )
    assert found["always"] == ["x:1"] and found["visible"] == ["x: 1 in served prose"]
    private = guard.banned_findings("x", 'const c = "made by ExampleCorpZZ staff";', banned)
    assert private["private"] == ["x:1"]  # matched by hash; the name itself is never reported
    assert guard.banned_findings("x", 'const d = "try zzappname today";', banned)["apps"]


def test_ancestry_fails_closed() -> None:
    head = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    root = subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-list", "--max-parents=0", "HEAD"], text=True
    ).split()[0]
    assert guard.ancestry_problems(head) == [] and guard.ancestry_problems(root) == []
    assert guard.ancestry_problems("")
    assert guard.ancestry_problems("0" * 40)
