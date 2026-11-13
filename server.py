# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2,<2", "av>=13", "pillow>=10"]
# ///
"""storyboard-mcp: MCP server that lets an assistant *see* a video without ffmpeg.

Tools
  video_info           duration, fps, frame count, size, codec, rotation
  video_contact_sheet  one grid image summarising the video, choosing the sharpest
                       frame of each stretch (or evenly spaced ones), with filters
                       for blur, near-duplicates and spacing; labels optional
  video_frames         chosen frames (by number, time or % of the video) at full
                       size, optionally snapped to the sharpest frame nearby

How frames are chosen (video_contact_sheet)
  1. Pass 1 decodes the chosen range once and measures, for every frame, its
     sharpness (variance of the edge image of a small greyscale copy) and a tiny
     32x18 thumbnail used to detect near-duplicates. Nothing big stays in memory.
  2. The range is cut into stretches: `count` equal stretches, or one stretch per
     `interval_s` seconds / `interval_pct` % of the range. In each stretch one
     frame is picked: the sharpest (mode="sharpest") or the one nearest the middle
     (mode="even"). Frames below the sharpness threshold are never picked.
  3. Picks closer than `min_gap_s` to the previous pick, or that differ from it by
     less than `min_difference`, are dropped.
  4. Pass 2 decodes again and keeps only the picked frames at full size.

Every "where in the video" parameter exists in seconds and in percent
(start_s / start_pct, end_s / end_pct, interval_s / interval_pct, times_s /
positions_pct); the sharpness threshold exists as an absolute value
(min_sharpness) and as a percentile (min_sharpness_pct).

Decoding uses PyAV (bundled FFmpeg libraries). iPhone videos carry a rotation in
their metadata; frames are rotated to look the way they were filmed.

bin/ffmpeg is a copy of the FFmpeg command line tool for anything PyAV is not
the right fit for, built from the official sources by build-ffmpeg.sh (LGPL, see
bin/LICENSE-ffmpeg.md). `server.py --is-ffmpeg-updated` tells whether it is the
newest FFmpeg release on the official GitHub repository (github.com/FFmpeg/FFmpeg).
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

import av
from mcp.server.fastmcp import FastMCP
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageStat

mcp = FastMCP("storyboard")


# ----------------------------------------------------------------------------- helpers
def _open(path: str):
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(p)
    c = av.open(str(p))
    return c, c.streams.video[0]


def _rotation(st) -> int:
    try:
        r = st.metadata.get("rotate") if st.metadata else None
        if r:
            return int(r) % 360
    except Exception:
        pass
    for sd in getattr(st, "side_data", []) or []:  # display matrix (newer PyAV)
        rot = getattr(sd, "rotation", None)
        if rot is not None:
            return int(-rot) % 360
    return 0


def _offset(st) -> float:
    """Time of the stream start (containers often start at a small non-zero pts):
    every time reported by this server is measured from it, so the first frame is 0.0 s."""
    try:
        return float(st.start_time * st.time_base) if st.start_time is not None and st.time_base else 0.0
    except Exception:
        return 0.0


def _decode(path: str, start_s: float = 0.0, end_s: float | None = None):
    """Yield (frame_index, time_s, av.VideoFrame, rotation) for frames in [start_s, end_s]."""
    c, st = _open(path)
    rot = _rotation(st)
    tb = float(st.time_base) if st.time_base else 0.0
    fps = float(st.average_rate or 30)
    off = _offset(st)
    try:
        for i, fr in enumerate(c.decode(st)):
            t = float(fr.pts * tb) - off if fr.pts is not None else i / fps
            if t < start_s:
                continue
            if end_s is not None and t > end_s:
                break
            yield i, t, fr, rot
    finally:
        c.close()


def _around(path: str, t: float, window_s: float):
    """Seek near t and yield (index, time_s, frame, rot) for frames in [t - window_s, t + window_s].
    Frame index is estimated from the time (seeking does not give it)."""
    c, st = _open(path)
    rot = _rotation(st)
    tb = float(st.time_base) if st.time_base else 0.0
    fps = float(st.average_rate or 30)
    off = _offset(st)
    lo, hi = max(0.0, t - window_s), t + max(window_s, 0.5 / fps)
    try:
        if tb:
            c.seek(int((max(0.0, lo - 1.0) + off) / tb), stream=st, backward=True, any_frame=False)
        last, given = None, False
        for fr in c.decode(st):
            ft = float(fr.pts * tb) - off if fr.pts is not None else 0.0
            if ft < lo - 0.5 / fps:
                last = (ft, fr); continue
            if ft > hi:
                if not given:                     # nothing inside the window (e.g. t=0 but the video
                    yield round(ft * fps), ft, fr, rot   # starts a bit later): give the next frame
                    given = True
                break
            yield round(ft * fps), ft, fr, rot
            given, last = True, None
        if not given and last is not None:        # t beyond the last frame: give the last one
            yield round(last[0] * fps), last[0], last[1], rot
    finally:
        c.close()


def _to_image(fr, rot: int) -> Image.Image:
    im = fr.to_image()
    return im.rotate(-rot, expand=True) if rot else im


def _sharpness(im: Image.Image, analysis_width: int) -> float:
    g = im.convert("L")
    if g.width > analysis_width:
        g = g.resize((analysis_width, round(g.height * analysis_width / g.width)))
    return ImageStat.Stat(g.filter(ImageFilter.FIND_EDGES)).var[0]


def _difference(a: Image.Image, b: Image.Image) -> float:
    return ImageStat.Stat(ImageChops.difference(a, b)).mean[0]


def _percentile(values: list[float], pct: float) -> float:
    v = sorted(values)
    if not v:
        return 0.0
    k = max(0, min(len(v) - 1, round(pct / 100 * (len(v) - 1))))
    return v[k]


def _font(size: int):
    for f in ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/System/Library/Fonts/Helvetica.ttc",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "C:/Windows/Fonts/arialbd.ttf"):
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            pass
    return ImageFont.load_default()


def _rgb(color: str) -> tuple[int, int, int]:
    c = color.lstrip("#")
    return tuple(int(c[k:k + 2], 16) for k in (0, 2, 4))  # type: ignore[return-value]


def _save(im: Image.Image, path: Path, quality: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() in (".jpg", ".jpeg"):
        im.convert("RGB").save(path, quality=quality)
    else:
        im.save(path)


def _range(dur: float, start_s, end_s, start_pct, end_pct) -> tuple[float, float]:
    t0 = start_s if start_s is not None else (dur * start_pct / 100 if start_pct is not None else 0.0)
    t1 = end_s if end_s is not None else (dur * end_pct / 100 if end_pct is not None else dur)
    t0, t1 = max(0.0, t0), min(dur, t1) if dur else t1
    if t1 <= t0:
        raise ValueError(f"empty range: start {t0:.2f}s >= end {t1:.2f}s")
    return t0, t1


# ----------------------------------------------------------------------------- tools
@mcp.tool()
def video_info(video_path: str) -> str:
    """Duration, fps, number of frames, size, codec and rotation of a video file."""
    c, st = _open(video_path)
    fps = float(st.average_rate) if st.average_rate else None
    dur = float(st.duration * st.time_base) if st.duration else (c.duration / 1e6 if c.duration else None)
    n = st.frames or (round(dur * fps) if dur and fps else None)
    rot = _rotation(st)
    w, h = st.codec_context.width, st.codec_context.height
    c.close()
    return json.dumps({"file": video_path, "duration_s": dur, "fps": fps, "frames": n,
                       "size": [w, h], "rotation_deg": rot,
                       "displayed_size": [h, w] if rot in (90, 270) else [w, h],
                       "codec": st.codec_context.name}, indent=1)


@mcp.tool()
def video_contact_sheet(
    video_path: str,
    out_png: str,
    count: int = 20,
    interval_s: float | None = None,
    interval_pct: float | None = None,
    mode: str = "sharpest",
    step_pct: float | None = None,
    step_s: float | None = None,
    positions_pct: list[float] | None = None,
    positions_s: list[float] | None = None,
    sharpest_within_s: float = 0.0,
    start_s: float | None = None,
    end_s: float | None = None,
    start_pct: float | None = None,
    end_pct: float | None = None,
    min_sharpness: float = 0.0,
    min_sharpness_pct: float = 0.0,
    min_gap_s: float = 0.0,
    min_difference: float = 0.0,
    columns: int = 4,
    thumb_width: int = 480,
    gap_px: int = 0,
    background: str = "#141414",
    label: bool = True,
    label_fields: list[str] | None = None,
    label_template: str | None = None,
    label_size: int | None = None,
    both_versions: bool = False,
    header: bool = True,
    footer: bool = True,
    sharpen: float = 0.5,
    analysis_width: int = 480,
    jpeg_quality: int = 90,
    save_frames_dir: str | None = None,
) -> str:
    """Grid image summarising a video.

    How many frames / where
      count            number of equal stretches (one frame each). Default 20.
      interval_s       instead of count: one stretch every N seconds.
      interval_pct     instead of count: one stretch every N % of the range (10 -> 10 frames).
      start_s, end_s   only look at this part of the video, in seconds ...
      start_pct, end_pct  ... or in % of the duration (e.g. 25 and 75 = middle half).
      mode             "sharpest" (least blurry frame of each stretch, default) or
                       "even" (frame nearest the middle of each stretch).

    Exact positions (computed by the server from the video duration; the video is
    not decoded in full, it seeks to each position, so it is fast on long films)
      step_pct         one frame every N % of the range, ends included:
                       10 -> 0 %, 10 %, 20 % ... 100 % (11 frames).
      step_s           the same in seconds: 30 -> 0 s, 30 s, 60 s ... end.
      positions_pct    explicit list of positions in % (e.g. [5, 50, 95]).
      positions_s      explicit list of times in seconds.
      sharpest_within_s  with any of the above: use the sharpest frame within
                       ± N seconds of each position instead of the exact one.
      (count / interval / mode / min_* filters do not apply in this mode.)

    Filters
      min_sharpness      absolute threshold; frames below are never picked.
      min_sharpness_pct  percentile threshold: 30 = ignore the blurriest 30 % of frames.
      min_gap_s          drop a pick closer than N seconds to the previous one.
      min_difference     drop a pick that looks almost like the previous one (mean
                         absolute difference 0-255 on a tiny thumbnail; ~4-8 removes
                         near-duplicates of a static shot).
      analysis_width     width used to measure sharpness (bigger = finer, slower).

    Layout
      columns, thumb_width (px), gap_px (space between and around tiles, px),
      background (#rrggbb), jpeg_quality (when out_png ends in .jpg; a .png
      out_png is written lossless).
      header        band on top with the video: file, size, fps, duration,
                    frames, codec. Default True.
      footer        band at the bottom with what the sheet shows: frames shown,
                    range, frames measured, sharpness min/median/max, dropped
                    frames and the threshold used. Default True.
      sharpen       unsharp-mask strength after downscaling each frame to its
                    tile (0 = off, 0.5 default, 1 = strong); keeps thumbnails
                    crisp instead of soft.

    Labels
      label         True/False: draw the label bar on each tile.
      label_fields  which fields, in order, from: "index" (1, 2, 3 ... position in
                    the sheet), "frame" (#123, frame number), "time" (4.1s),
                    "pct" (position in the video, 37%), "sharpness" (value),
                    "sharpness_pct" (sharpness relative to the sharpest picked frame),
                    "anchor" (primer/último fotograma, only on 0 % / 100 %).
                    Default ["frame", "time", "sharpness", "anchor"].
      label_template free text with placeholders instead of label_fields, e.g.
                    "{index}. {pct}% · fotograma {frame}" or "{time_hms}  #{frame}".
                    Placeholders: {index} {frame} {frames_total} {time} (seconds,
                    1 decimal) {time_hms} (h:mm:ss.s) {pct} (0 decimals) {pct1}
                    (1 decimal) {sharpness} {sharpness_pct} {anchor}.
      label_size    font size in px.
      both_versions also write the other version next to it
                    (<name>_labels.<ext> and <name>_sin_labels.<ext>).

    save_frames_dir also writes the picked frames at full size as f<frame>.jpg.
    Returns JSON: sheet path(s), picked frames (frame, time_s, pct, sharpness,
    sharpness_pct), dropped frames with the reason, and sharpness statistics
    (useful to choose min_sharpness).
    """
    if mode not in ("sharpest", "even"):
        raise ValueError('mode must be "sharpest" or "even"')
    fields = label_fields or ["frame", "time", "sharpness", "anchor"]
    bad = set(fields) - {"index", "frame", "time", "pct", "sharpness", "sharpness_pct", "anchor"}
    if bad:
        raise ValueError(f"unknown label_fields: {sorted(bad)}")
    if label_template:
        fields = list(fields) + ["sharpness"] if "{sharpness" in label_template else list(fields)
    info = json.loads(video_info(video_path))
    dur = info["duration_s"] or 0.0
    t0, t1 = _range(dur, start_s, end_s, start_pct, end_pct)

    exact = bool(step_pct or step_s or positions_pct or positions_s)
    anchors: dict[int, str] = {}
    if exact:
        targets: list[float] = []
        if step_pct and step_pct > 0:
            n = int(math.floor(100 / step_pct + 1e-9))
            targets += [t0 + (t1 - t0) * min(100.0, k * step_pct) / 100 for k in range(n + 1)]
        if step_s and step_s > 0:
            n = int(math.floor((t1 - t0) / step_s + 1e-9))
            targets += [t0 + k * step_s for k in range(n + 1)]
        targets += [dur * p / 100 for p in (positions_pct or [])]
        targets += list(positions_s or [])
        n_frames = info["frames"] or 0
        fps = info["fps"] or 30.0
        last_t = (n_frames - 1) / fps if n_frames else dur      # time of the last frame
        targets = sorted({round(min(max(t, 0.0), last_t), 4) for t in targets})
        meta, full = [], {}
        for tg in targets:
            best = None
            if tg <= 0.0 and sharpest_within_s <= 0:            # 0 % -> the first frame, exactly
                for i, t, fr, rot in _decode(video_path):
                    im = _to_image(fr, rot)
                    best = (0, 0, 0.0, _sharpness(im, analysis_width), im); break
                anchors[0] = "first_frame"
            elif tg >= last_t - 0.5 / fps and sharpest_within_s <= 0:   # 100 % -> the last frame, exactly
                for i, t, fr, rot in _around(video_path, last_t, 1.0 / fps):
                    im = _to_image(fr, rot)
                    best = (0, max(0, n_frames - 1), last_t, _sharpness(im, analysis_width), im)
                anchors[max(0, n_frames - 1)] = "last_frame"
            for i, t, fr, rot in ([] if best else _around(video_path, tg, sharpest_within_s)):
                im = _to_image(fr, rot)
                sc = _sharpness(im, analysis_width)
                key = sc if sharpest_within_s > 0 else -abs(t - tg)
                if best is None or key > best[0]:
                    best = (key, i, t, sc, im)
            if best and best[1] not in full:
                _, i, t, sc, im = best
                meta.append((i, t, sc, None)); full[i] = im.convert("RGB")
        if not meta:
            raise ValueError("no frames found at the requested positions")
        kept, dropped, thr = meta, [], 0.0
    else:
        kept, dropped, thr, meta, full = _select(video_path, t0, t1, fields, mode, count, interval_s, interval_pct,
                                                 min_sharpness, min_sharpness_pct, min_gap_s, min_difference,
                                                 analysis_width)
    if save_frames_dir:
        d = Path(save_frames_dir).expanduser()
        for i, im in full.items():
            _save(im, d / f"f{i:05d}.jpg", 95)

    order = [m for m in kept if m[0] in full]
    top = max((m[2] for m in order), default=0.0) or 1.0
    span = ((info["frames"] - 1) / info["fps"]) if info["frames"] and info["fps"] else dur   # 0 % = first frame, 100 % = last
    rows_out = [{"frame": i, "time_s": round(t, 2), "pct": round(min(100.0, 100 * t / span), 1) if span else None,
                 "sharpness": round(sc, 1), "sharpness_pct": round(100 * sc / top),
                 **({"anchor": anchors[i]} if i in anchors else {})} for i, t, sc, _ in order]
    for k, r in enumerate(rows_out, 1):
        r["index"] = k
    how = "posiciones exactas" if exact else ("más nítido por tramo" if mode == "sharpest" else "repartidos")
    return _sheet(order, full, rows_out, (fields, label_template), out_png, both_versions, label, columns,
                  thumb_width, gap_px, background, label_size, jpeg_quality, dropped, t0, t1, dur, meta, thr,
                  save_frames_dir, info, (header, footer, sharpen, how))


def _select(video_path, t0, t1, fields, mode, count, interval_s, interval_pct, min_sharpness, min_sharpness_pct,
            min_gap_s, min_difference, analysis_width):
    """Stretch-based selection (two passes). Returns kept, dropped, threshold, meta, full images."""
    need_sharp = mode == "sharpest" or min_sharpness > 0 or min_sharpness_pct > 0 \
        or any(f.startswith("sharpness") for f in fields)
    # pass 1: measure every frame in range (small copies only)
    meta: list[tuple[int, float, float, Image.Image | None]] = []
    for i, t, fr, rot in _decode(video_path, t0, t1):
        im = _to_image(fr, rot)
        sc = _sharpness(im, analysis_width) if need_sharp else 0.0
        tiny = im.convert("L").resize((32, 18)) if min_difference > 0 else None
        meta.append((i, t, sc, tiny))
    if not meta:
        raise ValueError("no frames in the requested range")

    thr = max(min_sharpness, _percentile([m[2] for m in meta], min_sharpness_pct) if min_sharpness_pct > 0 else 0.0)

    # stretches
    span = t1 - t0
    if interval_pct and interval_pct > 0:
        interval_s = span * interval_pct / 100
    if interval_s and interval_s > 0:
        nseg = max(1, math.ceil(span / interval_s - 1e-9))
        bounds = [t0 + k * interval_s for k in range(nseg)] + [t1 + 1e-6]
    else:
        nseg = max(1, count)
        bounds = [t0 + k * span / nseg for k in range(nseg)] + [t1 + 1e-6]
    seg: list[list] = [[] for _ in range(nseg)]
    k = 0
    for m in meta:
        while k < nseg - 1 and m[1] >= bounds[k + 1]:
            k += 1
        if m[2] >= thr:
            seg[k].append(m)

    picks = []
    for k, cand in enumerate(seg):
        if not cand:
            continue
        if mode == "sharpest":
            picks.append(max(cand, key=lambda m: m[2]))
        else:
            mid = (bounds[k] + bounds[k + 1]) / 2
            picks.append(min(cand, key=lambda m: abs(m[1] - mid)))

    kept, dropped = [], []
    for m in picks:
        if kept and min_gap_s > 0 and m[1] - kept[-1][1] < min_gap_s:
            dropped.append({"frame": m[0], "reason": "min_gap_s"}); continue
        if kept and min_difference > 0 and _difference(m[3], kept[-1][3]) < min_difference:
            dropped.append({"frame": m[0], "reason": "min_difference"}); continue
        kept.append(m)
    if not kept:
        raise ValueError("every frame was filtered out: lower min_sharpness(_pct) / min_difference / min_gap_s")

    # pass 2: grab the picked frames at full size
    want = {m[0] for m in kept}
    full: dict[int, Image.Image] = {}
    last = max(want)
    for i, t, fr, rot in _decode(video_path, t0, None):
        if i in want:
            full[i] = _to_image(fr, rot).convert("RGB")
        if i >= last:
            break
    return kept, dropped, thr, meta, full


def _hms(t: float) -> str:
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:04.1f}"


def _sheet(order, full, rows_out, fields, out_png, both_versions, label, columns, thumb_width, gap_px,
           background, label_size, jpeg_quality, dropped, t0, t1, dur, meta, thr, save_frames_dir, info,
           extras=(True, True, 0.5, "")) -> str:
    fields, template = fields
    header, footer, sharpen, how = extras
    sh = [m[2] for m in meta if m[2]]
    anchor_txt = {"first_frame": "primer fotograma", "last_frame": "último fotograma"}

    def text_for(r):
        t = r["time_s"]
        hms = f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:04.1f}"
        values = {"index": r["index"], "frame": r["frame"], "frames_total": info.get("frames"),
                  "time": f"{t:.1f}", "time_hms": hms, "pct": f"{r['pct']:.0f}", "pct1": f"{r['pct']:.1f}",
                  "sharpness": f"{r['sharpness']:.0f}", "sharpness_pct": r["sharpness_pct"],
                  "anchor": anchor_txt.get(r.get("anchor", ""), "")}
        if template:
            return template.format(**values).strip()
        parts = {"index": f"{r['index']}.", "frame": f"#{r['frame']}", "time": f"{t:.1f}s",
                 "pct": f"{r['pct']:.0f}%", "sharpness": f"nitidez {r['sharpness']:.0f}",
                 "sharpness_pct": f"nitidez {r['sharpness_pct']}%", "anchor": values["anchor"]}
        return "  ".join(parts[f] for f in fields if parts[f])

    def build(with_labels: bool) -> Image.Image:
        w0, h0 = full[order[0][0]].size
        tw, th = thumb_width, round(thumb_width * h0 / w0)
        cols = max(1, min(columns, len(order)))
        rows = math.ceil(len(order) / cols)
        sheet = Image.new("RGB", (cols * tw + (cols + 1) * gap_px, rows * th + (rows + 1) * gap_px), _rgb(background))
        dr = ImageDraw.Draw(sheet)
        fs = label_size or max(12, tw // 22)
        f = _font(fs)
        for j, ((i, *_), r) in enumerate(zip(order, rows_out)):
            x = gap_px + (j % cols) * (tw + gap_px)
            y = gap_px + (j // cols) * (th + gap_px)
            thumb = full[i].resize((tw, th), Image.LANCZOS)
            if sharpen > 0 and tw < full[i].width:          # downscaling softens: restore edge contrast
                thumb = thumb.filter(ImageFilter.UnsharpMask(radius=1.0, percent=int(120 * sharpen), threshold=2))
            sheet.paste(thumb, (x, y))
            if with_labels:
                txt, ff, size = text_for(r), f, fs
                while dr.textlength(txt, font=ff) > tw - 12 and size > max(9, fs * 0.6):   # shrink to fit the tile
                    size -= 1; ff = _font(size)
                while dr.textlength(txt, font=ff) > tw - 12 and len(txt) > 2:              # still too long: cut
                    txt = txt[:-2] + "…"
                dr.rectangle((x, y, x + tw, y + fs + 10), fill=(0, 0, 0))
                dr.text((x + 6, y + 4 + (fs - size) // 2), txt, font=ff, fill=(255, 220, 0))
        return frame_with_data(sheet)

    def frame_with_data(sheet: Image.Image) -> Image.Image:
        # The data the JSON already returns, on the image itself: the video on
        # top, what this sheet shows at the bottom.
        if not (header or footer):
            return sheet
        size = max(14, sheet.width // 70)
        big, small = _font(round(size * 1.25)), _font(size)
        pad, line = max(8, size // 2), round(size * 1.5)
        top_lines = []
        if header:
            w, h = info.get("displayed_size") or info.get("size") or [0, 0]
            parts = [f"{w}×{h}"] if w else []
            if info.get("fps"): parts.append(f"{info['fps']:.3g} fps")
            if dur: parts.append(_hms(dur))
            if info.get("frames"): parts.append(f"{info['frames']:,} fotogramas".replace(",", "."))
            if info.get("codec"): parts.append(info["codec"])
            top_lines = [(Path(info.get("file", "")).name, big), (" · ".join(parts), small)]
        bottom = []
        if footer:
            txt = [f"{len(rows_out)} fotogramas ({how})" if how else f"{len(rows_out)} fotogramas",
                   f"rango {_hms(t0)}–{_hms(t1)}" + (f" ({100 * t0 / dur:.0f}–{100 * t1 / dur:.0f} %)" if dur else ""),
                   f"medidos {len(meta):,}".replace(",", ".")]
            if sh:
                txt.append(f"nitidez mín {min(sh):.0f} · mediana {_percentile(sh, 50):.0f} · máx {max(sh):.0f}")
            if dropped: txt.append(f"descartados {len(dropped)}")
            if thr > 0: txt.append(f"umbral {thr:.0f}")
            bottom = [(" · ".join(txt), small)]
        head_h = (pad + round(size * 1.25) + pad // 2 + line + pad) if top_lines else 0
        foot_h = (pad + line + pad) if bottom else 0
        out = Image.new("RGB", (sheet.width, sheet.height + head_h + foot_h), _rgb(background))
        out.paste(sheet, (0, head_h))
        d = ImageDraw.Draw(out)
        y = pad
        for k, (text, font) in enumerate(top_lines):
            d.text((pad + gap_px, y), text, font=font, fill=(245, 245, 245) if k == 0 else (190, 190, 190))
            y += round(size * 1.25) + pad // 2 if k == 0 else line
        for text, font in bottom:
            while d.textlength(text, font=font) > sheet.width - 2 * pad and " · " in text:   # too long: drop from the end
                text = text.rsplit(" · ", 1)[0]
            d.text((pad + gap_px, head_h + sheet.height + pad), text, font=font, fill=(190, 190, 190))
        return out

    out = Path(out_png).expanduser()
    written = {}
    if both_versions:
        for flag, suffix in ((True, "_labels"), (False, "_sin_labels")):
            p = out.with_name(out.stem + suffix + out.suffix)
            _save(build(flag), p, jpeg_quality); written["con_labels" if flag else "sin_labels"] = str(p)
    else:
        _save(build(label), out, jpeg_quality); written["con_labels" if label else "sin_labels"] = str(out)

    return json.dumps({
        "images": written,
        "frames": rows_out,
        "dropped": dropped,
        "range_s": [round(t0, 2), round(t1, 2)],
        "range_pct": [round(100 * t0 / dur, 1), round(100 * t1 / dur, 1)] if dur else None,
        "frames_measured": len(meta),
        "sharpness_threshold_used": round(thr, 1),
        "sharpness_stats": {"min": round(min(sh), 1), "p25": round(_percentile(sh, 25), 1),
                            "median": round(_percentile(sh, 50), 1), "p75": round(_percentile(sh, 75), 1),
                            "max": round(max(sh), 1)} if sh else None,
        "frames_dir": save_frames_dir, "video": info}, indent=1)


@mcp.tool()
def video_frames(video_path: str, out_dir: str, frames: list[int] | None = None,
                 times_s: list[float] | None = None, positions_pct: list[float] | None = None,
                 sharpest_within_s: float = 0.0, analysis_width: int = 480, jpeg_quality: int = 95) -> str:
    """Save chosen frames at full resolution as JPEG (<out_dir>/f<frame>.jpg).

    frames             frame numbers.
    times_s            times in seconds.
    positions_pct      positions in % of the duration (0-100).
    sharpest_within_s  if > 0, each requested frame is replaced by the sharpest frame
                       within ± this many seconds of it (handy for handheld video).
    """
    info = json.loads(video_info(video_path))
    fps = info["fps"] or 30.0
    dur = info["duration_s"] or 0.0
    n = info["frames"] or 10**9
    req = set(frames or []) | {round(t * fps) for t in (times_s or [])} \
        | {round((n - 1) * p / 100) if n < 10**9 else round(dur * p / 100 * fps) for p in (positions_pct or [])}
    req = sorted(min(max(0, r), n - 1) for r in req)
    if not req:
        raise ValueError("give frames, times_s or positions_pct")
    win = round(sharpest_within_s * fps)
    lo, hi = max(0, req[0] - win), req[-1] + win
    best: dict[int, tuple[float, int, float, Image.Image]] = {}
    for i, t, fr, rot in _decode(video_path, max(0.0, lo / fps - 0.5), None):
        if i < lo:
            continue
        if i > hi:
            break
        targets = [r for r in req if abs(i - r) <= win]
        if not targets:
            continue
        im = _to_image(fr, rot)
        sc = _sharpness(im, analysis_width)
        for r in targets:
            if r not in best or (win and sc > best[r][0]) or (not win and i == r):
                best[r] = (sc, i, t, im)
    d = Path(out_dir).expanduser()
    saved = []
    for r in req:
        if r in best:
            sc, i, t, im = best[r]
            p = d / f"f{i:05d}.jpg"
            _save(im, p, jpeg_quality)
            saved.append({"requested": r, "frame": i, "time_s": round(t, 2),
                          "pct": round(100 * t / dur, 1) if dur else None,
                          "sharpness": round(sc, 1), "path": str(p)})
    return json.dumps({"saved": saved}, indent=1)


# ----------------------------------------------------------------------------- bundled ffmpeg
FFMPEG = Path(__file__).resolve().parent / "bin" / "ffmpeg"
FFMPEG_REPO = "FFmpeg/FFmpeg"  # official GitHub mirror of git.ffmpeg.org


def _version_key(tag: str) -> tuple[int, ...]:
    return tuple(int(x) for x in tag.lstrip("n").split("."))


def _bundled_ffmpeg_tag() -> str:
    """Release tag of bin/ffmpeg ("n9.0.2"), as `ffmpeg -version` reports it."""
    out = subprocess.run([str(FFMPEG), "-hide_banner", "-version"], capture_output=True,
                         text=True, timeout=30, check=True).stdout
    m = re.match(r"ffmpeg version (n?\d+(?:\.\d+)+)\b", out)
    if not m:
        first = out.splitlines()[0] if out else ""
        raise ValueError(f"`ffmpeg -version` no dice una versión publicada: {first!r}")
    v = m.group(1)
    return v if v.startswith("n") else "n" + v


def _latest_ffmpeg_tag() -> str:
    """Newest release tag on the official GitHub repository: nX.Y or nX.Y.Z, -dev tags left out.

    One request lists every tag starting with "n", so the check costs one of the 60
    requests an hour GitHub allows without a token (GITHUB_TOKEN is used if set)."""
    req = urllib.request.Request(
        f"https://api.github.com/repos/{FFMPEG_REPO}/git/matching-refs/tags/n",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "storyboard-mcp"})
    if os.environ.get("GITHUB_TOKEN"):
        req.add_header("Authorization", f"Bearer {os.environ['GITHUB_TOKEN']}")
    with urllib.request.urlopen(req, timeout=30) as r:
        refs = json.load(r)
    tags = [ref["ref"].rsplit("/", 1)[-1] for ref in refs]
    releases = [t for t in tags if re.fullmatch(r"n\d+\.\d+(?:\.\d+)?", t)]
    if not releases:
        raise ValueError("el repositorio no tiene etiquetas de versión publicadas")
    return max(releases, key=_version_key)


def is_ffmpeg_updated() -> int:
    """--is-ffmpeg-updated: 0 when bin/ffmpeg is the newest FFmpeg release on the official
    GitHub repository, 1 when there is a newer one, 2 when it cannot be told."""
    try:
        have = _bundled_ffmpeg_tag()
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        print(f"No se puede ejecutar {FFMPEG}: {e}", file=sys.stderr)
        return 2
    try:
        latest = _latest_ffmpeg_tag()
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"No se pudo consultar github.com/{FFMPEG_REPO}: {e}", file=sys.stderr)
        return 2
    if _version_key(have) >= _version_key(latest):
        print(f"bin/ffmpeg está al día: es {have}, la última versión publicada en "
              f"github.com/{FFMPEG_REPO}.")
        return 0
    print(f"Hay una versión más nueva: bin/ffmpeg es {have} y la última publicada en "
          f"github.com/{FFMPEG_REPO} es {latest}.\nPara actualizarlo: ./build-ffmpeg.sh {latest}")
    return 1


if __name__ == "__main__":
    if "--is-ffmpeg-updated" in sys.argv[1:]:
        sys.exit(is_ffmpeg_updated())
    mcp.run()
