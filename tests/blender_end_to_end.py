from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

import bpy  # noqa: E402

from orbital_render_3dgs_addon.blender_capture import (  # noqa: E402
    OWNER_PROPERTY,
    CaptureError,
    CaptureSession,
    create_plan_from_config,
)
from orbital_render_3dgs_addon.plan import load_plan  # noqa: E402


def build_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    target = bpy.data.collections.new("CaptureTarget")
    scene.collection.children.link(target)
    bpy.ops.mesh.primitive_cube_add(size=2.0, location=(0.0, 0.0, 0.0))
    cube = bpy.context.object
    cube.name = "SyntheticCube"
    for collection in list(cube.users_collection):
        collection.objects.unlink(cube)
    target.objects.link(cube)
    material = bpy.data.materials.new("SyntheticRed")
    material.diffuse_color = (0.8, 0.1, 0.05, 1.0)
    cube.data.materials.append(material)
    scene.world = bpy.data.worlds.new("SyntheticWorld")
    scene.world.color = (0.08, 0.08, 0.08)
    bpy.context.view_layer.update()


def config():
    return {
        "target": {
            "mode": "COLLECTION",
            "collection": "CaptureTarget",
            "look_at_z": 0.0,
        },
        "sampling": {
            "horizontal_radii": [4.0],
            "world_z_heights": [1.5],
            "azimuth_samples": 4,
            "azimuth_start_degrees": 0.0,
            "eval_every": 2,
        },
        "camera": {
            "lens_mm": 45.0,
            "sensor_width_mm": 36.0,
            "sensor_height_mm": 24.0,
            "sensor_fit": "HORIZONTAL",
            "shift_x": 0.03,
            "shift_y": -0.02,
            "clip_start": 0.1,
            "clip_end": 100.0,
        },
        "render": {
            "engine": "BLENDER_WORKBENCH",
            "resolution_x": 64,
            "resolution_y": 48,
            "pixel_aspect_x": 1.0,
            "pixel_aspect_y": 1.0,
            "image_format": "PNG",
            "transparent_background": False,
            "samples": 1,
        },
        "initialization": {"max_points": 100},
    }


def assert_colmap(output: Path):
    cameras = (output / "sparse" / "0" / "cameras.txt").read_text()
    images = (output / "sparse" / "0" / "images.txt").read_text()
    points = (output / "sparse" / "0" / "points3D.txt").read_text()
    assert "1 PINHOLE 64 48" in cameras
    image_rows = [
        line for line in images.splitlines() if line and not line.startswith("#")
    ]
    assert len(image_rows) == 4, image_rows
    assert all(f"frame_{index:06d}.png" in image_rows[index] for index in range(4))
    point_rows = [
        line for line in points.splitlines() if line and not line.startswith("#")
    ]
    assert len(point_rows) == 8, point_rows
    inria = json.loads((output / "cameras.json").read_text())
    assert len(inria) == 4
    assert set(inria[0]) == {
        "id",
        "img_name",
        "position",
        "rotation",
        "width",
        "height",
        "fx",
        "fy",
    }


def main():
    build_scene()
    root = Path(tempfile.mkdtemp(prefix="orbital-render-e2e-"))
    output = root / "dataset"
    try:
        plan, resolved = create_plan_from_config(bpy.context, config())
        try:
            CaptureSession(
                bpy.context,
                plan,
                root / "invalid-limit",
                resume=False,
                resolved_target=resolved,
            ).run_slice(max_frames=0)
        except CaptureError as exc:
            assert "max_frames" in str(exc)
        else:
            raise AssertionError("zero max_frames was accepted")
        assert not (root / "invalid-limit").exists()
        assert not any(obj.get(OWNER_PROPERTY) for obj in bpy.data.objects)

        original_scene_state = (
            bpy.context.scene.camera,
            bpy.context.scene.render.resolution_x,
            bpy.context.scene.render.resolution_y,
            bpy.context.scene.render.filepath,
        )
        first = CaptureSession(
            bpy.context, plan, output, resume=False, resolved_target=resolved
        ).run_slice(max_frames=2)
        assert first["complete"] is False, first
        assert (
            bpy.context.scene.camera,
            bpy.context.scene.render.resolution_x,
            bpy.context.scene.render.resolution_y,
            bpy.context.scene.render.filepath,
        ) == original_scene_state
        assert not any(obj.get(OWNER_PROPERTY) for obj in bpy.data.objects)
        assert first["new_frames"] == 2, first
        state = json.loads((output / "capture_state.json").read_text())
        assert state["complete"] is False
        assert len(state["completed"]) == 2
        points_digest = state["initialization"]["sha256"]

        frozen = load_plan(output / "capture_plan.json")
        second = CaptureSession(bpy.context, frozen, output, resume=True).run_slice(
            max_frames=2
        )
        assert second["complete"] is True, second
        assert second["new_frames"] == 2, second
        state = json.loads((output / "capture_state.json").read_text())
        assert state["complete"] is True
        assert state["initialization"]["sha256"] == points_digest
        assert len(list((output / "images").glob("*.png"))) == 4
        assert_colmap(output)

        mutation_output = root / "mid-capture-mutation"
        mutation_session = CaptureSession(
            bpy.context,
            plan,
            mutation_output,
            resume=False,
            resolved_target=resolved,
        )
        original_diffuse = tuple(bpy.data.materials["SyntheticRed"].diffuse_color)
        try:
            mutation_session.prepare()
            mutation_session.render_next()
            bpy.context.scene.render.use_compositing = True
            try:
                mutation_session.render_next()
            except CaptureError as exc:
                assert "settings changed" in str(exc)
            else:
                raise AssertionError("mid-capture render setting mutation was accepted")
            bpy.context.scene.render.use_compositing = False
            bpy.data.materials["SyntheticRed"].diffuse_color[0] = 0.2
            try:
                mutation_session.render_next()
            except CaptureError as exc:
                assert "source changed" in str(exc)
            else:
                raise AssertionError("mid-capture source mutation was accepted")
        finally:
            bpy.data.materials["SyntheticRed"].diffuse_color = original_diffuse
            mutation_session.close()
        mutation_state = json.loads(
            (mutation_output / "capture_state.json").read_text()
        )
        assert len(mutation_state["completed"]) == 1

        for image_format, extension in (("JPEG", ".jpg"), ("OPEN_EXR", ".exr")):
            format_config = config()
            format_config["render"]["image_format"] = image_format
            format_config["sampling"]["azimuth_samples"] = 2
            format_plan, format_target = create_plan_from_config(
                bpy.context, format_config
            )
            format_output = root / image_format.lower()
            result = CaptureSession(
                bpy.context,
                format_plan,
                format_output,
                resume=False,
                resolved_target=format_target,
            ).run_slice(max_frames=1)
            assert result["new_frames"] == 1
            images = list((format_output / "images").glob(f"*{extension}"))
            assert len(images) == 1 and images[0].stat().st_size > 0

        cube = bpy.data.objects["SyntheticCube"]
        cube.data.vertices[0].co.x += 0.125
        bpy.context.view_layer.update()
        try:
            CaptureSession(bpy.context, frozen, output, resume=True).prepare()
        except CaptureError as exc:
            assert "source signature" in str(exc)
        else:
            raise AssertionError("changed source geometry was accepted on resume")
        assert not any(obj.get(OWNER_PROPERTY) for obj in bpy.data.objects)
        print("BLENDER_END_TO_END_OK", output)
    finally:
        shutil.rmtree(root)


main()
