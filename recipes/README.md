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

## Other recipe operations

Recipes are an ordered array, so non-erosion OPCD operations can be mixed in.
The most useful for erosion prep is `subdividemesh`:

```json
{ "subdividemesh": { "subdivide_inset": 1 } }
```

**Behaviour (important):**
- Performs **one** subdivision per entry (`number_cuts=1`). For "2x", include the
  entry twice.
- `subdivide_inset` is **not** a count — it's how many loops in from the boundary
  to inset before cutting. `1` = subdivide the whole interior while preserving the
  boundary loop (matches erosion's boundary preservation).
- **It ignores the vertex selection** and subdivides the **entire mesh object's**
  interior. It also only acts on objects whose name contains both `Mesh` and
  `Spline`.
- Therefore only bake `subdividemesh` into a recipe when the target is a
  **separate mesh object** (e.g. cliffs split out from the hole). If the feature
  is part of a larger combined terrain mesh, this would over-densify everything —
  subdivide that selection manually instead and keep it out of the recipe.

## Recipes
- `sea_cliff.json` — rugged coastal cliff. **Confirmed working on-course.** **Now self-contained:** two
  `subdividemesh` steps (the 2x prep that worked on the Meloneras cliffs) run
  first, then Pass 1 main carve (channels/relief), then Pass 2 light skip-ratio +
  randomize to de-regularize and add outcrops. Assumes the cliffs are a **separate
  mesh object** (see subdivide note above). If your cliffs are part of a larger
  mesh, delete the two `subdividemesh` entries and subdivide the selection by hand.
- `inland_hill.json` — soft, rolling weathered hillside. Single gentle pass: high
  sediment + high fluidity for smooth rounded relief, low ruffle, light smoothing.
  Good for non-coastal slopes and gentle mounding.
- `rocky_headland.json` — sharp, exposed rock promontory. More aggressive than
  sea_cliff: low durability/sediment, higher erosion amount and ruffle for bare
  jagged faces. Pass 2 adds a heavier skip-ratio (0.20) randomize pass for strong
  resistant outcrops. Same subdivision prep as sea_cliff.

- `barranco_runoff.json` — rocky dry run-off valleys / ravines (barrancos) like
  those cutting through arid coastal courses. **High fluidity (14)** is the key:
  it connects flow into branching, incised dendritic channels rather than isolated
  pits. Low durability + high erosion amount cut deep; low sediment keeps the
  channels incised. Pass 2 (skip-ratio 0.15 + randomize) breaks the network up so
  it doesn't look uniform. **Confirmed working on-course.** **Self-contained:** two `subdividemesh` steps run first
  (same subdivide note as sea_cliff — assumes the ravine areas are **separate mesh
  objects**; delete those steps and subdivide by hand if they're part of a larger
  mesh).
- `arid_wasteland.json` — broad weathered desert scrubland between holes and
  surrounds. Granular roughness and shallow rills without cliff-scale drama:
  moderate erosion amount, **lower sediment (0.35) and smoothing OFF** so texture
  survives, ruffle 0.45 for surface grain, lower fluidity (8) to keep relief
  localized rather than channelised, plus a heavy skip-ratio (0.25) randomize pass
  for patchiness. **Confirmed working on-course** (once subdivided). **Now self-contained:** two `subdividemesh` steps run first — a
  coarse wasteland mesh has too few verts for erosion to bite, so without
  subdivision it stays smooth no matter the sliders (this density issue, not the
  settings, was the real cause of the early "too smooth" results). Assumes the
  wasteland is a **separate mesh object** (delete the subdivide steps if it's part
  of a larger mesh and subdivide the selection by hand). If still too tame after
  this, nudge `ruffle` to ~0.55 and `erosion_amount` up; if too rough, pull `ruffle`
  back toward 0.35.

All "Prep" notes assume **Use Erosion Selection** with the target verts selected.
