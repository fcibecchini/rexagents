"""Add the rexagents hooks to Claude Code's user settings, or take them out."""

from __future__ import annotations

import copy
import difflib
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Callable

from .hook import is_ours


def without_hooks(config: dict) -> dict:
    """`config` without any rexagents hook, dropping only the groups and events that leaves empty."""
    config = copy.deepcopy(config)
    hooks = config.get("hooks")
    if not isinstance(hooks, dict) or not hooks:
        return config
    for event, groups in list(hooks.items()):
        kept_groups = []
        for group in groups:
            handlers = group.get("hooks", [])
            kept = [h for h in handlers if not is_ours(h.get("command", ""))]
            if kept or not handlers:
                kept_groups.append(group if len(kept) == len(handlers) else {**group, "hooks": kept})
        if kept_groups or not groups:
            hooks[event] = kept_groups
        else:
            del hooks[event]
    if not hooks:
        del config["hooks"]
    return config


def with_hooks(config: dict, ours: dict) -> dict:
    """`config` with exactly one copy of our hooks, after the existing ones."""
    config = without_hooks(config)
    hooks = config.setdefault("hooks", {})
    for event, groups in ours["hooks"].items():
        hooks[event] = hooks.get(event, []) + copy.deepcopy(groups)
    return config


def _backup_path(path: Path) -> Path:
    stem = f"{path.name}.bak-rexagents-{time.strftime('%Y%m%d%H%M%S')}"
    candidate, n = path.with_name(stem), 1
    while candidate.exists():
        candidate, n = path.with_name(f"{stem}-{n}"), n + 1
    return candidate


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.rexagents-tmp")
    tmp.write_text(text)
    if path.exists():
        shutil.copymode(path, tmp)
    os.replace(tmp, path)


def edit(path: Path, change: Callable[[dict], dict], *, summary: str, done: str, unchanged: str,
         dry_run: bool, yes: bool) -> int:
    try:
        config = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(config, dict):
            raise ValueError("it isn't a JSON object")
    except (OSError, ValueError) as error:
        print(f"rexagents: can't read {path}: {error}. Nothing changed.", file=sys.stderr)
        return 1

    new = change(config)
    if new == config:
        print(f"rexagents: {unchanged} {path}.")
        return 0
    old_text, new_text = json.dumps(config, indent=2) + "\n", json.dumps(new, indent=2) + "\n"
    if dry_run:
        sys.stdout.writelines(difflib.unified_diff(old_text.splitlines(True), new_text.splitlines(True),
                                                   str(path), f"{path} (after)"))
        return 0

    print(f"rexagents: {summary} {path}. A backup is kept next to it.")
    if not yes:
        if not sys.stdin.isatty():
            print("rexagents: pass --yes to go ahead, or --dry-run to see the change.", file=sys.stderr)
            return 2
        if input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Nothing changed.")
            return 1
    backup = None
    if path.exists():
        backup = _backup_path(path)
        shutil.copy2(path, backup)
    _write(path, new_text)
    print(f"rexagents: {done}" + (f" Backup: {backup}" if backup else ""))
    return 0
