"""Headless command-line entry point for bounded capture slices."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import bpy

from .blender_capture import CaptureSession, create_plan_from_config
from .dataset import DatasetOutput
from .plan import PlanError, load_plan

RESULT_PREFIX = "ORBITAL_CAPTURE_RESULT "


def _arguments_after_separator(argv: list[str]) -> list[str]:
    return argv[argv.index("--") + 1 :] if "--" in argv else []


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orbital_capture",
        description="Create or execute an Orbital Render capture plan in Blender.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--config", type=Path, help="JSON capture request to resolve")
    source.add_argument("--plan", type=Path, help="Existing frozen capture_plan.json")
    parser.add_argument(
        "--output",
        type=Path,
        help="Dataset directory; required with --config, defaults to plan parent with --plan",
    )
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="Write the frozen plan and empty state without sampling or rendering",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Validate and continue a capture that has committed frames",
    )
    parser.add_argument(
        "--max-frames",
        type=int,
        help="Render at most this many new frames, then checkpoint and exit successfully",
    )
    return parser


def _read_config(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanError(f"could not read capture config {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PlanError("capture config must be a JSON object")
    return value


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.max_frames is not None and args.max_frames <= 0:
        raise PlanError("--max-frames must be greater than zero")
    if args.plan_only and args.resume:
        raise PlanError("--plan-only and --resume cannot be combined")
    if args.plan_only and args.max_frames is not None:
        raise PlanError("--plan-only and --max-frames cannot be combined")

    context = bpy.context
    resolved_target = None
    if args.config:
        if args.output is None:
            raise PlanError("--output is required with --config")
        config = _read_config(args.config.expanduser().resolve())
        plan, resolved_target = create_plan_from_config(context, config)
        output = args.output.expanduser().resolve()
    else:
        plan_path = args.plan.expanduser().resolve()
        plan = load_plan(plan_path)
        output = args.output.expanduser().resolve() if args.output else plan_path.parent

    if args.plan_only:
        dataset = DatasetOutput(output, plan, resume=False)
        dataset.prepare()
        return {
            "complete": False,
            "plan_only": True,
            "new_frames": 0,
            "completed_frames": 0,
            "total_frames": len(plan["frames"]),
            "output_directory": str(output),
            "plan_path": str(dataset.plan_path),
            "plan_sha256": plan["plan_sha256"],
        }

    session = CaptureSession(
        context,
        plan,
        output,
        resume=args.resume,
        resolved_target=resolved_target,
    )
    result = session.run_slice(args.max_frames)
    result["plan_only"] = False
    return result


def main(argv: list[str] | None = None) -> dict[str, Any]:
    arguments = _arguments_after_separator(sys.argv) if argv is None else argv
    args = _parser().parse_args(arguments)
    result = run(args)
    print(RESULT_PREFIX + json.dumps(result, sort_keys=True), flush=True)
    return result


if __name__ == "__main__":
    main()
