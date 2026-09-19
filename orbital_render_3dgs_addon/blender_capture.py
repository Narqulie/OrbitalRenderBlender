"""Blender adapters and the shared serial capture executor."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import random
import uuid
from array import array
from pathlib import Path
from typing import Any, Iterator

import bpy
from mathutils import Matrix, Vector

from .dataset import DatasetOutput
from .plan import build_plan, plan_digest, validate_plan

OWNER_PROPERTY = "orbital_render_owner"
GEOMETRY_TYPES = {"MESH", "CURVE", "SURFACE", "META", "FONT"}


class CaptureError(RuntimeError):
    """A Blender scene cannot satisfy the capture contract."""


def _matrix_values(matrix: Matrix) -> list[list[float]]:
    return [[float(matrix[row][column]) for column in range(4)] for row in range(4)]


def _round_values(values: Any, digits: int = 9) -> Any:
    if isinstance(values, (list, tuple)):
        return [_round_values(value, digits) for value in values]
    if isinstance(values, float):
        return round(values, digits)
    return values


def _instance_names(instance: Any) -> tuple[str, str | None]:
    original = getattr(instance.object, "original", instance.object)
    parent = getattr(instance, "parent", None)
    parent_original = getattr(parent, "original", parent) if parent else None
    return original.name_full, parent_original.name_full if parent_original else None


def _selector_for_target(
    context: bpy.types.Context, target: dict[str, Any]
) -> dict[str, Any]:
    mode = str(target.get("mode", "SELECTION")).upper()
    if mode in {"SELECTION", "OBJECT"}:
        configured = target.get("objects") or []
        names = [str(name) for name in configured]
        if not names and mode == "SELECTION":
            names = [obj.name_full for obj in context.selected_objects]
        if not names:
            raise CaptureError("object target requires at least one object")
        missing = [name for name in names if bpy.data.objects.get(name) is None]
        if missing:
            raise CaptureError(f"target objects do not exist: {', '.join(missing)}")
        return {"mode": "OBJECT", "objects": sorted(set(names))}
    if mode == "COLLECTION":
        name = str(target.get("collection", ""))
        collection = bpy.data.collections.get(name)
        if collection is None:
            raise CaptureError(f"target collection does not exist: {name!r}")
        member_names = sorted(obj.name_full for obj in collection.all_objects)
        return {
            "mode": "COLLECTION",
            "collection": name,
            "members": member_names,
        }
    if mode == "MANUAL_REGION":
        return {"mode": "MANUAL_REGION"}
    raise CaptureError(f"unsupported target mode: {mode}")


def _matches_selector(instance: Any, selector: dict[str, Any]) -> bool:
    if selector["mode"] == "MANUAL_REGION":
        return True
    object_name, parent_name = _instance_names(instance)
    if selector["mode"] == "OBJECT":
        names = set(selector["objects"])
    else:
        names = set(selector["members"])
    return object_name in names or parent_name in names


def _is_renderable_instance(instance: Any) -> bool:
    obj = instance.object
    original = getattr(obj, "original", obj)
    if original.get(OWNER_PROPERTY):
        return False
    return (
        obj.type in GEOMETRY_TYPES
        and not bool(getattr(original, "hide_render", False))
        and bool(getattr(instance, "show_self", True))
    )


def iter_target_instances(
    context: bpy.types.Context, selector: dict[str, Any]
) -> Iterator[Any]:
    depsgraph = context.evaluated_depsgraph_get()
    for instance in depsgraph.object_instances:
        if _is_renderable_instance(instance) and _matches_selector(instance, selector):
            yield instance


def _instance_bounds(instance: Any) -> tuple[Vector, Vector] | None:
    corners = getattr(instance.object, "bound_box", None)
    if not corners:
        return None
    transformed = [instance.matrix_world @ Vector(corner) for corner in corners]
    if not transformed or not all(
        math.isfinite(component) for point in transformed for component in point
    ):
        return None
    low = Vector((min(point[i] for point in transformed) for i in range(3)))
    high = Vector((max(point[i] for point in transformed) for i in range(3)))
    return low, high


def resolve_target(
    context: bpy.types.Context, target: dict[str, Any]
) -> dict[str, Any]:
    """Freeze object, collection, or manual-region targeting into world bounds."""
    selector = _selector_for_target(context, target)
    if selector["mode"] == "MANUAL_REGION":
        if "bounds_min" in target and "bounds_max" in target:
            low = Vector(target["bounds_min"])
            high = Vector(target["bounds_max"])
        else:
            center = Vector(target.get("center", (0.0, 0.0, 0.0)))
            size = Vector(target.get("size", (1.0, 1.0, 1.0)))
            if any(component <= 0.0 for component in size):
                raise CaptureError("manual target size must be positive on every axis")
            low = center - size * 0.5
            high = center + size * 0.5
    else:
        low = Vector((math.inf, math.inf, math.inf))
        high = Vector((-math.inf, -math.inf, -math.inf))
        count = 0
        for instance in iter_target_instances(context, selector):
            bounds = _instance_bounds(instance)
            if bounds is None:
                continue
            instance_low, instance_high = bounds
            for axis in range(3):
                low[axis] = min(low[axis], instance_low[axis])
                high[axis] = max(high[axis], instance_high[axis])
            count += 1
        if count == 0:
            raise CaptureError("target contains no evaluated renderable geometry")

    if any(not math.isfinite(value) for value in (*low, *high)) or any(
        low[axis] >= high[axis] for axis in range(3)
    ):
        raise CaptureError("resolved target bounds are empty or invalid")
    center = (low + high) * 0.5
    look_at_z = target.get("look_at_z")
    if look_at_z is None:
        look_at_z = center.z
    return {
        "mode": selector["mode"],
        "selectors": {
            key: value for key, value in selector.items() if key != "members"
        },
        "bounds_min": list(low),
        "bounds_max": list(high),
        "center_xy": [center.x, center.y],
        "look_at_z": float(look_at_z),
        "_selector": selector,
    }


def _hash_foreach(
    digest: Any,
    collection: Any,
    property_name: str,
    component_count: int,
    typecode: str,
) -> None:
    values = array(typecode, [0]) * (len(collection) * component_count)
    if values:
        collection.foreach_get(property_name, values)
        digest.update(values.tobytes())


def _node_tree_signature(node_tree: Any) -> dict[str, Any]:
    if node_tree is None:
        return {"tree": None, "nodes": [], "links": []}
    nodes = []
    for node in sorted(node_tree.nodes, key=lambda item: item.name):
        inputs = []
        for socket in node.inputs:
            if not hasattr(socket, "default_value"):
                continue
            value = socket.default_value
            try:
                serialized = [round(float(item), 9) for item in value]
            except (TypeError, ValueError):
                try:
                    serialized = round(float(value), 9)
                except (TypeError, ValueError):
                    serialized = str(value)
            inputs.append([socket.identifier, serialized])
        image = getattr(node, "image", None)
        nodes.append(
            {
                "name": node.name,
                "type": node.bl_idname,
                "mute": node.mute,
                "inputs": inputs,
                "image": image.filepath if image else None,
            }
        )
    links = sorted(
        [
            link.from_node.name,
            link.from_socket.identifier,
            link.to_node.name,
            link.to_socket.identifier,
        ]
        for link in node_tree.links
    )
    return {"tree": node_tree.name_full, "nodes": nodes, "links": links}


def _material_signature(material: bpy.types.Material | None) -> dict[str, Any] | None:
    if material is None:
        return None
    tree = _node_tree_signature(material.node_tree if material.use_nodes else None)
    return {
        "name": material.name_full,
        "diffuse": [round(float(value), 9) for value in material.diffuse_color],
        "use_nodes": material.use_nodes,
        **tree,
    }


def _mesh_data_signature(obj: bpy.types.Object, data: bpy.types.Mesh) -> dict[str, Any]:
    digest = hashlib.sha256()
    _hash_foreach(digest, data.vertices, "co", 3, "f")
    _hash_foreach(digest, data.edges, "vertices", 2, "i")
    _hash_foreach(digest, data.loops, "vertex_index", 1, "i")
    _hash_foreach(digest, data.polygons, "loop_start", 1, "i")
    _hash_foreach(digest, data.polygons, "loop_total", 1, "i")
    _hash_foreach(digest, data.polygons, "material_index", 1, "i")
    _hash_foreach(digest, data.polygons, "use_smooth", 1, "b")
    for uv_layer in data.uv_layers:
        digest.update(uv_layer.name.encode("utf-8"))
        digest.update(
            bytes((bool(uv_layer.active_render), bool(uv_layer.active_clone)))
        )
        _hash_foreach(digest, uv_layer.data, "uv", 2, "f")
    materials = [_material_signature(slot.material) for slot in obj.material_slots]
    return {
        "data": data.name_full,
        "type": obj.type,
        "vertices": len(data.vertices),
        "edges": len(data.edges),
        "polygons": len(data.polygons),
        "sha256": digest.hexdigest(),
        "materials": materials,
    }


def _geometry_signature(obj: bpy.types.Object) -> dict[str, Any]:
    if obj.type == "MESH":
        return _mesh_data_signature(obj, obj.data)
    mesh = obj.to_mesh()
    if mesh is None:
        return {"data": getattr(obj.data, "name_full", ""), "type": obj.type}
    try:
        return _mesh_data_signature(obj, mesh)
    finally:
        obj.to_mesh_clear()


def source_signature(context: bpy.types.Context) -> str:
    """Fingerprint render-visible evaluated geometry, lights, and source files."""
    records = []
    geometry_cache: dict[int, dict[str, Any]] = {}
    for instance in context.evaluated_depsgraph_get().object_instances:
        obj = instance.object
        original = getattr(obj, "original", obj)
        if original.get(OWNER_PROPERTY) or bool(
            getattr(original, "hide_render", False)
        ):
            continue
        if obj.type not in GEOMETRY_TYPES | {"LIGHT"}:
            continue
        name, parent_name = _instance_names(instance)
        data = getattr(obj, "data", None)
        pointer = data.as_pointer() if data is not None else 0
        if pointer not in geometry_cache:
            if obj.type == "LIGHT":
                geometry_cache[pointer] = {
                    "type": data.type,
                    "energy": round(float(data.energy), 9),
                    "color": [round(float(value), 9) for value in data.color],
                    "size": round(float(getattr(data, "size", 0.0)), 9),
                }
            else:
                geometry_cache[pointer] = _geometry_signature(obj)
        records.append(
            {
                "object": name,
                "parent": parent_name,
                "type": obj.type,
                "matrix": _round_values(_matrix_values(instance.matrix_world)),
                "data": geometry_cache[pointer],
            }
        )
    records.sort(
        key=lambda record: (
            record["object"],
            record["parent"] or "",
            json.dumps(record["matrix"], separators=(",", ":")),
        )
    )

    files = []
    paths: set[Path] = set()
    if bpy.data.filepath:
        paths.add(Path(bpy.data.filepath))
    for library in bpy.data.libraries:
        if library.filepath:
            paths.add(
                Path(
                    bpy.path.abspath(
                        library.filepath, library=getattr(library, "parent", None)
                    )
                )
            )
    images = []
    for image in bpy.data.images:
        if image.source != "FILE" or not image.filepath:
            continue
        absolute_path = Path(bpy.path.abspath(image.filepath, library=image.library))
        paths.add(absolute_path)
        packed_file = image.packed_file
        images.append(
            {
                "name": image.name_full,
                "path": str(absolute_path),
                "library": (
                    bpy.path.abspath(
                        image.library.filepath,
                        library=getattr(image.library, "parent", None),
                    )
                    if image.library
                    else None
                ),
                "colorspace": image.colorspace_settings.name,
                "alpha_mode": image.alpha_mode,
                "packed_sha256": (
                    hashlib.sha256(bytes(packed_file.data)).hexdigest()
                    if packed_file
                    else None
                ),
            }
        )
    for path in sorted(paths, key=lambda item: str(item)):
        try:
            stat = path.stat()
            files.append([str(path.resolve()), stat.st_size, stat.st_mtime_ns])
        except OSError:
            files.append([str(path), None, None])

    world = context.scene.world
    world_tree = _node_tree_signature(
        world.node_tree if world and world.use_nodes else None
    )
    payload = {
        "scene": context.scene.name_full,
        "view_layer": context.view_layer.name,
        "frame": context.scene.frame_current,
        "instances": records,
        "world": {
            "name": world.name_full if world else None,
            "color": [round(float(value), 9) for value in world.color]
            if world
            else None,
            "use_nodes": bool(world and world.use_nodes),
            **world_tree,
        },
        "compositor": _node_tree_signature(context.scene.compositing_node_group),
        "view_settings": {
            "display_device": context.scene.display_settings.display_device,
            "view_transform": context.scene.view_settings.view_transform,
            "look": context.scene.view_settings.look,
            "exposure": round(float(context.scene.view_settings.exposure), 9),
            "gamma": round(float(context.scene.view_settings.gamma), 9),
            "sequencer_colorspace": context.scene.sequencer_colorspace_settings.name,
        },
        "images": sorted(images, key=lambda item: item["name"]),
        "files": files,
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class SceneState:
    """Snapshot the user scene fields owned temporarily by capture."""

    def __init__(self, scene: bpy.types.Scene):
        render = scene.render
        image = render.image_settings
        self.scene = scene
        self.values = {
            "camera": scene.camera,
            "frame": scene.frame_current,
            "subframe": scene.frame_subframe,
            "engine": render.engine,
            "filepath": render.filepath,
            "use_file_extension": render.use_file_extension,
            "resolution_x": render.resolution_x,
            "resolution_y": render.resolution_y,
            "resolution_percentage": render.resolution_percentage,
            "pixel_aspect_x": render.pixel_aspect_x,
            "pixel_aspect_y": render.pixel_aspect_y,
            "film_transparent": render.film_transparent,
            "use_border": render.use_border,
            "use_crop_to_border": render.use_crop_to_border,
            "use_compositing": render.use_compositing,
            "file_format": image.file_format,
            "color_mode": image.color_mode,
            "color_depth": image.color_depth,
        }
        self.cycles_samples = scene.cycles.samples if hasattr(scene, "cycles") else None

    def restore(self) -> None:
        scene = self.scene
        render = scene.render
        image = render.image_settings
        render.engine = self.values["engine"]
        render.filepath = self.values["filepath"]
        render.use_file_extension = self.values["use_file_extension"]
        render.resolution_x = self.values["resolution_x"]
        render.resolution_y = self.values["resolution_y"]
        render.resolution_percentage = self.values["resolution_percentage"]
        render.pixel_aspect_x = self.values["pixel_aspect_x"]
        render.pixel_aspect_y = self.values["pixel_aspect_y"]
        render.film_transparent = self.values["film_transparent"]
        render.use_border = self.values["use_border"]
        render.use_crop_to_border = self.values["use_crop_to_border"]
        render.use_compositing = self.values["use_compositing"]
        image.file_format = self.values["file_format"]
        image.color_mode = self.values["color_mode"]
        image.color_depth = self.values["color_depth"]
        if self.cycles_samples is not None and hasattr(scene, "cycles"):
            scene.cycles.samples = self.cycles_samples
        scene.camera = self.values["camera"]
        scene.frame_set(self.values["frame"], subframe=self.values["subframe"])


def create_owned_camera(
    scene: bpy.types.Scene, run_id: str
) -> tuple[bpy.types.Object, bpy.types.Collection]:
    collection = bpy.data.collections.new(f"OrbitalRender_{run_id}")
    collection[OWNER_PROPERTY] = run_id
    scene.collection.children.link(collection)
    data = bpy.data.cameras.new(f"OrbitalRenderCamera_{run_id}")
    camera = bpy.data.objects.new(f"OrbitalRenderCamera_{run_id}", data)
    camera[OWNER_PROPERTY] = run_id
    collection.objects.link(camera)
    return camera, collection


def remove_owned_camera(
    camera: bpy.types.Object | None,
    collection: bpy.types.Collection | None,
    run_id: str,
) -> None:
    if camera is not None and camera.get(OWNER_PROPERTY) == run_id:
        data = camera.data
        bpy.data.objects.remove(camera, do_unlink=True)
        if data and data.users == 0:
            bpy.data.cameras.remove(data)
    if collection is not None and collection.get(OWNER_PROPERTY) == run_id:
        bpy.data.collections.remove(collection)


def apply_capture_settings(
    scene: bpy.types.Scene, camera: bpy.types.Object, plan: dict[str, Any]
) -> None:
    render_settings = plan["render"]
    camera_settings = plan["camera"]
    render = scene.render
    if render_settings["engine"]:
        render.engine = render_settings["engine"]
    render.resolution_x = render_settings["resolution_x"]
    render.resolution_y = render_settings["resolution_y"]
    render.resolution_percentage = 100
    render.pixel_aspect_x = render_settings["pixel_aspect_x"]
    render.pixel_aspect_y = render_settings["pixel_aspect_y"]
    render.film_transparent = render_settings["transparent_background"]
    render.use_border = False
    render.use_crop_to_border = False
    render.use_compositing = False
    render.use_file_extension = False
    render.image_settings.file_format = render_settings["image_format"]
    if render_settings["image_format"] == "JPEG":
        render.image_settings.color_mode = "RGB"
        render.image_settings.color_depth = "8"
    elif render_settings["image_format"] == "PNG":
        render.image_settings.color_mode = (
            "RGBA" if render_settings["transparent_background"] else "RGB"
        )
        render.image_settings.color_depth = "8"
    else:
        render.image_settings.color_mode = (
            "RGBA" if render_settings["transparent_background"] else "RGB"
        )
        render.image_settings.color_depth = "16"
    if render.engine == "CYCLES" and hasattr(scene, "cycles"):
        scene.cycles.samples = render_settings["samples"]

    data = camera.data
    data.type = "PERSP"
    data.lens = camera_settings["lens_mm"]
    data.sensor_width = camera_settings["sensor_width_mm"]
    data.sensor_height = camera_settings["sensor_height_mm"]
    data.sensor_fit = camera_settings["sensor_fit"]
    data.shift_x = camera_settings["shift_x"]
    data.shift_y = camera_settings["shift_y"]
    data.clip_start = camera_settings["clip_start"]
    data.clip_end = camera_settings["clip_end"]
    scene.camera = camera
    scene.frame_set(plan["scene"]["frame"])


def capture_settings_snapshot(
    scene: bpy.types.Scene, camera: bpy.types.Object
) -> dict[str, Any]:
    render = scene.render
    image = render.image_settings
    data = camera.data
    return {
        "active_camera": scene.camera == camera,
        "engine": render.engine,
        "resolution": [
            render.resolution_x,
            render.resolution_y,
            render.resolution_percentage,
        ],
        "pixel_aspect": [render.pixel_aspect_x, render.pixel_aspect_y],
        "film_transparent": render.film_transparent,
        "use_file_extension": render.use_file_extension,
        "use_border": render.use_border,
        "use_crop_to_border": render.use_crop_to_border,
        "use_compositing": render.use_compositing,
        "image": [image.file_format, image.color_mode, image.color_depth],
        "cycles_samples": scene.cycles.samples if hasattr(scene, "cycles") else None,
        "camera": [
            data.type,
            data.lens,
            data.sensor_width,
            data.sensor_height,
            data.sensor_fit,
            data.shift_x,
            data.shift_y,
            data.clip_start,
            data.clip_end,
        ],
    }


def orient_camera(
    camera: bpy.types.Object, position: list[float], target: list[float]
) -> None:
    position_vector = Vector(position)
    direction = Vector(target) - position_vector
    if direction.length_squared <= 1e-16:
        raise CaptureError("camera position and look-at target must differ")
    rotation = direction.to_track_quat("-Z", "Y").to_matrix().to_4x4()
    camera.matrix_world = Matrix.Translation(position_vector) @ rotation


def camera_intrinsics(
    scene: bpy.types.Scene, camera: bpy.types.Object
) -> dict[str, Any]:
    """Measure a PINHOLE calibration from Blender's evaluated camera frame."""
    width = int(scene.render.resolution_x * scene.render.resolution_percentage / 100)
    height = int(scene.render.resolution_y * scene.render.resolution_percentage / 100)
    if width <= 0 or height <= 0:
        raise CaptureError("effective render dimensions must be positive")
    corners = camera.data.view_frame(scene=scene)
    slopes = []
    for corner in corners:
        depth = -float(corner.z)
        if depth <= 0.0:
            raise CaptureError("camera frame is not a forward perspective frame")
        slopes.append((float(corner.x) / depth, float(corner.y) / depth))
    left = min(value[0] for value in slopes)
    right = max(value[0] for value in slopes)
    bottom = min(value[1] for value in slopes)
    top = max(value[1] for value in slopes)
    fx = width / (right - left)
    fy = height / (top - bottom)
    cx = -left * fx
    cy = top * fy
    return {
        "model": "PINHOLE",
        "width": width,
        "height": height,
        "fx": fx,
        "fy": fy,
        "cx": cx,
        "cy": cy,
    }


def camera_pose(
    camera: bpy.types.Object,
) -> tuple[list[list[float]], dict[str, list[float]]]:
    axis_conversion = Matrix.Diagonal((1.0, -1.0, -1.0, 1.0))
    camera_to_world_cv = camera.matrix_world @ axis_conversion
    world_to_camera_cv = camera_to_world_cv.inverted_safe()
    quaternion = world_to_camera_cv.to_quaternion().normalized()
    if quaternion.w < 0.0:
        quaternion.negate()
    colmap = {
        "quaternion_wxyz": [
            float(quaternion.w),
            float(quaternion.x),
            float(quaternion.y),
            float(quaternion.z),
        ],
        "translation": [float(value) for value in world_to_camera_cv.translation],
    }
    return _matrix_values(camera_to_world_cv), colmap


def materialize_plan(
    context: bpy.types.Context, config: dict[str, Any], resolved_target: dict[str, Any]
) -> dict[str, Any]:
    """Build a final plan containing camera records measured by Blender."""
    normalized = copy.deepcopy(config)
    normalized.setdefault("render", {})
    if not normalized["render"].get("engine"):
        normalized["render"]["engine"] = context.scene.render.engine
    identity = {
        "blend_file": bpy.data.filepath,
        "frame": context.scene.frame_current,
        "source_signature": source_signature(context),
    }
    public_target = {
        key: value for key, value in resolved_target.items() if not key.startswith("_")
    }
    plan = build_plan(normalized, public_target, identity)

    run_id = uuid.uuid4().hex[:12]
    snapshot = SceneState(context.scene)
    camera = None
    collection = None
    try:
        camera, collection = create_owned_camera(context.scene, run_id)
        apply_capture_settings(context.scene, camera, plan)
        intrinsics = camera_intrinsics(context.scene, camera)
        plan["camera_model"] = dict(intrinsics)
        for frame in plan["frames"]:
            orient_camera(camera, frame["camera_position"], frame["look_at"])
            context.view_layer.update()
            c2w, colmap = camera_pose(camera)
            frame["intrinsics"] = dict(intrinsics)
            frame["camera_c2w_cv"] = c2w
            frame["colmap_w2c"] = colmap
        plan["plan_sha256"] = plan_digest(plan)
        validate_plan(plan)
        return plan
    finally:
        snapshot.restore()
        remove_owned_camera(camera, collection, run_id)


def create_plan_from_config(
    context: bpy.types.Context, config: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    resolved_target = resolve_target(context, config.get("target", {}))
    return materialize_plan(context, config, resolved_target), resolved_target


def _linear_to_srgb(value: float) -> float:
    value = max(0.0, min(1.0, value))
    if value <= 0.0031308:
        return 12.92 * value
    return 1.055 * value ** (1.0 / 2.4) - 0.055


def _object_rgb(obj: bpy.types.Object) -> list[int]:
    for slot in getattr(obj, "material_slots", ()):
        material = slot.material
        if material:
            return [
                round(255.0 * _linear_to_srgb(float(value)))
                for value in material.diffuse_color[:3]
            ]
    color = getattr(obj, "color", (0.5, 0.5, 0.5, 1.0))
    return [round(255.0 * _linear_to_srgb(float(value))) for value in color[:3]]


def sample_initialization_points(
    context: bpy.types.Context,
    resolved_target: dict[str, Any],
    max_points: int,
) -> list[dict[str, Any]]:
    """Reservoir-sample evaluated instance vertices in the frozen target region."""
    selector = resolved_target["_selector"]
    low = Vector(resolved_target["bounds_min"])
    high = Vector(resolved_target["bounds_max"])
    random_source = random.Random(0)
    reservoir: list[dict[str, Any]] = []
    accepted = 0
    instances = []
    for instance in iter_target_instances(context, selector):
        name, parent_name = _instance_names(instance)
        instances.append(
            (name, parent_name or "", instance.matrix_world.copy(), instance.object)
        )
    instances.sort(
        key=lambda item: (
            item[0],
            item[1],
            json.dumps(_round_values(_matrix_values(item[2])), separators=(",", ":")),
        )
    )
    for _, _, matrix, obj in instances:
        temporary_mesh = obj.type != "MESH"
        mesh = obj.to_mesh() if temporary_mesh else getattr(obj, "data", None)
        if mesh is None:
            continue
        try:
            rgb = _object_rgb(obj)
            for vertex in mesh.vertices:
                point = matrix @ vertex.co
                if any(
                    point[axis] < low[axis] or point[axis] > high[axis]
                    for axis in range(3)
                ):
                    continue
                accepted += 1
                record = {"xyz": [float(value) for value in point], "rgb": rgb}
                if len(reservoir) < max_points:
                    reservoir.append(record)
                else:
                    replacement = random_source.randrange(accepted)
                    if replacement < max_points:
                        reservoir[replacement] = record
        finally:
            if temporary_mesh:
                obj.to_mesh_clear()
    if not reservoir:
        raise CaptureError("target produced no evaluated vertices for initialization")
    return reservoir


class CaptureSession:
    """Shared serial capture state used by both modal and headless front ends."""

    def __init__(
        self,
        context: bpy.types.Context,
        plan: dict[str, Any],
        output_directory: str | Path,
        *,
        resume: bool,
        resolved_target: dict[str, Any] | None = None,
    ):
        validate_plan(plan)
        self.context = context
        self.scene = context.scene
        self.plan = plan
        self.output = DatasetOutput(output_directory, plan, resume=resume)
        self.resolved_target = resolved_target
        self.run_id = uuid.uuid4().hex[:12]
        self.snapshot: SceneState | None = None
        self.camera: bpy.types.Object | None = None
        self.collection: bpy.types.Collection | None = None
        self.runtime_settings: dict[str, Any] | None = None
        self.closed = False

    def prepare(self) -> None:
        current_signature = source_signature(self.context)
        if current_signature != self.plan["scene"]["source_signature"]:
            raise CaptureError(
                "scene source signature differs from the frozen capture plan"
            )
        self.output.prepare()
        self.snapshot = SceneState(self.scene)
        try:
            self.camera, self.collection = create_owned_camera(self.scene, self.run_id)
            apply_capture_settings(self.scene, self.camera, self.plan)
            self.runtime_settings = capture_settings_snapshot(self.scene, self.camera)
            measured = camera_intrinsics(self.scene, self.camera)
            expected = self.plan["camera_model"]
            for key in ("fx", "fy", "cx", "cy"):
                if not math.isclose(
                    measured[key], expected[key], rel_tol=1e-10, abs_tol=1e-7
                ):
                    raise CaptureError(f"camera intrinsic {key} differs from the plan")
            if not self.output.initialization_complete:
                if self.resolved_target is None:
                    target = self.plan["target"]
                    request = dict(target.get("selectors", {}))
                    request["mode"] = target["mode"]
                    request["bounds_min"] = target["bounds_min"]
                    request["bounds_max"] = target["bounds_max"]
                    request["look_at_z"] = target["look_at_z"]
                    resolved = resolve_target(self.context, request)
                else:
                    resolved = self.resolved_target
                points = sample_initialization_points(
                    self.context, resolved, self.plan["initialization"]["max_points"]
                )
                self.output.commit_initialization(points)
            else:
                self.output.write_metadata()
        except Exception:
            self.close()
            raise

    def render_next(self) -> Path | None:
        index = self.output.completed_count
        if index >= len(self.plan["frames"]):
            return None
        if self.camera is None or self.runtime_settings is None:
            raise CaptureError("capture session is not prepared")
        if capture_settings_snapshot(self.scene, self.camera) != self.runtime_settings:
            raise CaptureError(
                "capture render or camera settings changed after preparation"
            )
        if source_signature(self.context) != self.plan["scene"]["source_signature"]:
            raise CaptureError("scene source changed after capture preparation")
        frame = self.plan["frames"][index]
        orient_camera(self.camera, frame["camera_position"], frame["look_at"])
        self.context.view_layer.update()
        measured_c2w, measured_colmap = camera_pose(self.camera)
        if not _nested_close(measured_c2w, frame["camera_c2w_cv"]):
            raise CaptureError(f"camera pose differs from plan for frame {index}")
        if not _nested_close(
            measured_colmap["translation"], frame["colmap_w2c"]["translation"]
        ) or not _nested_close(
            measured_colmap["quaternion_wxyz"],
            frame["colmap_w2c"]["quaternion_wxyz"],
        ):
            raise CaptureError(f"COLMAP pose differs from plan for frame {index}")

        temporary = self.output.temporary_image_path(frame)
        if temporary.exists():
            temporary.unlink()
        self.scene.render.filepath = str(temporary)
        bpy.ops.render.render(write_still=True)
        return self.output.commit_image(frame, temporary)

    def run_slice(self, max_frames: int | None = None) -> dict[str, Any]:
        if max_frames is not None and max_frames <= 0:
            raise CaptureError("max_frames must be greater than zero")
        self.prepare()
        start = self.output.completed_count
        limit = len(self.plan["frames"]) if max_frames is None else max_frames
        try:
            while self.output.completed_count < len(self.plan["frames"]):
                if self.output.completed_count - start >= limit:
                    break
                self.render_next()
            complete = self.output.completed_count == len(self.plan["frames"])
            return {
                "complete": complete,
                "new_frames": self.output.completed_count - start,
                "completed_frames": self.output.completed_count,
                "total_frames": len(self.plan["frames"]),
                "output_directory": str(self.output.root),
                "plan_sha256": self.plan["plan_sha256"],
            }
        finally:
            self.close()

    def cancel(self) -> None:
        self.output.mark_cancelled()
        self.close()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            if self.snapshot is not None:
                self.snapshot.restore()
        finally:
            remove_owned_camera(self.camera, self.collection, self.run_id)
            self.camera = None
            self.collection = None
            self.runtime_settings = None


def _nested_close(left: Any, right: Any) -> bool:
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return len(left) == len(right) and all(
            _nested_close(a, b) for a, b in zip(left, right)
        )
    return math.isclose(float(left), float(right), rel_tol=1e-10, abs_tol=1e-7)
