# STMapBBox

A Nuke tool that predicts the bounding box of a CG render after an STMap
is applied, and tightens the render's bbox to that region.

When you apply an STMap to a CG render, the distorted output usually
occupies less than the full frame. STMapBBox figures out exactly where
the active content lives so downstream nodes only process that region —
saving render time, processing time, and disk on shots where the
distortion shrinks the active area significantly (lens distortion, lens
warp removal, anamorphic squeeze/stretch, reformat sub-region maps).

![STMapBBox panel](docs/panel.png)

## Why use this

- A typical lens-distortion STMap on a 4K plate puts the active
  content into ~60–80% of the frame. The unused border still gets
  processed by everything downstream — every Merge, every render,
  every disk write.
- Setting the bbox manually is a guess, has to be re-done per shot,
  and breaks when the CG bbox is animated.
- STMapBBox computes it from the actual STMap so the bbox is correct
  for that specific lens / shot / sequence, and updates per frame
  for animated CG.

## Install

Drop `install_bbox.py` somewhere accessible and run it from Nuke's
Script Editor:

```python
exec(open('/path/to/install_bbox.py').read())
```

A new `STMapBBox` node appears at the cursor. Connect:

- **input 0 (`img`)** — your CG render (the bbox being modified).
- **input 1 (`stmap`)** — the STMap that will be applied downstream.

The `stmap` input is analysis-only; it does not get passed through.

To make this permanent, add the same `exec(...)` line to a button or
menu entry in your `init.py` / `menu.py`.

## Quick start

1. Connect both inputs.
2. Press **Compute Distorted BBox** under the Analysis section.
3. The internal Crop tightens to the active region. The bbox
   values appear under the *Result* twirly.

For animated CG bboxes, see *Animation* below.

## Knobs reference

### Analysis
- **grid resolution** — number of samples per axis. Higher = tighter
  bbox boundary, slower compute. 96 is fine for typical lens distortion.
- **bake grid** — separate, lower default (48) used only during Bake to
  keep it fast. Live update / Compute use the main grid_resolution.
- **extra margin** — pixels added on top of the auto grid-spacing margin.
  Bump up if you see clipping at the bbox edges.

### Compute / Test Inputs / Reset
- **Compute Distorted BBox** — runs the analysis and updates the bbox
  for the current frame.
- **Test Inputs** — prints both inputs' formats, bboxes, channel layouts,
  and 3×3 sample grids from `rgba`, `forward.*`, `backward.*`. Useful
  to figure out which channels hold the actual UV data.
- **Reset (no crop)** — clears the bbox back to a full-frame pass-through.

### Animation
- **first frame / last frame / step** — bake range.
- **Bake Animation** — runs Compute at every frame in the range and
  writes keyframes onto the bbox knobs. Drives Nuke's UI cycle to
  keep `cg.bbox()` fresh per frame, so values match live update exactly.
- **Clear Keyframes** — removes all baked keys.
- **Extract External Crop** — creates a standalone, disconnected `Crop`
  node in the parent graph with all animation curves copied from the
  bbox knobs onto its `box` channels. The new Crop is independent
  afterwards.
- **live update** — recomputes on every frame change. Useful while
  iterating or for the manual scrub-to-bake workflow.

### Result *(closed twirly)*
- **bbox report** — formatted summary of the latest compute.
- **Live values** — `live_x/y/r/t`, always reflect the most recent
  fresh compute. Never animated.
- **Baked values** — `cg_bbox_x/y/r/t`, drive the internal Crop.
  Animated by Bake. Scrub the timeline to compare with Live; if
  they differ at a frame, that key is wrong.

### Channels
- **U channel / V channel** — which channels hold the UV data.
  Defaults: `rgba.red` / `rgba.green`. Common alternatives: `forward.u`,
  `backward.u` (and `.v`).
- **UV space** — `auto` (default) detects from sample magnitudes,
  `normalized 0-1` and `absolute (CG pixels)` force a specific mode.
- **bbox space** —
  - `target (post-distort)` *(default)* — the region in the STMap's
    output where the distorted CG will have content. Place STMapBBox
    AFTER the STMap or on the post-distortion read.
  - `source (pre-distort)` — the region of the CG that the STMap
    will sample. Only useful when the STMap addresses a sub-region
    of source (tight crop maps, reformat sub-region). Most
    lens-distortion STMaps address the full plate, so this gives
    back the full source format — not useful in that case.

### Formats
- **mode** — `auto` reads source from `img`, target from `stmap`.
  `manual` uses the four W/H knobs below.
- **source W/H, target W/H** — explicit format dims. For STMaps
  that retarget (CG 4K → delivery 2K, anamorphic squeeze, etc.).
- **Pick Up Formats** — seeds the four numbers from the current
  inputs and switches to manual.

### Edge Trim *(closed twirly)*
- **left / right / bottom / top** — pixels to ignore at each edge of
  the STMap before sampling. Use to skip ringing or black bleed.

## Animation workflow

For a CG render with a *static* bbox: just press Bake. Done.

For a CG render with an *animated* bbox there are two paths:

### Path A: Bake (automatic)

Press **Bake Animation**. The tool drives Nuke's UI cycle frame-by-frame,
computing fresh values, and writes keys to the bbox knobs and the
internal Crop. The settle pass at the end captures the final frame
correctly. Then press **Extract External Crop** if you want a
standalone Crop in your tree.

This is sluggish (5–15 seconds per 100 frames depending on machine)
because each frame goes through the full UI cycle. That's the price
of correctness — see ROADMAP.md for the C++ port that fixes this
structurally.

### Path B: Live update + scrub

1. Toggle **live update** on.
2. Scrub or play through your frame range in the viewer.
3. Each frame change writes a keyframe via the live path.
4. Press **Extract External Crop** when done.
5. Toggle live update off.

This is the same code path as Bake under the hood; just controlled
manually by your timeline scrubbing instead of a QTimer loop. Useful
when you want to verify per-frame as you go.

### Verifying the bake

Open the *Result* twirly. Live values and Baked values sit side-by-side.
Scrub through your range — if Baked tracks Live frame-by-frame, the
bake captured correctly. If Live changes but Baked stays the same at
some frames, those keys are stale.

## Tested on

- Windows 11 23H2
- Nuke 14.1, 15.0, 15.1, 16.0, 17.0
- The Python is version-agnostic; should run unchanged on Linux /
  macOS Nuke installs (untested).

## Roadmap

The current Python implementation works but has performance limits
(see *Known limitations* in `ROADMAP.md`). Future v5.0 is a native
C++ NDK plugin that solves the staleness issue structurally and runs
50× faster. See `ROADMAP.md` for full architecture and build plan.

## Credit

Marten Blumen — [github.com/bratgot/STMapBBox](https://github.com/bratgot/STMapBBox)

## Licence

MIT. See `LICENSE`.
