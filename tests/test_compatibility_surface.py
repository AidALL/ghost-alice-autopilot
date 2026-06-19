"""Compatibility-surface checks between Ghost-ALICE core and this addon."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_user_facing_install_docs_do_not_reference_deleted_p6_branches() -> None:
    stale_refs = ("p6-privileged-adapter", "p6-autopilot")
    for rel in ("README.md", "README_ko.md"):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        for stale in stale_refs:
            assert stale not in text, f"{rel} still references deleted branch/worktree {stale!r}"


def test_public_docs_do_not_expose_internal_p6_phase_label() -> None:
    public_surfaces = (
        "README.md",
        "README_ko.md",
        "addons/autopilot-mode/skill/SKILL.md",
    )
    for rel in public_surfaces:
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "P6" not in text, f"{rel} exposes internal phase label 'P6'"


def test_core_repo_auto_discovery_uses_current_main_checkout_not_deleted_worktree() -> None:
    text = (REPO_ROOT / "tests" / "test_privileged_adapter.py").read_text(encoding="utf-8")
    assert ".worktrees" not in text
    assert "p6-autopilot" not in text


def test_user_docs_warn_old_core_can_install_inert_skill_without_adapter() -> None:
    english_expected = ("0.1.3", "inert", "without wiring the privileged adapter")
    for rel in ("README.md", "addons/autopilot-mode/skill/SKILL.md"):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        for phrase in english_expected:
            assert phrase in text, f"{rel} does not document old-core inert install behavior: {phrase!r}"
    korean = (REPO_ROOT / "README_ko.md").read_text(encoding="utf-8")
    for phrase in ("0.1.3", "skill만 복사", "privileged adapter", "inert"):
        assert phrase in korean, f"README_ko.md does not document old-core inert install behavior: {phrase!r}"
