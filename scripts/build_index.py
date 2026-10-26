#!/usr/bin/env python3
"""Build this repo's indexes from its own files.

  INDEX.md     every file, grouped by folder, with a one-line description taken from the file
  KEYWORDS.md  each term listed in keywords.txt, with the files that use it
  llms.txt     a short map for agents (llmstxt.org format): what the repo is and where to start
  llms-full.txt  the main documents in full, in reading order, for review tools and model reviewers

Usage: python3 scripts/build_index.py [--check]
--check exits 1 when a generated file is stale, so `make check` catches drift. Standard library only;
copy it into any repo. Links point at the GitHub `origin` remote (or INDEX_BASE_URL when set). The
reading order is `read_order` in architecture.json when that file exists, otherwise README.md,
AGENTS.md, DISCLAIMER.md and then the Markdown files in the document folders below.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

GENERATED = ("INDEX.md", "KEYWORDS.md", "llms.txt", "llms-full.txt")
SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "dist",
    "build",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "private",
    "test-results",
    ".hypothesis",
}
SKIP_FILES = {"uv.lock", "pnpm-lock.yaml", "package-lock.json", *GENERATED}
TEXT_SUFFIXES = {
    ".md",
    ".py",
    ".json",
    ".jsonc",
    ".toml",
    ".yml",
    ".yaml",
    ".sh",
    ".js",
    ".mjs",
    ".ts",
    ".tsx",
    ".css",
    ".html",
    ".sql",
    ".txt",
    ".cfg",
}
BARE_NAMES = {"Makefile", "LICENSE", "Dockerfile"}
MAX_FILES_PER_TERM = 12
# Folders whose Markdown pages are listed in llms.txt and, by default, read in full in llms-full.txt.
DOC_FOLDERS = ("docs", "guides", "event", "meta-platform", "conformance-draft", "conformance")
GITHUB = re.compile(r"github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")


def link_base(root: Path) -> str:
    """Prefix for links in llms.txt: INDEX_BASE_URL, else the GitHub origin remote, else relative."""
    if os.environ.get("INDEX_BASE_URL"):
        return os.environ["INDEX_BASE_URL"].rstrip("/") + "/"
    try:
        url = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""
    match = GITHUB.search(url)  # owner and repo only, so a credential in the URL never gets copied
    return f"https://github.com/{match.group(1)}/{match.group(2)}/blob/main/" if match else ""


def files(root: Path) -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-co", "--exclude-standard"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        paths = [root / line for line in out if line]
    except (OSError, subprocess.CalledProcessError):
        paths = [p for p in root.rglob("*") if p.is_file()]
    keep = []
    for p in paths:
        rel = p.relative_to(root)
        if not p.is_file() or set(rel.parts) & SKIP_DIRS or p.name in SKIP_FILES:
            continue
        if (
            p.suffix.lower() in TEXT_SUFFIXES
            or p.name in BARE_NAMES
            or (p.name.startswith(".") and p.suffix == "")
        ):
            keep.append(p)
    return sorted(keep, key=lambda p: p.relative_to(root).as_posix())


def first_sentence(text: str, limit: int = 110) -> str:
    text = re.sub(r"\s+", " ", text).strip().strip("#").strip()
    text = re.sub(r"[*_`]", "", text)
    match = re.match(r"(.+?[.!?])(\s|$)", text)
    sentence = match.group(1) if match else text
    return sentence if len(sentence) <= limit else sentence[: limit - 1].rstrip() + "…"


def describe(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return ""
    suffix = path.suffix.lower()
    lines = [ln.strip() for ln in text.splitlines()]
    if suffix == ".md":
        for ln in lines:
            if ln.startswith("#"):
                return first_sentence(ln.lstrip("#"))
        return first_sentence(next((ln for ln in lines if ln and not ln.startswith(">")), ""))
    if suffix == ".py":
        try:
            doc = ast.get_docstring(ast.parse(text))
        except SyntaxError:
            doc = None
        if doc:
            return first_sentence(doc)
    if suffix in {".json", ".jsonc"}:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            for key in ("title", "description", "name"):
                if isinstance(data.get(key), str):
                    prefix = f"{data['task_id']}: " if isinstance(data.get("task_id"), str) else ""
                    return first_sentence(prefix + data[key])
            return "JSON object: " + ", ".join(list(data)[:6])
        if isinstance(data, list):
            return f"JSON list of {len(data)} items"
    markers = ("//", "#", "--", "/*", "*")
    for ln in lines:
        if ln.startswith("#!") or not ln:
            continue
        if ln.startswith(markers):
            comment = ln.lstrip("/#-* ").strip()
            if comment and not comment.startswith(("!", "noqa", "type:")):
                return first_sentence(comment)
        break
    if path.name == "LICENSE":
        return first_sentence(lines[0] if lines else "")
    return ""


def readme_summary(root: Path) -> tuple[str, str]:
    readme = root / "README.md"
    if not readme.exists():
        return root.name, ""
    title, paragraph, collecting = root.name, [], False
    for ln in readme.read_text(encoding="utf-8").splitlines():
        s = ln.strip()
        if s.startswith("# ") and title == root.name:
            title = s[2:].strip()
            continue
        if s.startswith(">") and not paragraph:
            continue
        if s and not s.startswith(("#", "|", "```", "-")):
            paragraph.append(s)
            collecting = True
        elif collecting:
            break
    summary = re.sub(r"[*_`]|\[([^\]]+)\]\([^)]+\)", r"\1", " ".join(paragraph))
    return title, summary[:400]


def build_index(root: Path, paths: list[Path]) -> str:
    groups: dict[str, list[Path]] = defaultdict(list)
    for p in paths:
        rel = p.relative_to(root)
        groups["." if len(rel.parts) == 1 else rel.parts[0]].append(p)
    out = [
        f"# Index of {root.name}",
        "",
        "Every file in this repo with a one-line description taken from the file itself. Generated by "
        "`scripts/build_index.py`; run `make index` after adding files, never edit by hand.",
        "",
    ]
    for group in sorted(groups, key=lambda g: "" if g == "." else g):
        members = groups[group]
        out += [
            f"## {'Top level' if group == '.' else group + '/'} ({len(members)})",
            "",
            "| File | What it is |",
            "|---|---|",
        ]
        for p in members:
            rel = p.relative_to(root).as_posix()
            out.append(f"| [{rel}]({rel}) | {describe(p).replace('|', '/')} |")
        out.append("")
    return "\n".join(out)


def load_terms(root: Path) -> list[tuple[str, list[str]]]:
    path = root / "keywords.txt"
    if not path.exists():
        return []
    terms = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        term, _, aliases = ln.partition(":")
        terms.append((term.strip(), [a.strip() for a in aliases.split(",") if a.strip()]))
    return sorted(terms, key=lambda t: t[0].lower())


def build_keywords(root: Path, paths: list[Path]) -> str:
    texts = {}
    for p in paths:
        try:
            texts[p.relative_to(root).as_posix()] = p.read_text(encoding="utf-8").lower()
        except (UnicodeDecodeError, OSError):
            continue
    out = [
        f"# Keyword index of {root.name}",
        "",
        "Where each term appears. Terms come from `keywords.txt` (add a line there, then `make index`); "
        f"each row lists up to {MAX_FILES_PER_TERM} files, most mentions first.",
        "",
        "| Term | Files |",
        "|---|---|",
    ]
    for term, aliases in load_terms(root):
        patterns = [
            re.compile(r"(?<![\w-])" + re.escape(t.lower()) + r"(?![\w-])")
            for t in [term, *aliases]
        ]
        hits = []
        for rel, text in texts.items():
            count = sum(len(p.findall(text)) for p in patterns)
            if count:
                hits.append((count, rel))
        hits.sort(key=lambda h: (-h[0], h[1]))
        cell = ", ".join(f"[{rel}]({rel})" for _, rel in hits[:MAX_FILES_PER_TERM]) or "none yet"
        label = term + (f" ({', '.join(aliases)})" if aliases else "")
        out.append(f"| {label} | {cell} |")
    return "\n".join(out) + "\n"


def build_llms(root: Path, paths: list[Path]) -> str:
    title, summary = readme_summary(root)
    base = link_base(root)
    # INDEX.md and KEYWORDS.md are written by this script, so they are always listed.
    start = [p for p in ("README.md", "AGENTS.md") if (root / p).exists()]
    start += ["INDEX.md", "KEYWORDS.md", "llms-full.txt"]
    docs = [
        p
        for p in paths
        if p.suffix == ".md"
        and p.relative_to(root).as_posix() not in start
        and (len(p.relative_to(root).parts) == 1 or p.relative_to(root).parts[0] in DOC_FOLDERS)
    ]
    out = [
        f"# {title}",
        "",
        f"> {summary}" if summary else f"> The {root.name} repository.",
        "",
        "## Start here",
    ]
    names = {
        "README.md": "what this is and how to run it",
        "AGENTS.md": "rules every agent follows here",
        "INDEX.md": "every file with a description",
        "KEYWORDS.md": "where each term appears",
        "llms-full.txt": "the main documents in full, in reading order",
    }
    out += [f"- [{p}]({base}{p}): {names[p]}" for p in start]
    if docs:
        out += ["", "## Documents"]
        for p in docs:
            rel = p.relative_to(root).as_posix()
            out.append(f"- [{rel}]({base}{rel}): {describe(p)}")
    return "\n".join(out) + "\n"


def read_order(root: Path, paths: list[Path]) -> tuple[list[str], str]:
    """The documents for llms-full.txt and where their order came from."""
    known = {p.relative_to(root).as_posix() for p in paths}
    manifest = root / "architecture.json"
    if manifest.exists():
        try:
            order = json.loads(manifest.read_text(encoding="utf-8")).get("read_order")
        except json.JSONDecodeError:
            order = None
        if isinstance(order, list) and all(isinstance(x, str) for x in order):
            return [x for x in order if x in known], "read_order in architecture.json"
    first = [p for p in ("README.md", "AGENTS.md", "DISCLAIMER.md") if p in known]
    rest = sorted(
        rel
        for rel in known
        if rel.endswith(".md") and rel.split("/", 1)[0] in DOC_FOLDERS and "/" in rel
    )
    return first + rest, "README.md, AGENTS.md, DISCLAIMER.md, then the document folders"


def build_llms_full(root: Path, paths: list[Path]) -> str:
    title, summary = readme_summary(root)
    base = link_base(root)
    order, source = read_order(root, paths)
    out = [
        f"# {title}: full text",
        "",
        f"> {summary}" if summary else f"> The {root.name} repository.",
        "",
        f"The main documents of this repo in full, in reading order ({source}), so a review tool or a model can "
        "read the whole design from one file. Generated by `scripts/build_index.py`; the files themselves win "
        "if they ever differ.",
        "",
        "Contents: " + ", ".join(order),
        "",
    ]
    for rel in order:
        text = (root / rel).read_text(encoding="utf-8").rstrip()
        out += [
            "=" * 72,
            f"FILE: {rel}",
            f"URL: {base}{rel}" if base else f"PATH: {rel}",
            "=" * 72,
            "",
            text,
            "",
        ]
    return "\n".join(out)


def main(argv: list[str]) -> int:
    root = Path(__file__).resolve().parent.parent
    paths = files(root)
    outputs = {
        "INDEX.md": build_index(root, paths),
        "KEYWORDS.md": build_keywords(root, paths),
        "llms.txt": build_llms(root, paths),
        "llms-full.txt": build_llms_full(root, paths),
    }
    if "--check" in argv:
        stale = [
            n
            for n, text in outputs.items()
            if not (root / n).exists() or (root / n).read_text(encoding="utf-8") != text
        ]
        if stale:
            print(f"index: stale {', '.join(stale)}; run `make index`", file=sys.stderr)
            return 1
        print("index: up to date")
        return 0
    for name, text in outputs.items():
        (root / name).write_text(text, encoding="utf-8")
    print(f"index: wrote {', '.join(outputs)} ({len(paths)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
