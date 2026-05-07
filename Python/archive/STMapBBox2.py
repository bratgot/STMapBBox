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

Repo: https://github.com/bratgot/STMapExtension
"""
import nuke


COMPUTE_CODE = r'''
import nuke

n   = nuke.thisNode()
cg  = n.input(0)   # img -- bbox we are updating
src = n.input(1)   # stmap -- analysis source

if cg is None:
    nuke.message("Connect the CG to input 0 (img).")
elif src is None:
    nuke.message("Connect the STMap to input 1 (stmap).")
else:
    # ---- read CG bbox X (the area that has CG content right now) ----
    cgb = cg.bbox()
    cg_x_min = int(cgb.x())
    cg_y_min = int(cgb.y())
    cg_x_max = cg_x_min + int(cgb.w()) - 1
    cg_y_max = cg_y_min + int(cgb.h()) - 1

    cg_fmt = cg.format()
    cg_w = int(cg_fmt.width()); cg_h = int(cg_fmt.height())

    if cg_x_max - cg_x_min < 1 or cg_y_max - cg_y_min < 1:
        nuke.message("CG (input 0) has an empty bbox.")
    else:
        # ---- read knobs ----
        u_chan = n["u_channel"].value() or "rgba.red"
        v_chan = n["v_channel"].value() or "rgba.green"
        space  = n["uv_space"].value()
        margin = int(n["bbox_margin"].value())
        grid_n = max(16, int(n["grid_resolution"].value()))
        tL = int(n["trim_left"].value());   tR = int(n["trim_right"].value())
        tB = int(n["trim_bottom"].value()); tT = int(n["trim_top"].value())

        # ---- STMap sampling region ----
        sb  = src.bbox()
        sx0 = int(sb.x()) + max(0, tL)
        sy0 = int(sb.y()) + max(0, tB)
        sx1 = int(sb.x()) + int(sb.w()) - 1 - max(0, tR)
        sy1 = int(sb.y()) + int(sb.h()) - 1 - max(0, tT)

        if sx1 - sx0 < 4 or sy1 - sy0 < 4:
            nuke.message("STMap region (after trim) too small.")
        else:
            step_x = max(1, (sx1 - sx0) // (grid_n - 1))
            step_y = max(1, (sy1 - sy0) // (grid_n - 1))

            # ---- detect UV space from a sparse probe ----
            if space == "auto":
                test_max = 0.0
                for tx in (sx0, (sx0 + sx1) // 2, sx1):
                    for ty in (sy0, (sy0 + sy1) // 2, sy1):
                        try:
                            uu = src.sample(u_chan, tx + 0.5, ty + 0.5)
                            vv = src.sample(v_chan, tx + 0.5, ty + 0.5)
                            if abs(uu) > test_max: test_max = abs(uu)
                            if abs(vv) > test_max: test_max = abs(vv)
                        except Exception:
                            pass
                use_space = "absolute (CG pixels)" if test_max > 5.0 else "normalized 0-1"
            else:
                use_space = space
            is_absolute = (use_space == "absolute (CG pixels)")

            # ---- grid walk: which output pixels sample inside CG bbox X ----
            amin_x = amin_y =  10**9
            amax_x = amax_y = -10**9
            n_active = 0; n_total = 0

            x_iter = list(range(sx0, sx1 + 1, step_x))
            if x_iter[-1] != sx1: x_iter.append(sx1)
            y_iter = list(range(sy0, sy1 + 1, step_y))
            if y_iter[-1] != sy1: y_iter.append(sy1)

            for x in x_iter:
                for y in y_iter:
                    n_total += 1
                    try:
                        u = src.sample(u_chan, x + 0.5, y + 0.5)
                        v = src.sample(v_chan, x + 0.5, y + 0.5)
                    except Exception:
                        continue
                    if is_absolute:
                        u_pix = u; v_pix = v
                    else:
                        u_pix = u * cg_w; v_pix = v * cg_h
                    if (cg_x_min <= u_pix <= cg_x_max and
                        cg_y_min <= v_pix <= cg_y_max):
                        if x < amin_x: amin_x = x
                        if x > amax_x: amax_x = x
                        if y < amin_y: amin_y = y
                        if y > amax_y: amax_y = y
                        n_active += 1

            if n_active == 0:
                nuke.message(
                    "No active output pixels found.\n\n"
                    "The STMap's UVs never land inside the CG bbox. "
                    "Possible reasons:\n"
                    "  - Wrong U/V channels (try forward.u/forward.v)\n"
                    "  - Wrong UV space (try the override knob)\n"
                    "  - The STMap really doesn't address this CG\n"
                    "Press Test Inputs to see raw sample values."
                )
            else:
                # margin = grid spacing (uncertainty between samples) + user margin
                grid_margin = max(step_x, step_y)
                total_margin = grid_margin + margin

                # final bbox in STMap output space
                src_fmt = src.format()
                src_w = int(src_fmt.width()); src_h = int(src_fmt.height())

                ax0 = max(0,     amin_x - total_margin)
                ay0 = max(0,     amin_y - total_margin)
                ax1 = min(src_w, amax_x + total_margin)
                ay1 = min(src_h, amax_y + total_margin)

                # write bbox knobs (knobChanged callback mirrors to internal Crop)
                n["cg_bbox_x"].setValue(ax0); n["cg_bbox_y"].setValue(ay0)
                n["cg_bbox_r"].setValue(ax1); n["cg_bbox_t"].setValue(ay1)

                # belt-and-suspenders: write directly + flip intersect off so
                # the bbox can grow past the input
                cn = n.node("cg_crop")
                if cn is not None:
                    try: cn["intersect"].setValue(False)
                    except Exception: pass
                    cn["box"].setValue([float(ax0), float(ay0),
                                        float(ax1), float(ay1)])

                ow = ax1 - ax0; oh = ay1 - ay0
                iw = cg_x_max - cg_x_min + 1; ih = cg_y_max - cg_y_min + 1

                n["bbox_report"].setValue(
                    '<p style="margin:0; color:#cde; font-size:11px;">'
                    '<b>New bbox</b> (post-distortion)&nbsp; x=[%d, %d]&nbsp; '
                    'y=[%d, %d]&nbsp; %d&times;%d<br>'
                    '<span style="color:#888;">'
                    'CG was %d&times;%d at [%d, %d]&ndash;[%d, %d] &middot; '
                    'active samples %d/%d (%.1f%%) &middot; '
                    'UV space: <b>%s</b></span></p>'
                    % (ax0, ax1, ay0, ay1, ow, oh,
                       iw, ih, cg_x_min, cg_y_min, cg_x_max, cg_y_max,
                       n_active, n_total, 100.0 * n_active / n_total,
                       use_space)
                )

                print("=== STMapBBox (post-distortion bbox) ===========")
                print("CG node:           %s" % cg.name())
                print("CG bbox in:        x=[%d, %d]  y=[%d, %d]  (%dx%d)"
                      % (cg_x_min, cg_x_max, cg_y_min, cg_y_max, iw, ih))
                print("CG format:         %dx%d" % (cg_w, cg_h))
                print("STMap node:        %s" % src.name())
                print("Sample region:     x=[%d, %d]  y=[%d, %d]"
                      % (sx0, sx1, sy0, sy1))
                print("Sample grid:       %dx%d  (steps %d, %d)"
                      % (len(x_iter), len(y_iter), step_x, step_y))
                print("Channels:          U=%s  V=%s" % (u_chan, v_chan))
                print("UV space:          %s" % use_space)
                print("Active samples:    %d / %d  (%.1f%%)"
                      % (n_active, n_total, 100.0 * n_active / n_total))
                print("Active raw bbox:   x=[%d, %d]  y=[%d, %d]"
                      % (amin_x, amax_x, amin_y, amax_y))
                print("Margin:            %d px  (grid %d + safety %d)"
                      % (total_margin, grid_margin, margin))
                print("New bbox out:      x=[%d, %d]  y=[%d, %d]  (%dx%d)"
                      % (ax0, ax1, ay0, ay1, ow, oh))
                print("================================================")
'''


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


BAKE_CODE = r'''
import nuke

n = nuke.thisNode()
cg = n.input(0); src = n.input(1)
if cg is None or src is None:
    nuke.message("Connect both inputs first.")
else:
    first = int(n["bake_first"].value())
    last  = int(n["bake_last"].value())
    step  = max(1, int(n["bake_step"].value()))

    if last < first:
        nuke.message("Last frame must be >= first frame.")
    else:
        cn = n.node("cg_crop")

        # clear any prior animation, set fresh animated state
        for nm in ("cg_bbox_x", "cg_bbox_y", "cg_bbox_r", "cg_bbox_t"):
            k = n[nm]
            if k.isAnimated():
                k.clearAnimated()
            k.setAnimated()
        if cn is not None:
            for ch in (0, 1, 2, 3):
                if cn["box"].isAnimated(ch):
                    cn["box"].clearAnimated(ch)
                cn["box"].setAnimated(ch)

        saved_frame = nuke.frame()
        was_live = bool(n.knob("live_update") and n["live_update"].value())
        if was_live:
            n["live_update"].setValue(False)

        try:
            frames = list(range(first, last + 1, step))
            task = nuke.ProgressTask("Baking STMapBBox keyframes")
            try:
                for i, frame in enumerate(frames):
                    if task.isCancelled():
                        break
                    task.setProgress(int(100.0 * i / max(1, len(frames))))
                    task.setMessage("Frame %d / %d  (%d of %d)" %
                                    (frame, last, i + 1, len(frames)))

                    nuke.frame(frame)
                    # compute calls setValue() on cg_bbox_* and cn["box"];
                    # both are animated now, so each setValue creates a key
                    # at the current frame
                    n["compute"].execute()
            finally:
                del task
        finally:
            nuke.frame(saved_frame)
            if was_live:
                n["live_update"].setValue(True)

        print("STMapBBox: baked frames %d-%d step %d (%d keyframes)" %
              (first, last, step, len(frames)))
'''


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


# Per-node updateUI: fires on UI updates (frame change, knob edits...).
# Throttled to one compute per frame via _live_last_frame.
UPDATE_UI_CODE = '''
g = nuke.thisNode()
if not (g.knob("live_update") and g["live_update"].value()):
    pass
elif g.input(0) is None or g.input(1) is None:
    pass
else:
    cur = int(nuke.frame())
    lf = g.knob("_live_last_frame")
    last = int(lf.value()) if lf is not None else -10**9
    if cur != last:
        if lf is not None:
            lf.setValue(cur)
        try:
            g["compute"].execute()
        except Exception:
            pass
'''


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
        'For each pixel on a grid across <b>stmap</b>, samples (U, V) and '
        'tests whether it lands inside <b>img</b>\'s bbox. The bbox of all '
        'hits is the new (distorted) CG bbox.</span></p>'
    )
    grp.addKnob(nuke.Text_Knob('title_hdr', '', title))

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

    # ---- trim (twirly, closed by default) ----
    grp.addKnob(nuke.Tab_Knob('trim_group', 'Edge Trim', nuke.TABBEGINCLOSEDGROUP))
    grp.addKnob(nuke.Text_Knob('trim_help', '',
        '<span style="color:#888; font-size:10px; font-style:italic;">'
        'Pixels to ignore at each edge of the STMap before sampling. Use '
        'to skip ringing or black bleed at the edges.</span>'))
    ktl = nuke.Int_Knob('trim_left',   'left');   ktl.setRange(0, 64); ktl.setValue(0); grp.addKnob(ktl)
    ktr = nuke.Int_Knob('trim_right',  'right');  ktr.setRange(0, 64); ktr.setValue(0); grp.addKnob(ktr)
    ktb = nuke.Int_Knob('trim_bottom', 'bottom'); ktb.setRange(0, 64); ktb.setValue(0); grp.addKnob(ktb)
    ktt = nuke.Int_Knob('trim_top',    'top');    ktt.setRange(0, 64); ktt.setValue(0); grp.addKnob(ktt)
    grp.addKnob(nuke.Tab_Knob('trim_group_end', '', nuke.TABENDGROUP))

    # ---- analysis ----
    grp.addKnob(nuke.Text_Knob('sec_opts', '<b>Analysis</b>'))

    kg = nuke.Int_Knob('grid_resolution', 'grid resolution')
    kg.setRange(16, 512); kg.setValue(96)
    kg.setTooltip('Number of samples per axis (96 -> ~9216 sample pairs). '
                  'Higher = tighter bbox boundary. The grid spacing is added '
                  'to the safety margin automatically.')
    grp.addKnob(kg)

    km = nuke.Int_Knob('bbox_margin', 'extra margin')
    km.setRange(0, 256); km.setValue(2)
    km.setTooltip('Extra pixels added on top of the grid-spacing margin. '
                  'Bump up if you see clipping at the bbox edges.')
    grp.addKnob(km)

    # ---- compute / test / reset ----
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

    # ---- result ----
    grp.addKnob(nuke.Text_Knob('sec_out', '<b>Result</b>'))
    grp.addKnob(nuke.Text_Knob('bbox_report', '',
                               '<i style="color:#888;">(not computed)</i>'))

    defaults = [('cg_bbox_x', 'x', 0), ('cg_bbox_y', 'y', 0),
                ('cg_bbox_r', 'r', 16384), ('cg_bbox_t', 't', 16384)]
    for nm, lbl, val in defaults:
        k = nuke.Int_Knob(nm, lbl)
        k.setRange(0, 16384); k.setValue(val)
        grp.addKnob(k)

    grp.addKnob(nuke.Text_Knob('out_hint', '',
        '<span style="color:#888; font-size:10px; font-style:italic;">'
        'Mirrored onto the internal Crop. Compute switches Crop\'s intersect '
        'off so the bbox can grow past the input bbox.</span>'))

    grp['knobChanged'].setValue(SYNC_CB)

    # ---- animation ----
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
        'Bake = static keyframes (cheap playback). Live = recompute on each '
        'frame change (always current, slower).</span>'))

    # wire the per-node updateUI for live mode
    try:
        grp['updateUI'].setValue(UPDATE_UI_CODE)
    except Exception as e:
        print("STMapBBox: failed to set updateUI: " + str(e))

    # ---- notes ----
    notes = (
        '<p style="color:#b8b8b8; font-size:11px; line-height:1.4;">'
        'Predicts the bbox of the distorted CG: for each pixel on a grid '
        'across the STMap, samples (U, V), checks whether (U&middot;cg_w, '
        'V&middot;cg_h) lands inside the <b>img</b> input\'s bbox X. The '
        'set of output pixels that pass this test is the <i>active region</i> '
        'Y &mdash; where the distorted CG will have content. Y is written to '
        'the bbox knobs and to the internal Crop.<br><br>'
        '<b>Inputs</b><br>'
        '&nbsp;&nbsp;<i>img</i> (input 0) &mdash; the CG render whose bbox '
        'is being updated. Its current bbox X drives the test.<br>'
        '&nbsp;&nbsp;<i>stmap</i> (input 1) &mdash; the STMap that will be '
        'applied downstream.<br><br>'
        '<b>Y can grow past X</b> &mdash; that\'s the whole point. The '
        'internal Crop is configured with <i>intersect=False</i> on Compute '
        'so the new bbox isn\'t clipped to the original. Pixels in Y but '
        'outside X get the default Crop fill (black) since there is no '
        'original CG data there.<br><br>'
        '<b>Channels</b><br>'
        'Default reads rgba.red / rgba.green. If your STMap stores UVs in '
        'forward.u / forward.v (or backward.*), change the channel knobs '
        'to point at those instead. Test Inputs samples all three pairs '
        'so you can see which one has the real STMap data.<br><br>'
        '<b>Grid resolution</b><br>'
        'The boundary of Y is uncertain to within one grid cell, so the '
        'tool adds <i>max(step_x, step_y)</i> to the safety margin '
        'automatically. Higher resolution &rarr; tighter bbox &rarr; '
        'slower compute. 96 is fine for most lens distortion; bump to '
        '256 for tight optimisation passes.<br><br>'
        '<b>Animation</b><br>'
        '&nbsp;&nbsp;<i>Bake</i> &mdash; runs Compute at every frame in '
        '[first, last] step N and writes keyframes onto the bbox knobs and '
        'the internal Crop. Existing keys are cleared first. Use this once '
        'the shot is locked.<br>'
        '&nbsp;&nbsp;<i>Live update</i> &mdash; recomputes on every frame '
        'change. Throttled to one compute per frame. Useful while iterating '
        'or scrubbing; switch off (or bake) before playback.</p>'
    )
    grp.addKnob(nuke.Tab_Knob('notes_group', 'Notes', nuke.TABBEGINCLOSEDGROUP))
    grp.addKnob(nuke.Text_Knob('notes_body', '', notes))
    grp.addKnob(nuke.Tab_Knob('notes_group_end', '', nuke.TABENDGROUP))

    # ---- credit ----
    grp.addKnob(nuke.Text_Knob('sec_div', ''))
    credit = (
        '<p style="color:#777; font-size:10px; margin:0;">'
        'Marten Blumen &middot; '
        '<a href="https://github.com/bratgot/STMapExtension" style="color:#888;">'
        'github.com/bratgot/STMapExtension</a> '
        '&middot; v2.0</p>'
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