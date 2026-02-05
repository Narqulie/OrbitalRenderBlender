# Orbital Render for 3D Gaussian Splatting

<img width="1579" height="1198" alt="3dgsOrbitalRender" src="https://github.com/user-attachments/assets/baf2d76d-f9f1-4911-895e-16e208f47171" />


Blender addon that generates multi-view orbital renders for 3D Gaussian Splatting reconstruction.

## What it does

Creates a series of rendered images from camera positions arranged in a sphere around your selected object. Saves camera transforms in a format compatible with 3DGS training.

## Installation

1. Download or clone this repository
2. In Blender: Edit > Preferences > Add-ons > Install
3. Select the `orbital_render_3dgs_addon` folder
4. Enable the addon in the list

## Usage

1. Select an object in your scene
2. Open the sidebar (N key) and find the "3DGS Render" tab
3. Configure settings:
   - Elevation rings: Number of vertical camera positions
   - Azimuth samples: Number of horizontal camera positions per ring
   - Distance levels: Number of different camera distances
4. Set output directory
5. Click "Generate Orbital Renders"

## Settings

**Orbital Sampling**: Controls how many camera positions are generated. More samples = better reconstruction but longer render time.

**Distance Multipliers**: How far the camera is from the object at each distance level (multiplied by object radius).

**Camera Settings**: Standard camera parameters (focal length, sensor width).

**Render Settings**: Resolution and sample count for each render.

**Output**: Directory for saved images and transforms.json file.

## Output

Creates numbered files and a `transforms.json` containing camera intrinsics and extrinsics for 3DGS training.
