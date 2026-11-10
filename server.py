# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2,<2", "av>=13", "pillow>=10"]
# ///
"""MCP server: look at a video (e.g. an iPhone .mov) without ffmpeg installed.

Tools:
  1. video_info           -> duration, fps, frame count, size, rotation
  2. video_contact_sheet  -> evenly spread frames (or the SHARPEST one in each
                             stretch of the video) pasted into one labelled image
                             (frame number + time) to see the whole video at once
  3. video_frames         -> save chosen frames (by number or time) at full size

Notes:
  * Decoding uses PyAV (bundled FFmpeg libraries), so nothing else to install.
  * iPhone videos carry a rotation in their metadata; frames are rotated so they
    look the way they were filmed.
  * "Sharpest" = highest variance of the edge image (a Laplacian-style blur
    measure): a handheld video has many motion-blurred frames, and picking evenly
    spaced ones often lands on the worst.
"""
from __future__ import annotations

import json
from pathlib import Path

import av
from mcp.server.fastmcp import FastMCP
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageStat

mcp = FastMCP("video")


def _open(path: str):
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(p)
    c = av.open(str(p))
    return c, c.streams.video[0]


def _rotation(st) -> int:
    try:
        r = st.metadata.get("rotate") if st.metadata else None
        if r: return int(r) % 360
    except Exception:
        pass
    for sd in getattr(st, "side_data", []) or []:          # display matrix (newer PyAV)
        rot = getattr(sd, "rotation", None)
        if rot is not None: return int(-rot) % 360
    return 0


def _image(frame, rot: int) -> Image.Image:
    im = frame.to_image()
    if rot: im = im.rotate(-rot, expand=True)
    return im


def _sharpness(im: Image.Image) -> float:
    g = im.convert("L"); g.thumbnail((480, 480))
    return ImageStat.Stat(g.filter(ImageFilter.FIND_EDGES)).var[0]


def _decode(path: str):
    """Yield (index, time_s, PIL image) for every frame."""
    c, st = _open(path)
    rot = _rotation(st)
    tb = float(st.time_base) if st.time_base else 0.0
    for i, fr in enumerate(c.decode(st)):
        t = float(fr.pts * tb) if fr.pts is not None else i / float(st.average_rate or 30)
        yield i, t, _image(fr, rot)
    c.close()


def _font(size: int):
    for f in ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/System/Library/Fonts/Helvetica.ttc"):
        try: return ImageFont.truetype(f, size)
        except OSError: pass
    return ImageFont.load_default()


@mcp.tool()
def video_info(video_path: str) -> str:
    """Duration, fps, number of frames, size and rotation of a video file."""
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
def video_contact_sheet(video_path: str, out_png: str, count: int = 20, columns: int = 4,
                        thumb_width: int = 480, sharpest: bool = True, save_frames_dir: str | None = None) -> str:
    """One image with `count` frames of the whole video in a grid, each labelled
    with its frame number and time. With sharpest=True (default) the video is cut
    into `count` equal stretches and the least blurry frame of each is used;
    sharpest=False takes evenly spaced frames. save_frames_dir also saves those
    frames at full size (f<frame>.jpg) so you can look at the good ones."""
    info = json.loads(video_info(video_path))
    n = info["frames"] or 1
    count = max(1, min(count, n))
    edges = [round(k * n / count) for k in range(count + 1)]
    best: list[tuple[float, int, float, Image.Image] | None] = [None] * count
    targets = {round((edges[k] + edges[k + 1] - 1) / 2): k for k in range(count)}
    k = 0
    for i, t, im in _decode(video_path):
        while k < count - 1 and i >= edges[k + 1]: k += 1
        if sharpest:
            sc = _sharpness(im)
            if best[k] is None or sc > best[k][0]: best[k] = (sc, i, t, im)
        elif i in targets:
            best[targets[i]] = (0.0, i, t, im)
    picks = [b for b in best if b]
    if save_frames_dir:
        d = Path(save_frames_dir).expanduser(); d.mkdir(parents=True, exist_ok=True)
        for _, i, _, im in picks: im.convert("RGB").save(d / f"f{i:05d}.jpg", quality=95)
    w0, h0 = picks[0][3].size
    tw, th = thumb_width, round(thumb_width * h0 / w0)
    rows = (len(picks) + columns - 1) // columns
    sheet = Image.new("RGB", (columns * tw, rows * th), (20, 20, 20))
    dr = ImageDraw.Draw(sheet); f = _font(max(12, tw // 22))
    for j, (sc, i, t, im) in enumerate(picks):
        x, y = (j % columns) * tw, (j // columns) * th
        sheet.paste(im.convert("RGB").resize((tw, th), Image.LANCZOS), (x, y))
        label = f"#{i}  {t:.1f}s" + (f"  nitidez {sc:.0f}" if sharpest else "")
        dr.rectangle((x, y, x + tw, y + f.size + 10), fill=(0, 0, 0))
        dr.text((x + 6, y + 4), label, font=f, fill=(255, 220, 0))
    out = Path(out_png).expanduser(); out.parent.mkdir(parents=True, exist_ok=True); sheet.save(out)
    return json.dumps({"png": str(out), "frames": [{"frame": i, "time_s": round(t, 2), "sharpness": round(sc, 1)}
                                                   for sc, i, t, _ in picks],
                       "frames_dir": save_frames_dir, "video": info}, indent=1)


@mcp.tool()
def video_frames(video_path: str, out_dir: str, frames: list[int] | None = None,
                 times_s: list[float] | None = None) -> str:
    """Save chosen frames at full resolution as JPEG (<out_dir>/f<frame>.jpg).
    Give frame numbers (`frames`) and/or times in seconds (`times_s`)."""
    info = json.loads(video_info(video_path))
    fps = info["fps"] or 30.0
    want = set(frames or []) | {round(t * fps) for t in (times_s or [])}
    if not want: raise ValueError("give frames or times_s")
    d = Path(out_dir).expanduser(); d.mkdir(parents=True, exist_ok=True)
    saved = []
    last = max(want)
    for i, t, im in _decode(video_path):
        if i in want:
            p = d / f"f{i:05d}.jpg"; im.convert("RGB").save(p, quality=95)
            saved.append({"frame": i, "time_s": round(t, 2), "path": str(p), "sharpness": round(_sharpness(im), 1)})
        if i >= last: break
    return json.dumps({"saved": saved}, indent=1)


if __name__ == "__main__":
    mcp.run()
