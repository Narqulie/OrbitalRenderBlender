"""Blender property, operator, and panel integration."""

from __future__ import annotations

import math
import traceback
from pathlib import Path
from typing import Any

import bpy
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import Operator, Panel, PropertyGroup

from .blender_capture import (
    OWNER_PROPERTY,
    CaptureSession,
    create_plan_from_config,
    resolve_target,
)
from .dataset import DatasetOutput
from .plan import PlanError, parse_number_list

PREVIEW_OWNER = "path-preview"


class OrbitalRenderSettings(PropertyGroup):
    target_mode: EnumProperty(
        name="Target",
        items=[
            (
                "SELECTION",
                "Selected objects",
                "Bounds of the selected objects or collection instances",
            ),
            (
                "COLLECTION",
                "Collection",
                "Bounds of all evaluated instances under a collection",
            ),
            ("MANUAL_REGION", "Manual region", "Explicit world-space region"),
        ],
        default="SELECTION",
    )
    target_collection: PointerProperty(name="Collection", type=bpy.types.Collection)
    manual_center: FloatVectorProperty(
        name="Region center", size=3, subtype="XYZ", default=(0.0, 0.0, 0.0)
    )
    manual_size: FloatVectorProperty(
        name="Region size",
        size=3,
        subtype="XYZ",
        default=(10.0, 10.0, 10.0),
        min=0.0001,
    )
    auto_look_at_z: BoolProperty(
        name="Look at target centre Z",
        default=True,
        description="Use the resolved target bounds centre as look-at Z",
    )
    look_at_z: FloatProperty(
        name="Look-at Z",
        default=0.0,
        description="World-space Blender Z, not verified sea-level altitude",
    )

    horizontal_radii: StringProperty(
        name="Horizontal radii",
        default="",
        description="Comma-separated world-space radii; blank derives 2.5x, 3.5x, and 5x target radius",
    )
    world_z_heights: StringProperty(
        name="World Z heights",
        default="",
        description="Comma-separated absolute Blender Z values; blank derives three target-relative heights",
    )
    azimuth_samples: IntProperty(name="Views per path", default=36, min=1, max=10000)
    azimuth_start_degrees: FloatProperty(
        name="Azimuth start", default=0.0, subtype="ANGLE"
    )
    eval_every: IntProperty(
        name="Hold out every",
        default=8,
        min=0,
        max=10000,
        description="Zero disables hold-out; otherwise must be at least 2",
    )

    lens_mm: FloatProperty(name="Focal length", default=50.0, min=1.0, unit="CAMERA")
    sensor_width_mm: FloatProperty(name="Sensor width", default=36.0, min=1.0)
    sensor_height_mm: FloatProperty(name="Sensor height", default=24.0, min=1.0)
    sensor_fit: EnumProperty(
        name="Sensor fit",
        items=[
            ("AUTO", "Auto", "Fit based on render aspect"),
            ("HORIZONTAL", "Horizontal", "Fit sensor width"),
            ("VERTICAL", "Vertical", "Fit sensor height"),
        ],
        default="HORIZONTAL",
    )
    shift_x: FloatProperty(name="Shift X", default=0.0)
    shift_y: FloatProperty(name="Shift Y", default=0.0)
    clip_start: FloatProperty(name="Clip start", default=0.1, min=0.0001)
    clip_end: FloatProperty(name="Clip end", default=10000.0, min=0.001)

    resolution_x: IntProperty(name="Width", default=1920, min=16, max=32768)
    resolution_y: IntProperty(name="Height", default=1080, min=16, max=32768)
    pixel_aspect_x: FloatProperty(name="Pixel aspect X", default=1.0, min=0.01)
    pixel_aspect_y: FloatProperty(name="Pixel aspect Y", default=1.0, min=0.01)
    render_samples: IntProperty(name="Samples", default=64, min=1, max=65536)
    transparent_background: BoolProperty(name="Transparent background", default=False)
    image_format: EnumProperty(
        name="Image format",
        items=[
            ("PNG", "PNG", "Lossless 8-bit image"),
            ("JPEG", "JPEG", "8-bit lossy image"),
            ("OPEN_EXR", "OpenEXR", "16-bit floating-point image"),
        ],
        default="PNG",
    )
    max_initialization_points: IntProperty(
        name="Initial points", default=20000, min=1, max=1000000
    )

    output_directory: StringProperty(
        name="Output directory", default="//orbital_capture", subtype="DIR_PATH"
    )
    resume_capture: BoolProperty(
        name="Resume validated capture",
        default=False,
        description="Continue only when plan, source, settings, and committed files match",
    )

    is_rendering: BoolProperty(default=False, options={"HIDDEN"})
    cancel_requested: BoolProperty(default=False, options={"HIDDEN"})
    render_progress: FloatProperty(
        name="Progress", default=0.0, min=0.0, max=100.0, subtype="PERCENTAGE"
    )
    render_status: StringProperty(name="Status", default="")


def _target_request(settings: OrbitalRenderSettings) -> dict[str, Any]:
    target: dict[str, Any] = {"mode": settings.target_mode}
    if settings.target_mode == "COLLECTION":
        if settings.target_collection is None:
            raise PlanError("choose a target collection")
        target["collection"] = settings.target_collection.name_full
    elif settings.target_mode == "MANUAL_REGION":
        target["center"] = list(settings.manual_center)
        target["size"] = list(settings.manual_size)
    if not settings.auto_look_at_z:
        target["look_at_z"] = settings.look_at_z
    return target


def _settings_config(context: bpy.types.Context) -> dict[str, Any]:
    settings = context.scene.orbital_render_settings
    target = _target_request(settings)
    resolved = resolve_target(context, target)
    low = resolved["bounds_min"]
    high = resolved["bounds_max"]
    half_extent = [0.5 * (high[index] - low[index]) for index in range(3)]
    radius = math.sqrt(sum(value * value for value in half_extent))
    center_z = 0.5 * (low[2] + high[2])

    radii = (
        parse_number_list(settings.horizontal_radii, "horizontal radii")
        if settings.horizontal_radii.strip()
        else [2.5 * radius, 3.5 * radius, 5.0 * radius]
    )
    heights = (
        parse_number_list(settings.world_z_heights, "world Z heights")
        if settings.world_z_heights.strip()
        else [center_z - 0.6 * radius, center_z, center_z + 0.6 * radius]
    )
    if settings.eval_every == 1:
        raise PlanError("hold-out interval must be zero or at least two")

    return {
        "target": target,
        "sampling": {
            "horizontal_radii": radii,
            "world_z_heights": heights,
            "azimuth_samples": settings.azimuth_samples,
            "azimuth_start_degrees": math.degrees(settings.azimuth_start_degrees),
            "eval_every": settings.eval_every,
        },
        "camera": {
            "lens_mm": settings.lens_mm,
            "sensor_width_mm": settings.sensor_width_mm,
            "sensor_height_mm": settings.sensor_height_mm,
            "sensor_fit": settings.sensor_fit,
            "shift_x": settings.shift_x,
            "shift_y": settings.shift_y,
            "clip_start": settings.clip_start,
            "clip_end": settings.clip_end,
        },
        "render": {
            "engine": context.scene.render.engine,
            "resolution_x": settings.resolution_x,
            "resolution_y": settings.resolution_y,
            "pixel_aspect_x": settings.pixel_aspect_x,
            "pixel_aspect_y": settings.pixel_aspect_y,
            "image_format": settings.image_format,
            "transparent_background": settings.transparent_background,
            "samples": settings.render_samples,
        },
        "initialization": {"max_points": settings.max_initialization_points},
    }


def _output_path(settings: OrbitalRenderSettings) -> Path:
    return Path(bpy.path.abspath(settings.output_directory)).expanduser().resolve()


def _remove_preview(scene: bpy.types.Scene) -> bool:
    removed = False
    for obj in list(bpy.data.objects):
        if obj.get(OWNER_PROPERTY) == PREVIEW_OWNER:
            mesh = obj.data
            bpy.data.objects.remove(obj, do_unlink=True)
            if mesh and mesh.users == 0:
                bpy.data.meshes.remove(mesh)
            removed = True
    for collection in list(bpy.data.collections):
        if collection.get(OWNER_PROPERTY) == PREVIEW_OWNER:
            bpy.data.collections.remove(collection)
            removed = True
    return removed


class ORBITAL_RENDER_OT_preview(Operator):
    bl_idname = "orbital_render.preview"
    bl_label = "Toggle path preview"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        if _remove_preview(context.scene):
            self.report({"INFO"}, "Removed capture path preview")
            return {"FINISHED"}
        try:
            plan, _ = create_plan_from_config(context, _settings_config(context))
            vertices = [frame["camera_position"] for frame in plan["frames"]]
            edges = []
            count = plan["sampling"]["azimuth_samples"]
            for start in range(0, len(vertices), count):
                edges.extend(
                    (start + offset, start + ((offset + 1) % count))
                    for offset in range(count)
                )
            mesh = bpy.data.meshes.new("OrbitalRenderPathPreview")
            mesh.from_pydata(vertices, edges, [])
            collection = bpy.data.collections.new("OrbitalRenderPathPreview")
            collection[OWNER_PROPERTY] = PREVIEW_OWNER
            context.scene.collection.children.link(collection)
            obj = bpy.data.objects.new("OrbitalRenderPathPreview", mesh)
            obj[OWNER_PROPERTY] = PREVIEW_OWNER
            obj.display_type = "WIRE"
            obj.show_in_front = True
            obj.hide_render = True
            collection.objects.link(obj)
            self.report({"INFO"}, f"Previewed {len(vertices)} camera positions")
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class ORBITAL_RENDER_OT_stats(Operator):
    bl_idname = "orbital_render.stats"
    bl_label = "Validate and count"

    def execute(self, context):
        try:
            plan, _ = create_plan_from_config(context, _settings_config(context))
            target = plan["target"]
            message = (
                f"{len(plan['frames'])} frames; bounds Z "
                f"{target['bounds_min'][2]:.3f} to {target['bounds_max'][2]:.3f}; "
                f"plan {plan['plan_sha256'][:12]}"
            )
            print("Orbital Render:", message)
            self.report({"INFO"}, message)
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class ORBITAL_RENDER_OT_save_plan(Operator):
    bl_idname = "orbital_render.save_plan"
    bl_label = "Save capture plan"

    def execute(self, context):
        try:
            plan, _ = create_plan_from_config(context, _settings_config(context))
            output = DatasetOutput(
                _output_path(context.scene.orbital_render_settings), plan
            )
            output.prepare()
            self.report({"INFO"}, f"Saved {output.plan_path}")
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class ORBITAL_RENDER_OT_generate(Operator):
    bl_idname = "orbital_render.generate"
    bl_label = "Generate COLMAP dataset"
    bl_options = {"REGISTER"}

    _timer = None
    _session: CaptureSession | None = None

    def execute(self, context):
        settings = context.scene.orbital_render_settings
        try:
            plan, resolved = create_plan_from_config(context, _settings_config(context))
            self._session = CaptureSession(
                context,
                plan,
                _output_path(settings),
                resume=settings.resume_capture,
                resolved_target=resolved,
            )
            self._session.prepare()
            settings.is_rendering = True
            settings.cancel_requested = False
            settings.render_progress = (
                100.0 * self._session.output.completed_count / len(plan["frames"])
            )
            settings.render_status = "Capture prepared"
            context.window_manager.progress_begin(0, len(plan["frames"]))
            context.window_manager.progress_update(self._session.output.completed_count)
            self._timer = context.window_manager.event_timer_add(
                0.05, window=context.window
            )
            context.window_manager.modal_handler_add(self)
            return {"RUNNING_MODAL"}
        except Exception as exc:
            if self._session:
                self._session.close()
            settings.is_rendering = False
            settings.render_status = f"Error: {exc}"
            traceback.print_exc()
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

    def modal(self, context, event):
        settings = context.scene.orbital_render_settings
        if event.type == "ESC":
            settings.cancel_requested = True
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        if settings.cancel_requested:
            return self._finish(context, cancelled=True)
        assert self._session is not None
        try:
            index = self._session.output.completed_count
            total = len(self._session.plan["frames"])
            if index >= total:
                return self._finish(context, cancelled=False)
            frame = self._session.plan["frames"][index]
            settings.render_status = (
                f"Rendering {index + 1}/{total}: radius {frame['horizontal_radius']:.3f}, "
                f"Z {frame['world_z']:.3f}"
            )
            self._session.render_next()
            completed = self._session.output.completed_count
            context.window_manager.progress_update(completed)
            settings.render_progress = 100.0 * completed / total
            if completed >= total:
                return self._finish(context, cancelled=False)
            return {"RUNNING_MODAL"}
        except Exception as exc:
            traceback.print_exc()
            self.report({"ERROR"}, str(exc))
            return self._finish(context, cancelled=True, error_message=str(exc))

    def _finish(self, context, *, cancelled: bool, error_message: str | None = None):
        settings = context.scene.orbital_render_settings
        completed = self._session.output.completed_count if self._session else 0
        total = len(self._session.plan["frames"]) if self._session else 0
        try:
            if self._session:
                if cancelled:
                    self._session.cancel()
                else:
                    self._session.close()
        finally:
            if self._timer:
                context.window_manager.event_timer_remove(self._timer)
                self._timer = None
            context.window_manager.progress_end()
            settings.is_rendering = False
            settings.cancel_requested = False
        if cancelled:
            if error_message:
                settings.render_status = f"Error after {completed}/{total}: {error_message}; resume is available"
            else:
                settings.render_status = (
                    f"Cancelled after {completed}/{total}; resume is available"
                )
                self.report({"WARNING"}, settings.render_status)
            return {"CANCELLED"}
        settings.render_progress = 100.0
        settings.render_status = f"Complete: {completed}/{total}"
        self.report({"INFO"}, settings.render_status)
        return {"FINISHED"}

    def cancel(self, context):
        return self._finish(context, cancelled=True)


class ORBITAL_RENDER_OT_cancel(Operator):
    bl_idname = "orbital_render.cancel"
    bl_label = "Cancel after current frame"

    def execute(self, context):
        settings = context.scene.orbital_render_settings
        if not settings.is_rendering:
            self.report({"INFO"}, "No capture is running")
            return {"CANCELLED"}
        settings.cancel_requested = True
        self.report({"INFO"}, "Cancellation requested after the current frame")
        return {"FINISHED"}


class ORBITAL_RENDER_PT_panel(Panel):
    bl_label = "Orbital Render for 3DGS"
    bl_idname = "ORBITAL_RENDER_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "3DGS Render"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.orbital_render_settings

        target = layout.box()
        target.label(text="Target")
        target.prop(settings, "target_mode")
        if settings.target_mode == "COLLECTION":
            target.prop(settings, "target_collection")
        elif settings.target_mode == "MANUAL_REGION":
            target.prop(settings, "manual_center")
            target.prop(settings, "manual_size")
        target.prop(settings, "auto_look_at_z")
        if not settings.auto_look_at_z:
            target.prop(settings, "look_at_z")
        target.label(text="Z values use Blender coordinates, not MSL", icon="INFO")

        sampling = layout.box()
        sampling.label(text="Capture paths")
        sampling.prop(settings, "horizontal_radii")
        sampling.prop(settings, "world_z_heights")
        sampling.prop(settings, "azimuth_samples")
        sampling.prop(settings, "azimuth_start_degrees")
        sampling.prop(settings, "eval_every")
        row = sampling.row(align=True)
        row.operator("orbital_render.stats", icon="CHECKMARK")
        row.operator("orbital_render.preview", icon="CURVE_BEZCIRCLE")

        camera = layout.box()
        camera.label(text="Camera")
        camera.prop(settings, "lens_mm")
        camera.prop(settings, "sensor_width_mm")
        camera.prop(settings, "sensor_height_mm")
        camera.prop(settings, "sensor_fit")
        row = camera.row(align=True)
        row.prop(settings, "shift_x")
        row.prop(settings, "shift_y")
        row = camera.row(align=True)
        row.prop(settings, "clip_start")
        row.prop(settings, "clip_end")

        render = layout.box()
        render.label(text="Render")
        row = render.row(align=True)
        row.prop(settings, "resolution_x")
        row.prop(settings, "resolution_y")
        row = render.row(align=True)
        row.prop(settings, "pixel_aspect_x")
        row.prop(settings, "pixel_aspect_y")
        render.prop(settings, "render_samples")
        render.prop(settings, "transparent_background")
        render.prop(settings, "image_format")
        render.prop(settings, "max_initialization_points")

        output = layout.box()
        output.label(text="Output")
        output.prop(settings, "output_directory")
        output.prop(settings, "resume_capture")
        output.operator("orbital_render.save_plan", icon="FILE_TICK")

        if settings.render_status:
            status = layout.box()
            status.prop(settings, "render_progress", slider=True)
            status.label(text=settings.render_status)

        action = layout.column()
        action.scale_y = 1.6
        if settings.is_rendering:
            action.operator("orbital_render.cancel", icon="CANCEL")
        else:
            action.operator("orbital_render.generate", icon="RENDER_ANIMATION")


CLASSES = (
    OrbitalRenderSettings,
    ORBITAL_RENDER_OT_preview,
    ORBITAL_RENDER_OT_stats,
    ORBITAL_RENDER_OT_save_plan,
    ORBITAL_RENDER_OT_generate,
    ORBITAL_RENDER_OT_cancel,
    ORBITAL_RENDER_PT_panel,
)
