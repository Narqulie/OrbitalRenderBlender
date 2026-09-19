"""Orbital Render for 3D Gaussian Splatting Blender add-on."""

from __future__ import annotations

bl_info = {
    "name": "Orbital Render for 3DGS",
    "author": "Jheaminoff and contributors",
    "version": (2, 0, 0),
    "blender": (5, 0, 0),
    "location": "View3D > Sidebar > 3DGS Render",
    "description": "Render resumable orbital COLMAP datasets for 3D Gaussian Splatting",
    "category": "Render",
}

try:
    import bpy
    from bpy.props import PointerProperty
except ModuleNotFoundError:  # Plan and dataset helpers remain usable outside Blender.
    bpy = None

if bpy is not None:
    from .ui import CLASSES, OrbitalRenderSettings
else:
    CLASSES = ()
    OrbitalRenderSettings = None


def register() -> None:
    if bpy is None:
        raise RuntimeError(
            "the Orbital Render add-on must be registered inside Blender"
        )
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.orbital_render_settings = PointerProperty(
        type=OrbitalRenderSettings
    )


def unregister() -> None:
    if bpy is None:
        return
    if hasattr(bpy.types.Scene, "orbital_render_settings"):
        del bpy.types.Scene.orbital_render_settings
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
