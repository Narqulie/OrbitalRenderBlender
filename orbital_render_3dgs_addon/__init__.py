"""
Orbital Render for 3D Gaussian Splatting - Blender Addon
Creates multi-view renders around selected objects with dense sampling
for optimal 3DGS reconstruction.
"""

bl_info = {
    "name": "Orbital Render for 3DGS",
    "author": "Jheaminoff",
    "version": (1, 3, 0),
    "blender": (5, 0, 0),
    "location": "View3D > Sidebar > 3DGS Render",
    "description": "Generate orbital multi-view renders for 3D Gaussian Splatting reconstruction",
    "category": "Render",
    "doc_url": "",
    "tracker_url": "",
}

import bpy
import math
import json
from pathlib import Path
from mathutils import Vector, Matrix
from bpy.types import Operator, Panel, PropertyGroup
from bpy.props import (
    IntProperty,
    FloatProperty,
    StringProperty,
    BoolProperty,
    EnumProperty,
    PointerProperty,
    FloatVectorProperty,
)


# ==================== HELPER FUNCTIONS ====================

def get_object_bounds(obj):
    """Calculate bounding box center and radius for the selected object."""
    bbox_corners = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
    center = sum(bbox_corners, Vector()) / 8
    radius = max((corner - center).length for corner in bbox_corners)
    return center, radius


def spherical_to_cartesian(radius, elevation, azimuth):
    """
    Convert spherical coordinates to Cartesian.
    
    Args:
        radius: Distance from origin
        elevation: Elevation angle in radians
        azimuth: Azimuth angle in radians
    
    Returns:
        Vector: Cartesian coordinates (x, y, z)
    """
    x = radius * math.cos(elevation) * math.cos(azimuth)
    y = radius * math.cos(elevation) * math.sin(azimuth)
    z = radius * math.sin(elevation)
    return Vector((x, y, z))


def create_or_get_camera(name="RenderCamera_3DGS"):
    """Create a new camera or get existing one."""
    camera = bpy.data.objects.get(name)
    
    if camera is None:
        cam_data = bpy.data.cameras.new(name=name)
        camera = bpy.data.objects.new(name, cam_data)
        bpy.context.collection.objects.link(camera)
    
    return camera


def orient_camera_to_target(camera, target_location):
    """Orient camera to look at target location."""
    direction = target_location - camera.location
    rot_quat = direction.to_track_quat('-Z', 'Y')
    camera.rotation_euler = rot_quat.to_euler()


def setup_render_settings(settings):
    """Configure render settings."""
    scene = bpy.context.scene
    
    scene.render.resolution_x = settings.render_resolution_x
    scene.render.resolution_y = settings.render_resolution_y
    scene.render.image_settings.file_format = settings.image_format
    
    if settings.use_transparent_bg:
        scene.render.film_transparent = True
    
    if scene.render.engine == 'CYCLES':
        scene.cycles.samples = settings.render_samples


def save_camera_transform(camera, target_center, output_path, frame_name):
    """Save camera transform data for 3DGS processing."""
    scene = bpy.context.scene
    cam_data = camera.data
    
    # Calculate intrinsic matrix
    resolution_x = scene.render.resolution_x
    resolution_y = scene.render.resolution_y
    
    sensor_width = cam_data.sensor_width
    focal_length = cam_data.lens
    fx = (focal_length / sensor_width) * resolution_x
    fy = fx
    cx = resolution_x / 2
    cy = resolution_y / 2
    
    # Get extrinsic matrix
    matrix_world = camera.matrix_world.copy()
    
    # Convert Blender's camera coordinate system to CV convention
    rotation_fix = Matrix((
        (1,  0,  0, 0),
        (0, -1,  0, 0),
        (0,  0, -1, 0),
        (0,  0,  0, 1)
    ))
    
    transform_matrix = matrix_world @ rotation_fix
    
    return {
        "frame_name": frame_name,
        "camera_position": list(camera.location),
        "target_position": list(target_center),
        "transform_matrix": [list(row) for row in transform_matrix],
        "intrinsics": {
            "fx": fx,
            "fy": fy,
            "cx": cx,
            "cy": cy,
            "width": resolution_x,
            "height": resolution_y,
        }
    }


# ==================== PROPERTY GROUP ====================

class OrbitalRender3DGSSettings(PropertyGroup):
    """Settings for the Orbital Render 3DGS addon."""
    
    # Orbital sampling parameters
    num_elevation_rings: IntProperty(
        name="Elevation Rings",
        description="Number of elevation angles (latitude lines)",
        default=7,
        min=1,
        max=50,
    )
    
    num_azimuth_samples: IntProperty(
        name="Azimuth Samples",
        description="Number of rotation samples per ring",
        default=36,
        min=4,
        max=360,
    )
    
    num_distance_levels: IntProperty(
        name="Distance Levels",
        description="Number of distance tiers",
        default=3,
        min=1,
        max=10,
    )
    
    # Elevation constraints
    limit_elevation: BoolProperty(
        name="Above Horizon Only",
        description="Keep all camera positions above the horizontal plane (positive Z only)",
        default=False,
    )
    
    min_elevation_angle: FloatProperty(
        name="Min Elevation",
        description="Minimum elevation angle in degrees (0° = horizon, 90° = zenith)",
        default=0.0,
        min=-89.0,
        max=89.0,
    )
    
    max_elevation_angle: FloatProperty(
        name="Max Elevation",
        description="Maximum elevation angle in degrees (0° = horizon, 90° = zenith)",
        default=80.0,
        min=-89.0,
        max=89.0,
    )
    
    # Distance multipliers
    distance_mult_close: FloatProperty(
        name="Close",
        description="Close distance multiplier",
        default=2.5,
        min=1.0,
        max=50.0,
    )
    
    distance_mult_medium: FloatProperty(
        name="Medium",
        description="Medium distance multiplier",
        default=3.5,
        min=1.0,
        max=50.0,
    )
    
    distance_mult_far: FloatProperty(
        name="Far",
        description="Far distance multiplier",
        default=5.0,
        min=1.0,
        max=50.0,
    )
    
    # Camera settings
    camera_focal_length: FloatProperty(
        name="Focal Length",
        description="Camera focal length in mm",
        default=50.0,
        min=1.0,
        max=500.0,
        unit='CAMERA',
    )
    
    camera_sensor_width: FloatProperty(
        name="Sensor Width",
        description="Camera sensor width in mm",
        default=36.0,
        min=1.0,
        max=100.0,
        unit='CAMERA',
    )
    
    # Render settings
    render_resolution_x: IntProperty(
        name="Resolution X",
        description="Render resolution width",
        default=1920,
        min=128,
        max=8192,
    )
    
    render_resolution_y: IntProperty(
        name="Resolution Y",
        description="Render resolution height",
        default=1080,
        min=128,
        max=8192,
    )
    
    render_samples: IntProperty(
        name="Render Samples",
        description="Number of render samples for quality",
        default=128,
        min=1,
        max=4096,
    )
    
    use_transparent_bg: BoolProperty(
        name="Transparent Background",
        description="Use transparent background for renders",
        default=True,
    )
    
    # Output settings
    output_directory: StringProperty(
        name="Output Directory",
        description="Directory to save rendered images",
        default="/tmp/3dgs_renders",
        subtype='DIR_PATH',
    )
    
    image_format: EnumProperty(
        name="Image Format",
        description="Output image format",
        items=[
            ('PNG', "PNG", "PNG format"),
            ('JPEG', "JPEG", "JPEG format"),
            ('OPEN_EXR', "OpenEXR", "OpenEXR format"),
        ],
        default='PNG',
    )
    
    save_camera_transforms: BoolProperty(
        name="Save Camera Transforms",
        description="Save transforms.json for 3DGS training",
        default=True,
    )
    
    cleanup_camera: BoolProperty(
        name="Cleanup Camera",
        description="Delete camera after rendering",
        default=False,
    )
    
    # Progress tracking (internal use)
    render_progress: FloatProperty(
        name="Render Progress",
        description="Current rendering progress (0-100)",
        default=0.0,
        min=0.0,
        max=100.0,
        subtype='PERCENTAGE',
    )
    
    render_status: StringProperty(
        name="Render Status",
        description="Current rendering status message",
        default="",
    )
    
    is_rendering: BoolProperty(
        name="Is Rendering",
        description="Whether a render is currently in progress",
        default=False,
    )


# ==================== OPERATORS ====================

class ORBITAL3DGS_OT_generate_renders(Operator):
    """Generate orbital renders around the selected object"""
    bl_idname = "orbital3dgs.generate_renders"
    bl_label = "Generate Orbital Renders"
    bl_options = {'REGISTER'}
    
    _timer = None
    _camera = None
    _render_queue = []
    _current_idx = 0
    _transforms_data = {}
    _output_dir = None
    _target_center = None
    
    def modal(self, context, event):
        settings = context.scene.orbital_3dgs_settings
        
        # Check for ESC key to cancel
        if event.type == 'ESC':
            return self.cancel(context)
        
        if event.type == 'TIMER':
            # Check if we're done
            if self._current_idx >= len(self._render_queue):
                return self.finish(context)
            
            # Get current render task
            task = self._render_queue[self._current_idx]
            
            # Update progress
            progress_pct = (self._current_idx / len(self._render_queue)) * 100
            settings.render_status = (
                f"Rendering {self._current_idx+1}/{len(self._render_queue)} ({progress_pct:.1f}%) - "
                f"D:{task['dist_mult']:.1f}x E:{task['elevation_deg']:+.0f}° A:{task['azimuth_deg']:.0f}°"
            )
            settings.render_progress = progress_pct
            
            # Position camera
            self._camera.location = task['camera_pos']
            orient_camera_to_target(self._camera, self._target_center)
            
            # Render
            context.scene.render.filepath = str(task['output_path'])
            bpy.ops.render.render(write_still=True)
            
            # Save transform data
            if settings.save_camera_transforms:
                transform = save_camera_transform(
                    self._camera, 
                    self._target_center, 
                    task['output_path'], 
                    task['frame_name']
                )
                self._transforms_data["frames"].append(transform)
            
            # Console output
            print(f"[{self._current_idx+1}/{len(self._render_queue)}] ({progress_pct:.1f}%) - "
                  f"Dist: {task['dist_mult']:.1f}x, Elev: {task['elevation_deg']:+.1f}°, "
                  f"Azim: {task['azimuth_deg']:.1f}° -> {task['frame_name']}")
            
            # Move to next
            self._current_idx += 1
            
        return {'PASS_THROUGH'}
    
    def cancel(self, context):
        """Cancel the render and cleanup."""
        settings = context.scene.orbital_3dgs_settings
        wm = context.window_manager
        
        # Save partial camera transforms if any were completed
        if settings.save_camera_transforms and self._transforms_data.get("frames"):
            transforms_path = self._output_dir / "transforms_partial.json"
            with open(transforms_path, 'w') as f:
                json.dump(self._transforms_data, f, indent=2)
            print(f"Partial camera transforms saved to: {transforms_path}")
        
        # Cleanup camera if requested
        if settings.cleanup_camera and self._camera:
            bpy.data.objects.remove(self._camera, do_unlink=True)
        
        # End progress tracking
        wm.progress_end()
        settings.is_rendering = False
        settings.render_progress = 0.0
        settings.render_status = f"Cancelled after {self._current_idx}/{len(self._render_queue)} renders"
        
        # Remove timer
        if self._timer:
            wm.event_timer_remove(self._timer)
        
        self.report({'WARNING'}, f"Render cancelled! {self._current_idx} of {len(self._render_queue)} images saved to: {self._output_dir}")
        
        return {'CANCELLED'}
    
    def finish(self, context):
        settings = context.scene.orbital_3dgs_settings
        wm = context.window_manager
        
        # Save camera transforms to JSON
        if settings.save_camera_transforms:
            transforms_path = self._output_dir / "transforms.json"
            with open(transforms_path, 'w') as f:
                json.dump(self._transforms_data, f, indent=2)
            print(f"Camera transforms saved to: {transforms_path}")
        
        # Cleanup camera if requested
        if settings.cleanup_camera and self._camera:
            bpy.data.objects.remove(self._camera, do_unlink=True)
        
        # End progress tracking
        wm.progress_end()
        settings.is_rendering = False
        settings.render_progress = 100.0
        settings.render_status = f"Complete! {len(self._render_queue)} renders saved"
        
        # Remove timer
        wm.event_timer_remove(self._timer)
        
        self.report({'INFO'}, f"Render complete! {len(self._render_queue)} images saved to: {self._output_dir}")
        
        return {'FINISHED'}
    
    def execute(self, context):
        settings = context.scene.orbital_3dgs_settings
        wm = context.window_manager
        
        # Validate selection
        if not context.selected_objects:
            self.report({'ERROR'}, "No object selected! Please select an object to render.")
            return {'CANCELLED'}
        
        target_object = context.active_object
        if target_object is None:
            target_object = context.selected_objects[0]
        
        self.report({'INFO'}, f"Starting orbital renders for: {target_object.name}")
        
        # Get object bounds
        center, radius = get_object_bounds(target_object)
        self._target_center = center
        
        # Setup output directory
        output_dir = Path(settings.output_directory)
        output_dir.mkdir(parents=True, exist_ok=True)
        self._output_dir = output_dir
        
        # Create or get camera
        camera = create_or_get_camera("RenderCamera_3DGS")
        camera.data.lens = settings.camera_focal_length
        camera.data.sensor_width = settings.camera_sensor_width
        context.scene.camera = camera
        self._camera = camera
        
        # Setup render settings
        setup_render_settings(settings)
        
        # Get distance multipliers
        distance_multipliers = [
            settings.distance_mult_close,
            settings.distance_mult_medium,
            settings.distance_mult_far,
        ][:settings.num_distance_levels]
        
        # Build render queue
        self._render_queue = []
        self._transforms_data = {
            "camera_model": "SIMPLE_PINHOLE",
            "frames": []
        }
        
        for dist_idx, dist_mult in enumerate(distance_multipliers):
            distance = radius * dist_mult
            
            for elev_idx in range(settings.num_elevation_rings):
                # Distribute elevations based on user constraints
                if settings.limit_elevation:
                    # Use custom min/max elevation angles
                    min_elev = settings.min_elevation_angle
                    max_elev = settings.max_elevation_angle
                else:
                    # Default: -80° to +80°
                    min_elev = -80.0
                    max_elev = 80.0
                
                elevation_range = max_elev - min_elev
                elevation_ratio = elev_idx / max(1, settings.num_elevation_rings - 1)
                elevation_deg = min_elev + (elevation_ratio * elevation_range)
                elevation_rad = math.radians(elevation_deg)
                
                for azim_idx in range(settings.num_azimuth_samples):
                    # Distribute azimuths evenly around 360°
                    azimuth_deg = (azim_idx / settings.num_azimuth_samples) * 360
                    azimuth_rad = math.radians(azimuth_deg)
                    
                    # Calculate camera position
                    camera_offset = spherical_to_cartesian(distance, elevation_rad, azimuth_rad)
                    camera_pos = center + camera_offset
                    
                    # Generate filename
                    frame_name = f"frame_d{dist_idx:02d}_e{elev_idx:02d}_a{azim_idx:03d}.png"
                    output_path = output_dir / frame_name
                    
                    # Add to queue
                    self._render_queue.append({
                        'camera_pos': camera_pos,
                        'dist_mult': dist_mult,
                        'elevation_deg': elevation_deg,
                        'azimuth_deg': azimuth_deg,
                        'frame_name': frame_name,
                        'output_path': output_path,
                    })
        
        total_renders = len(self._render_queue)
        self.report({'INFO'}, f"Total renders to generate: {total_renders}")
        
        # Initialize progress tracking
        wm.progress_begin(0, total_renders)
        settings.is_rendering = True
        settings.render_progress = 0.0
        settings.render_status = "Starting render..."
        self._current_idx = 0
        
        # Add timer and start modal
        self._timer = wm.event_timer_add(0.01, window=context.window)
        wm.modal_handler_add(self)
        
        return {'RUNNING_MODAL'}


class ORBITAL3DGS_OT_calculate_stats(Operator):
    """Calculate and display render statistics"""
    bl_idname = "orbital3dgs.calculate_stats"
    bl_label = "Calculate Statistics"
    bl_options = {'REGISTER'}
    
    def execute(self, context):
        settings = context.scene.orbital_3dgs_settings
        
        # Validate selection
        if not context.selected_objects:
            self.report({'WARNING'}, "No object selected")
            return {'CANCELLED'}
        
        target_object = context.active_object or context.selected_objects[0]
        center, radius = get_object_bounds(target_object)
        
        total_renders = (settings.num_elevation_rings * 
                        settings.num_azimuth_samples * 
                        settings.num_distance_levels)
        
        distance_multipliers = [
            settings.distance_mult_close,
            settings.distance_mult_medium,
            settings.distance_mult_far,
        ][:settings.num_distance_levels]
        
        distances = [radius * mult for mult in distance_multipliers]
        
        info = (
            f"\n{'='*60}\n"
            f"Target: {target_object.name}\n"
            f"Center: ({center.x:.2f}, {center.y:.2f}, {center.z:.2f})\n"
            f"Radius: {radius:.2f} units\n"
            f"\nRender Configuration:\n"
            f"   • Elevation rings: {settings.num_elevation_rings}\n"
            f"   • Azimuth samples: {settings.num_azimuth_samples}\n"
            f"   • Distance levels: {settings.num_distance_levels}\n"
        )
        
        for i, (mult, dist) in enumerate(zip(distance_multipliers, distances)):
            info += f"   • Distance {i+1}: {dist:.2f} units ({mult:.1f}x)\n"
        
        info += (
            f"\nTotal renders: {total_renders}\n"
            f"Resolution: {settings.render_resolution_x}x{settings.render_resolution_y}\n"
            f"Output: {settings.output_directory}\n"
            f"{'='*60}\n"
        )
        
        print(info)
        self.report({'INFO'}, f"Statistics calculated - check console for details")
        
        return {'FINISHED'}


class ORBITAL3DGS_OT_cancel_render(Operator):
    """Cancel the current render operation"""
    bl_idname = "orbital3dgs.cancel_render"
    bl_label = "Cancel Render"
    bl_options = {'REGISTER'}
    
    def execute(self, context):
        settings = context.scene.orbital_3dgs_settings
        
        if not settings.is_rendering:
            self.report({'INFO'}, "No render in progress")
            return {'CANCELLED'}
        
        # The modal operator will handle the actual cancellation via ESC
        # This just provides a button interface
        self.report({'INFO'}, "Cancelling render...")
        
        return {'FINISHED'}


# ==================== UI PANEL ====================

class ORBITAL3DGS_PT_main_panel(Panel):
    """Main panel for Orbital Render 3DGS addon"""
    bl_label = "Orbital Render for 3DGS"
    bl_idname = "ORBITAL3DGS_PT_main_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = '3DGS Render'
    
    def draw(self, context):
        layout = self.layout
        settings = context.scene.orbital_3dgs_settings
        
        # Header info
        box = layout.box()
        box.label(text="Selected Object:", icon='OBJECT_DATA')
        if context.active_object:
            box.label(text=f"   {context.active_object.name}")
        else:
            box.label(text="   (No object selected)", icon='ERROR')
        
        layout.separator()
        
        # Sampling settings
        box = layout.box()
        box.label(text="Orbital Sampling", icon='SPHERE')
        box.prop(settings, "num_elevation_rings")
        box.prop(settings, "num_azimuth_samples")
        box.prop(settings, "num_distance_levels")
        
        # Elevation constraints
        box.separator()
        box.prop(settings, "limit_elevation")
        if settings.limit_elevation:
            row = box.row(align=True)
            row.prop(settings, "min_elevation_angle")
            row.prop(settings, "max_elevation_angle")
        
        # Distance multipliers
        box = layout.box()
        box.label(text="Distance Multipliers", icon='EMPTY_SINGLE_ARROW')
        box.prop(settings, "distance_mult_close")
        if settings.num_distance_levels >= 2:
            box.prop(settings, "distance_mult_medium")
        if settings.num_distance_levels >= 3:
            box.prop(settings, "distance_mult_far")
        
        layout.separator()
        
        # Camera settings
        box = layout.box()
        box.label(text="Camera Settings", icon='CAMERA_DATA')
        box.prop(settings, "camera_focal_length")
        box.prop(settings, "camera_sensor_width")
        
        layout.separator()
        
        # Render settings
        box = layout.box()
        box.label(text="Render Settings", icon='RENDER_STILL')
        row = box.row(align=True)
        row.prop(settings, "render_resolution_x")
        row.prop(settings, "render_resolution_y")
        box.prop(settings, "render_samples")
        box.prop(settings, "use_transparent_bg")
        
        layout.separator()
        
        # Output settings
        box = layout.box()
        box.label(text="Output Settings", icon='FILE_FOLDER')
        box.prop(settings, "output_directory")
        box.prop(settings, "image_format")
        box.prop(settings, "save_camera_transforms")
        box.prop(settings, "cleanup_camera")
        
        layout.separator()
        
        # Statistics
        layout.operator("orbital3dgs.calculate_stats", icon='INFO')
        
        layout.separator()
        
        # Progress indicator
        if settings.render_status:
            box = layout.box()
            box.label(text="Progress", icon='TIME')
            box.prop(settings, "render_progress", slider=True)
            
            # Wrap status text nicely
            status_lines = settings.render_status.split(' - ')
            for line in status_lines:
                box.label(text=line)
        
        layout.separator()
        
        # Main action button
        col = layout.column()
        col.scale_y = 2.0
        
        if settings.is_rendering:
            # Show cancel button when rendering
            col.operator("orbital3dgs.cancel_render", icon='X', text="Cancel Render (or press ESC)")
        else:
            # Show start button when not rendering
            col.operator("orbital3dgs.generate_renders", icon='RENDER_ANIMATION')


# ==================== REGISTRATION ====================

classes = (
    OrbitalRender3DGSSettings,
    ORBITAL3DGS_OT_generate_renders,
    ORBITAL3DGS_OT_cancel_render,
    ORBITAL3DGS_OT_calculate_stats,
    ORBITAL3DGS_PT_main_panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    
    bpy.types.Scene.orbital_3dgs_settings = PointerProperty(type=OrbitalRender3DGSSettings)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    
    del bpy.types.Scene.orbital_3dgs_settings


if __name__ == "__main__":
    register()
