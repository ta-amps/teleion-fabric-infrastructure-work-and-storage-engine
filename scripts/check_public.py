#!/usr/bin/env python3
"""Fail if a file headed for a public repo holds a secret or a private name.

Scans the files git tracks (or the working tree before `git init`) for:
  * credentials: GitHub, Meta Model API, Hugging Face, AWS, Slack and OpenAI-style keys, worker keys,
    private keys;
  * private names: products, accounts and domains this repo must not name. Only SHA-256 hashes of them
    are stored, in `private-names.sha256` next to this script, so the list never leaks the names.

Usage: python3 scripts/check_public.py [ROOT]
       python3 scripts/check_public.py --hash      (prompts for a name, prints the line to add)
Exit 0 when clean, 1 with a report otherwise. Matches are masked in the report.
A line ending in the marker `check-public: allow` is skipped; so are paths listed in
`.check-public-allow` (one glob per line). Stdlib only; copy it into any repo.
"""

from __future__ import annotations

import fnmatch
import getpass
import hashlib
import re
import subprocess
import sys
from pathlib import Path

SECRET_PATTERNS: dict[str, re.Pattern[str]] = {
    "github token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"),
    "meta model api key": re.compile(r"\bLLM_[A-Za-z0-9_\-]{20,}"),
    "hugging face token": re.compile(r"\bhf_[A-Za-z0-9]{30,}"),
    "worker key": re.compile(r"\btsk_[A-Za-z0-9_\-]{30,}"),
    "aws access key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "slack token": re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    "openai-style key": re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{24,}"),
    "private key": re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"),
    "assigned secret": re.compile(
        r"(?i)\b(?:api[_-]?key|secret|token|password)\b\s*[:=]\s*['\"][A-Za-z0-9_\-/+]{20,}['\"]"
    ),
}

HASH_FILE = Path(__file__).resolve().with_name("private-names.sha256")
HEX64 = re.compile(r"[0-9a-f]{64}")
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


def load_hashes(path: Path = HASH_FILE) -> frozenset[str]:
    """Hashes from a file of one SHA-256 hex digest per line; `#` starts a comment."""
    if not path.exists():
        return frozenset()
    found = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.split("#", 1)[0].strip().lower()
        if HEX64.fullmatch(value):
            found.add(value)
    return frozenset(found)


PRIVATE_NAME_HASHES = load_hashes()


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


def private_names(line: str, hashes: frozenset[str] = PRIVATE_NAME_HASHES) -> bool:
    lowered = line.lower()
    tokens = set(WORD.findall(lowered)) | set(COMPOUND.findall(lowered))
    return any(hashlib.sha256(t.encode()).hexdigest() in hashes for t in tokens)


def scan(root: Path, hashes: frozenset[str] = PRIVATE_NAME_HASHES) -> list[str]:
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
            if private_names(line, hashes):
                findings.append(
                    f"{rel}:{number}: private name (compare with your private name list)"
                )
    return findings


def hash_line(name: str) -> str:
    """The line to add to private-names.sha256 for one name. Names match whole words or hyphenated words."""
    token = name.strip().lower()
    if not (WORD.fullmatch(token) or COMPOUND.fullmatch(token)):
        raise ValueError(
            "use one word or one hyphenated word (letters and digits), for example acme or acme-labs"
        )
    return hashlib.sha256(token.encode()).hexdigest()


def main(argv: list[str]) -> int:
    if len(argv) > 1 and argv[1] == "--hash":
        name = getpass.getpass("Name (hidden): ") if sys.stdin.isatty() else sys.stdin.readline()
        try:
            print(hash_line(name))
        except ValueError as exc:
            print(f"check_public: {exc}", file=sys.stderr)
            return 2
        return 0
    root = Path(argv[1] if len(argv) > 1 else ".").resolve()
    findings = scan(root)
    if findings:
        print(f"check_public: {len(findings)} finding(s) in {root}", file=sys.stderr)
        for f in findings:
            print("  " + f, file=sys.stderr)
        return 1
    print(f"check_public: clean ({root.name}, {len(PRIVATE_NAME_HASHES)} private names)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
