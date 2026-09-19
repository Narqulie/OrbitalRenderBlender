"""Serializable capture-plan construction and validation.

This module does not import Blender. Unit tests and external tooling can inspect a
plan without starting Blender; scene-dependent target resolution lives in
``blender_capture.py``.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1
GENERATOR_NAME = "orbital-render-3dgs"
GENERATOR_VERSION = [2, 0, 0]

IMAGE_EXTENSIONS = {
    "PNG": ".png",
    "JPEG": ".jpg",
    "OPEN_EXR": ".exr",
}


class PlanError(ValueError):
    """The requested or serialized capture plan is invalid."""


def parse_number_list(value: str | Iterable[float], label: str) -> list[float]:
    """Parse a comma-separated list or numeric iterable into finite floats."""
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",") if part.strip()]
        if not parts:
            raise PlanError(f"{label} must contain at least one number")
        try:
            values = [float(part) for part in parts]
        except ValueError as exc:
            raise PlanError(f"{label} contains a non-numeric value") from exc
    else:
        values = [float(item) for item in value]
        if not values:
            raise PlanError(f"{label} must contain at least one number")

    if not all(math.isfinite(item) for item in values):
        raise PlanError(f"{label} must contain only finite numbers")
    return values


def _finite_vector(value: Any, length: int, label: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise PlanError(f"{label} must contain {length} numbers")
    result = [float(item) for item in value]
    if not all(math.isfinite(item) for item in result):
        raise PlanError(f"{label} must contain only finite numbers")
    return result


def _positive_int(value: Any, label: str) -> int:
    result = int(value)
    if result <= 0:
        raise PlanError(f"{label} must be greater than zero")
    return result


def _positive_float(value: Any, label: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result <= 0.0:
        raise PlanError(f"{label} must be a finite number greater than zero")
    return result


def _canonical_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def plan_digest(plan: dict[str, Any]) -> str:
    """Return the digest of a plan, excluding its self-referential digest field."""
    unsigned = dict(plan)
    unsigned.pop("plan_sha256", None)
    return hashlib.sha256(_canonical_bytes(unsigned)).hexdigest()


def build_plan(
    config: dict[str, Any],
    resolved_target: dict[str, Any],
    scene_identity: dict[str, Any],
) -> dict[str, Any]:
    """Resolve a validated config and target into an explicit immutable plan."""
    sampling = config.get("sampling", {})
    radii = parse_number_list(
        sampling.get("horizontal_radii", []), "sampling.horizontal_radii"
    )
    if any(radius <= 0.0 for radius in radii):
        raise PlanError("sampling.horizontal_radii values must be greater than zero")
    heights = parse_number_list(
        sampling.get("world_z_heights", []), "sampling.world_z_heights"
    )
    azimuth_samples = _positive_int(
        sampling.get("azimuth_samples", 0), "sampling.azimuth_samples"
    )
    azimuth_start = float(sampling.get("azimuth_start_degrees", 0.0))
    if not math.isfinite(azimuth_start):
        raise PlanError("sampling.azimuth_start_degrees must be finite")
    eval_every = int(sampling.get("eval_every", 0))
    if eval_every < 0 or eval_every == 1:
        raise PlanError("sampling.eval_every must be zero or at least two")

    bounds_min = _finite_vector(
        resolved_target.get("bounds_min"), 3, "target.bounds_min"
    )
    bounds_max = _finite_vector(
        resolved_target.get("bounds_max"), 3, "target.bounds_max"
    )
    if any(low >= high for low, high in zip(bounds_min, bounds_max)):
        raise PlanError("target bounds must have positive size on every axis")
    center_xy = _finite_vector(resolved_target.get("center_xy"), 2, "target.center_xy")
    look_at_z = float(resolved_target.get("look_at_z"))
    if not math.isfinite(look_at_z):
        raise PlanError("target.look_at_z must be finite")

    camera_config = config.get("camera", {})
    sensor_fit = str(camera_config.get("sensor_fit", "HORIZONTAL")).upper()
    if sensor_fit not in {"AUTO", "HORIZONTAL", "VERTICAL"}:
        raise PlanError("camera.sensor_fit must be AUTO, HORIZONTAL, or VERTICAL")
    camera = {
        "lens_mm": _positive_float(
            camera_config.get("lens_mm", 50.0), "camera.lens_mm"
        ),
        "sensor_width_mm": _positive_float(
            camera_config.get("sensor_width_mm", 36.0), "camera.sensor_width_mm"
        ),
        "sensor_height_mm": _positive_float(
            camera_config.get("sensor_height_mm", 24.0), "camera.sensor_height_mm"
        ),
        "sensor_fit": sensor_fit,
        "shift_x": float(camera_config.get("shift_x", 0.0)),
        "shift_y": float(camera_config.get("shift_y", 0.0)),
        "clip_start": _positive_float(
            camera_config.get("clip_start", 0.1), "camera.clip_start"
        ),
        "clip_end": _positive_float(
            camera_config.get("clip_end", 10000.0), "camera.clip_end"
        ),
    }
    if camera["clip_end"] <= camera["clip_start"]:
        raise PlanError("camera.clip_end must be greater than camera.clip_start")
    if not math.isfinite(camera["shift_x"]) or not math.isfinite(camera["shift_y"]):
        raise PlanError("camera shifts must be finite")

    render_config = config.get("render", {})
    image_format = str(render_config.get("image_format", "PNG")).upper()
    if image_format not in IMAGE_EXTENSIONS:
        raise PlanError(f"unsupported render.image_format: {image_format}")
    render = {
        "resolution_x": _positive_int(
            render_config.get("resolution_x", 1920), "render.resolution_x"
        ),
        "resolution_y": _positive_int(
            render_config.get("resolution_y", 1080), "render.resolution_y"
        ),
        # Captures always force 100 percent. This removes a hidden source of
        # disagreement between the requested and actual image dimensions.
        "resolution_percentage": 100,
        "pixel_aspect_x": _positive_float(
            render_config.get("pixel_aspect_x", 1.0), "render.pixel_aspect_x"
        ),
        "pixel_aspect_y": _positive_float(
            render_config.get("pixel_aspect_y", 1.0), "render.pixel_aspect_y"
        ),
        "image_format": image_format,
        "file_extension": IMAGE_EXTENSIONS[image_format],
        "transparent_background": bool(
            render_config.get("transparent_background", False)
        ),
        "samples": _positive_int(render_config.get("samples", 64), "render.samples"),
        "engine": str(render_config.get("engine", "")),
    }

    initialization_config = config.get("initialization", {})
    initialization = {
        "max_points": _positive_int(
            initialization_config.get("max_points", 20000),
            "initialization.max_points",
        )
    }

    frames: list[dict[str, Any]] = []
    target_position = [center_xy[0], center_xy[1], look_at_z]
    extension = render["file_extension"]
    index = 0
    for radius in radii:
        for world_z in heights:
            for azimuth_index in range(azimuth_samples):
                angle_degrees = (
                    azimuth_start + 360.0 * azimuth_index / azimuth_samples
                ) % 360.0
                angle = math.radians(angle_degrees)
                position = [
                    center_xy[0] + radius * math.cos(angle),
                    center_xy[1] + radius * math.sin(angle),
                    world_z,
                ]
                split = "eval" if eval_every and index % eval_every == 0 else "train"
                frames.append(
                    {
                        "index": index,
                        "image_name": f"frame_{index:06d}{extension}",
                        "camera_position": position,
                        "look_at": list(target_position),
                        "horizontal_radius": radius,
                        "world_z": world_z,
                        "azimuth_degrees": angle_degrees,
                        "split": split,
                    }
                )
                index += 1

    target = {
        "mode": str(resolved_target.get("mode", "MANUAL_REGION")),
        "selectors": resolved_target.get("selectors", {}),
        "bounds_min": bounds_min,
        "bounds_max": bounds_max,
        "center_xy": center_xy,
        "look_at_z": look_at_z,
    }
    scene = {
        "blend_file": str(scene_identity.get("blend_file", "")),
        "frame": int(scene_identity.get("frame", 1)),
        "source_signature": str(scene_identity.get("source_signature", "")),
    }
    if not scene["source_signature"]:
        raise PlanError("scene.source_signature is required")

    plan: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generator": {"name": GENERATOR_NAME, "version": GENERATOR_VERSION},
        "scene": scene,
        "target": target,
        "sampling": {
            "horizontal_radii": radii,
            "world_z_heights": heights,
            "azimuth_samples": azimuth_samples,
            "azimuth_start_degrees": azimuth_start,
            "eval_every": eval_every,
        },
        "camera": camera,
        "render": render,
        "initialization": initialization,
        "frames": frames,
    }
    plan["plan_sha256"] = plan_digest(plan)
    validate_plan(plan, require_camera_records=False)
    return plan


def validate_plan(plan: dict[str, Any], *, require_camera_records: bool = True) -> None:
    """Validate structural invariants required by every capture executor."""
    if not isinstance(plan, dict):
        raise PlanError("capture plan must be a JSON object")
    if plan.get("schema_version") != SCHEMA_VERSION:
        raise PlanError(
            f"unsupported capture plan schema: {plan.get('schema_version')!r}"
        )
    expected_digest = plan_digest(plan)
    if plan.get("plan_sha256") != expected_digest:
        raise PlanError("capture plan digest is missing or does not match its contents")
    frames = plan.get("frames")
    if not isinstance(frames, list) or not frames:
        raise PlanError("capture plan must contain at least one frame")

    expected_names: set[str] = set()
    for expected_index, frame in enumerate(frames):
        if not isinstance(frame, dict) or frame.get("index") != expected_index:
            raise PlanError("capture frame indices must be contiguous and zero-based")
        name = frame.get("image_name")
        if (
            not isinstance(name, str)
            or not name
            or Path(name).name != name
            or name in expected_names
        ):
            raise PlanError("capture frame image names must be unique base names")
        expected_names.add(name)
        _finite_vector(frame.get("camera_position"), 3, "frame.camera_position")
        _finite_vector(frame.get("look_at"), 3, "frame.look_at")
        if frame.get("split") not in {"train", "eval"}:
            raise PlanError("capture frame split must be train or eval")
        if require_camera_records:
            intrinsics = frame.get("intrinsics", {})
            for key in ("fx", "fy", "cx", "cy"):
                if not math.isfinite(float(intrinsics.get(key, math.nan))):
                    raise PlanError(f"frame intrinsics {key} must be finite")
            try:
                width = int(intrinsics.get("width"))
                height = int(intrinsics.get("height"))
            except (TypeError, ValueError) as exc:
                raise PlanError("frame intrinsics dimensions must be integers") from exc
            if width <= 0 or height <= 0:
                raise PlanError("frame intrinsics dimensions must be positive")
            matrix = frame.get("camera_c2w_cv")
            if not isinstance(matrix, list) or len(matrix) != 4:
                raise PlanError("frame camera_c2w_cv must be a 4x4 matrix")
            for row in matrix:
                _finite_vector(row, 4, "frame.camera_c2w_cv row")
            pose = frame.get("colmap_w2c", {})
            _finite_vector(pose.get("quaternion_wxyz"), 4, "frame quaternion")
            _finite_vector(pose.get("translation"), 3, "frame translation")

    if require_camera_records:
        camera_model = plan.get("camera_model", {})
        if camera_model.get("model") != "PINHOLE":
            raise PlanError("materialized capture plan must use a PINHOLE camera")
        for key in ("width", "height", "fx", "fy", "cx", "cy"):
            if not math.isfinite(float(camera_model.get(key, math.nan))):
                raise PlanError(f"camera_model.{key} must be finite")

    render = plan.get("render", {})
    if render.get("resolution_percentage") != 100:
        raise PlanError("capture plans must render at 100 percent resolution")
    extension = IMAGE_EXTENSIONS.get(render.get("image_format"))
    if not extension or render.get("file_extension") != extension:
        raise PlanError("render image format and file extension disagree")
    if any(not frame["image_name"].endswith(extension) for frame in frames):
        raise PlanError("frame image extension disagrees with render format")
    if require_camera_records:
        camera_model = plan["camera_model"]
        if camera_model["width"] != render.get("resolution_x") or camera_model[
            "height"
        ] != render.get("resolution_y"):
            raise PlanError("camera model dimensions disagree with render dimensions")
        for frame in frames:
            intrinsics = frame["intrinsics"]
            for key in ("width", "height", "fx", "fy", "cx", "cy"):
                if not math.isclose(
                    float(intrinsics[key]),
                    float(camera_model[key]),
                    rel_tol=1e-12,
                    abs_tol=1e-9,
                ):
                    raise PlanError(
                        f"frame {frame['index']} intrinsic {key} differs from camera model"
                    )


def load_plan(path: str | Path) -> dict[str, Any]:
    plan_path = Path(path)
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanError(f"could not read capture plan {plan_path}: {exc}") from exc
    validate_plan(plan)
    return plan
