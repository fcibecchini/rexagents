"""Where in Rex this process runs."""

from __future__ import annotations

import os
from typing import Mapping


def location(env: Mapping[str, str] | None = None) -> tuple[str, str] | None:
    """(the session to address Rex commands to, this pane's block), or None outside a Rex pane.

    Newer Rex versions set only REX_BLOCK, and take a block id wherever a session goes.
    """
    env = os.environ if env is None else env
    block = env.get("REX_BLOCK")
    return (env.get("REX_SESSION") or block, block) if block else None
