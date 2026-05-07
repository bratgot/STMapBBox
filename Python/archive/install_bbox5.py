"""
STMapBBox installer for Nuke.

Two-input tool that predicts the *post-distortion* bounding box of a CG
render: given a CG with bbox X and an STMap that will be applied to it,
the new bbox Y is the area of the output where the STMap's UVs land inside
X. After distortion the CG's content occupies Y, so the bbox should be
updated to Y to "capture the new distorted image".

Inputs:
    0 (img)   -- the CG render whose bbox to update (input bbox X is read
                 from this node)
    1 (stmap) -- the STMap that will be applied downstream
                 (rgba.red = U, rgba.green = V by default; configurable)

Algorithm:
    For each pixel (x, y) on a grid across the STMap, sample (U, V).
    Convert to CG pixel space (multiply by CG format, or use directly if
    UVs are already absolute pixels). If that pixel lands inside X, this
    output pixel is "active" -- the CG's content reaches here after
    distortion. Take the bbox of all active grid points + a grid-spacing
    safety margin. Apply that as the new bbox via an internal Crop with
    intersect=False so the bbox can grow past the original.

Usage:
    Open the Nuke Script Editor and run:
        exec(open('/path/to/install_bbox.py').read())

Repo: https://github.com/bratgot/STMapBBox
"""
import nuke


COMPUTE_BODY = r'''
    # expects in scope: n, verbose, silent_errors, target_frame.
    # target_frame: None -> use current nuke.frame(), write via setValue.
    #               int  -> pass to sample() and write keyframes via setValueAt.
    cg  = n.input(0)
    src = n.input(1)

    if cg is None:
        if not silent_errors:
            nuke.message("Connect the CG to input 0 (img).")
        return False
    if src is None:
        if not silent_errors:
            nuke.message("Connect the STMap to input 1 (stmap).")
        return False

    # explicit frame for sample() reads and setValueAt writes; falling back
    # to nuke.frame() for the "live"/manual-Compute path keeps that case
    # identical to before
    if target_frame is None:
        eval_frame = nuke.frame()
    else:
        eval_frame = int(target_frame)

    cgb = cg.bbox()
    cg_x_min = int(cgb.x())
    cg_y_min = int(cgb.y())
    cg_x_max = cg_x_min + int(cgb.w()) - 1
    cg_y_max = cg_y_min + int(cgb.h()) - 1

    fmt_mode = n["fmt_mode"].value()
    if fmt_mode == "auto":
        sf = cg.format();   src_w = int(sf.width()); src_h = int(sf.height())
        tf = src.format();  tgt_w = int(tf.width()); tgt_h = int(tf.height())
    else:
        src_w = int(n["source_w"].value()); src_h = int(n["source_h"].value())
        tgt_w = int(n["target_w"].value()); tgt_h = int(n["target_h"].value())

    if src_w < 1 or src_h < 1 or tgt_w < 1 or tgt_h < 1:
        if not silent_errors:
            nuke.message("Source / target format must be >= 1.")
        return False
    if cg_x_max - cg_x_min < 1 or cg_y_max - cg_y_min < 1:
        if not silent_errors:
            nuke.message("CG (input 0) has an empty bbox.")
        return False

    u_chan = n["u_channel"].value() or "rgba.red"
    v_chan = n["v_channel"].value() or "rgba.green"
    space  = n["uv_space"].value()
    bbox_space = n["bbox_space"].value()  # "source (pre-distort)" or "target (post-distort)"
    margin = int(n["bbox_margin"].value())
    grid_n = max(16, int(n["grid_resolution"].value()))
    tL = int(n["trim_left"].value());   tR = int(n["trim_right"].value())
    tB = int(n["trim_bottom"].value()); tT = int(n["trim_top"].value())

    sb  = src.bbox()
    sx0 = int(sb.x()) + max(0, tL)
    sy0 = int(sb.y()) + max(0, tB)
    sx1 = int(sb.x()) + int(sb.w()) - 1 - max(0, tR)
    sy1 = int(sb.y()) + int(sb.h()) - 1 - max(0, tT)

    if sx1 - sx0 < 4 or sy1 - sy0 < 4:
        if not silent_errors:
            nuke.message("STMap region (after trim) too small.")
        return False

    step_x = max(1, (sx1 - sx0) // (grid_n - 1))
    step_y = max(1, (sy1 - sy0) // (grid_n - 1))

    # local sample helper: pass frame= explicitly so reads are frame-specific
    # regardless of OutputContext
    def _samp(chan, x, y):
        return src.sample(chan, x + 0.5, y + 0.5, 0, 0, eval_frame)

    if space == "auto":
        test_max = 0.0
        for tx in (sx0, (sx0 + sx1) // 2, sx1):
            for ty in (sy0, (sy0 + sy1) // 2, sy1):
                try:
                    uu = _samp(u_chan, tx, ty)
                    vv = _samp(v_chan, tx, ty)
                    if abs(uu) > test_max: test_max = abs(uu)
                    if abs(vv) > test_max: test_max = abs(vv)
                except Exception:
                    pass
        use_space = "absolute (CG pixels)" if test_max > 5.0 else "normalized 0-1"
    else:
        use_space = space
    is_absolute = (use_space == "absolute (CG pixels)")

    amin = [10**9, 10**9]; amax = [-10**9, -10**9]   # accumulator: [x/u, y/v]
    n_active = 0; n_total = 0
    is_source = (bbox_space == "source (pre-distort)")

    x_iter = list(range(sx0, sx1 + 1, step_x))
    if x_iter[-1] != sx1: x_iter.append(sx1)
    y_iter = list(range(sy0, sy1 + 1, step_y))
    if y_iter[-1] != sy1: y_iter.append(sy1)

    for x in x_iter:
        for y in y_iter:
            n_total += 1
            try:
                u = _samp(u_chan, x, y)
                v = _samp(v_chan, x, y)
            except Exception:
                continue
            if is_absolute:
                u_pix = u; v_pix = v
            else:
                u_pix = u * src_w; v_pix = v * src_h

            if is_source:
                # SOURCE MODE: track every UV sample position.
                # The bbox is the region of the CG that the STMap will read.
                # No filter -- we want the full required CG region.
                if u_pix < amin[0]: amin[0] = u_pix
                if u_pix > amax[0]: amax[0] = u_pix
                if v_pix < amin[1]: amin[1] = v_pix
                if v_pix > amax[1]: amax[1] = v_pix
                n_active += 1
            else:
                # TARGET MODE: track output pixels where UV lands in CG bbox.
                # The bbox is where the post-distortion output has content.
                if (cg_x_min <= u_pix <= cg_x_max and
                    cg_y_min <= v_pix <= cg_y_max):
                    if x < amin[0]: amin[0] = x
                    if x > amax[0]: amax[0] = x
                    if y < amin[1]: amin[1] = y
                    if y > amax[1]: amax[1] = y
                    n_active += 1

    amin_x, amin_y = amin[0], amin[1]
    amax_x, amax_y = amax[0], amax[1]

    if n_active == 0:
        if not silent_errors:
            nuke.message(
                "No active output pixels found.\n\n"
                "The STMap's UVs never land inside the CG bbox.\n"
                "Press Test Inputs to see raw sample values."
            )
        return False

    grid_margin = max(step_x, step_y)
    total_margin = grid_margin + margin

    # Clamp bbox to the right format -- source dims for source mode (the CG's
    # native space), target dims for target mode (where the distorted output
    # will live).
    if is_source:
        clamp_w, clamp_h = src_w, src_h
    else:
        clamp_w, clamp_h = tgt_w, tgt_h

    ax0 = max(0,       int(amin_x) - total_margin)
    ay0 = max(0,       int(amin_y) - total_margin)
    ax1 = min(clamp_w, int(amax_x) + 1 + total_margin)
    ay1 = min(clamp_h, int(amax_y) + 1 + total_margin)

    cn = n.node("cg_crop")

    # ALWAYS write to live_x/y/r/t -- these are display-only knobs that
    # show the most recent freshly-computed bbox. Use setValue (no
    # animation) so they always reflect the latest compute, never a
    # baked curve. This lets the user compare live values against the
    # animated cg_bbox_* values to verify bake correctness.
    try:
        n["live_x"].setValue(int(ax0)); n["live_y"].setValue(int(ay0))
        n["live_r"].setValue(int(ax1)); n["live_t"].setValue(int(ay1))
    except Exception:
        pass

    if target_frame is None:
        # current-frame mode: setValue (matches existing behaviour)
        n["cg_bbox_x"].setValue(ax0); n["cg_bbox_y"].setValue(ay0)
        n["cg_bbox_r"].setValue(ax1); n["cg_bbox_t"].setValue(ay1)
        if cn is not None:
            try: cn["intersect"].setValue(False)
            except Exception: pass
            cn["box"].setValue([float(ax0), float(ay0),
                                float(ax1), float(ay1)])
    else:
        # bake mode: setValueAt with explicit frame -- no dependence on
        # what nuke.frame() returns, no dependence on UI cycle
        f = float(target_frame)
        n["cg_bbox_x"].setValueAt(float(ax0), f)
        n["cg_bbox_y"].setValueAt(float(ay0), f)
        n["cg_bbox_r"].setValueAt(float(ax1), f)
        n["cg_bbox_t"].setValueAt(float(ay1), f)
        if cn is not None:
            try: cn["intersect"].setValue(False)
            except Exception: pass
            cn["box"].setValueAt(float(ax0), f, 0)
            cn["box"].setValueAt(float(ay0), f, 1)
            cn["box"].setValueAt(float(ax1), f, 2)
            cn["box"].setValueAt(float(ay1), f, 3)

    if verbose:
        ow = ax1 - ax0; oh = ay1 - ay0
        iw = cg_x_max - cg_x_min + 1; ih = cg_y_max - cg_y_min + 1

        n["bbox_report"].setValue(
            '<p style="margin:0; color:#cde; font-size:11px;">'
            '<b>New bbox</b> (%s)&nbsp; x=[%d, %d]&nbsp; '
            'y=[%d, %d]&nbsp; %d&times;%d<br>'
            '<span style="color:#888;">'
            'CG was %d&times;%d at [%d, %d]&ndash;[%d, %d] &middot; '
            'src %d&times;%d &rarr; tgt %d&times;%d &middot; '
            'active %d/%d (%.1f%%) &middot; '
            'UV space: <b>%s</b></span></p>'
            % ("source / pre-distort" if is_source else "target / post-distort",
               ax0, ax1, ay0, ay1, ow, oh,
               iw, ih, cg_x_min, cg_y_min, cg_x_max, cg_y_max,
               src_w, src_h, tgt_w, tgt_h,
               n_active, n_total, 100.0 * n_active / n_total,
               use_space)
        )

        rescale = "(no rescale)" if (src_w == tgt_w and src_h == tgt_h) \
                  else "(RESCALE %dx%d -> %dx%d)" % (src_w, src_h, tgt_w, tgt_h)
        print("=== STMapBBox =================================")
        print("Frame:             %d" % eval_frame)
        print("Mode:              %s"
              % ("source / pre-distort (CG sample region)" if is_source
                 else "target / post-distort (output content region)"))
        print("CG node:           %s" % cg.name())
        print("CG bbox in:        x=[%d, %d]  y=[%d, %d]  (%dx%d)"
              % (cg_x_min, cg_x_max, cg_y_min, cg_y_max, iw, ih))
        print("Source format:     %dx%d  (UVs normalized against this)" % (src_w, src_h))
        print("Target format:     %dx%d  %s" % (tgt_w, tgt_h, rescale))
        print("STMap node:        %s" % src.name())
        print("Sample region:     x=[%d, %d]  y=[%d, %d]"
              % (sx0, sx1, sy0, sy1))
        print("Sample grid:       %dx%d  (steps %d, %d)"
              % (len(x_iter), len(y_iter), step_x, step_y))
        print("Channels:          U=%s  V=%s" % (u_chan, v_chan))
        print("UV space:          %s" % use_space)
        print("Active samples:    %d / %d  (%.1f%%)"
              % (n_active, n_total, 100.0 * n_active / n_total))
        print("Active raw bbox:   %s=[%d, %d]  %s=[%d, %d]"
              % ("u" if is_source else "x", int(amin_x), int(amax_x),
                 "v" if is_source else "y", int(amin_y), int(amax_y)))
        print("Margin:            %d px  (grid %d + safety %d)"
              % (total_margin, grid_margin, margin))
        print("New bbox out:      x=[%d, %d]  y=[%d, %d]  (%dx%d, in %s space)"
              % (ax0, ax1, ay0, ay1, ow, oh,
                 "source" if is_source else "target"))
        print("================================================")

    return True
'''


COMPUTE_CODE = (
    "import nuke\n"
    "def _compute(n, verbose, silent_errors, target_frame=None):\n"
    + COMPUTE_BODY
    + "\n_compute(nuke.thisNode(), True, False, None)\n"
)




TEST_CODE = r'''
import nuke

n = nuke.thisNode()
cg = n.input(0); src = n.input(1)

print("=== STMapBBox: input test ======================")

if cg is None:
    print("Input 0 (img):   NOT CONNECTED")
else:
    fmt = cg.format(); bb = cg.bbox()
    print("Input 0 (img):   %s" % cg.name())
    print("  format:        %dx%d  (%s)"
          % (fmt.width(), fmt.height(), fmt.name() or "unnamed"))
    print("  bbox:          x=[%d, %d]  y=[%d, %d]  (%dx%d)"
          % (int(bb.x()), int(bb.x() + bb.w() - 1),
             int(bb.y()), int(bb.y() + bb.h() - 1),
             int(bb.w()), int(bb.h())))

if src is None:
    print("Input 1 (stmap): NOT CONNECTED")
else:
    fmt = src.format(); bb = src.bbox()
    print("Input 1 (stmap): %s" % src.name())
    print("  format:        %dx%d" % (fmt.width(), fmt.height()))
    print("  bbox:          x=[%d, %d]  y=[%d, %d]"
          % (int(bb.x()), int(bb.x() + bb.w() - 1),
             int(bb.y()), int(bb.y() + bb.h() - 1)))
    print("  channels:      %s" % ", ".join(src.channels()[:12]))

    x0, y0 = int(bb.x()), int(bb.y())
    x1, y1 = x0 + int(bb.w()) - 1, y0 + int(bb.h()) - 1

    pairs = [("rgba.red", "rgba.green"),
             ("forward.u", "forward.v"),
             ("backward.u", "backward.v")]

    for u_chan, v_chan in pairs:
        print("  %s / %s 3x3 grid:" % (u_chan, v_chan))
        ok = True
        for fy in (0.1, 0.5, 0.9):
            cells = []
            for fx in (0.1, 0.5, 0.9):
                x = x0 + int(fx * (x1 - x0))
                y = y0 + int(fy * (y1 - y0))
                try:
                    u = src.sample(u_chan, x + 0.5, y + 0.5)
                    v = src.sample(v_chan, x + 0.5, y + 0.5)
                    cells.append("(%4d,%4d)->(%+8.4f, %+8.4f)" % (x, y, u, v))
                except Exception as e:
                    ok = False
                    cells.append("(%4d,%4d)-> NO CHANNEL" % (x, y))
                    break
            print("   ", " | ".join(cells))
            if not ok: break
print("================================================")
'''


SYNC_CB = '''
g = nuke.thisNode()
k = nuke.thisKnob()
if k is not None and k.name() in ("cg_bbox_x", "cg_bbox_y", "cg_bbox_r", "cg_bbox_t"):
    cn = g.node("cg_crop")
    if cn is not None:
        cn["box"].setValue([
            float(g["cg_bbox_x"].value()),
            float(g["cg_bbox_y"].value()),
            float(g["cg_bbox_r"].value()),
            float(g["cg_bbox_t"].value()),
        ])
'''


BAKE_CODE = (
    "import nuke\n"
    "try:\n"
    "    from PySide2.QtCore import QTimer\n"
    "except ImportError:\n"
    "    from PySide6.QtCore import QTimer\n"
    "\n"
    "n = nuke.thisNode()\n"
    "cg = n.input(0); src = n.input(1)\n"
    "if cg is None or src is None:\n"
    "    nuke.message('Connect both inputs first.')\n"
    "else:\n"
    "    first = int(n['bake_first'].value())\n"
    "    last  = int(n['bake_last'].value())\n"
    "    step  = max(1, int(n['bake_step'].value()))\n"
    "\n"
    "    if last < first:\n"
    "        nuke.message('Last frame must be >= first frame.')\n"
    "    else:\n"
    "        cn = n.node('cg_crop')\n"
    "\n"
    "        # Animate the bbox knobs (clear any prior keys first).\n"
    "        for nm in ('cg_bbox_x', 'cg_bbox_y', 'cg_bbox_r', 'cg_bbox_t'):\n"
    "            k = n[nm]\n"
    "            if k.isAnimated(): k.clearAnimated()\n"
    "            k.setAnimated()\n"
    "        if cn is not None:\n"
    "            for ch in (0, 1, 2, 3):\n"
    "                if cn['box'].isAnimated(ch): cn['box'].clearAnimated(ch)\n"
    "                cn['box'].setAnimated(ch)\n"
    "\n"
    "        saved_frame = nuke.frame()\n"
    "        try: was_live = bool(int(n['live_update'].value()))\n"
    "        except Exception: was_live = False\n"
    "\n"
    "        # SPEEDUP 1: temporarily lower grid resolution for the bake.\n"
    "        # Bake walks 100s of frames -- a tighter grid gets recovered by\n"
    "        # the auto grid-margin in _compute. Live update keeps using the\n"
    "        # main grid_resolution knob.\n"
    "        saved_grid = int(n['grid_resolution'].value())\n"
    "        try: bake_grid = int(n['bake_grid_resolution'].value())\n"
    "        except Exception: bake_grid = saved_grid\n"
    "        if bake_grid != saved_grid:\n"
    "            n['grid_resolution'].setValue(bake_grid)\n"
    "\n"
    "        # SPEEDUP 2: pre-detect UV space ONCE here instead of redoing\n"
    "        # the 9-sample auto-detect every frame inside _compute. We pin\n"
    "        # uv_space to the detected concrete value during the bake and\n"
    "        # restore 'auto' afterwards.\n"
    "        saved_uv = n['uv_space'].value()\n"
    "        if saved_uv == 'auto':\n"
    "            try:\n"
    "                sb = src.bbox()\n"
    "                _sx0, _sy0 = int(sb.x()), int(sb.y())\n"
    "                _sx1 = _sx0 + int(sb.w()) - 1\n"
    "                _sy1 = _sy0 + int(sb.h()) - 1\n"
    "                _u_ch = n['u_channel'].value() or 'rgba.red'\n"
    "                _v_ch = n['v_channel'].value() or 'rgba.green'\n"
    "                _max = 0.0\n"
    "                for _tx in (_sx0, (_sx0 + _sx1) // 2, _sx1):\n"
    "                    for _ty in (_sy0, (_sy0 + _sy1) // 2, _sy1):\n"
    "                        try:\n"
    "                            _u = src.sample(_u_ch, _tx + 0.5, _ty + 0.5)\n"
    "                            _v = src.sample(_v_ch, _tx + 0.5, _ty + 0.5)\n"
    "                            _ax = abs(_u); _ay = abs(_v)\n"
    "                            if _ax > _max: _max = _ax\n"
    "                            if _ay > _max: _max = _ay\n"
    "                        except Exception: pass\n"
    "                _detected = ('absolute (CG pixels)' if _max > 5.0\n"
    "                             else 'normalized 0-1')\n"
    "                n['uv_space'].setValue(_detected)\n"
    "                print('STMapBBox: pre-detected UV space as %s '\n"
    "                      '(saves ~9 samples/frame during bake)' % _detected)\n"
    "            except Exception: pass\n"
    "\n"
    "        # Force live_update ON for the duration of the bake. Each\n"
    "        # nuke.frame(f) below triggers Nuke's UI cycle, which fires\n"
    "        # updateUI -> _compute(target_frame=None). _compute reads a\n"
    "        # FRESH cg.bbox() (because UI cycle just re-validated upstream)\n"
    "        # and writes via setValue on the now-animated cg_bbox_* knobs,\n"
    "        # which creates a key at the current frame. SYNC_CB syncs the\n"
    "        # internal Crop. live_x/y/r/t also gets the same value via\n"
    "        # _compute's display write.\n"
    "        n['live_update'].setValue(True)\n"
    "\n"
    "        # Reset the live throttle so updateUI fires for the very first\n"
    "        # frame even if the timeline is already there.\n"
    "        try: n['_live_last_frame'].setValue(-99999)\n"
    "        except Exception: pass\n"
    "\n"
    "        frames = list(range(first, last + 1, step))\n"
    "        state = {'idx': 0, 'task': nuke.ProgressTask('Baking STMapBBox')}\n"
    "\n"
    "        def _step():\n"
    "            t = state['task']\n"
    "            done = (state['idx'] >= len(frames)) or t.isCancelled()\n"
    "            if done:\n"
    "                # SETTLE PASS: the last frame's nuke.frame() call was\n"
    "                # only ~10ms ago, and Nuke's UI cycle (which fires\n"
    "                # updateUI -> _compute -> setValue) may not have run\n"
    "                # yet. If we cleanup now, the final key is missed.\n"
    "                # Wait one more longer interval, then nudge the throttle\n"
    "                # and bounce frames once to force a final updateUI.\n"
    "                if not state.get('settled'):\n"
    "                    state['settled'] = True\n"
    "                    # Bump throttle so updateUI accepts a re-fire on\n"
    "                    # the same frame.\n"
    "                    try: n['_live_last_frame'].setValue(-99999)\n"
    "                    except Exception: pass\n"
    "                    # Nudge: go to a different frame and back so Nuke\n"
    "                    # definitely runs a UI cycle for the last frame.\n"
    "                    last_f = frames[-1] if frames else nuke.frame()\n"
    "                    nudge = last_f + 1 if not t.isCancelled() else nuke.frame()\n"
    "                    nuke.frame(nudge)\n"
    "                    QTimer.singleShot(80, lambda: (\n"
    "                        nuke.frame(last_f), QTimer.singleShot(80, _step)\n"
    "                    ))\n"
    "                    return\n"
    "                # Now actually clean up.\n"
    "                nuke.frame(saved_frame)\n"
    "                n['live_update'].setValue(was_live)\n"
    "                try: n['grid_resolution'].setValue(saved_grid)\n"
    "                except Exception: pass\n"
    "                try: n['uv_space'].setValue(saved_uv)\n"
    "                except Exception: pass\n"
    "                state['task'] = None\n"
    "                msg = 'STMapBBox: baked %d / %d frames%s' % (\n"
    "                    state['idx'], len(frames),\n"
    "                    ' (cancelled)' if state['idx'] < len(frames) else '')\n"
    "                print(msg)\n"
    "                return\n"
    "            f = frames[state['idx']]\n"
    "            t.setProgress(int(100.0 * state['idx'] / max(1, len(frames))))\n"
    "            t.setMessage('Frame %d (%d/%d)' %\n"
    "                         (f, state['idx'] + 1, len(frames)))\n"
    "            state['idx'] += 1\n"
    "            # Advance the timeline -- triggers UI cycle -> updateUI ->\n"
    "            # _compute -> setValue (creates key on animated knob).\n"
    "            nuke.frame(f)\n"
    "            # 10ms delay between steps. Final frame gets an additional\n"
    "            # settle pass via the done-branch above.\n"
    "            QTimer.singleShot(10, _step)\n"
    "\n"
    "        QTimer.singleShot(0, _step)\n"
    "        print('STMapBBox: baking %d frames via UI cycle '\n"
    "              '(grid %d, %s)...' %\n"
    "              (len(frames), bake_grid, n['uv_space'].value()))\n"
)



CLEAR_ANIM_CODE = '''
import nuke
n = nuke.thisNode()
removed = 0
for nm in ("cg_bbox_x", "cg_bbox_y", "cg_bbox_r", "cg_bbox_t"):
    k = n[nm]
    if k.isAnimated():
        k.clearAnimated(); removed += 1
cn = n.node("cg_crop")
if cn is not None:
    for ch in (0, 1, 2, 3):
        if cn["box"].isAnimated(ch):
            cn["box"].clearAnimated(ch); removed += 1
print("STMapBBox: cleared %d animated channels" % removed)
'''


# Creates a standalone Crop node in the parent graph, copies all static
# values and keyframes from cg_bbox_* knobs onto its box. Useful for taking
# the result of a live_update + scrub workflow (or any animation we have
# on the bbox knobs) and exporting it as an independent node.
EXTRACT_CROP_CODE = '''
import nuke

n = nuke.thisNode()

# CRITICAL: nuke.nodes.X from inside a Group's knob script creates the new
# node INSIDE the group (where the user can't see it). Force creation into
# the parent context (root, or whichever Group contains STMapBBox).
try:
    parent_ctx = n.parent()
    if parent_ctx is None:
        parent_ctx = nuke.root()
except Exception:
    parent_ctx = nuke.root()

# Deselect everything first so createNode doesn't auto-wire to a selection
for sel in nuke.selectedNodes():
    sel.setSelected(False)

new_crop = None
parent_ctx.begin()
try:
    new_crop = nuke.nodes.Crop()
finally:
    parent_ctx.end()

if new_crop is None:
    nuke.message("STMapBBox: failed to create Crop node.")
else:
    # rename (uncollide kwarg is supported on Nuke 13+, fall back if not)
    try:
        new_crop.setName("STMapBBox_Crop", uncollide=True)
    except TypeError:
        try:
            new_crop["name"].setValue("STMapBBox_Crop")
        except Exception:
            pass

    # position next to STMapBBox
    try:
        new_crop.setXYpos(int(n.xpos()) + 110, int(n.ypos()))
    except Exception:
        pass

    # Leave the new Crop disconnected -- user wires it up themselves.

    # configure to match the internal Crop's behaviour
    try: new_crop["reformat"].setValue(False)
    except Exception: pass
    try: new_crop["intersect"].setValue(False)
    except Exception: pass

    mapping = [("cg_bbox_x", 0), ("cg_bbox_y", 1),
               ("cg_bbox_r", 2), ("cg_bbox_t", 3)]

    # static values
    for src_name, ch in mapping:
        try:
            new_crop["box"].setValue(float(n[src_name].value()), ch)
        except Exception as e:
            print("STMapBBox: failed setting box[%d]: %s" % (ch, str(e)))

    # animation curves
    n_keys = 0
    for src_name, ch in mapping:
        try:
            src_knob = n[src_name]
            if src_knob.isAnimated(0):
                new_crop["box"].setAnimated(ch)
                anim = src_knob.animation(0)
                if anim is not None:
                    for key in anim.keys():
                        new_crop["box"].setValueAt(float(key.y), float(key.x), ch)
                        n_keys += 1
        except Exception as e:
            print("STMapBBox: failed copying %s: %s" % (src_name, str(e)))

    # select + zoom so the user actually sees the new node
    try:
        new_crop.setSelected(True)
        nuke.zoomToFitSelected()
    except Exception:
        pass

    msg = "STMapBBox: created '%s' (disconnected). Animation: %s" % (
        new_crop.name(),
        ("%d keyframes copied" % n_keys) if n_keys > 0
        else "static values only (no keyframes on source)"
    )
    print(msg)
'''


# Per-node updateUI: fires on UI updates (frame change, knob edits...).
# Throttled to one compute per frame via _live_last_frame.
UPDATE_UI_CODE = (
    "def _compute(n, verbose, silent_errors, target_frame=None):\n"
    + COMPUTE_BODY
    + "\n"
    "g = nuke.thisNode()\n"
    "if not (g.knob('live_update') and g['live_update'].value()):\n"
    "    pass\n"
    "elif g.input(0) is None or g.input(1) is None:\n"
    "    pass\n"
    "else:\n"
    "    cur = int(nuke.frame())\n"
    "    lf = g.knob('_live_last_frame')\n"
    "    last = int(lf.value()) if lf is not None else -10**9\n"
    "    if cur != last:\n"
    "        if lf is not None:\n"
    "            lf.setValue(cur)\n"
    "        try:\n"
    "            _compute(g, False, True, None)\n"
    "        except Exception:\n"
    "            pass\n"
)



def build_stmap_bbox():
    grp = nuke.createNode('Group', inpanel=False)
    grp.setName('STMapBBox', uncollide=True)
    grp['tile_color'].setValue(int('4a90e2ff', 16))
    grp['note_font_size'].setValue(13)

    # ---- internal graph ----
    grp.begin()
    try:
        inp_img = nuke.nodes.Input(name='img')
        try: inp_img['number'].setValue(0)
        except Exception: pass

        inp_stm = nuke.nodes.Input(name='stmap')
        try: inp_stm['number'].setValue(1)
        except Exception: pass

        crop = nuke.nodes.Crop(name='cg_crop')
        crop.setInput(0, inp_img)
        # default: intersect=True, box = 16k -> pass-through (clipped to format)
        # compute will switch intersect off so the bbox can grow past input
        crop['box'].setValue([0.0, 0.0, 16384.0, 16384.0])
        try: crop['intersect'].setValue(True)
        except Exception: pass

        out = nuke.nodes.Output()
        out.setInput(0, crop)
    finally:
        grp.end()

    # ---- header ----
    title = (
        '<p style="margin:0;">'
        '<span style="color:#4a90e2; font-size:15px; font-weight:bold;">'
        'ST Map BBox</span><br>'
        '<span style="color:#cde; font-size:11px;">'
        'Predicts the post-distortion bounding box of a CG render.</span><br>'
        '<span style="color:#888; font-size:10px; font-style:italic;">'
        'For each pixel on a grid across <b>stmap</b>, samples (U, V) and tests<br>'
        'whether it lands inside <b>img</b>\'s bbox. The bbox of all hits is<br>'
        'the new (distorted) CG bbox.</span></p>'
    )
    grp.addKnob(nuke.Text_Knob('title_hdr', '', title))

    # ---- analysis (moved above Compute so settings come first) ----
    grp.addKnob(nuke.Text_Knob('sec_opts', '<b>Analysis</b>'))

    kg = nuke.Int_Knob('grid_resolution', 'grid resolution')
    kg.setRange(16, 512); kg.setValue(96)
    kg.setTooltip('Number of samples per axis (96 -> ~9216 sample pairs). '
                  'Higher = tighter bbox boundary. The grid spacing is added '
                  'to the safety margin automatically.')
    grp.addKnob(kg)

    kbg = nuke.Int_Knob('bake_grid_resolution', 'bake grid')
    kbg.setRange(16, 512); kbg.setValue(48)
    kbg.clearFlag(nuke.STARTLINE)
    kbg.setTooltip('Grid resolution used during Bake only. Lower = faster '
                   'bake, slightly less precise bbox boundary. Live update '
                   'and Compute use the main grid_resolution above.')
    grp.addKnob(kbg)

    km = nuke.Int_Knob('bbox_margin', 'extra margin')
    km.setRange(0, 256); km.setValue(2)
    km.setTooltip('Extra pixels added on top of the grid-spacing margin. '
                  'Bump up if you see clipping at the bbox edges.')
    grp.addKnob(km)

    # ---- primary actions (moved to top for quick access) ----
    kb = nuke.PyScript_Knob('compute', 'Compute Distorted BBox')
    kb.setFlag(nuke.STARTLINE); kb.setValue(COMPUTE_CODE)
    grp.addKnob(kb)

    kt = nuke.PyScript_Knob('test_inputs', 'Test Inputs')
    kt.setTooltip('Print both inputs\' formats, bboxes, channels, and 3x3 '
                  'sample grids from rgba / forward / backward. Use this to '
                  'pick the right channels.')
    kt.setValue(TEST_CODE)
    grp.addKnob(kt)

    kreset = nuke.PyScript_Knob('reset_bbox', 'Reset (no crop)')
    kreset.setValue(
        'g = nuke.thisNode()\n'
        'g["cg_bbox_x"].setValue(0); g["cg_bbox_y"].setValue(0)\n'
        'g["cg_bbox_r"].setValue(16384); g["cg_bbox_t"].setValue(16384)\n'
        'cn = g.node("cg_crop")\n'
        'if cn is not None:\n'
        '    try: cn["intersect"].setValue(True)\n'
        '    except Exception: pass\n'
        '    cn["box"].setValue([0.0, 0.0, 16384.0, 16384.0])\n'
        'g["bbox_report"].setValue(\'<i style="color:#888;">(reset)</i>\')\n'
    )
    grp.addKnob(kreset)

    # ---- animation (moved up under Compute for quick access) ----
    grp.addKnob(nuke.Text_Knob('sec_anim', '<b>Animation</b>'))

    try:
        proj_first = int(nuke.root()['first_frame'].value())
        proj_last  = int(nuke.root()['last_frame'].value())
    except Exception:
        proj_first, proj_last = 1, 100

    kbf = nuke.Int_Knob('bake_first', 'first frame')
    kbf.setRange(-100000, 100000); kbf.setValue(proj_first)
    grp.addKnob(kbf)

    kbl = nuke.Int_Knob('bake_last', 'last frame')
    kbl.setRange(-100000, 100000); kbl.setValue(proj_last)
    kbl.clearFlag(nuke.STARTLINE)
    grp.addKnob(kbl)

    kbs = nuke.Int_Knob('bake_step', 'step')
    kbs.setRange(1, 60); kbs.setValue(1)
    kbs.setTooltip('Compute every N frames. >1 leaves Nuke to interpolate the '
                   'in-betweens (cheap, slightly less accurate at non-key frames).')
    grp.addKnob(kbs)

    kbake = nuke.PyScript_Knob('bake_anim', 'Bake Animation')
    kbake.setFlag(nuke.STARTLINE)
    kbake.setTooltip('Compute the bbox at every frame in [first, last] (step) '
                     'and write keyframes onto the bbox knobs and the internal '
                     'Crop. Existing keys are cleared first.')
    kbake.setValue(BAKE_CODE)
    grp.addKnob(kbake)

    kclear = nuke.PyScript_Knob('clear_anim', 'Clear Keyframes')
    kclear.setTooltip('Remove all keyframes from the bbox knobs and internal '
                      'Crop. Useful when switching from baked to live, or '
                      'before re-baking with a different range.')
    kclear.setValue(CLEAR_ANIM_CODE)
    grp.addKnob(kclear)

    kextract = nuke.PyScript_Knob('extract_crop', 'Extract External Crop')
    kextract.setTooltip(
        'Create a standalone Crop node in the parent graph, copying all '
        'static values AND animation curves from the bbox knobs onto its '
        'box. The new Crop is disconnected so you can wire it wherever '
        'you need.\n\n'
        'Workflow for animated CG bbox:\n'
        '  1. Enable live update.\n'
        '  2. Scrub or play through your frame range -- live writes a '
        'key per frame onto cg_bbox_*.\n'
        '  3. Press Extract External Crop -- a new Crop appears with the '
        'same animation curves, ready to drop into your tree.')
    kextract.setValue(EXTRACT_CROP_CODE)
    grp.addKnob(kextract)

    klive = nuke.Boolean_Knob('live_update', 'live update')
    klive.setFlag(nuke.STARTLINE)
    klive.setTooltip('Recompute the bbox automatically when the current frame '
                     'changes. Slows down playback (one compute pass per '
                     'frame). Throttled so only frame changes trigger work.')
    klive.setValue(False)
    grp.addKnob(klive)

    # hidden frame tracker for live update throttle
    klf = nuke.Int_Knob('_live_last_frame', '')
    klf.setRange(-100000, 100000); klf.setValue(-1)
    klf.setFlag(nuke.INVISIBLE)
    grp.addKnob(klf)

    grp.addKnob(nuke.Text_Knob('anim_hint', '',
        '<span style="color:#888; font-size:10px; font-style:italic;">'
        'Bake = static keyframes (cheap playback). Live = recompute on each<br>'
        'frame change (always current, slower).</span>'))

    # wire the per-node updateUI for live mode
    try:
        grp['updateUI'].setValue(UPDATE_UI_CODE)
    except Exception as e:
        print("STMapBBox: failed to set updateUI: " + str(e))

    # ---- result (twirly, closed by default) ----
    grp.addKnob(nuke.Tab_Knob('result_group', 'Result',
                              nuke.TABBEGINCLOSEDGROUP))
    grp.addKnob(nuke.Text_Knob('bbox_report', '',
                               '<i style="color:#888;">(not computed)</i>'))

    # Live values: always reflect the most recent fresh compute, never
    # animated. Used to verify bake correctness by scrubbing.
    grp.addKnob(nuke.Text_Knob('sec_live', '<b>Live values</b>',
        '<span style="color:#888; font-size:10px; font-style:italic;">'
        'Updated by Compute and live_update. Never animated.</span>'))

    live_defaults = [('live_x', 'x', 0), ('live_y', 'y', 0),
                     ('live_r', 'r', 0), ('live_t', 't', 0)]
    for nm, lbl, val in live_defaults:
        k = nuke.Int_Knob(nm, lbl)
        k.setRange(0, 16384); k.setValue(val)
        grp.addKnob(k)

    # Baked values: animated by Bake, drive the internal Crop.
    grp.addKnob(nuke.Text_Knob('sec_baked', '<b>Baked values</b>',
        '<span style="color:#888; font-size:10px; font-style:italic;">'
        'Drive the internal Crop. Animated by Bake. Scrub to compare with<br>'
        'Live above &mdash; if they differ at a frame, that key is wrong.</span>'))

    defaults = [('cg_bbox_x', 'x', 0), ('cg_bbox_y', 'y', 0),
                ('cg_bbox_r', 'r', 16384), ('cg_bbox_t', 't', 16384)]
    for nm, lbl, val in defaults:
        k = nuke.Int_Knob(nm, lbl)
        k.setRange(0, 16384); k.setValue(val)
        grp.addKnob(k)

    grp.addKnob(nuke.Text_Knob('out_hint', '',
        '<span style="color:#888; font-size:10px; font-style:italic;">'
        'Baked values mirror onto the internal Crop. Compute switches Crop\'s<br>'
        'intersect off so the bbox can grow past the input.</span>'))

    grp.addKnob(nuke.Tab_Knob('result_group_end', '', nuke.TABENDGROUP))

    grp['knobChanged'].setValue(SYNC_CB)

    # ---- channels ----
    grp.addKnob(nuke.Text_Knob('sec_chan', '<b>Channels</b>'))
    ku = nuke.String_Knob('u_channel', 'U channel', 'rgba.red')
    ku.setTooltip('Channel that holds the U values. Common alternatives: '
                  '"forward.u", "backward.u".')
    grp.addKnob(ku)
    kv = nuke.String_Knob('v_channel', 'V channel', 'rgba.green')
    kv.setTooltip('Channel that holds the V values. Common alternatives: '
                  '"forward.v", "backward.v".')
    grp.addKnob(kv)

    ksp = nuke.Enumeration_Knob('uv_space', 'UV space',
        ['auto', 'normalized 0-1', 'absolute (CG pixels)'])
    ksp.setValue('auto')
    ksp.setTooltip('How to interpret the UV values. auto detects from '
                   'magnitudes (>5 -> absolute pixels, else 0-1).')
    grp.addKnob(ksp)

    kbs_space = nuke.Enumeration_Knob('bbox_space', 'bbox space',
        ['target (post-distort)', 'source (pre-distort)'])
    kbs_space.setValue('target (post-distort)')
    kbs_space.setTooltip(
        'Which bbox to compute & apply.\n'
        'target (post-distort): the region in the STMap\'s output where the '
        'distorted CG will have content. Place STMapBBox AFTER the STMap or '
        'on the post-distortion read. Default for typical lens distortion.\n'
        'source (pre-distort): the region of the CG that the STMap will '
        'sample. Only useful when the STMap addresses LESS than the full '
        'source plate (e.g. tight crop maps, reformat sub-region). Most '
        'lens-distortion STMaps address the full plate, so this gives '
        'back the full source format -- not useful.')
    grp.addKnob(kbs_space)

    # ---- formats (source -> target, supports resolution change) ----
    grp.addKnob(nuke.Text_Knob('sec_fmt', '<b>Formats</b>'))
    grp.addKnob(nuke.Text_Knob('fmt_help', '',
        '<span style="color:#888; font-size:10px; font-style:italic;">'
        'Source = the resolution the STMap\'s UVs are normalized against (usually<br>'
        'the CG plate). Target = where the distorted output lives. When the<br>'
        'STMap rescales (e.g. CG 4K &rarr; delivery 2K), set these differently.</span>'))

    kfm = nuke.Enumeration_Knob('fmt_mode', 'mode',
        ['auto', 'manual'])
    kfm.setValue('auto')
    kfm.setTooltip('auto: source = img format, target = stmap format. '
                   'manual: use the four width/height knobs below.')
    grp.addKnob(kfm)

    ksw = nuke.Int_Knob('source_w', 'source W')
    ksw.setRange(1, 16384); ksw.setValue(2048); ksw.setFlag(nuke.STARTLINE)
    grp.addKnob(ksw)
    kshk = nuke.Int_Knob('source_h', 'H')
    kshk.setRange(1, 16384); kshk.setValue(1556); kshk.clearFlag(nuke.STARTLINE)
    grp.addKnob(kshk)

    ktw = nuke.Int_Knob('target_w', 'target W')
    ktw.setRange(1, 16384); ktw.setValue(2048); ktw.setFlag(nuke.STARTLINE)
    grp.addKnob(ktw)
    kth = nuke.Int_Knob('target_h', 'H')
    kth.setRange(1, 16384); kth.setValue(1556); kth.clearFlag(nuke.STARTLINE)
    grp.addKnob(kth)

    kfp = nuke.PyScript_Knob('fmt_pickup', 'Pick Up Formats')
    kfp.setFlag(nuke.STARTLINE)
    kfp.setTooltip('Copy current input formats into the manual fields, then '
                   'switch mode to manual. Useful for capturing formats that '
                   'might change later, or for editing them by hand.')
    kfp.setValue(
        'g = nuke.thisNode()\n'
        'i0 = g.input(0); i1 = g.input(1)\n'
        'if i0 is not None:\n'
        '    f = i0.format()\n'
        '    g["source_w"].setValue(int(f.width()))\n'
        '    g["source_h"].setValue(int(f.height()))\n'
        'if i1 is not None:\n'
        '    f = i1.format()\n'
        '    g["target_w"].setValue(int(f.width()))\n'
        '    g["target_h"].setValue(int(f.height()))\n'
        'g["fmt_mode"].setValue("manual")\n'
        'print("STMapBBox: picked up formats, switched to manual mode")\n'
    )
    grp.addKnob(kfp)

    # ---- trim (twirly, closed by default) ----
    grp.addKnob(nuke.Tab_Knob('trim_group', 'Edge Trim', nuke.TABBEGINCLOSEDGROUP))
    grp.addKnob(nuke.Text_Knob('trim_help', '',
        '<span style="color:#888; font-size:10px; font-style:italic;">'
        'Pixels to ignore at each edge of the STMap before sampling.<br>'
        'Use to skip ringing or black bleed at edges.</span>'))
    ktl = nuke.Int_Knob('trim_left',   'left');   ktl.setRange(0, 64); ktl.setValue(0); grp.addKnob(ktl)
    ktr = nuke.Int_Knob('trim_right',  'right');  ktr.setRange(0, 64); ktr.setValue(0); grp.addKnob(ktr)
    ktb = nuke.Int_Knob('trim_bottom', 'bottom'); ktb.setRange(0, 64); ktb.setValue(0); grp.addKnob(ktb)
    ktt = nuke.Int_Knob('trim_top',    'top');    ktt.setRange(0, 64); ktt.setValue(0); grp.addKnob(ktt)
    grp.addKnob(nuke.Tab_Knob('trim_group_end', '', nuke.TABENDGROUP))

    # ---- notes ----
    notes = (
        '<p style="color:#b8b8b8; font-size:11px; line-height:1.4;">'
        'Predicts the bbox of the distorted CG: for each pixel on a grid across<br>'
        'the STMap, samples (U, V), checks whether (U&middot;src_w, V&middot;src_h) lands inside the<br>'
        '<b>img</b> input\'s bbox X. The set of output pixels that pass this test is<br>'
        'the <i>active region</i> Y &mdash; where the distorted CG will have content. Y lives<br>'
        'in the <i>target</i> plate\'s pixel space and is written to the bbox knobs and<br>'
        'the internal Crop.<br><br>'
        '<b>Inputs</b><br>'
        '&nbsp;&nbsp;<i>img</i> (input 0) &mdash; the CG render whose bbox is being updated.<br>'
        '&nbsp;&nbsp;Its current bbox X drives the test.<br>'
        '&nbsp;&nbsp;<i>stmap</i> (input 1) &mdash; the STMap that will be applied downstream.<br><br>'
        '<b>Resolution change</b> &mdash; if your STMap retargets (e.g. CG at 4K &rarr; delivery<br>'
        'at 2K, or anamorphic squeeze/stretch), the source and target formats differ. Default<br>'
        '<i>auto</i> mode reads source from <b>img</b> and target from <b>stmap</b>, which works<br>'
        'as long as both inputs sit at their natural resolution. For unusual setups<br>'
        '(manually reformatted upstream, target format differs from STMap container, etc.) flip<br>'
        'to <i>manual</i> and set the four numbers; <i>Pick Up Formats</i> seeds them from<br>'
        'the current inputs.<br><br>'
        '<b>Y can grow past X</b> &mdash; that\'s the whole point. The internal Crop is<br>'
        'configured with <i>intersect=False</i> on Compute so the new bbox isn\'t clipped to<br>'
        'the original. Pixels in Y but outside X get the default Crop fill (black)<br>'
        'since there is no original CG data there.<br><br>'
        '<b>Channels</b><br>'
        'Default reads rgba.red / rgba.green. If your STMap stores UVs in forward.u / forward.v<br>'
        '(or backward.*), change the channel knobs to point at those instead. Test Inputs<br>'
        'samples all three pairs so you can see which one has the real STMap data.<br><br>'
        '<b>Grid resolution</b><br>'
        'The boundary of Y is uncertain to within one grid cell, so the tool adds<br>'
        '<i>max(step_x, step_y)</i> to the safety margin automatically. Higher resolution &rarr; tighter<br>'
        'bbox &rarr; slower compute. 96 is fine for most lens distortion; bump to 256<br>'
        'for tight optimisation passes. <i>bake grid</i> is a separate, lower default (48) used<br>'
        'only during Bake to keep it fast.<br><br>'
        '<b>Animation</b><br>'
        '&nbsp;&nbsp;<i>Bake</i> &mdash; runs Compute at every frame in [first, last] step N and<br>'
        '&nbsp;&nbsp;writes keyframes onto the bbox knobs and the internal Crop. Existing keys<br>'
        '&nbsp;&nbsp;are cleared first. Drives the same UI cycle as live_update so values match.<br>'
        '&nbsp;&nbsp;Use once the shot is locked.<br>'
        '&nbsp;&nbsp;<i>Live update</i> &mdash; recomputes on every frame change. Throttled to one<br>'
        '&nbsp;&nbsp;compute per frame. Useful while iterating or scrubbing; switch off (or bake)<br>'
        '&nbsp;&nbsp;before playback.<br>'
        '&nbsp;&nbsp;<i>Extract External Crop</i> &mdash; copies the current bbox values and animation<br>'
        '&nbsp;&nbsp;curves to a new standalone Crop node in the parent graph. The new node<br>'
        '&nbsp;&nbsp;is disconnected so you can wire it wherever you need.</p>'
    )
    grp.addKnob(nuke.Tab_Knob('notes_group', 'Notes', nuke.TABBEGINCLOSEDGROUP))
    grp.addKnob(nuke.Text_Knob('notes_body', '', notes))
    grp.addKnob(nuke.Tab_Knob('notes_group_end', '', nuke.TABENDGROUP))

    # ---- credit ----
    grp.addKnob(nuke.Text_Knob('sec_div', ''))
    credit = (
        '<p style="color:#777; font-size:10px; margin:0;">'
        'Marten Blumen &middot; '
        '<a href="https://github.com/bratgot/STMapBBox" style="color:#888;">'
        'github.com/bratgot/STMapBBox</a> '
        '&middot; v4.0</p>'
    )
    grp.addKnob(nuke.Text_Knob('credit', '', credit))

    return grp


if __name__ == '__main__' or 'nuke' in dir():
    try:
        node = build_stmap_bbox()
        print('STMapBBox created: ' + node.name())
    except Exception as e:
        print('STMapBBox install failed: ' + str(e))
        raise
