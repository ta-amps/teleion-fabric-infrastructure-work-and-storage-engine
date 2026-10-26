"""check_public finds secrets and private names, masks them, and honours the allow marker."""

from __future__ import annotations

import importlib.util
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


def test_private_name_hashes_only() -> None:
    source = (REPO / "scripts" / "check_public.py").read_text()
    source += (REPO / "scripts" / "private-names.sha256").read_text()
    # The scanner must flag a private name without containing it in plain text.
    assert check_public.private_names("see " + "".join(["tel", "eon"]) + ".dev for details")
    assert "".join(["tel", "eon"]) not in source.lower()


def test_ordinary_words_pass() -> None:
    assert not check_public.private_names("Telemetry and the baton relay are fine words.")
