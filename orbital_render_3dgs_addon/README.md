# Orbital Render for 3DGS add-on

This Blender 5 add-on renders calibrated orbital views and writes a resumable standard COLMAP dataset.

After installation, open the **3DGS Render** tab in the 3D View sidebar. Choose selected objects, a collection, or a manual region. Horizontal radii and camera Z heights are explicit Blender world coordinates. Use **Validate and count** before starting a large capture.

The dataset contains `images/`, `sparse/0/cameras.txt`, `sparse/0/images.txt`, `sparse/0/points3D.txt`, the frozen `capture_plan.json`, atomic `capture_state.json`, `splits.json`, and a derived Inria `cameras.json` camera sidecar. Capture temporarily disables border rendering, crop-to-border, and compositing so exported full-frame intrinsics match every image; the original scene state is restored afterward.

See the repository `README.md` for the config schema, bounded headless commands, resume rules, Brush split settings, and validation commands.
