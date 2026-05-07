# STMapBBox Roadmap

A Nuke tool that predicts the bounding box of a CG render after an STMap
is applied, and tightens the render's bbox to that region. Saves render
and processing time on lens-distortion / reformatting workflows where the
distorted output occupies less than the full frame.

---

## Current State — v4.0 (Python)

`install_bbox.py` produces a `Group` node, `STMapBBox`, with two inputs and
a self-contained UI panel. Architecture:

- Two inputs: `img` (the CG render whose bbox is being modified) and `stmap`
  (the displacement map, analysis-only).
- Internal graph: `img → Crop("cg_crop") → Output`. The `stmap` input is
  not passed through.
- The `Compute` button samples a configurable grid across the STMap, classifies
  each output pixel against the CG's current bbox, and writes a bounding box
  to `cg_bbox_x/y/r/t` knobs that drive `cg_crop.box` via a `knobChanged`
  callback (`SYNC_CB`).
- Two bbox modes (`bbox_space` knob):
    - **target (post-distort, default)** — tracks output `(x,y)` whose UV
      lands inside `img`'s bbox; bbox of those points is the post-distortion
      content region. The mode that's useful for typical lens distortion.
    - **source (pre-distort)** — tracks every UV·src sample across the grid;
      bbox is the CG region the STMap reads. Only useful when the STMap
      addresses a sub-region of source (tight crop maps, reformat sub-region).
- Source/target format split (`source_w/h`, `target_w/h`, `fmt_mode` knobs)
  to handle resolution-changing STMaps (e.g. CG 4K → delivery 2K).
- Auto-detection for normalized 0-1 vs absolute pixel UVs (`uv_space`).
- `live_update` Boolean fires `_compute` from the per-node `updateUI`
  callback on each frame change. Throttled via a hidden `_live_last_frame`
  knob so a single compute runs per frame.
- `Bake Animation` runs a QTimer-driven async loop. Each step calls
  `nuke.frame(f)` and waits 10 ms for Nuke's UI cycle to fire `updateUI`,
  which calls `_compute` with a fresh `cg.bbox()` and writes a key via
  `setValue` on the (now animated) `cg_bbox_*` knobs. Same code path as
  `live_update`, so bake values match live values exactly. A settle pass
  at the end forces the last frame's UI cycle to flush before cleanup,
  avoiding a missed final key.
- Three bake speedups: a separate `bake_grid_resolution` knob (lower than
  the live grid, default 48 vs 96), a one-shot UV-space pre-detection
  before the loop instead of per-frame probing, and a 10 ms inter-frame
  delay (down from 50 ms).
- Live and Baked values displayed side-by-side in the Result twirly so
  any per-frame divergence is visible at a glance during scrub-verification.
- `Extract External Crop` button creates a standalone, disconnected `Crop`
  node in the parent graph and copies all four `cg_bbox_*` channels —
  including animation curves — onto its `box` knob. The new Crop is
  independent of STMapBBox afterwards.

### Panel layout (top → bottom)

- Title and explainer.
- **Analysis** — `grid_resolution`, `bake_grid_resolution`, `bbox_margin`.
- **Compute / Test Inputs / Reset** action row.
- **Animation** — bake range, `Bake Animation`, `Clear Keyframes`,
  `Extract External Crop`, `live update` toggle.
- **Result** (closed twirly) — bbox report, Live values, Baked values.
- Channels — U/V channel knobs, `uv_space`, `bbox_space`.
- Formats — auto/manual mode, source/target W/H, Pick Up Formats.
- Edge Trim (closed twirly).
- Notes (closed twirly).
- Credit.

### Known limitations of the Python implementation

- **Bake is slow.** Each frame runs through the full UI cycle to keep
  `cg.bbox()` fresh; a 100-frame bake at grid 48 takes roughly 5–15 s
  depending on machine. Acceptable for a one-off bake, painful for
  iteration. Live update is similarly limited per scrub event.
- **`src.sample()` overhead.** ~2300 sample pairs per frame at the bake
  grid (~9000 at the live grid); Python-side dispatch dominates.
  Single-frame compute is in the 50–500 ms range.
- **No native render-tree integration.** The bbox modifies the internal
  `cg_crop`'s `box` knob; downstream Reads/renderers don't get an
  ROI through Nuke's normal `request()` propagation unless the
  user wires the `cg_bbox_*` knobs into their ROI manually.
- **`cg.bbox()` staleness when called outside the UI cycle.** The
  reason the bake is forced to drive itself through `nuke.frame()` +
  `updateUI` rather than just looping in pure Python. A direct
  `cg.bbox()` after `nuke.frame(f)` returns whatever the upstream
  `OutputContext` was last validated against — not the current frame.
  This is what motivates the C++ port.

---

## v5.0 — C++ NDK Plugin (Windows 11, Nuke 14.1 → 17)

Native rewrite as an NDK plugin. Solves the staleness, performance, and
animation-bake issues structurally rather than working around them.

### Why C++

- **`_validate()` is called per-`OutputContext` natively.** Reading
  `input(0)->info().box()` from inside `_validate` always returns the bbox
  at the current frame. No `cg.bbox()` workarounds, no `tcl('update')`
  hacks, no live/bake distinction — animated CG bboxes Just Work.
- **`sample()` overhead disappears.** Replace per-pixel Python `src.sample()`
  with a single `input(1)->request()` + tile pointer arithmetic, or an
  `Iop::engine()` pull on a tile of UVs. Order-of-magnitude faster, sample
  grid resolution can go from 96 to 512+ for free.
- **Bbox propagates naturally downstream.** Nuke's normal bbox propagation
  through the tree means upstream Reads see the tightened ROI through the
  tree's `request()` calls — actual render-time savings in Reads, not just
  a cosmetic crop.
- **No internal Crop child node.** A single `Iop` does both the analysis
  and the bbox modification by setting `info_.set_bbox()` directly.
- **No QTimer-driven bake.** No bake at all in the normal sense — the
  plugin produces frame-correct bboxes on demand. The `Extract External
  Crop` workflow becomes optional, only useful when sending to a renderer
  that can't run the plugin itself.

### Architecture

- **Class:** `STMapBBox : public DD::Image::Iop`
- **Inputs:** `node_inputs() = 2`, named `img` and `stmap`. Input 0 is the
  source the bbox modifies; input 1 is analysis-only (UVs).
- **Knobs:** mirror the Python panel — `bbox_space`, `uv_space`,
  `u_channel`, `v_channel`, `bbox_margin`, `grid_resolution`,
  trim L/R/T/B, source_w/h, target_w/h, fmt_mode. Plus the four output ints
  (`cg_bbox_x/y/r/t`) exposed for downstream linking.
- **`_validate(for_real)`:**
    1. Validate both inputs (`input(0)->validate(for_real)` etc.).
    2. Read `info_` from input 0 to get format / channel layout.
    3. Run the bbox computation:
        - For target mode: walk the STMap's bbox on a `grid_resolution`
          grid, sample `(u, v)` via a temporary tile pull on input(1),
          test membership in `input(0)->info().box()`, accumulate active
          bbox.
        - For source mode: same walk, accumulate UV·src min/max, no
          membership test.
    4. `info_.set(info_.format(), Box(ax0, ay0, ax1, ay1), info_.channels())`
       — the new bbox propagates downstream.
    5. Cache the computed bbox keyed on a hash of input bboxes + knob values
       so repeat `_validate` calls don't re-walk the grid.
- **`_request(...)`:** request input(0) only over the new bbox. This is
  what gives upstream Reads the real ROI savings.
- **`engine(...)`:** identity copy on input(0) within the new bbox; black
  outside (matches `intersect=false, black_outside=false` behaviour of the
  Python tool's internal Crop).
- **`hash` / `Op::Description`:** standard NDK boilerplate. Hash includes
  all knobs + `input(1)->hash()` so the bbox cache invalidates correctly
  when the STMap changes.

### Algorithm port

The Python `COMPUTE_BODY` translates almost directly:

| Python                                | C++ (NDK)                              |
| ------------------------------------- | -------------------------------------- |
| `cg.bbox()`                           | `input(0)->info().box()`               |
| `cg.format().width()/height()`        | `input(0)->info().format().width()` etc. |
| `src.sample(chan, x+0.5, y+0.5, 0,0,frame)` | `Tile(input(1), bbox, channels)` then `tile[chan][y][x]` |
| `setValueAt(val, f, ch)`              | not needed — `_validate` runs per frame natively |
| `nuke.frame(f)` + `tcl('update')`     | not needed                             |
| QTimer-driven bake loop               | not needed                             |

Sample readback uses `Tile` / `RowCacheTile` rather than per-pixel API calls
— pull a single tile covering the trusted region once, index it directly.
For grids that don't fit in one tile (huge formats, small memory), fall back
to per-row `get(...)` calls.

### Animation handling

There is no separate "bake" step. Because `_validate` evaluates per
`OutputContext`, the modified bbox is intrinsically frame-specific.
Downstream nodes see the right bbox at every frame without any keyframe
machinery.

For users who want explicit baked keys (e.g. for light-render submission to
ROI-aware renderers without running the plugin at render time), a
Python-side helper script (`stmapbbox_bake.py`) walks the frame range,
evaluates the plugin via `nuke.execute()` on a tiny no-op write, reads the
bbox knobs, and writes a normal `Crop` node with `setValueAt` keys. This
is short and reliable because `nuke.execute()` does properly drive
`_validate` per frame.

### Build — CMake, Windows 11, MSVC 2022

`CMakeLists.txt` follows the SpectralRenderer / VDBRender pattern:

```cmake
cmake_minimum_required(VERSION 3.20)
project(STMapBBox LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# -DNUKE_VERSION=14.1 / 15.0 / 16.0 / 17.0 selects the SDK
set(NUKE_VERSION "14.1" CACHE STRING "Target Nuke version")
set(NUKE_ROOT "C:/Program Files/Nuke${NUKE_VERSION}v1" CACHE PATH "")

find_path(NUKE_INCLUDE_DIR DDImage/Iop.h
    HINTS "${NUKE_ROOT}/include")
find_library(NUKE_DDIMAGE_LIB
    NAMES DDImage FdkBase.${NUKE_VERSION}
    HINTS "${NUKE_ROOT}")

add_library(STMapBBox SHARED
    src/STMapBBox.cpp
    src/SamplingGrid.cpp
    src/UvSpaceDetect.cpp
)

target_include_directories(STMapBBox PRIVATE
    ${NUKE_INCLUDE_DIR}
    src/)

target_link_libraries(STMapBBox PRIVATE ${NUKE_DDIMAGE_LIB})

# Foundry NDK plugins on Windows want .dll with no "lib" prefix
set_target_properties(STMapBBox PROPERTIES
    PREFIX ""
    SUFFIX ".dll"
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/Nuke${NUKE_VERSION}"
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/Nuke${NUKE_VERSION}")

if (MSVC)
    target_compile_options(STMapBBox PRIVATE /W4 /MP /EHsc)
    target_compile_definitions(STMapBBox PRIVATE
        _USE_MATH_DEFINES NOMINMAX WIN32_LEAN_AND_MEAN)
endif()
```

Build from a Developer PowerShell:

```powershell
cd C:\dev\STMapBBox

cmake -S . -B build/Nuke14 -G "Visual Studio 17 2022" -A x64 -DNUKE_VERSION=14.1
cmake --build build/Nuke14 --config Release

cmake -S . -B build/Nuke15 -G "Visual Studio 17 2022" -A x64 -DNUKE_VERSION=15.0
cmake --build build/Nuke15 --config Release
# ... etc for 16, 17
```

Output path: `build/Nuke{VER}/STMapBBox.dll`. Install by dropping into
`%USERPROFILE%/.nuke/Nuke{VER}/` or whatever the facility's plugin dir is.

### Multi-version compatibility (Nuke 14 → 17)

NDK API changes between major versions — usually small but binary-breaking.
Approach:

- **One source tree, one `CMakeLists.txt`, multiple builds.** Same code
  compiles against each NDK; the build script produces one DLL per
  version. No `#ifdef NUKE_VERSION` ladders unless absolutely necessary —
  the `Iop` API surface we use is stable across 14–17.
- **Compatibility shims in `src/Compat.h`** for the few things that did
  change (e.g. `Op::Description` registration arity, channel-name macros
  if they shift). Keep this file small — most of `STMapBBox.cpp` should be
  version-agnostic.
- **CI matrix:** Windows 11, MSVC 2022, NDK 14.1 / 15.0 / 16.0 / 17.0.
  Manual local build for now; GitHub Actions runner with a Foundry SDK
  cache later if the project warrants it.
- **Tested on:** Windows 11 23H2, MSVC 2022 17.9+. Linux/macOS not in scope
  for v5.0 but the source is portable; only the build system is
  Windows-specific.

### Migration path

1. **Phase 1 — feature parity.** Port the algorithm, panel, and channels
   handling. Validate that v5.0 produces the same bbox values as v4.0 on a
   battery of test STMaps (radial distortion, anamorphic, fisheye).
2. **Phase 2 — animation acid test.** Take a shot with an animated CG bbox
   (the case that's slow in the Python tool's bake) and verify v5.0 is
   correct frame-by-frame with no special workflow and renders fast.
3. **Phase 3 — performance.** Benchmark v5.0 vs v4.0 on a long shot.
   Target: ≥ 50× faster bbox compute, 200+ grid resolution at < 5 ms/frame.
4. **Phase 4 — release.** Ship `STMapBBox.dll` per Nuke version with
   matching `menu.py` snippet and a `README.md` covering install steps.
5. **Phase 5 (optional) — Linux build.** Same source, GCC + Make/CMake,
   `STMapBBox.so`. Mostly a CMake config issue, no source changes expected.

### Out of scope for v5.0

- Linux / macOS builds (Phase 5 stretch).
- A standalone Open RV plugin equivalent. STMaps are a Nuke-graph concept;
  RV doesn't have the same node model.
- CUDA / OptiX acceleration. The grid-walk is already trivial CPU work
  once Python overhead is gone; GPU is unwarranted.
- Inverse / deformation analysis (e.g. solving for "what UV map would
  produce this bbox change"). That's a separate research direction.

---

## Repo layout (target)

```
STMapBBox/
├── CMakeLists.txt
├── README.md
├── ROADMAP.md                    ← this file
├── python/
│   ├── install_bbox.py           ← v4.x Python tool (current)
│   └── stmapbbox_bake.py         ← optional bake helper for v5.x
├── src/
│   ├── STMapBBox.cpp             ← Iop subclass, _validate / engine
│   ├── SamplingGrid.{h,cpp}      ← grid walk, accumulator
│   ├── UvSpaceDetect.{h,cpp}     ← auto-detect normalized vs absolute
│   ├── Compat.h                  ← per-Nuke-version shims
│   └── Knobs.h                   ← knob enum / labels / tooltips
├── tests/
│   ├── golden/                   ← reference STMap + expected bboxes
│   └── nuke_compare.py           ← runs both v4 and v5 on golden, diffs
└── docs/
    ├── algorithm.md              ← math + diagrams
    └── build_windows.md          ← step-by-step build guide
```

---

## Git setup — push from `C:\dev\STMapBBox`

Open a PowerShell at the project root:

```powershell
cd C:\dev\STMapBBox
```

### One-time setup — initialise repo and link to GitHub

Create `https://github.com/bratgot/STMapBBox` on the GitHub website first.
Make it an **empty** repo — no README, no .gitignore, no licence — we
commit those locally to avoid a divergent first push.

```powershell
# Initialise local repo on a 'main' branch
git init -b main

# Create a sensible .gitignore for the C++ + Python project
@"
# Build artefacts
build/
*.dll
*.lib
*.exp
*.pdb
*.obj
*.ilk

# CMake
CMakeFiles/
CMakeCache.txt
cmake_install.cmake
Makefile
*.cmake

# Visual Studio
.vs/
*.vcxproj.user
*.suo
out/

# Python
__pycache__/
*.pyc
*.pyo

# Nuke autosaves
*.autosave
*.nk~

# OS / editor
Thumbs.db
.DS_Store
.idea/
.vscode/
"@ | Out-File -Encoding utf8 .gitignore

# Stage everything currently in the folder
git add .

git commit -m "Initial commit: STMapBBox v4.0 (Python tool, ROADMAP)"

# Hook up the GitHub remote
git remote add origin https://github.com/bratgot/STMapBBox.git

# Push and set upstream
git push -u origin main
```

### Routine update — daily commit + push

Once the remote is hooked up, each iteration is:

```powershell
cd C:\dev\STMapBBox
git status                       # see what changed
git add -A                       # stage everything
git commit -m "Describe change"  # local commit
git push                         # to GitHub
```

### If GitHub already has a README / licence (created remotely)

If the GitHub repo was initialised with a README, the remote has a
commit yours doesn't, and `git push` will reject. Pull-rebase first:

```powershell
git pull --rebase origin main
git push -u origin main
```

### Tagging a release

When v4.0 is in a state worth fixing, tag and push:

```powershell
git tag -a v4.0 -m "v4.0 - Python tool, animated bake, Extract Crop"
git push origin v4.0
```

GitHub will show the tag under Releases. Future C++ work tags as
`v5.0-alpha1` etc. to make the major-version split obvious.
