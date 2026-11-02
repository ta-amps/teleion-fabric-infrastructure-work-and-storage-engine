"""The deploy guard's light, banned-word and ancestry checks (scripts/deploy_guard.py)."""

from __future__ import annotations

import codecs
import importlib.util
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


def test_the_worker_sources_are_light_and_free_of_banned_words() -> None:
    problems: list[str] = []
    for path in guard.worker_sources():
        text = path.read_text(encoding="utf-8")
        problems += guard.light_problems(path.name, text) + guard.banned_problems(path.name, text)
    assert problems == []


def test_dark_pages_and_banned_words_fail() -> None:
    assert guard.light_problems("x", LIGHT) == []  # a dark button on a light page is fine
    assert guard.light_problems(
        "x", LIGHT.replace("body{background:#F5F2EA}", "body{background:#102332}")
    )
    assert guard.light_problems("x", LIGHT.replace("--bg:#F5F2EA", "--bg:#091923"))
    assert guard.light_problems("x", LIGHT + "@media (prefers-color-scheme: dark){}")
    assert guard.light_problems("x", "<!doctype html><html><body>no scheme pinned</body></html>")
    word = codecs.decode("flagurgvp", "rot13")
    assert guard.banned_problems("x", f"a {word} page") and not guard.banned_problems(
        "x", "an example page"
    )


def test_ancestry_fails_closed() -> None:
    head = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    root = subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-list", "--max-parents=0", "HEAD"], text=True
    ).split()[0]
    assert guard.ancestry_problems(head) == [] and guard.ancestry_problems(root) == []
    assert guard.ancestry_problems("")
    assert guard.ancestry_problems("0" * 40)
