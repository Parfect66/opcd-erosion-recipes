# OPCD Erosion Recipes

JSON-driven erosion recipes for the **OPCD Mesh Editing Tools** Blender addon, plus
the small addon patch that makes the OPCD **Apply Recipe** button able to run them.

The stock OPCD recipe dispatcher (`wm.readoperations`) only understands bunker,
water, vertex-paint and loop operations — it has no erosion branch. This repo adds
one, so you can save erosion settings as reusable JSON and apply them to any
selected mesh instead of dialling sliders by hand each time.

## Layout
```
recipes/    JSON erosion recipes + schema reference (README.md)
patches/    afrod_operators.stock.py, afrod_operators.patched.py, and a portable .diff
```

## Applying the patch
The patch adds an `elif "erosion"` branch to `WM_OT_readoperations.execute()` in
`operators/afrod/afrod_operators.py` of the OPCD addon. It reads
`scene.opcd_erosion_props`, sets only the keys present in the JSON, then calls
`bpy.ops.opcd.erosion_apply()`.

Addon path (Blender 4.5, Windows):
`%APPDATA%\Blender Foundation\Blender\4.5\extensions\user_default\opcd_blender_tools\`

**Option A — git apply (clean install):**
```
cd <addon root>
git apply /path/to/patches/erosion_recipe_support.diff
```

**Option B — drop-in:** copy `patches/afrod_operators.patched.py` over the addon's
`operators/afrod/afrod_operators.py` (only valid for the addon version this was
built against — see below).

Then in Blender: disable + re-enable the OPCD addon (or restart) to re-register.

## Re-merging after an OPCD update
OPCD updates overwrite the addon files. To restore erosion-recipe support:
1. `git apply patches/erosion_recipe_support.diff` against the freshly updated addon.
2. If the surrounding code moved and the patch won't apply cleanly, open
   `patches/afrod_operators.stock.py` vs `.patched.py` (or this repo's commit 2 diff)
   to see the exact change and re-insert the `elif "erosion"` block by hand.

## Built against
OPCD Mesh Editing Tools **v3.5.8-beta**, Blender **4.5**.

## Usage
1. Select the cliff/terrain vertices to erode.
2. OPCD panel → **Apply Recipes** → **Apply Recipe** → choose a recipe from `recipes/`.
3. Each `erosion` entry runs as one pass, in order.

See `recipes/README.md` for the full recipe schema and all available keys.
