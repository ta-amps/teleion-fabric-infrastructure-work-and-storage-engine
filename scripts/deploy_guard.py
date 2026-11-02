"""Guards to run before every Tessera Worker deploy (rules 15 and 16). Each fails closed, and `make deploy` runs them.

1. Clean: `deploy/` has no uncommitted change, so the deployed code is HEAD.
2. Light, owner rule 13:
   - every HTML page the Worker builds pins color-scheme "only light" in its meta tag and on :root;
   - no stylesheet uses prefers-color-scheme or a dark color-scheme;
   - page-level backgrounds (html, body, :root --bg, main, header, section, the agent-start panel) have
     relative luminance of at least 0.25.
   Buttons and other small elements may be darker.
3. Banned words, rule 16. They are read at deploy time from the ONE shared list named by BANNED_WORDS_FILE.
   The list stays outside this repository, and no pattern from it is copied here. The checks:
   - `always` patterns anywhere in a Worker source;
   - private names, by the sha256 of each lowercase letters-and-digits token (never printed);
   - agent-app names in served prose;
   - `visible_text` patterns in served prose (human-readable string literals).
   VISIBLE_TEXT_GATE=report only counts the visible-text findings; any other value blocks.
4. Ancestry: the live source commit is an ancestor of HEAD. It comes from LIVE_SOURCE_COMMIT, or from the
   shared deploy log through DEPLOY_LOCK (a path to deploy_lock.py), whose `live tessera` prints the last
   logged commit. Neither set, or an unknown commit, is a failure.

    BANNED_WORDS_FILE=<list.json> LIVE_SOURCE_COMMIT=<sha> uv run python scripts/deploy_guard.py
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "deploy" / "cloudflare"
PAGE_SELECTORS = re.compile(
    r"(?<![\w\-])(html|body|:root|main|header|section|\.ta-start)\s*\{([^}]*)\}", re.I
)
DARK_SCHEME = re.compile(
    r"prefers-color-scheme|color-scheme\s*:\s*(dark|light\s+dark|dark\s+light|normal)"
    r"|content=['\"](dark|light dark|dark light)['\"]",
    re.I,
)
STRING = re.compile(r"`(?:\\.|[^\\`])*`|'(?:\\.|[^'\\\n])*'|\"(?:\\.|[^\"\\\n])*\"", re.S)
PLACEHOLDER = re.compile(r"\$\{[^}]*\}|<code>.*?</code>|`[^`]*`|\{[a-z_]+\}")
MARKUP = re.compile(r"<(script|style|code|pre)\b.*?</\1>|<[^>]+>", re.S | re.I)


def luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    if len(h) in (3, 4):
        h = "".join(c * 2 for c in h[:3])
    r, g, b = (int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))

    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def light_problems(name: str, text: str) -> list[str]:
    found = [f"{name}: {m.group(0)}" for m in DARK_SCHEME.finditer(text)]
    if re.search(r"<!doctype html>", text, re.I):
        if not re.search(r"name=['\"]color-scheme['\"]\s+content=['\"]only light['\"]", text):
            found.append(
                f"{name}: an HTML page without <meta name=color-scheme content='only light'>"
            )
        if not re.search(r":root\s*\{[^}]*color-scheme\s*:\s*only light", text):
            found.append(f"{name}: an HTML page without :root{{color-scheme:only light}}")
    for m in PAGE_SELECTORS.finditer(text):
        for prop, color in re.findall(
            r"(background(?:-color)?|--bg)\s*:\s*(#[0-9a-fA-F]{3,8})\b", m.group(2)
        ):
            if luminance(color) < 0.25:
                found.append(f"{name}: {m.group(1)} {prop}:{color} is dark")
    return found


def served_prose(text: str) -> str:
    """Human-readable string literals with placeholders and markup removed."""
    out = []
    for m in STRING.finditer(text):
        body = m.group(0)[1:-1]
        if " " in body and re.search(r"[A-Za-z]{3,} [A-Za-z]{2,}", body):
            out.append(MARKUP.sub(" ", PLACEHOLDER.sub(" ", body)))
    return "\n".join(out)


def load_banned(path: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(Path(path).read_text(encoding="utf-8"))
    flags = re.I if data.get("case_insensitive", True) else 0
    return {
        "always": [re.compile(p, flags) for p in data["always"]["patterns"]],
        "visible": [re.compile(p, flags) for p in data["visible_text"]["patterns"]],
        "apps": [
            re.compile(p, flags)
            for p in data.get("agent_apps_outside_models_page", {}).get("patterns", [])
        ],
        "private": set(data["private_names"]["sha256"]),
    }


def banned_findings(name: str, text: str, banned: dict[str, Any]) -> dict[str, list[str]]:
    """Findings by kind; a finding names the file and line or section, never the matched text."""
    found: dict[str, list[str]] = {"always": [], "private": [], "apps": [], "visible": []}
    for n, line in enumerate(text.splitlines(), 1):
        if any(p.search(line) for p in banned["always"]):
            found["always"].append(f"{name}:{n}")
        tokens = re.findall(r"[a-z0-9]+", line.lower())
        if any(hashlib.sha256(t.encode()).hexdigest() in banned["private"] for t in tokens):
            found["private"].append(f"{name}:{n}")
    prose = served_prose(text)
    for kind in ("apps", "visible"):
        hits = sum(len(p.findall(prose)) for p in banned[kind])
        if hits:
            found[kind].append(f"{name}: {hits} in served prose")
    return found


def worker_sources() -> list[Path]:
    exts = {".js", ".mjs", ".json", ".jsonc", ".html", ".css"}
    return sorted(p for p in WORKER.rglob("*") if p.is_file() and p.suffix in exts)


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=False
    )


def live_commit() -> str:
    if commit := os.environ.get("LIVE_SOURCE_COMMIT", "").strip():
        return commit
    if lock := os.environ.get("DEPLOY_LOCK", "").strip():
        out = subprocess.run(
            [sys.executable, lock, "live", "tessera"], capture_output=True, text=True, check=False
        )
        return out.stdout.strip().split()[-1] if out.returncode == 0 and out.stdout.strip() else ""
    return ""


def ancestry_problems(commit: str) -> list[str]:
    if not commit:
        return ["ancestry: set LIVE_SOURCE_COMMIT (or DEPLOY_LOCK) to the live source commit"]
    if git("cat-file", "-e", f"{commit}^{{commit}}").returncode != 0:
        return [f"ancestry: live commit {commit} is unknown here; fetch it first"]
    if git("merge-base", "--is-ancestor", commit, "HEAD").returncode != 0:
        return [f"ancestry: live commit {commit} is not an ancestor of HEAD; merge it first"]
    return []


def main() -> int:
    problems = [
        f"clean: {line}"
        for line in git("status", "--porcelain", "--", "deploy").stdout.splitlines()
    ]
    notes: list[str] = []
    list_path = os.environ.get("BANNED_WORDS_FILE", "").strip()
    banned = load_banned(list_path) if list_path and Path(list_path).is_file() else None
    if banned is None:
        problems.append("banned words: set BANNED_WORDS_FILE to the shared list (rule 16)")
    report_visible = os.environ.get("VISIBLE_TEXT_GATE", "") == "report"
    for path in worker_sources():
        text = path.read_text(encoding="utf-8")
        name = str(path.relative_to(ROOT))
        problems += light_problems(name, text)
        if banned is not None:
            found = banned_findings(name, text, banned)
            problems += [f"always: {f}" for f in found["always"]] + [
                f"private name: {f}" for f in found["private"]
            ]
            problems += [f"agent app name: {f}" for f in found["apps"]]
            (notes if report_visible else problems).extend(
                f"visible text: {f}" for f in found["visible"]
            )
    problems += ancestry_problems(live_commit())
    for note in notes:
        print("deploy guard (report only):", note, file=sys.stderr)
    for problem in problems:
        print("deploy guard:", problem, file=sys.stderr)
    if not problems:
        head = git("rev-parse", "--short", "HEAD").stdout.strip()
        print(
            f"deploy guard: clean, light, banned words gated, live commit is an ancestor of {head}"
        )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
