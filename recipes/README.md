# OPCD Erosion Recipes

JSON recipes for the OPCD Erosion tool, run via the OPCD **Apply Recipe** button
(`wm.readoperations`).

## How to use
1. In Blender, select the cliff/terrain vertices you want to erode (the recipe
   honours **Use Erosion Selection**).
2. OPCD panel → **Apply Recipes** → **Apply Recipe** → pick a `.json` here.
3. Each `erosion` entry in the array runs as one full erosion pass, in order.

## Patch required
The stock OPCD recipe dispatcher does **not** handle erosion. An `elif "erosion"`
branch was added to `WM_OT_readoperations.execute()` in
`operators/afrod/afrod_operators.py`. It sets `scene.opcd_erosion_props.<key>`
for each key present, then calls `bpy.ops.opcd.erosion_apply()`.
Re-apply this patch after any OPCD update (tracked in Git).

## Schema
Root is a JSON **array**. Each element is `{ "erosion": { ...props... } }`.
Only the keys you include are set; everything else keeps its current panel value,
so passes can be short. Available keys (from `opcd_erosion_props`):

| Key | Type | Notes |
|---|---|---|
| `iterations` | int | erosion passes |
| `erosion_durability` | float | resistance; lower = carves more |
| `erosion_amount` | float | depth of carve per pass |
| `sediment_amount` | float | redeposit; higher = smoother infill |
| `erosion_fluidity_iterations` | int | flow spread; higher = coherent channels |
| `ruffle` | float | roughness/jitter. 0.85 spikes, 0.15 smooth, ~0.4 rock |
| `use_erosion_selection` | bool | limit to selected verts |
| `erosion_selection_skip_ratio` | float | fraction of selected verts left untouched |
| `erosion_selection_randomize` | bool | scatter the skipped verts (use when skip_ratio > 0) |
| `skip_loop_1`..`skip_loop_5` | bool | exempt a whole loop ring |
| `shrink_fatten_loop_1`..`_5` | bool | pinch/expand loop before erosion |
| `shrink_fatten_amount_loop_1`..`_5` | float | amount for the above |
| `use_smoothing` | bool | post-erosion smoothing |
| `smoothing_strength` | float | keep <= 0.10 for cliffs; 0.30 erases relief |
| `smoothing_iterations` | int | |

## Recipes
- `sea_cliff.json` — rugged coastal cliff. Pass 1: main carve (channels/relief).
  Pass 2: light skip-ratio + randomize pass to de-regularize and add outcrops.
  Prep: subdivide the cliff selection to ~0.5-1.0 m edges first, or erosion has
  no detail to bite into.
