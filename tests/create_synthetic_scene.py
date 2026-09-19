from __future__ import annotations

import sys
from pathlib import Path

import bpy

arguments = sys.argv[sys.argv.index("--") + 1 :]
if len(arguments) != 1:
    raise SystemExit(
        "usage: Blender --python tests/create_synthetic_scene.py -- OUTPUT.blend"
    )
output = Path(arguments[0]).resolve()

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
target = bpy.data.collections.new("CaptureTarget")
scene.collection.children.link(target)
bpy.ops.mesh.primitive_cube_add(size=2.0)
cube = bpy.context.object
cube.name = "SyntheticCube"
for collection in list(cube.users_collection):
    collection.objects.unlink(cube)
target.objects.link(cube)
material = bpy.data.materials.new("SyntheticBlue")
material.diffuse_color = (0.05, 0.2, 0.8, 1.0)
cube.data.materials.append(material)
scene.world = bpy.data.worlds.new("SyntheticWorld")
scene.world.color = (0.08, 0.08, 0.08)
output.parent.mkdir(parents=True, exist_ok=True)
bpy.ops.wm.save_as_mainfile(filepath=str(output))
print("SYNTHETIC_SCENE_OK", output)
