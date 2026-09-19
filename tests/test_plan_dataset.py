from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from orbital_render_3dgs_addon.dataset import (
    DatasetOutput,
    OutputError,
    atomic_write_json,
    file_sha256,
)
from orbital_render_3dgs_addon.plan import PlanError, build_plan, plan_digest


CONFIG = {
    "sampling": {
        "horizontal_radii": [10.0, 20.0],
        "world_z_heights": [5.0],
        "azimuth_samples": 4,
        "azimuth_start_degrees": 0.0,
        "eval_every": 3,
    },
    "camera": {
        "lens_mm": 50.0,
        "sensor_width_mm": 36.0,
        "sensor_height_mm": 24.0,
        "sensor_fit": "HORIZONTAL",
    },
    "render": {
        "resolution_x": 64,
        "resolution_y": 32,
        "image_format": "PNG",
        "samples": 1,
        "engine": "BLENDER_WORKBENCH",
    },
    "initialization": {"max_points": 10},
}
TARGET = {
    "mode": "MANUAL_REGION",
    "selectors": {},
    "bounds_min": [-1.0, -2.0, -3.0],
    "bounds_max": [1.0, 2.0, 3.0],
    "center_xy": [0.0, 0.0],
    "look_at_z": 0.5,
}
SCENE = {"blend_file": "", "frame": 1, "source_signature": "abc"}


def materialized_plan():
    plan = build_plan(CONFIG, TARGET, SCENE)
    intrinsics = {
        "model": "PINHOLE",
        "width": 64,
        "height": 32,
        "fx": 40.0,
        "fy": 41.0,
        "cx": 31.0,
        "cy": 15.0,
    }
    plan["camera_model"] = dict(intrinsics)
    for frame in plan["frames"]:
        frame["intrinsics"] = dict(intrinsics)
        frame["camera_c2w_cv"] = [
            [1.0, 0.0, 0.0, frame["camera_position"][0]],
            [0.0, 1.0, 0.0, frame["camera_position"][1]],
            [0.0, 0.0, 1.0, frame["camera_position"][2]],
            [0.0, 0.0, 0.0, 1.0],
        ]
        frame["colmap_w2c"] = {
            "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0],
            "translation": [0.0, 0.0, 0.0],
        }
    plan["plan_sha256"] = plan_digest(plan)
    return plan


class PlanTests(unittest.TestCase):
    def test_order_names_and_holdout_match_lexicographic_order(self):
        plan = materialized_plan()
        names = [frame["image_name"] for frame in plan["frames"]]
        self.assertEqual(names, sorted(names))
        self.assertEqual(len(names), 8)
        self.assertEqual(
            [frame["index"] for frame in plan["frames"] if frame["split"] == "eval"],
            [0, 3, 6],
        )
        self.assertEqual(plan["frames"][0]["camera_position"], [10.0, 0.0, 5.0])
        self.assertAlmostEqual(plan["frames"][1]["camera_position"][1], 10.0)

    def test_digest_changes_with_render_or_source(self):
        original = materialized_plan()
        changed = json.loads(json.dumps(original))
        changed["render"]["resolution_x"] += 1
        self.assertNotEqual(plan_digest(original), plan_digest(changed))
        changed = json.loads(json.dumps(original))
        changed["scene"]["source_signature"] = "different"
        self.assertNotEqual(plan_digest(original), plan_digest(changed))

    def test_image_formats_use_matching_extensions(self):
        for image_format, extension in (
            ("PNG", ".png"),
            ("JPEG", ".jpg"),
            ("OPEN_EXR", ".exr"),
        ):
            config = json.loads(json.dumps(CONFIG))
            config["render"]["image_format"] = image_format
            plan = build_plan(config, TARGET, SCENE)
            self.assertTrue(
                all(frame["image_name"].endswith(extension) for frame in plan["frames"])
            )

    def test_eval_every_one_is_rejected(self):
        config = json.loads(json.dumps(CONFIG))
        config["sampling"]["eval_every"] = 1
        with self.assertRaises(PlanError):
            build_plan(config, TARGET, SCENE)


class OutputTests(unittest.TestCase):
    def test_foreign_nonempty_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "unrelated.txt").write_text("keep", encoding="utf-8")
            with self.assertRaises(OutputError):
                DatasetOutput(root, materialized_plan()).prepare()
            self.assertEqual((root / "unrelated.txt").read_text(), "keep")

    def test_plan_only_directory_can_be_opened_without_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            plan = materialized_plan()
            first = DatasetOutput(temporary, plan)
            first.prepare()
            second = DatasetOutput(temporary, plan)
            state = second.prepare()
            self.assertEqual(state["completed"], [])

    def test_committed_initialization_is_validated(self):
        with tempfile.TemporaryDirectory() as temporary:
            plan = materialized_plan()
            output = DatasetOutput(temporary, plan)
            output.prepare()
            output.commit_initialization([{"xyz": [0, 0, 0], "rgb": [1, 2, 3]}])
            resumed = DatasetOutput(temporary, plan)
            resumed.prepare()
            points = Path(temporary) / "sparse" / "0" / "points3D.txt"
            points.write_text("tampered", encoding="utf-8")
            with self.assertRaises(OutputError):
                DatasetOutput(temporary, plan).prepare()

    def test_pending_frame_is_recovered_without_rerender(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = materialized_plan()
            output = DatasetOutput(root, plan)
            output.prepare()
            output.commit_initialization([{"xyz": [0, 0, 0], "rgb": [1, 2, 3]}])
            frame = plan["frames"][0]
            staging = output.temporary_image_path(frame)
            staging.write_bytes(b"synthetic-image")
            record = {
                "index": 0,
                "image_name": frame["image_name"],
                "size": staging.stat().st_size,
                "sha256": file_sha256(staging),
            }
            pending = dict(output.state)
            pending["pending_frame"] = record
            atomic_write_json(output.state_path, pending)

            recovered = DatasetOutput(root, plan, resume=True)
            state = recovered.prepare()
            self.assertEqual(len(state["completed"]), 1)
            self.assertIsNone(state["pending_frame"])
            self.assertFalse(staging.exists())
            self.assertTrue((root / "images" / frame["image_name"]).is_file())


if __name__ == "__main__":
    unittest.main()
