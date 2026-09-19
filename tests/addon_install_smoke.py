from __future__ import annotations

import os
import sys
from pathlib import Path

import bpy

if not os.environ.get("BLENDER_USER_SCRIPTS") or not os.environ.get(
    "BLENDER_USER_CONFIG"
):
    raise RuntimeError(
        "addon install smoke requires isolated BLENDER_USER_SCRIPTS and "
        "BLENDER_USER_CONFIG directories"
    )

arguments = sys.argv[sys.argv.index("--") + 1 :]
if len(arguments) != 1:
    raise SystemExit(
        "usage: Blender --python tests/addon_install_smoke.py -- ADDON.zip"
    )
archive = Path(arguments[0]).resolve()
if not archive.is_file():
    raise SystemExit(f"add-on archive does not exist: {archive}")

bpy.ops.preferences.addon_install(filepath=str(archive), overwrite=True)
bpy.ops.preferences.addon_enable(module="orbital_render_3dgs_addon")
assert "orbital_render_3dgs_addon" in bpy.context.preferences.addons
assert hasattr(bpy.types.Scene, "orbital_render_settings")
assert hasattr(bpy.ops.orbital_render, "generate")
bpy.ops.preferences.addon_disable(module="orbital_render_3dgs_addon")
print("ADDON_INSTALL_SMOKE_OK", archive)
