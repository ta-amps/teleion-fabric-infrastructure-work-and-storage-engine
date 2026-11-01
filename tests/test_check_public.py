"""check_public finds secrets and private names, masks them, and honours the allow marker."""

from __future__ import annotations

import hashlib
import importlib.util
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("check_public", REPO / "scripts" / "check_public.py")
assert _spec and _spec.loader
check_public = importlib.util.module_from_spec(_spec)
sys.modules["check_public"] = check_public
_spec.loader.exec_module(check_public)

FAKE_GH = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
FAKE_META = "LLM_" + "abcdefghijklmnopqrstuvwxyz012345"


def test_clean_tree(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("Tessera moves atomic tiles of work.\n")
    assert check_public.scan(tmp_path) == []


def test_finds_and_masks_tokens(tmp_path: Path) -> None:
    (tmp_path / "config.py").write_text(f"TOKEN = '{FAKE_GH}'\nKEY = '{FAKE_META}'\n")
    findings = check_public.scan(tmp_path)
    kinds = " ".join(findings)
    assert "github token" in kinds and "meta model api key" in kinds
    assert FAKE_GH not in kinds and FAKE_META not in kinds


def test_allow_marker_skips_line(tmp_path: Path) -> None:
    (tmp_path / "fixture.py").write_text(f"x = '{FAKE_GH}'  # check-public: allow\n")
    assert check_public.scan(tmp_path) == []


def test_a_listed_name_is_flagged_by_its_hash_alone(tmp_path: Path) -> None:
    # A made-up name, hashed into a temporary list inside the test: no real private name is
    # written or rebuilt here, and the checker sees only the hash.
    made_up = "examplecorp-zz"
    listed = tmp_path / "names.sha256"
    listed.write_text(hashlib.sha256(made_up.encode()).hexdigest() + "  # made up for this test\n")
    hashes = check_public.load_hashes(listed)
    assert check_public.private_names(f"Thanks to {made_up} for the review.", hashes)
    assert check_public.private_names(f"Thanks to {made_up.upper()} for the review.", hashes)
    assert not check_public.private_names("Thanks to the reviewers for the review.", hashes)
    assert not check_public.private_names("Thanks to examplecorp for the review.", hashes)
    (tmp_path / "tree").mkdir()
    (tmp_path / "tree" / "notes.txt").write_text(f"Credits: {made_up}\n")
    findings = check_public.scan(tmp_path / "tree", hashes)
    assert len(findings) == 1 and "private name" in findings[0]
    assert made_up not in findings[0]
    (tmp_path / "tree" / "notes.txt").write_text("Credits: the reviewers\n")
    assert check_public.scan(tmp_path / "tree", hashes) == []


def test_the_real_list_holds_only_hashes_and_is_the_default() -> None:
    # make public (part of make check, run in CI) scans every tracked file against this list.
    path = REPO / "scripts" / "private-names.sha256"
    entries = {line.split("#", 1)[0].strip() for line in path.read_text().splitlines()} - {""}
    assert entries and all(re.fullmatch(r"[0-9a-f]{64}", entry) for entry in entries)
    assert check_public.PRIVATE_NAME_HASHES == check_public.load_hashes(path) == frozenset(entries)


def test_ordinary_words_pass() -> None:
    assert not check_public.private_names("Telemetry and the baton relay are fine words.")
