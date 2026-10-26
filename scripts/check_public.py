#!/usr/bin/env python3
"""Fail if a file headed for a public repo holds a secret or a private name.

Scans the files git tracks (or the working tree before `git init`) for:
  * credentials: GitHub, Meta Model API, Hugging Face, AWS, Slack and OpenAI-style keys, private keys;
  * private names: products and accounts that must not be linked from this project. Only SHA-256 hashes
    of those names live here, so this file never leaks them itself.

Usage: python3 scripts/check_public.py [ROOT]
Exit 0 when clean, 1 with a report otherwise. Matches are masked in the report.
A line ending in the marker `check-public: allow` is skipped; so are paths listed in
`.check-public-allow` (one glob per line).
"""

from __future__ import annotations

import fnmatch
import hashlib
import re
import subprocess
import sys
from pathlib import Path

SECRET_PATTERNS: dict[str, re.Pattern[str]] = {
    "github token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"),
    "meta model api key": re.compile(r"\bLLM_[A-Za-z0-9_\-]{20,}"),
    "hugging face token": re.compile(r"\bhf_[A-Za-z0-9]{30,}"),
    "tessera key": re.compile(r"\btsk_[A-Za-z0-9_\-]{30,}"),
    "aws access key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "slack token": re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    "openai-style key": re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{24,}"),
    "private key": re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"),
    "assigned secret": re.compile(
        r"(?i)\b(?:api[_-]?key|secret|token|password)\b\s*[:=]\s*['\"][A-Za-z0-9_\-/+]{20,}['\"]"
    ),
}

# SHA-256 of lowercased private names (products, accounts and domains kept out of public repos).
PRIVATE_NAME_HASHES = frozenset(
    {
        "c56a6b2453ec7e601ec43eb9eff3dc57639b189b4047f36c854bbc97d3e31611",
        "250cbac9037ac2bf5b7ae28410f9a7fc5a5d14bdd0c2208a1fb78dd94d23b84f",
        "395db1170c8432dc6903d3e0f0073876afa2dc7a264a5da1897386ace3a72c14",
        "f2a94c3d85aa8aa25172ed8a044651185719c5ef2ebe6704aba68e31a696ca72",
        "52bba447c070c819244cd5cb783cafbc9df4f1036f48a92f168968e4a0792df1",
        "01f029eec716e1a97c4dc5c948fd9df7f486f18b92be4fd6c23df75d38feab30",
        "14522cf6de1e7deb81a88d54ca15624bf3a23a5c0a633b4349caf36673c9f70e",
        "3b12ea4035d39940ea4dfa3d7345fdeaf17d6658bfe0060a48e3007237364a9d",
        "253c0ed8af21c5f8fce99b5e1efa92711d5833a979f7ad7e08472f661e27ec54",
        "c13c416a27186d4df2a0fa8840e922d6ce63fe66603ff3c0658c9bd6ce63a59d",
        "6933746bf5e89d143a03dd8e3a7c79aba711d634efd0ef724629191ab38fcf5d",
        "85b44b3af2ad97c8be1db24cf7a69b297cdd4d1748be608b7e0851b396ea47d0",
        "6bca81d7bdcf9fb6f98f4c4653c323f7106125cd07d7efd2aa05e2e21172c02c",
        "c33ad02d415db403ff4336eb3cd3de97c05f144cfe50816b450c3968bc187580",
        "6ccccbd9ccb22840d21fc81a2eb745f2dd020430472c4bf872287606b2a609a2",
        "4dac5ef5a91ee7822c105e3649a3869b48cbb65c6294e428f17c4e38f93fec4b",
        "434e1978f5a7214f757b43cd50483a75f0ebd2132f870de0a515c0a853cfe8f2",
    }
)

WORD = re.compile(r"[a-z0-9]+")
COMPOUND = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)+")
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
    ".tessera",
    ".shamash",
    "test-results",
}
SKIP_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".pdf",
    ".woff",
    ".woff2",
    ".ttf",
    ".mp4",
    ".mov",
    ".wav",
    ".mp3",
    ".zip",
    ".gz",
    ".db",
    ".sqlite",
    ".lock",
}
ALLOW_MARKER = "check-public: allow"


def tracked_files(root: Path) -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-co", "--exclude-standard"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        files = [root / line for line in out.splitlines() if line]
    except (OSError, subprocess.CalledProcessError):
        files = [p for p in root.rglob("*") if p.is_file()]
    return [
        p
        for p in files
        if p.is_file()
        and not (set(p.relative_to(root).parts) & SKIP_DIRS)
        and p.suffix.lower() not in SKIP_SUFFIXES
    ]


def allow_globs(root: Path) -> list[str]:
    path = root / ".check-public-allow"
    if not path.exists():
        return []
    return [
        ln.strip() for ln in path.read_text().splitlines() if ln.strip() and not ln.startswith("#")
    ]


def mask(text: str) -> str:
    return text[:4] + "…" + f"({len(text)} chars)"


def private_names(line: str) -> bool:
    lowered = line.lower()
    tokens = set(WORD.findall(lowered)) | set(COMPOUND.findall(lowered))
    return any(hashlib.sha256(t.encode()).hexdigest() in PRIVATE_NAME_HASHES for t in tokens)


def scan(root: Path) -> list[str]:
    findings: list[str] = []
    globs = allow_globs(root)
    for path in tracked_files(root):
        rel = path.relative_to(root).as_posix()
        if any(fnmatch.fnmatch(rel, g) for g in globs):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if ALLOW_MARKER in line:
                continue
            for kind, pattern in SECRET_PATTERNS.items():
                for match in pattern.finditer(line):
                    findings.append(f"{rel}:{number}: {kind}: {mask(match.group(0))}")
            if private_names(line):
                findings.append(
                    f"{rel}:{number}: private name (see the name map in your private notes)"
                )
    return findings


def main(argv: list[str]) -> int:
    root = Path(argv[1] if len(argv) > 1 else ".").resolve()
    findings = scan(root)
    if findings:
        print(f"check_public: {len(findings)} finding(s) in {root}", file=sys.stderr)
        for f in findings:
            print("  " + f, file=sys.stderr)
        return 1
    print(f"check_public: clean ({root.name})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
