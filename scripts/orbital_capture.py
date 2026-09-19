"""Blender --python shim for orbital_render_3dgs_addon.cli."""

from __future__ import annotations

import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from orbital_render_3dgs_addon.cli import main  # noqa: E402

main()
