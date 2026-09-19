# Orbital Render v2 implementation report

Date: 2026-09-19

## Outcome

The Blender add-on now produces a frozen, resumable orbital capture dataset whose authoritative representation is standard COLMAP text. The same materialized camera records drive rendering, COLMAP export, split metadata, and the derived Inria `cameras.json` sidecar.

The prior monolithic add-on was replaced with a small module split:

- `orbital_render_3dgs_addon/plan.py`: canonical plan construction, hashing, and validation
- `orbital_render_3dgs_addon/blender_capture.py`: evaluated Blender scene adapters, camera math, initialization sampling, and shared capture session
- `orbital_render_3dgs_addon/dataset.py`: output ownership, two-phase commits, recovery, checksums, and serializers
- `orbital_render_3dgs_addon/ui.py`: sidebar properties, validation, path preview, modal serial capture, and cancellation
- `orbital_render_3dgs_addon/cli.py`: headless plan, bounded slice, and resume entry point
- `orbital_render_3dgs_addon/__init__.py`: registration only

## Capture contract

### Plan and sampling

`capture_plan.json` is canonical JSON with a SHA-256 over all content except its digest field. It records:

- source blend identity and render-visible source signature
- resolved target bounds and look-at height
- independent horizontal radii and absolute Blender world-Z heights
- zero-padded deterministic frame names
- train/eval intent from `frame_index % eval_every`
- exact render settings and camera data
- measured `PINHOLE` intrinsics
- camera-to-world OpenCV matrices
- COLMAP world-to-camera quaternions and translations

The camera positions are `(center_x + radius*cos(a), center_y + radius*sin(a), world_z)`. Radius is horizontal. Height is a Blender scene coordinate, not an asserted sea-level altitude.

### Camera calibration

Intrinsics are measured from `Camera.view_frame(scene=scene)` after the temporary owned camera and render settings are applied. This incorporates sensor fit, camera shifts, pixel aspect, and effective image dimensions without duplicating Blender's fit rules.

The pose conversion is:

```text
C2W_cv = camera.matrix_world @ diag(1, -1, -1, 1)
W2C_cv = inverse(C2W_cv)
```

COLMAP receives the normalized `W2C_cv` quaternion in `qw qx qy qz` order and its translation. The Inria sidecar receives the unmodified `C2W_cv` rotation and position. Viewer-specific axis conversion is not baked into either authoritative pose.

### Scene evaluation and initialization

Bounds and point sampling use evaluated dependency-graph instances. Linked collection instances retain each instance's world transform. Meshes are sampled directly; curves, surfaces, metaballs, and text use Blender's evaluated temporary mesh conversion.

`points3D.txt` contains a deterministic capped reservoir sample of target vertices and RGB fallback colors. It intentionally contains no fabricated feature tracks.

The source signature includes evaluated geometry, transforms, topology, UV coordinates, polygon smoothing, material and node inputs, render-visible lights, world settings, Blender color-management settings, external source files, and linked-library-relative image paths. It is checked at preparation and before every frame.

### Ownership and scene safety

Every run creates a uniquely named camera and private collection tagged with an add-on ownership property. No user camera is reused or removed.

The capture snapshots and restores the active camera, frame/subframe, engine, output path, resolution, pixel aspect, transparency, image format, samples, border rendering, crop-to-border, and compositor state. Border, crop, and compositing are disabled during capture so every image matches the exported full-frame calibration.

The modal cancel action sets a request consumed by the active operator. Cancellation occurs after the current synchronous Blender still render finishes. Success, cancellation, and errors use one idempotent cleanup path.

## Output and resume protocol

The output tree is:

```text
capture_plan.json
capture_state.json
cameras.json
splits.json
images/frame_NNNNNN.ext
sparse/0/cameras.txt
sparse/0/images.txt
sparse/0/points3D.txt
```

Initialization and frame publication use recoverable two-phase state:

1. write and hash a plan-scoped staging artifact
2. atomically publish a pending state record
3. atomically move the artifact to its final path
4. regenerate deterministic metadata
5. atomically publish the completed state record

On resume, a matching pending record completes the interrupted transaction without rerendering. Every committed image and the point cloud are verified by byte size and SHA-256. The completed frame list must be a contiguous plan prefix. Uncommitted final images, mismatched metadata, changed source, changed plan, and foreign nonempty output directories are rejected rather than overwritten. Plan-scoped staging names are the only artifacts eligible for cleanup or recovery.

`--max-frames N` limits new frames in one Blender invocation. A successful partial invocation reports `complete: false`; the frozen full plan is unchanged. This supports guarded fresh-process slices without an internal process launcher.

## Consumer alignment

### Brush

The export uses one COLMAP `PINHOLE` camera and lexicographically sortable zero-padded names. With `eval_every: N`, Brush must receive `--eval-split-every N`; both then select indices where `index % N == 0` as evaluation views.

A guarded Brush interoperability smoke used the generated eight-frame synthetic dataset:

```text
/usr/local/bin/brush /private/tmp/orbital-render-synthetic-20260919/dataset \
  --total-train-iters 100 \
  --eval-split-every 4 \
  --eval-every 100 \
  --eval-save-to-disk \
  --export-every 100 \
  --export-path /private/tmp/orbital-brush-smoke-20260919-v1/exports \
  --max-resolution 96 \
  --max-splats 2000 \
  --refine-every 25 \
  --max-scene-batch-cache-size 128MiB \
  --alpha-mode transparent
```

Receipt: return code 0, 9.898 seconds, 133,251,072-byte peak RSS, 176,227,408-byte peak physical use, and no guard breach. `export_100.ply` contained 18 splats and 59 finite float properties. `eval_100` contained exactly `frame_000000` and `frame_000004`, matching the intended held-out indices. This is interoperability evidence, not quality acceptance; eight initialization points and 100 iterations produced an intentionally undertrained blurry result.

### SuperSplat

`cameras.json` follows the Inria fields consumed by SuperSplat: `id`, `img_name`, `position`, `rotation`, `width`, `height`, `fx`, and `fy`. It is derived from the same frozen CV camera-to-world records used to generate COLMAP poses.

A native SuperSplat 3.3.0 visual smoke imported the Brush `export_100.ply` plus the generated `cameras.json`. `/tmp/orbital-supersplat-smoke-20260919/viewer-verification.json` recorded 18 splats, eight enabled cyan camera glyphs, 160 glyph vertices, WebGPU, and no page errors; `supersplat-demo.png` preserves the visual result. Native camera import requires a real file whose contents are available to SuperSplat's importer. A URL-only `?load=cameras.json` does not work because the importer reads `file.contents`; this is a viewer workflow constraint, not an export-schema failure.

## Validation evidence

Validated with Blender 5.2.2 LTS and the repository Python environment:

```text
pyenv exec python -m unittest discover -s tests -p 'test_*.py' -v
# 8 tests passed

Blender --background --factory-startup --python tests/blender_projection_test.py
# BLENDER_PROJECTION_TEST_OK

Blender --background --factory-startup --python tests/blender_end_to_end.py
# BLENDER_END_TO_END_OK

Blender --background scene.blend --python scripts/orbital_capture.py -- ...
# plan-only + one-frame first slice + one-frame resumed slice
# complete progressed false -> false -> true; HEADLESS_CLI_SLICE_OK

pyenv exec ruff check orbital_render_3dgs_addon tests scripts
# All checks passed

pyenv exec ruff format --check orbital_render_3dgs_addon tests scripts
# All files formatted
```

The Blender checks independently compare projected points against emitted calibration, exercise sensor fit/shift/pixel aspect, linked-instance bounds, linked-library texture fingerprinting, UV/smoothing/color-management changes, curve initialization, state restoration, bounded resume, mid-capture mutation rejection, source-change rejection, and real PNG/JPEG/OpenEXR writes.

The installable package is built deterministically by `scripts/build_addon.py` as `orbital_render_3dgs_addon.zip` (SHA-256 `173d30796d65f1ac32e6022693aade167ddf62cdd8a644e52f1d1bd6e327caa5`). Package installation and registration are covered by `tests/addon_install_smoke.py`, which now requires isolated `BLENDER_USER_CONFIG` and `BLENDER_USER_SCRIPTS` directories.

An earlier smoke was run without that isolation and installed the package with `overwrite=True` at `/Users/narqulie/Library/Application Support/Blender/5.2/scripts/addons/orbital_render_3dgs_addon`. The add-on was disabled afterward, but the installed files were not deleted or reverted because their prior state was unknown. Final smoke evidence uses an isolated temporary profile.

## Independent review disposition

Two fresh read-only reviewers inspected the implementation. Their concrete findings were dispositioned as follows:

- Missing color-management/compositor coherence: fixed by fingerprinting view settings, disabling/restoring compositing, and checking capture-owned settings before every frame.
- Non-mesh targets could resolve but not initialize: fixed with evaluated temporary-mesh sampling and cleanup.
- Scene or render-setting edits between modal frames could mix datasets: fixed with per-frame source and capture-setting validation.
- UV and polygon-smoothing changes were absent from the source fingerprint: fixed and covered by Blender regressions.
- PNG-only format evidence: expanded with actual JPEG and OpenEXR render checks.
- Unrecorded staging cleanup concern: no code change to the ownership rule was needed because the mission explicitly permits cleanup of add-on-owned temporaries. Staging names now also include the matching plan digest and are considered owned only inside a directory whose plan/state already validate.
- Consumer compatibility claim: bounded native Brush and SuperSplat smokes were completed and recorded above.

A final focused reviewer inspected the capture-setting guard and mutation regression after the fixes and returned **Merge verdict: OK**, with no remaining P0/P1 findings. The main session then reran the pure Python suite, Blender projection test, Blender end-to-end test, lint/format gates, deterministic build, and isolated-profile install smoke.

## Deliberate limits

- Rendering is serial. Blender's still-render operator blocks UI cancellation until the current image finishes.
- Resume is strict. User edits to generated metadata or committed images are treated as corruption, not merged.
- The source signature is intentionally capture-specific, not a generic hash of every Blender RNA property.
- The add-on does not run Brush, train a model, launch viewers, or manage child processes.
- The real Tampere capture and quality acceptance remain separate guarded operations owned by the parent workflow.
