"""Locate the JSONL file Claude Code is writing for a given cwd."""

from __future__ import annotations

from pathlib import Path
from typing import Optional


def project_dir_for_cwd(cwd: str) -> Path:
    # Claude Code encodes both '/' and '_' as '-' in the project dir name.
    encoded = cwd.replace("/", "-").replace("_", "-")
    return Path.home() / ".claude" / "projects" / encoded


def active_jsonl_for_cwd(cwd: str) -> Optional[Path]:
    """Most-recently-modified .jsonl under the encoded project dir."""
    proj = project_dir_for_cwd(cwd)
    if not proj.exists():
        return None
    files = list(proj.glob("*.jsonl"))
    if not files:
        return None
    files.sort(key=lambda p: p.stat().st_mtime)
    return files[-1]
