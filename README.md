# Orbital Render for 3DGS

Orbital Render is a Blender 5 add-on for producing calibrated, resumable COLMAP datasets from a Blender scene. It places one temporary camera on horizontal paths around an object, collection, or manual region, renders each view, and exports the same poses used for rendering.

The standard COLMAP output is loadable by Brush. A derived `cameras.json` sidecar follows the Inria camera schema used by SuperSplat. COLMAP remains the authoritative dataset representation.

## What changed in version 2

Version 2 replaces the old custom `transforms.json` export with a standard COLMAP text model. It also fixes several sources of incorrect calibration and unsafe scene mutation:

- Camera intrinsics come from Blender's evaluated camera frame. Resolution, sensor fit, camera shift, and pixel aspect are included.
- Poses use COLMAP's world-to-camera OpenCV convention.
- The add-on creates and removes its own camera. It does not reuse or delete a user's named camera.
- Render settings, the active camera, and the scene frame are restored after success, cancellation, or error. Border rendering, crop-to-border, and compositing are disabled during capture so full-frame intrinsics remain valid, then restored.
- Images and progress use recoverable two-phase commits. Resume verifies the plan, render-visible source geometry and appearance settings, initialization point cloud, image sizes, image checksums, and generated metadata.
- Object and collection bounds use evaluated dependency-graph instances. Linked collection instances keep their world transforms.
- Horizontal radius and world Z are independent. Dataset Z is a Blender scene coordinate, not a verified sea-level altitude.
- Headless captures can stop successfully after a bounded number of new frames, then continue in a fresh Blender process.

## Installation

Build the installable zip:

```bash
pyenv exec python scripts/build_addon.py
```

In Blender, open **Edit > Preferences > Add-ons > Install from Disk**, select `orbital_render_3dgs_addon.zip`, and enable **Orbital Render for 3DGS**.

## Blender UI

1. Select one or more objects, or choose a collection or manual region in the **3DGS Render** sidebar.
2. Enter comma-separated horizontal radii and absolute world Z heights. Leave either field blank to derive three values from the target bounds.
3. Set the look-at height, camera calibration, render settings, and output directory.
4. Use **Validate and count** to inspect the frame count and target bounds.
5. Use **Toggle path preview** to add or remove a non-rendering wire preview.
6. Choose **Save capture plan** to freeze a plan without rendering, or **Generate COLMAP dataset** to begin capture.

The cancel button requests cancellation after the current still finishes. Blender's still render call is synchronous, so it cannot stop safely halfway through an image. Editing render-visible scene state during a capture aborts the session before the next frame.

## Headless capture

The tracked example at `examples/capture_config.json` documents every config field. Collection or manual-region targets are preferable in background mode.

Create a plan without sampling geometry or rendering:

```bash
BLENDER=/Applications/Blender.app/Contents/MacOS/Blender

"$BLENDER" --background scene.blend \
  --python scripts/orbital_capture.py -- \
  --config examples/capture_config.json \
  --output /absolute/path/to/dataset \
  --plan-only
```

Render at most 12 new frames in one process:

```bash
"$BLENDER" --background scene.blend \
  --python scripts/orbital_capture.py -- \
  --plan /absolute/path/to/dataset/capture_plan.json \
  --max-frames 12
```

Continue with a validated fresh process:

```bash
"$BLENDER" --background scene.blend \
  --python scripts/orbital_capture.py -- \
  --plan /absolute/path/to/dataset/capture_plan.json \
  --resume --max-frames 12
```

Each successful invocation prints one machine-readable line beginning with `ORBITAL_CAPTURE_RESULT`. A bounded slice exits successfully with `"complete": false`. The final slice reports `"complete": true`. The frozen full plan never changes between slices.

Do not pass `--resume` for the first rendered slice after `--plan-only`. Pass it once the state contains committed frames. A changed plan, source scene, render setting, or artifact stops the run instead of overwriting data.

## Output

```text
dataset/
  capture_plan.json
  capture_state.json
  cameras.json
  splits.json
  images/
    frame_000000.png
    ...
  sparse/0/
    cameras.txt
    images.txt
    points3D.txt
```

`capture_plan.json` is the frozen authority. It contains the target region, source signature, explicit frame names and camera positions, measured intrinsics, camera-to-world CV matrices, and COLMAP world-to-camera poses.

`capture_state.json` contains the committed frame prefix, per-image size and SHA-256, initialization point-cloud digest, and the explicit `complete` flag.

`points3D.txt` contains a deterministic capped reservoir sample of evaluated target geometry vertices and material fallback colors. Curves, surfaces, metaballs, and text are converted through Blender's evaluated mesh API for sampling. The file intentionally has no fabricated feature tracks. Brush consumes the point XYZ/RGB values for initialization and ignores COLMAP tracks.

`splits.json` records train and evaluation names. With `eval_every: 8`, pass `--eval-split-every 8` to Brush. Brush sorts the zero-padded image names before selecting every eighth frame, so its split matches the plan.

`cameras.json` is derived from the same unmodified poses. It uses the Inria fields `id`, `img_name`, `position`, `rotation`, `width`, `height`, `fx`, and `fy`. Viewer-specific axis conversion belongs in the viewer and is not baked into this file.

## Validation

Run the focused checks from the repository root:

```bash
pyenv exec python -m unittest discover -s tests -p 'test_*.py' -v

/Applications/Blender.app/Contents/MacOS/Blender \
  --background --factory-startup \
  --python tests/blender_projection_test.py

/Applications/Blender.app/Contents/MacOS/Blender \
  --background --factory-startup \
  --python tests/blender_end_to_end.py

pyenv exec python scripts/build_addon.py
SMOKE_PROFILE=$(mktemp -d)
BLENDER_USER_CONFIG="$SMOKE_PROFILE/config" \
BLENDER_USER_SCRIPTS="$SMOKE_PROFILE/scripts" \
/Applications/Blender.app/Contents/MacOS/Blender \
  --background --factory-startup \
  --python tests/addon_install_smoke.py -- orbital_render_3dgs_addon.zip
rm -rf "$SMOKE_PROFILE"
```

The projection test compares independently projected Blender points with the exported PINHOLE calibration and COLMAP pose. It also checks linked-instance bounds, linked-library texture fingerprints, UV and color-management changes, evaluated curve sampling, and render-state restoration. The end-to-end test renders PNG slices plus JPEG and OpenEXR frames, validates resume and metadata, confirms scene restoration, and rejects both mid-capture and resumed source changes.
