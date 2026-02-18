"""
Tests for project_manager and CLAUDE.md propagation into worktrees.

Level 1 — Mechanical: verifies CLAUDE.md is present wherever Claude runs.
Level 2 — Behavioral: manual instructions for verifying agents follow the rules.
"""
import subprocess
from pathlib import Path
from unittest.mock import patch


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_claude_md(tmp_path: Path) -> Path:
    """Write a minimal CLAUDE.md template into tmp_path/config."""
    tpl = tmp_path / "CLAUDE.md"
    tpl.write_text("# Agent Rules\n- Never give up.\n")
    return tpl


# ── Level 1: mechanical tests ─────────────────────────────────────────────────

def test_create_project_copies_claude_md(tmp_path):
    """New project must contain CLAUDE.md committed to its git repo."""
    tpl = _make_claude_md(tmp_path)

    with patch("openclaw.tools.project_manager.PROJECTS_ROOT", tmp_path / "projects"), \
         patch("openclaw.tools.project_manager.CLAUDE_MD_TEMPLATE", tpl):
        from openclaw.tools.project_manager import create_project
        path = create_project("my-app")

    assert (path / "CLAUDE.md").exists(), "CLAUDE.md must exist in project root"
    # Verify it is actually committed (not just copied)
    result = subprocess.run(
        ["git", "log", "--oneline", "--", "CLAUDE.md"],
        cwd=path, capture_output=True, text=True
    )
    assert "CLAUDE.md" in result.stdout or result.stdout.strip() != "", \
        "CLAUDE.md must be committed, not just present on disk"


def test_claude_md_content_matches_template(tmp_path):
    """CLAUDE.md in project must match the template exactly."""
    tpl = _make_claude_md(tmp_path)

    with patch("openclaw.tools.project_manager.PROJECTS_ROOT", tmp_path / "projects"), \
         patch("openclaw.tools.project_manager.CLAUDE_MD_TEMPLATE", tpl):
        from openclaw.tools.project_manager import create_project
        path = create_project("my-app")

    assert (path / "CLAUDE.md").read_text() == tpl.read_text()


def test_worktree_inherits_claude_md(tmp_path):
    """A git worktree branched from the project must contain CLAUDE.md."""
    tpl = _make_claude_md(tmp_path)
    worktrees = tmp_path / "worktrees"

    with patch("openclaw.tools.project_manager.PROJECTS_ROOT", tmp_path / "projects"), \
         patch("openclaw.tools.project_manager.CLAUDE_MD_TEMPLATE", tpl), \
         patch("openclaw.tools.git.repo.WORKTREES", worktrees) \
            if False else patch("openclaw.adapters.git.repo.WORKTREES", worktrees):
        from openclaw.tools.project_manager import create_project
        from openclaw.adapters.git.repo import create_worktree, remove_worktree

        project = create_project("my-app")
        wt_path = create_worktree("test-worker", repo_root=project)

        try:
            assert (wt_path / "CLAUDE.md").exists(), \
                "CLAUDE.md must be present in every worker's worktree"
            assert (wt_path / "CLAUDE.md").read_text() == tpl.read_text()
        finally:
            remove_worktree("test-worker", repo_root=project)


def test_list_projects_returns_only_git_dirs(tmp_path):
    """list_projects must only return directories that have a .git folder."""
    tpl = _make_claude_md(tmp_path)

    with patch("openclaw.tools.project_manager.PROJECTS_ROOT", tmp_path / "projects"), \
         patch("openclaw.tools.project_manager.CLAUDE_MD_TEMPLATE", tpl):
        from openclaw.tools.project_manager import create_project, list_projects

        # Non-git dir should be ignored
        (tmp_path / "projects" / "not-a-repo").mkdir(parents=True)
        create_project("real-project")

        projects = list_projects()

    assert projects == ["real-project"]
