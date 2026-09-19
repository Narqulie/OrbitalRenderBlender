"""Atomic output state and standard COLMAP text serialization."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable

STATE_SCHEMA_VERSION = 1


class OutputError(RuntimeError):
    """The output directory cannot be used without risking unrelated data."""


def _json_text(value: Any) -> str:
    return (
        json.dumps(
            value,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    )


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.orbital-render.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, _json_text(value))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _initial_state(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "plan_sha256": plan["plan_sha256"],
        "status": "planned",
        "complete": False,
        "initialization": None,
        "pending_initialization": None,
        "pending_frame": None,
        "completed": [],
    }


class DatasetOutput:
    """Own and validate one capture directory.

    The class never removes unknown files. Resume accepts only artifacts already
    recorded with their size and digest in the atomic state file.
    """

    def __init__(self, root: str | Path, plan: dict[str, Any], resume: bool = False):
        self.root = Path(root).expanduser().resolve()
        self.images_dir = self.root / "images"
        self.sparse_dir = self.root / "sparse" / "0"
        self.plan_path = self.root / "capture_plan.json"
        self.state_path = self.root / "capture_state.json"
        self.plan = plan
        self.resume = resume
        self.state: dict[str, Any]

    def prepare(self) -> dict[str, Any]:
        existed = self.root.exists()
        if existed and not self.root.is_dir():
            raise OutputError(f"output path is not a directory: {self.root}")
        entries = list(self.root.iterdir()) if existed else []

        if not entries:
            self.images_dir.mkdir(parents=True, exist_ok=True)
            self.sparse_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self.plan_path, self.plan)
            self.state = _initial_state(self.plan)
            atomic_write_json(self.state_path, self.state)
            return self.state

        if not self.plan_path.is_file() or not self.state_path.is_file():
            raise OutputError(
                "output directory is not empty and is not an owned capture directory"
            )
        try:
            existing_plan = json.loads(self.plan_path.read_text(encoding="utf-8"))
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OutputError(
                f"could not read existing capture metadata: {exc}"
            ) from exc

        if existing_plan.get("plan_sha256") != self.plan["plan_sha256"]:
            raise OutputError("existing capture plan does not match the requested plan")
        # Comparing the canonical parsed objects also catches a forged/stale hash.
        if existing_plan != self.plan:
            raise OutputError(
                "existing capture plan contents differ from the requested plan"
            )
        if state.get("schema_version") != STATE_SCHEMA_VERSION:
            raise OutputError("unsupported capture state schema")
        if state.get("plan_sha256") != self.plan["plan_sha256"]:
            raise OutputError("capture state belongs to a different plan")
        state = self._recover_pending(state)
        completed = state.get("completed")
        if not isinstance(completed, list):
            raise OutputError("capture state completed field is invalid")
        if bool(state.get("complete")) != (len(completed) == len(self.plan["frames"])):
            raise OutputError(
                "capture state completion flag disagrees with frame count"
            )
        if completed and not self.resume:
            raise OutputError(
                "capture is already in progress; pass --resume to continue"
            )

        initialization = state.get("initialization")
        points_path = self.sparse_dir / "points3D.txt"
        if initialization is not None:
            if not isinstance(initialization, dict) or not points_path.is_file():
                raise OutputError("initialization point cloud record is invalid")
            if points_path.stat().st_size != initialization.get("size"):
                raise OutputError("initialization point cloud size differs")
            if file_sha256(points_path) != initialization.get("sha256"):
                raise OutputError("initialization point cloud checksum differs")
        elif points_path.exists():
            raise OutputError(
                "uncommitted initialization point cloud exists and will not be overwritten"
            )

        expected_frames = {frame["index"]: frame for frame in self.plan["frames"]}
        completed_indices: set[int] = set()
        for expected_order, record in enumerate(completed):
            if not isinstance(record, dict):
                raise OutputError("capture state contains an invalid completion record")
            index = record.get("index")
            if index != expected_order or index not in expected_frames:
                raise OutputError("completed frames must be a contiguous plan prefix")
            frame = expected_frames[index]
            if record.get("image_name") != frame["image_name"]:
                raise OutputError("completed frame image name differs from the plan")
            image_path = self.images_dir / frame["image_name"]
            if not image_path.is_file():
                raise OutputError(f"completed image is missing: {image_path}")
            size = image_path.stat().st_size
            if size <= 0 or size != record.get("size"):
                raise OutputError(f"completed image size differs: {image_path}")
            if file_sha256(image_path) != record.get("sha256"):
                raise OutputError(f"completed image checksum differs: {image_path}")
            completed_indices.add(index)

        for frame in self.plan["frames"]:
            image_path = self.images_dir / frame["image_name"]
            if frame["index"] not in completed_indices and image_path.exists():
                raise OutputError(
                    f"uncommitted planned image exists and will not be overwritten: {image_path}"
                )

        initialization_staging = self.initialization_staging_path
        if initialization is None and initialization_staging.exists():
            initialization_staging.unlink()
        generated_paths = [
            self.sparse_dir / "cameras.txt",
            self.sparse_dir / "images.txt",
            self.root / "splits.json",
            self.root / "cameras.json",
        ]
        if initialization is None:
            collisions = [
                str(path) for path in [*generated_paths, points_path] if path.exists()
            ]
            if collisions:
                raise OutputError(
                    "uncommitted generated output exists and will not be overwritten: "
                    + ", ".join(collisions)
                )
        else:
            completed_frames = self.plan["frames"][: len(completed)]
            expected = {
                self.sparse_dir / "cameras.txt": _cameras_text(
                    self.plan["camera_model"]
                ),
                self.sparse_dir / "images.txt": _images_text(completed_frames),
                self.root / "splits.json": _json_text(_splits_json(self.plan)),
                self.root / "cameras.json": _json_text(
                    _inria_cameras(completed_frames)
                ),
            }
            for path, text in expected.items():
                try:
                    actual = path.read_text(encoding="utf-8")
                except OSError as exc:
                    raise OutputError(f"generated metadata is missing: {path}") from exc
                if actual != text:
                    raise OutputError(f"generated metadata differs: {path}")

        self.images_dir.mkdir(parents=True, exist_ok=True)
        self.sparse_dir.mkdir(parents=True, exist_ok=True)
        self.state = state
        return state

    def _recover_pending(self, state: dict[str, Any]) -> dict[str, Any]:
        pending_initialization = state.get("pending_initialization")
        if pending_initialization is not None:
            if state.get("initialization") is not None:
                raise OutputError(
                    "capture state has conflicting initialization records"
                )
            points_path = self.sparse_dir / "points3D.txt"
            staging = self.initialization_staging_path
            self._finish_pending_artifact(staging, points_path, pending_initialization)
            final_state = dict(state)
            final_state["initialization"] = pending_initialization
            final_state["pending_initialization"] = None
            final_state["status"] = "ready"
            self.state = final_state
            self.write_metadata()
            atomic_write_json(self.state_path, final_state)
            state = final_state

        pending_frame = state.get("pending_frame")
        if pending_frame is not None:
            if state.get("initialization") is None:
                raise OutputError(
                    "pending frame exists before initialization was committed"
                )
            completed = state.get("completed")
            if not isinstance(completed, list):
                raise OutputError("capture state completed field is invalid")
            index = pending_frame.get("index")
            if index != len(completed) or index >= len(self.plan["frames"]):
                raise OutputError("pending frame is not the next plan frame")
            frame = self.plan["frames"][index]
            if pending_frame.get("image_name") != frame["image_name"]:
                raise OutputError("pending frame image name differs from the plan")
            temporary = self.temporary_image_path(frame)
            destination = self.images_dir / frame["image_name"]
            self._finish_pending_artifact(temporary, destination, pending_frame)
            final_state = dict(state)
            final_state["pending_frame"] = None
            final_state["completed"] = [*completed, pending_frame]
            is_complete = len(final_state["completed"]) == len(self.plan["frames"])
            final_state["complete"] = is_complete
            final_state["status"] = "complete" if is_complete else "capturing"
            self.state = final_state
            self.write_metadata()
            atomic_write_json(self.state_path, final_state)
            state = final_state
        return state

    @staticmethod
    def _finish_pending_artifact(
        staging: Path, destination: Path, record: dict[str, Any]
    ) -> None:
        def matches(path: Path) -> bool:
            return (
                path.is_file()
                and path.stat().st_size == record.get("size")
                and file_sha256(path) == record.get("sha256")
            )

        if destination.exists():
            if not matches(destination):
                raise OutputError(f"pending destination differs: {destination}")
            if staging.exists():
                if not matches(staging):
                    raise OutputError(f"pending staging artifact differs: {staging}")
                staging.unlink()
            return
        if not matches(staging):
            raise OutputError(
                f"pending staging artifact is missing or differs: {staging}"
            )
        os.replace(staging, destination)

    @property
    def initialization_staging_path(self) -> Path:
        digest = self.plan["plan_sha256"][:16]
        return self.sparse_dir / f".points3D.{digest}.orbital-render.pending"

    @property
    def completed_count(self) -> int:
        return len(self.state.get("completed", []))

    @property
    def initialization_complete(self) -> bool:
        return self.state.get("initialization") is not None

    def commit_initialization(self, points: Iterable[dict[str, Any]]) -> Path:
        if self.initialization_complete:
            raise OutputError("initialization point cloud is already committed")
        points_path = self.sparse_dir / "points3D.txt"
        staging = self.initialization_staging_path
        if points_path.exists():
            raise OutputError(f"refusing to overwrite point cloud: {points_path}")
        if staging.exists():
            staging.unlink()
        atomic_write_text(staging, _points_text(points))
        record = {
            "size": staging.stat().st_size,
            "sha256": file_sha256(staging),
        }
        pending_state = dict(self.state)
        pending_state["pending_initialization"] = record
        atomic_write_json(self.state_path, pending_state)
        os.replace(staging, points_path)
        final_state = dict(pending_state)
        final_state["initialization"] = record
        final_state["pending_initialization"] = None
        final_state["status"] = "ready"
        self.state = final_state
        self.write_metadata()
        atomic_write_json(self.state_path, final_state)
        return points_path

    def temporary_image_path(self, frame: dict[str, Any]) -> Path:
        digest = self.plan["plan_sha256"][:16]
        return self.images_dir / (
            f".{Path(frame['image_name']).stem}.{digest}.orbital-render.tmp"
            f"{self.plan['render']['file_extension']}"
        )

    def commit_image(self, frame: dict[str, Any], temporary_path: Path) -> Path:
        if frame["index"] != self.completed_count:
            raise OutputError("frames must be committed in plan order")
        if not temporary_path.is_file() or temporary_path.stat().st_size <= 0:
            raise OutputError(
                f"renderer did not create a non-empty image: {temporary_path}"
            )
        destination = self.images_dir / frame["image_name"]
        if destination.exists():
            raise OutputError(f"refusing to overwrite image: {destination}")
        record = {
            "index": frame["index"],
            "image_name": frame["image_name"],
            "size": temporary_path.stat().st_size,
            "sha256": file_sha256(temporary_path),
        }
        pending_state = dict(self.state)
        pending_state["pending_frame"] = record
        atomic_write_json(self.state_path, pending_state)
        os.replace(temporary_path, destination)
        final_state = dict(pending_state)
        final_state["pending_frame"] = None
        final_state["completed"] = [*self.state["completed"], record]
        is_complete = len(final_state["completed"]) == len(self.plan["frames"])
        final_state["complete"] = is_complete
        final_state["status"] = "complete" if is_complete else "capturing"
        self.state = final_state
        self.write_metadata()
        atomic_write_json(self.state_path, final_state)
        return destination

    def mark_cancelled(self) -> None:
        new_state = dict(self.state)
        if new_state.get("status") != "complete":
            new_state["status"] = "cancelled"
            atomic_write_json(self.state_path, new_state)
            self.state = new_state

    def write_metadata(self, points: Iterable[dict[str, Any]] | None = None) -> None:
        completed_count = self.completed_count
        frames = self.plan["frames"][:completed_count]
        camera = self.plan["camera_model"]
        atomic_write_text(self.sparse_dir / "cameras.txt", _cameras_text(camera))
        atomic_write_text(self.sparse_dir / "images.txt", _images_text(frames))
        points_path = self.sparse_dir / "points3D.txt"
        if points is not None:
            atomic_write_text(points_path, _points_text(points))
        atomic_write_json(self.root / "splits.json", _splits_json(self.plan))
        atomic_write_json(self.root / "cameras.json", _inria_cameras(frames))


def _number(value: float) -> str:
    return format(float(value), ".17g")


def _cameras_text(camera: dict[str, Any]) -> str:
    header = (
        "# Camera list with one line of data per camera:\n"
        "#   CAMERA_ID, MODEL, WIDTH, HEIGHT, PARAMS[]\n"
        "# Number of cameras: 1\n"
    )
    params = " ".join(_number(camera[key]) for key in ("fx", "fy", "cx", "cy"))
    return header + f"1 PINHOLE {camera['width']} {camera['height']} {params}\n"


def _images_text(frames: Iterable[dict[str, Any]]) -> str:
    frames = list(frames)
    lines = [
        "# Image list with two lines of data per image:",
        "#   IMAGE_ID, QW, QX, QY, QZ, TX, TY, TZ, CAMERA_ID, NAME",
        "#   POINTS2D[] as (X, Y, POINT3D_ID)",
        f"# Number of images: {len(frames)}, mean observations per image: 0",
    ]
    for frame in frames:
        pose = frame["colmap_w2c"]
        values = [*pose["quaternion_wxyz"], *pose["translation"]]
        line = " ".join(_number(value) for value in values)
        lines.append(f"{frame['index'] + 1} {line} 1 {frame['image_name']}")
        # COLMAP's text model uses a second, possibly empty, observations line.
        lines.append("")
    return "\n".join(lines) + "\n"


def _points_text(points: Iterable[dict[str, Any]]) -> str:
    points = list(points)
    lines = [
        "# 3D point list with one line of data per point:",
        "#   POINT3D_ID, X, Y, Z, R, G, B, ERROR, TRACK[]",
        "# Number of points: %d, mean track length: 0" % len(points),
    ]
    for point_id, point in enumerate(points, start=1):
        xyz = " ".join(_number(value) for value in point["xyz"])
        rgb = " ".join(str(max(0, min(255, int(value)))) for value in point["rgb"])
        lines.append(f"{point_id} {xyz} {rgb} 0")
    return "\n".join(lines) + "\n"


def _splits_json(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "plan_sha256": plan["plan_sha256"],
        "eval_every": plan["sampling"]["eval_every"],
        "train": [
            frame["image_name"] for frame in plan["frames"] if frame["split"] == "train"
        ],
        "eval": [
            frame["image_name"] for frame in plan["frames"] if frame["split"] == "eval"
        ],
    }


def _inria_cameras(frames: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    cameras = []
    for frame in frames:
        intrinsics = frame["intrinsics"]
        matrix = frame["camera_c2w_cv"]
        cameras.append(
            {
                "id": frame["index"],
                "img_name": frame["image_name"],
                "position": [matrix[row][3] for row in range(3)],
                "rotation": [row[:3] for row in matrix[:3]],
                "width": intrinsics["width"],
                "height": intrinsics["height"],
                "fx": intrinsics["fx"],
                "fy": intrinsics["fy"],
            }
        )
    return cameras
