from __future__ import annotations

import math
import os
import sys
import tempfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

import bpy  # noqa: E402
from bpy_extras.object_utils import world_to_camera_view  # noqa: E402
from mathutils import Vector  # noqa: E402

from orbital_render_3dgs_addon.blender_capture import (  # noqa: E402
    SceneState,
    apply_capture_settings,
    camera_intrinsics,
    camera_pose,
    create_owned_camera,
    orient_camera,
    remove_owned_camera,
    resolve_target,
    sample_initialization_points,
    source_signature,
)


def reset_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def make_plan_stub(scene, camera_values, render_values):
    return {
        "scene": {"frame": 1},
        "camera": {
            "lens_mm": 47.0,
            "sensor_width_mm": 35.0,
            "sensor_height_mm": 22.0,
            "sensor_fit": camera_values[0],
            "shift_x": camera_values[1],
            "shift_y": camera_values[2],
            "clip_start": 0.1,
            "clip_end": 1000.0,
        },
        "render": {
            "engine": "BLENDER_WORKBENCH",
            "resolution_x": 320,
            "resolution_y": 180,
            "resolution_percentage": 100,
            "pixel_aspect_x": render_values[0],
            "pixel_aspect_y": render_values[1],
            "image_format": "PNG",
            "transparent_background": False,
            "samples": 1,
        },
    }


def project_with_plan(world_point, c2w, intrinsics):
    from mathutils import Matrix

    point_cv = Matrix(c2w).inverted() @ Vector((*world_point, 1.0))
    return (
        intrinsics["fx"] * point_cv.x / point_cv.z + intrinsics["cx"],
        intrinsics["fy"] * point_cv.y / point_cv.z + intrinsics["cy"],
    )


def test_source_fingerprint():
    with tempfile.TemporaryDirectory(prefix="orbital-render-linked-image-") as tmp:
        root = Path(tmp)
        texture_path = root / "texture.png"
        library_path = root / "texture-library.blend"

        reset_scene()
        image = bpy.data.images.new("LinkedTexture", width=1, height=1)
        image.use_fake_user = True
        image.generated_color = (0.2, 0.4, 0.6, 1.0)
        image.filepath_raw = str(texture_path)
        image.file_format = "PNG"
        image.save()
        image.filepath = "//texture.png"
        bpy.ops.wm.save_as_mainfile(filepath=str(library_path))

        reset_scene()
        with bpy.data.libraries.load(str(library_path), link=True) as (
            data_from,
            data_to,
        ):
            assert "LinkedTexture" in data_from.images
            data_to.images = ["LinkedTexture"]
        linked_image = bpy.data.images["LinkedTexture"]
        assert linked_image.library is not None
        before = source_signature(bpy.context)
        texture_path.write_bytes(texture_path.read_bytes() + b"\0")
        stat = texture_path.stat()
        os.utime(texture_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        assert source_signature(bpy.context) != before

    reset_scene()
    bpy.ops.mesh.primitive_cube_add()
    cube = bpy.context.object
    uv_layer = cube.data.uv_layers.new(name="ResumeUV")
    before = source_signature(bpy.context)
    uv_layer.uv[0].vector.x += 0.125
    assert source_signature(bpy.context) != before

    before = source_signature(bpy.context)
    cube.data.polygons[0].use_smooth = not cube.data.polygons[0].use_smooth
    assert source_signature(bpy.context) != before

    before = source_signature(bpy.context)
    bpy.context.scene.view_settings.exposure += 0.5
    assert source_signature(bpy.context) != before

    scene = bpy.context.scene
    before = source_signature(bpy.context)
    scene.compositing_node_group = bpy.data.node_groups.new(
        "ResumeCompositor", "CompositorNodeTree"
    )
    assert source_signature(bpy.context) != before


def test_projection():
    reset_scene()
    scene = bpy.context.scene
    cases = [
        ("HORIZONTAL", 0.0, 0.0, 1.0, 1.0),
        ("AUTO", 0.12, -0.08, 1.0, 1.3),
        ("VERTICAL", -0.15, 0.09, 1.4, 1.0),
    ]
    for case_index, (fit, shift_x, shift_y, aspect_x, aspect_y) in enumerate(cases):
        run_id = f"projection-{case_index}"
        camera, collection = create_owned_camera(scene, run_id)
        scene.render.use_border = True
        scene.render.use_crop_to_border = True
        scene.render.use_compositing = True
        snapshot = SceneState(scene)
        try:
            plan = make_plan_stub(scene, (fit, shift_x, shift_y), (aspect_x, aspect_y))
            apply_capture_settings(scene, camera, plan)
            assert not scene.render.use_border
            assert not scene.render.use_crop_to_border
            assert not scene.render.use_compositing
            orient_camera(camera, [4.0, -3.0, 2.5], [0.0, 0.0, 0.5])
            bpy.context.view_layer.update()
            intrinsics = camera_intrinsics(scene, camera)
            c2w, _ = camera_pose(camera)
            width = intrinsics["width"]
            height = intrinsics["height"]
            for point in (
                Vector((0.0, 0.0, 0.5)),
                Vector((0.4, 0.1, 0.7)),
                Vector((-0.25, 0.3, 0.2)),
            ):
                ndc = world_to_camera_view(scene, camera, point)
                blender_pixel = (ndc.x * width, (1.0 - ndc.y) * height)
                plan_pixel = project_with_plan(point, c2w, intrinsics)
                case_description = (fit, shift_x, shift_y, aspect_x, aspect_y)
                assert abs(blender_pixel[0] - plan_pixel[0]) < 1e-5, (
                    case_description,
                    point,
                    blender_pixel,
                    plan_pixel,
                )
                assert abs(blender_pixel[1] - plan_pixel[1]) < 1e-5, (
                    case_description,
                    point,
                    blender_pixel,
                    plan_pixel,
                )
        finally:
            snapshot.restore()
            assert scene.render.use_border
            assert scene.render.use_crop_to_border
            assert scene.render.use_compositing
            remove_owned_camera(camera, collection, run_id)


def test_linked_instance_bounds_and_points():
    reset_scene()
    scene = bpy.context.scene
    source = bpy.data.collections.new("LinkedSource")
    cube_data = bpy.data.meshes.new("LinkedCubeMesh")
    cube = bpy.data.objects.new("LinkedCube", cube_data)
    source.objects.link(cube)
    vertices = [
        (-1, -1, -1),
        (1, -1, -1),
        (1, 1, -1),
        (-1, 1, -1),
        (-1, -1, 1),
        (1, -1, 1),
        (1, 1, 1),
        (-1, 1, 1),
    ]
    cube_data.from_pydata(vertices, [], [])

    target = bpy.data.collections.new("TargetInstances")
    scene.collection.children.link(target)
    for name, x in (("InstanceA", -5.0), ("InstanceB", 7.0)):
        empty = bpy.data.objects.new(name, None)
        empty.instance_type = "COLLECTION"
        empty.instance_collection = source
        empty.location.x = x
        target.objects.link(empty)
    bpy.context.view_layer.update()

    resolved = resolve_target(
        bpy.context,
        {"mode": "COLLECTION", "collection": target.name, "look_at_z": 0.0},
    )
    assert all(
        math.isclose(a, b, abs_tol=1e-6)
        for a, b in zip(resolved["bounds_min"], [-6.0, -1.0, -1.0])
    ), resolved
    assert all(
        math.isclose(a, b, abs_tol=1e-6)
        for a, b in zip(resolved["bounds_max"], [8.0, 1.0, 1.0])
    ), resolved
    points = sample_initialization_points(bpy.context, resolved, 100)
    xs = [point["xyz"][0] for point in points]
    assert min(xs) == -6.0 and max(xs) == 8.0, xs


def test_curve_initialization():
    reset_scene()
    curve_data = bpy.data.curves.new("CaptureCurve", type="CURVE")
    curve_data.dimensions = "3D"
    curve_data.bevel_depth = 0.1
    spline = curve_data.splines.new("POLY")
    spline.points.add(3)
    for point, coordinates in zip(
        spline.points,
        (
            (-1.0, -1.0, 0.0, 1.0),
            (1.0, -1.0, 0.0, 1.0),
            (1.0, 1.0, 0.0, 1.0),
            (-1.0, 1.0, 0.0, 1.0),
        ),
    ):
        point.co = coordinates
    spline.use_cyclic_u = True
    curve = bpy.data.objects.new("CaptureCurve", curve_data)
    bpy.context.scene.collection.objects.link(curve)
    curve.select_set(True)
    bpy.context.view_layer.objects.active = curve
    bpy.context.view_layer.update()
    target = resolve_target(bpy.context, {"mode": "SELECTION"})
    points = sample_initialization_points(bpy.context, target, 128)
    assert points


def main():
    test_source_fingerprint()
    test_projection()
    test_linked_instance_bounds_and_points()
    test_curve_initialization()
    print("BLENDER_PROJECTION_TEST_OK")


main()
