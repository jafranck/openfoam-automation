"""Shared entry-point helper for the small command scripts."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cfd_toolkit.cli import main  # noqa: E402


def run(action: str) -> int:
    return main([action, *sys.argv[1:]])
