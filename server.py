# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.10,<2", "av>=14", "pillow>=10.1", "pillow-heif>=0.16", "pydantic>=2.7"]
# ///
"""storyboard-mcp: MCP server that turns a video, or a list of images, into pages of
panels an assistant can look at (it cannot play a video). No ffmpeg needed.

Tools (they all take the same JSON sections; only "input" is required)
  media_info       what the input is: the video's duration, fps, frames, size and
                   rotation, or each image's name, format and size
  make_storyboard  pages of panels: grid (contact sheet), storyboard (number above,
                   notes box below), list or strip; PNG / JPG pages or one PDF, plus
                   a shot list (JSON / CSV) and the full-size frames if asked
  extract_frames   chosen video frames at full size, or copies of chosen images
  get_schema       the JSON Schema of every tool with a minimal and a full example

Sections: input (video | images | folder), select (which frames / images become
panels), panels (notes per panel), layout, style, output.

The Pydantic models below are the single source of truth: the tools' input schemas,
the error messages, get_schema and schema.json (`server.py --schema`, checked by
tests/test_server.py) all come from them.

How frames are chosen (select)
  - stretches (default for video): the range is cut into `count` equal stretches and
    one frame is taken from each: the sharpest one (mode "sharpest") or the one in
    the middle ("even"). Every frame of the range is measured first (sharpness =
    variance of the edge image of a small grey copy), so filters can drop blurry
    frames (min_sharpness / drop_blurriest_pct), picks too close in time (min_gap_s)
    or near-duplicates of the previous pick (min_difference).
  - exact positions (step_pct, step_s, at_pct, at_s, at_frames, at_images,
    at_names): the server seeks to each one, without decoding the whole video.
  - panels[] with positions: each panel says which frame or image it shows.
Images work the same way, with the list order in place of time.

Decoding uses PyAV (bundled FFmpeg libraries). Phone videos store a rotation and
phone photos an EXIF orientation; pictures are turned to look the way they were shot.

bin/ffmpeg is a copy of the FFmpeg command line tool for anything PyAV is not
the right fit for, built from the official sources by build-ffmpeg.sh (LGPL, see
bin/LICENSE-ffmpeg.md). `server.py --is-ffmpeg-updated` tells whether it is the
newest FFmpeg release on the official GitHub repository (github.com/FFmpeg/FFmpeg).
"""
import base64
import csv
import difflib
import fnmatch
import hashlib
import io
import json
import math
import os
import re
import shutil
import string
import struct
import subprocess
import sys
import tempfile
import types as pytypes
import typing
import urllib.request
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Callable, Literal

import anyio
import av
import mcp.types as mt
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.stdio import stdio_server
from PIL import Image, ImageChops, ImageColor, ImageDraw, ImageFilter, ImageFont, ImageOps, ImageStat
from pydantic import (AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, ValidationError,
                      ValidationInfo, WithJsonSchema, field_validator, model_validator)

try:  # HEIC / HEIF photos (iPhone)
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF = True
except Exception:  # pragma: no cover - optional
    HEIF = False

VERSION = "2.0.0"
REPO = "https://github.com/Eliaxs1900/storyboard-mcp"
MAX_IMAGES = 1000                 # images in input.images / input.folder
MAX_PANELS = 500                  # panels in one call
MAX_PAGES = 200
MAX_PAGE_PIXELS = 150_000_000     # one page (e.g. 12 000 × 12 500 px)
Image.MAX_IMAGE_PIXELS = 200_000_000
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"} | (
    {".heic", ".heif"} if HEIF else set())
SUPPORTED_IMAGES = "jpg, png, webp, gif, bmp, tiff" + (", heic" if HEIF else "")

ASPECT_RATIOS = {"16:9": 16 / 9, "9:16": 9 / 16, "4:3": 4 / 3, "3:4": 3 / 4, "1:1": 1.0, "4:5": 0.8,
                 "1.85:1": 1.85, "2:1": 2.0, "2.39:1": 2.39}
Aspect = Literal["16:9", "9:16", "4:3", "3:4", "1:1", "4:5", "1.85:1", "2:1", "2.39:1"]
PAGE_INCHES = {"A4": (8.268, 11.693), "Letter": (8.5, 11.0), "16:9": (7.5, 13.333)}   # (short, long) side

# Texts drawn on the pages (layout.language).
STRINGS = {
    "es": {"frames": "fotogramas", "images": "imágenes", "sharpness": "nitidez", "of": "de",
           "first_frame": "primer fotograma", "last_frame": "último fotograma", "page": "página {p} de {n}",
           "range": "rango", "measured": "medidos", "dropped": "descartados", "threshold": "umbral",
           "min": "mín", "median": "mediana", "max": "máx",
           "mode": {"sharpest": "el más nítido por tramo", "even": "el del centro de cada tramo", "all": "todos",
                    "best": "los más nítidos", "positions": "posiciones exactas", "panels": "elegidos en panels"},
           "mode_images": {"sharpest": "la más nítida por tramo", "even": "la del centro de cada tramo",
                           "all": "todas", "best": "las más nítidas", "positions": "posiciones exactas",
                           "panels": "elegidas en panels"},
           "notes": {"shot": "PLANO", "camera": "CÁMARA", "action": "ACCIÓN", "dialogue": "DIÁLOGO",
                     "sound": "SONIDO", "transition": "TRANSICIÓN", "duration_s": "DURACIÓN"}},
    "en": {"frames": "frames", "images": "images", "sharpness": "sharpness", "of": "of",
           "first_frame": "first frame", "last_frame": "last frame", "page": "page {p} of {n}",
           "range": "range", "measured": "measured", "dropped": "dropped", "threshold": "threshold",
           "min": "min", "median": "median", "max": "max",
           "mode": {"sharpest": "sharpest per stretch", "even": "middle of each stretch", "all": "all",
                    "best": "the sharpest", "positions": "exact positions", "panels": "chosen in panels"},
           "notes": {"shot": "SHOT", "camera": "CAMERA", "action": "ACTION", "dialogue": "DIALOGUE",
                     "sound": "SOUND", "transition": "TRANSITION", "duration_s": "DURATION"}},
}

# Theme defaults; every key can be overridden in "style".
THEMES = {
    "dark": dict(background="#000000", text="#ffffff", text_secondary="#a3a3a3", accent="#facc15",
                 label_background="#000000", label_text="#facc15", panel_border="#3f3f46",
                 notes_background="#0a0a0a", notes_text="#e5e5e5", notes_rule="#2e2e2e", guides="#22d3ee"),
    "light": dict(background="#ffffff", text="#000000", text_secondary="#525252", accent="#b45309",
                  label_background="#ffffff", label_text="#000000", panel_border="#d4d4d4",
                  notes_background="#ffffff", notes_text="#171717", notes_rule="#e5e5e5", guides="#0891b2"),
}

# Rows per fixed page (landscape, portrait) when layout.rows is not given.
DEFAULT_ROWS = {"grid": (3, 5), "storyboard": (2, 3), "list": (2, 4), "strip": (1, 1)}

# Template defaults; columns, gap, margin and border_width can be overridden.
TEMPLATES = {
    "grid": dict(columns=4, gap=0, margin=0, border=0, caption="overlay", notes=False),
    "storyboard": dict(columns=3, gap=28, margin=48, border=1, caption="line", notes=True),
    "list": dict(columns=1, gap=24, margin=48, border=1, caption="line", notes=True),
    "strip": dict(columns=8, gap=12, margin=24, border=1, caption="overlay", notes=True),
}

INSTRUCTIONS = f"""\
Turns a video, or a list of images, into pages of panels you can look at (you cannot
play a video), with one JSON entry per panel.
Tools: media_info (what the input is) -> make_storyboard (pages as PNG, JPG or PDF;
the answer includes a preview of page 1) -> extract_frames (chosen frames at full size).
get_schema returns the JSON Schema of every tool with a minimal and a full example.
All tools take the same JSON sections; only "input" is required:
  input   exactly one of {{"video": path}}, {{"images": [paths]}}, {{"folder": path}}
  select  which frames / images become panels (default: video, the sharpest frame of
          each of 12 equal stretches; images, all of them in order)
  panels  notes per panel: shot, camera, action, dialogue, sound, transition, duration_s
  layout  template (grid | storyboard | list | strip), columns, rows, page (auto, A4,
          Letter, 16:9), labels, numbering, guides, language
  style   theme (dark | light) and any colour, font or size
  output  dir, name, format (png | jpg | pdf), shot_list (json, csv), frames
- Paths: absolute or ~/... (a relative path is resolved against the server's working
  directory). Files go to output.dir, or to a temporary folder named in the answer.
- Positions: video frames 0..frames-1; seconds from the first frame (0.0); % from the
  first (0) to the last frame or image (100); images by index 0..count-1 or file name.
- Exact times: step_s / at_s / at_pct (+ snap_s to avoid blurred frames). The sharpest
  of each stretch: count / every_s (irregular times). The N sharpest: mode "best".
- A mistake in the arguments returns every problem with its path, e.g.
  "layout.columns: Input should be greater than or equal to 1 (got 0)".
Guide with example images: {REPO}/blob/main/docs/formatos.md"""


# ============================================================================ schema
class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _checking(info: ValidationInfo | None) -> bool:
    """Paths are checked on disk unless validating examples (context skip_path_checks)."""
    return not (info and info.context and info.context.get("skip_path_checks"))


def _abs(p: str) -> Path:
    return Path(p).expanduser().resolve()


def _relative_hint(p: str) -> str:
    if Path(p).is_absolute() or p.startswith("~"):
        return ""
    return f" (relative paths are resolved against the server's working directory, {os.getcwd()}; " \
           "give an absolute path)"


_HEX = re.compile(r"#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})")
_ES_COLOURS = {"blanco": "white", "negro": "black", "rojo": "red", "verde": "green", "azul": "blue",
               "amarillo": "yellow", "naranja": "orange", "gris": "gray", "morado": "purple", "violeta": "violet",
               "rosa": "pink", "marrón": "brown", "marron": "brown", "dorado": "gold", "plateado": "silver",
               "celeste": "skyblue", "turquesa": "turquoise", "granate": "maroon", "beis": "beige"}


def _check_color(v: str) -> str:
    s = v.strip()
    if _HEX.fullmatch(s):
        return s.lower()
    if re.fullmatch(r"[A-Za-z]+", s):
        try:
            ImageColor.getrgb(s.lower())
            return s.lower()
        except ValueError:
            pass
    en = _ES_COLOURS.get(s.lower())
    raise ValueError(f'"{v}" is not a valid colour; use #RRGGBB or #RRGGBBAA (also #RGB, #RGBA) '
                     'or a CSS colour name in English such as "white", "black" or "navy"'
                     + (f' (for "{v}": "{en}")' if en else ""))


Color = Annotated[str, AfterValidator(_check_color), WithJsonSchema({
    "type": "string", "pattern": "^(#([0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})|[A-Za-z]+)$"})]


def _ci(lit):
    """Accept the values of a Literal in any case ("PDF", "a4"), and give back the canonical spelling."""
    canon = {str(c).lower(): c for c in typing.get_args(lit)}
    return Annotated[lit, BeforeValidator(lambda v: canon.get(v.strip().lower(), v) if isinstance(v, str) else v)]


RESERVED_FORMATS = ("webp", "gif", "mp4", "webm")


def _out_format(v):
    if isinstance(v, str):
        v = {"jpeg": "jpg"}.get(v.strip().lower(), v.strip().lower())
        if v in RESERVED_FORMATS:
            raise ValueError(f'"{v}" is planned for animated output and not available yet; use "png", "jpg" or "pdf"')
    return v


OutFormat = Annotated[Literal["png", "jpg", "pdf"], BeforeValidator(_out_format)]


def _rgba(c: str) -> tuple[int, int, int, int]:
    return ImageColor.getcolor(c, "RGBA")  # type: ignore[return-value]


def _image_file(v: str, info: ValidationInfo) -> str:
    if not _checking(info):
        return v
    p = _abs(v)
    if not p.is_file():
        raise ValueError(f"image not found: {p}" + _relative_hint(v))
    try:
        with Image.open(p):
            pass
    except Exception:
        raise ValueError(f"not an image this server can read: {p.name} (supported: {SUPPORTED_IMAGES})")
    return str(p)


ImageFile = Annotated[str, AfterValidator(_image_file)]
Pct = Annotated[float, Field(ge=0, le=100)]
NonNegFloat = Annotated[float, Field(ge=0)]
NonNegInt = Annotated[int, Field(ge=0)]


class InputSpec(Model):
    """What to turn into panels: exactly one of video, images or folder."""
    video: str | None = Field(None, description="Path of a video file (mp4, mov, mkv, webm, avi...). "
                                                "Absolute or ~/...", examples=["~/Movies/clip.mp4"])
    images: list[ImageFile] | None = Field(
        None, min_length=1, max_length=MAX_IMAGES,
        description=f"Paths of image files, in panel order (1 to {MAX_IMAGES}). Formats: {SUPPORTED_IMAGES}.",
        examples=[["~/shots/001.jpg", "~/shots/002.jpg"]])
    folder: str | None = Field(None, description="A folder: every image in it (not in subfolders) becomes "
                                                 "the input, sorted by file name (img2 before img10).")
    pattern: str | None = Field(None, description='With folder: only file names matching this pattern, '
                                                  'e.g. "*.png" or "shot_*". Default: every image.')

    @field_validator("video")
    @classmethod
    def _video(cls, v: str | None, info: ValidationInfo) -> str | None:
        if v is None or not _checking(info):
            return v
        p = _abs(v)
        if p.is_dir():
            raise ValueError(f"{p} is a folder; for a folder of images use input.folder")
        if not p.is_file():
            raise ValueError(f"file not found: {p}" + _relative_hint(v))
        return str(p)

    @field_validator("folder")
    @classmethod
    def _folder(cls, v: str | None, info: ValidationInfo) -> str | None:
        if v is None or not _checking(info):
            return v
        p = _abs(v)
        if not p.is_dir():
            raise ValueError(f"folder not found: {p}" + _relative_hint(v))
        return str(p)

    @model_validator(mode="after")
    def _exactly_one(self):
        given = [k for k in ("video", "images", "folder") if getattr(self, k) is not None]
        if len(given) != 1:
            raise ValueError('give exactly one of "video", "images" or "folder"'
                             + (f" (got {', '.join(given)})" if given else ""))
        if self.pattern is not None and self.folder is None:
            raise ValueError('"pattern" only works with "folder"')
        return self


class SelectSpec(Model):
    """Which frames (video) or images become panels. Default: video, the sharpest frame of each
    of 12 equal stretches; images, every image in order. Exact positions (step_pct, step_s,
    at_pct, at_s, at_frames, at_images, at_names) can be combined; when any is given, mode,
    count, every_* and the min_* filters are not used."""
    mode: _ci(Literal["auto", "sharpest", "even", "best", "all"]) = Field(
        "auto", description='"sharpest": the range is cut into count equal stretches and the least blurry '
                            'frame of each is taken (picks at least half a stretch apart). "even": the frame in '
                            'the middle of each stretch. "best": the count sharpest of the whole range, never '
                            'the same picture twice, in their order (they may all come from one part of a '
                            'video). "all": every frame / image of the range '
                            f'(at most {MAX_PANELS}). "auto" (default): "sharpest" for video; for images "all", '
                            'or "sharpest" when count or every_pct is given.')
    count: int = Field(12, ge=1, le=MAX_PANELS, description="How many panels: number of equal stretches "
                                                            '(sharpest, even) or of pictures ("best").')
    every_s: float | None = Field(None, gt=0, description="Video, instead of count: cut the range into stretches "
                                  "of N seconds and take one frame from each (the sharpest; \"even\": the middle "
                                  "one). Times come out irregular; for exactly every N seconds use step_s.")
    every_pct: float | None = Field(None, gt=0, le=100,
                                    description="Instead of count: one stretch every N % of the range.")
    step_pct: float | None = Field(None, gt=0, le=100, description="Exact positions: one panel every N % of the "
                                   "range, both ends included (25 -> 0, 25, 50, 75 and 100 %).")
    step_s: float | None = Field(None, gt=0, description="Video, exact positions: a frame exactly every N seconds "
                                 "from the start of the range (0, N, 2N...), even if it is blurred: add snap_s to "
                                 "move the blurred ones to a sharp frame nearby.")
    at_pct: list[Pct] | None = Field(None, description="Exact positions in % (0 = first frame or image, "
                                                       "100 = last).", examples=[[0, 50, 100]])
    at_s: list[NonNegFloat] | None = Field(None, description="Video, exact positions: times in seconds "
                                                             "(0.0 = first frame).", examples=[[1.5, 12]])
    at_frames: list[NonNegInt] | None = Field(None, description="Video, exact positions: frame numbers "
                                                                "(0 = first frame).", examples=[[0, 240]])
    at_images: list[NonNegInt] | None = Field(None, description="Images, exact positions: indexes in the "
                                                                "input (0 = first image).")
    at_names: list[str] | None = Field(None, description='Images, exact positions: file names, e.g. '
                                                         '["IMG_0042.jpg"].')
    snap_s: float = Field(0, ge=0, le=10, description="Video, exact positions: when the exact frame is clearly "
                          "blurrier (under 80 %) than the sharpest within ± N seconds, use that one instead; sharp "
                          "positions stay exact (0.5 suits handheld video).")
    start_s: float | None = Field(None, ge=0, description="Video: only this part, from this second...")
    end_s: float | None = Field(None, gt=0, description="Video: ... to this second.")
    start_pct: float | None = Field(None, ge=0, le=100, description="Only this part, from this % (0 = first "
                                                                    "frame or image) ...")
    end_pct: float | None = Field(None, ge=0, le=100, description="... to this % (100 = last).")
    min_sharpness: float = Field(0, ge=0, description="Never pick a frame or image less sharp than this "
                                 "(typical values: selection.sharpness_stats of an earlier answer).")
    drop_blurriest_pct: float = Field(0, ge=0, lt=100, description="Ignore the blurriest N % of the measured "
                                      "frames or images (30 = keep the sharpest 70 %). The sharpness value this "
                                      "gives is selection.threshold. Sharpness measures edges: a flat picture "
                                      "(plain colours, a logo) scores low without being blurred.")
    min_gap_s: float = Field(0, ge=0, description="Video: drop a pick closer than N seconds to the previous one.")
    min_difference: float = Field(0, ge=0, le=255, description="Drop a pick that looks almost the same as the "
                                  "previous one kept (\"best\": as any one kept): mean difference 0-255 of small "
                                  "64×36 colour copies. 1-2 removes only near-identical pictures, 3 suits small "
                                  "movements on a fixed background, 4-8 removes repeats of a still shot. \"best\" "
                                  "uses 1 unless you set it (0 = only exact repeats).")
    analysis_width: int = Field(480, ge=64, le=4096, description="Width in px at which sharpness is measured "
                                "(bigger = finer and slower).")


class Panel(Model):
    """One panel's notes (all optional). Without positions, panels[i] goes with the i-th chosen
    panel. With positions (all panels must have one), the panels themselves are the selection:
    each shows the frame or image it names, in the given order."""
    frame: int | None = Field(None, ge=0, description="Video: frame number this panel shows.")
    at_s: float | None = Field(None, ge=0, description="Video: second this panel shows.")
    at_pct: float | None = Field(None, ge=0, le=100, description="Position this panel shows, in %.")
    image: int | None = Field(None, ge=0, description="Images: index of the image this panel shows (0 = first).")
    image_name: str | None = Field(None, description="Images: file name of the image this panel shows.")
    number: str | None = Field(None, max_length=24, description='Number to print instead of the automatic '
                                                                'one, e.g. "12A".')
    shot: str | None = Field(None, max_length=200, description='Shot size or type, e.g. "PG", "plano medio", '
                                                               '"close-up".')
    camera: str | None = Field(None, max_length=300, description='Camera movement, e.g. "paneo a la derecha".')
    action: str | None = Field(None, max_length=2000, description="What happens.")
    dialogue: str | None = Field(None, max_length=2000, description="Dialogue or voice-over.")
    sound: str | None = Field(None, max_length=1000, description="Sound effects or music.")
    transition: str | None = Field(None, max_length=200, description='Transition to the next panel, e.g. '
                                                                     '"corte", "fundido a negro".')
    duration_s: float | None = Field(None, gt=0, le=36000, description="Planned duration, in seconds.")


LabelField = Literal["number", "frame", "time", "timecode", "pct", "sharpness", "sharpness_pct", "anchor",
                     "image", "name", "size"]
NoteField = Literal["shot", "camera", "action", "dialogue", "sound", "transition", "duration_s"]
VIDEO_LABELS = {"frame", "time", "timecode", "anchor"}
IMAGE_LABELS = {"image", "name", "size"}
TEMPLATE_KEYS = {"number", "n", "frame", "frames_total", "time", "timecode", "pct", "pct1", "sharpness",
                 "sharpness_pct", "anchor", "image", "name", "images_total", "size"}
VIDEO_KEYS = {"frame", "frames_total", "time", "timecode", "anchor"}
IMAGE_KEYS = {"image", "name", "images_total", "size"}


class LabelSpec(Model):
    """Caption of each panel: a band over the picture (grid, strip) or a line next to the panel
    number (storyboard, list)."""
    show: bool = Field(True, description="false: no caption (panel numbers in storyboard and list stay).")
    fields: list[_ci(LabelField)] | None = Field(
        None, description='What the caption says, in this order. Video: number, frame (#123), time (4.1s), '
                          'timecode (0:00:04.1), pct (20%), sharpness, sharpness_pct, anchor (first / last '
                          'frame). Images: number, image (#3), name (file name), size (4032×3024), pct, '
                          'sharpness, sharpness_pct. Default: video ["number", "time", "frame", "anchor"], '
                          'images ["number", "name"].')
    template: str | None = Field(None, max_length=200, description='Free text instead of fields, with '
                                 'placeholders: {number} {frame} {frames_total} {time} {timecode} {pct} {pct1} '
                                 '{sharpness} {sharpness_pct} {anchor} {image} {name} {images_total} {size}. '
                                 'E.g. "{number}. {timecode}".')
    position: _ci(Literal["top", "bottom"]) = Field("top", description="grid and strip: band at the top or the "
                                                                  "bottom of the picture.")


class NumberingSpec(Model):
    """Panel numbers (panels[i].number overrides one)."""
    start: int = Field(1, ge=0, le=1_000_000, description="First number.")
    step: int = Field(1, ge=1, le=10_000, description="Increment: 10 with start 10 and format \"SH{n:03d}\" "
                                                      "gives SH010, SH020, SH030...")
    format: str = Field("{n}", max_length=40, description='{n} is the running number and accepts Python '
                        'format specs: "5.{n}" -> 5.1, 5.2...; "SH{n:03d}" -> SH001; "{n}A" -> 1A.')


class NotesSpec(Model):
    """Notes box of each panel, filled with the texts of panels[i]."""
    show: bool | None = Field(None, description="Default: true for storyboard and list; for strip, true when "
                                                "panels[] has notes; false for grid.")
    fields: list[_ci(NoteField)] = Field(["shot", "camera", "action", "dialogue", "sound", "transition", "duration_s"],
                                    description="Which panels[] texts are printed, in this order.")
    blank_lines: int = Field(3, ge=0, le=20, description="Ruled empty lines in the box of a panel without "
                                                         "notes, to write by hand (0 = no box).")
    max_lines: int = Field(8, ge=1, le=60, description='Most text lines per box; longer notes end in "…".')


class GuidesSpec(Model):
    """Lines drawn over each picture."""
    aspects: list[Aspect] = Field([], description="Frame lines for these aspect ratios: what a crop to "
                                                  "2.39:1, 9:16... would keep.", examples=[["2.39:1"]])
    shade: bool = Field(True, description="Darken what falls outside the first aspect in aspects.")
    safe_zone: bool = Field(False, description="Draw the safe area: a dashed box of safe_zone_pct of the "
                                               "picture.")
    safe_zone_pct: float = Field(90, ge=50, le=100, description="Size of the safe area, % of the picture.")
    thirds: bool = Field(False, description="Draw the rule-of-thirds grid.")


class LayoutSpec(Model):
    """How the panels are arranged on the pages."""
    template: _ci(Literal["grid", "storyboard", "list", "strip"]) = Field(
        "grid", description='"grid": contact sheet, caption band over each picture. "storyboard": panel '
                            'number above each picture and a notes box below. "list": one panel per row, '
                            'picture left, number and notes right. "strip": one row of panels with notes '
                            'below.')
    columns: int | None = Field(None, ge=1, le=24, description="Panels per row. Default: grid 4, storyboard 3, "
                                                               "list 1, strip all the panels (up to 8).")
    rows: int | None = Field(None, ge=1, le=200, description="Rows per page; more panels make more pages. "
                             'Default: all rows on one page (page "auto"), as many as fit (fixed page), '
                             "strip 1.")
    page: _ci(Literal["auto", "A4", "Letter", "16:9"]) = Field(
        "auto", description='"auto": the page grows to fit the panels. "A4", "Letter", "16:9": fixed page '
                            "size (at output.dpi); the panels are scaled to fit.")
    orientation: _ci(Literal["landscape", "portrait"]) = Field("landscape", description="Fixed pages only.")
    panel_width: int = Field(480, ge=64, le=4096, description='Width of each picture, px. With a fixed page '
                             "it is computed to fill the page.")
    aspect: Literal["source", "16:9", "9:16", "4:3", "3:4", "1:1", "4:5", "1.85:1", "2:1", "2.39:1"] = Field(
        "source", description='Shape of the picture boxes: "source" = the video\'s (or the first image\'s) '
                              "shape, or a fixed ratio.")
    fit: _ci(Literal["contain", "cover"]) = Field("contain", description='"contain": the whole picture, with bars '
                                             'if its shape differs from the box; "cover": fill the box, '
                                             "cropping the centre.")
    margin: int | None = Field(None, ge=0, le=2000, description="Space around the page content, px. Default: "
                                                                "grid 0, storyboard and list 48, strip 24.")
    gap: int | None = Field(None, ge=0, le=2000, description="Space between panels, px. Default: grid 0, "
                                                             "storyboard 28, list 24, strip 12.")
    padding: int | None = Field(None, ge=0, le=500, description="Space inside the header, footer, captions "
                                                                "and notes boxes, px. Default: from the text "
                                                                "size.")
    header: bool = Field(True, description="Band on top with a title and a subtitle.")
    title: str | None = Field(None, max_length=200, description='Header title. Default: the video\'s file '
                              'name, or the folder name / "N imágenes". "" hides it.')
    subtitle: str | None = Field(None, max_length=300, description="Header second line. Default: the "
                                 "video's size, fps, duration, frames and codec, or the images' count, "
                                 'formats and sizes. "" hides it.')
    footer: bool = Field(True, description="Band at the bottom: how the panels were chosen, and the page number "
                                           "when there are several pages.")
    footer_text: str | None = Field(None, max_length=300, description="Replaces the automatic footer text "
                                                                      "(the page number stays).")
    label: LabelSpec = Field(default_factory=LabelSpec)
    numbering: NumberingSpec = Field(default_factory=NumberingSpec)
    notes: NotesSpec = Field(default_factory=NotesSpec)
    guides: GuidesSpec = Field(default_factory=GuidesSpec)
    language: _ci(Literal["es", "en"]) = Field("es", description="Language of the texts drawn on the pages "
                                                            "(header, footer, captions, notes titles).")


def _font_check(v: str | None, info: ValidationInfo) -> str | None:
    if v is None or not _checking(info):
        return v
    return resolve_font(v)


class StyleSpec(Model):
    """Colours, fonts and sizes. Everything has a default from the theme; set only what you
    want to change. Colours: "#RRGGBB", "#RRGGBBAA" (with alpha), "#RGB", "#RGBA" or a CSS name
    ("white", "navy")."""
    theme: _ci(Literal["dark", "light"]) = Field("dark", description='"dark": black page (#000000) and white text; '
                                                                '"light": white page (#ffffff) and black text.')
    background: Color | None = Field(None, description='Page background (also the bars of "contain"). Default: '
                                     'dark #000000, light #ffffff. PNG keeps alpha: "#00000000" is transparent.')
    text: Color | None = Field(None, description="Main text: title, panel numbers. Default: dark #ffffff, "
                                                 "light #000000.")
    text_secondary: Color | None = Field(None, description="Subtitle, footer, captions in storyboard and list. "
                                                           "Default: dark #a3a3a3, light #525252.")
    accent: Color | None = Field(None, description="Notes titles (ACCIÓN, DIÁLOGO...). Default: dark #facc15, "
                                                   "light #b45309.")
    label_background: Color | None = Field(None, description="Caption band over the picture (grid, strip). "
                                           "Default: dark #000000, light #ffffff. Alpha allowed: #000000aa.")
    label_text: Color | None = Field(None, description="Caption band text. Default: dark #facc15, light #000000.")
    panel_border: Color | None = Field(None, description="Border of pictures and notes boxes. Default: dark "
                                                         "#3f3f46, light #d4d4d4.")
    border_width: int | None = Field(None, ge=0, le=40, description="Border width, px (0 = none). Default: "
                                                                     "grid 0, the others 1.")
    notes_background: Color | None = Field(None, description="Notes boxes. Default: dark #0a0a0a, light #ffffff.")
    notes_text: Color | None = Field(None, description="Notes text. Default: dark #e5e5e5, light #171717.")
    notes_rule: Color | None = Field(None, description="Ruled lines of empty notes boxes. Default: dark #2e2e2e, "
                                                       "light #e5e5e5.")
    guides: Color | None = Field(None, description="Guide lines (aspects, safe area, thirds). Default: dark "
                                                   "#22d3ee, light #0891b2.")
    font: str | None = Field(None, description='Font file (.ttf, .otf, .ttc) or installed font name ("Helvetica", '
                             '"Arial", "DejaVu Sans"). Default: Arial, Helvetica or DejaVu Sans, whichever exists.')
    font_bold: str | None = Field(None, description="Bold font, same rules. Default: the bold version of font.")
    title_size: int | None = Field(None, ge=6, le=400, description="Header title, px. Default: from the page width.")
    text_size: int | None = Field(None, ge=6, le=400, description="Subtitle and footer, px. Default: from the "
                                                                  "page width.")
    label_size: int | None = Field(None, ge=6, le=400, description="Captions, px. Default: picture width / 22.")
    number_size: int | None = Field(None, ge=6, le=400, description="Panel numbers (storyboard, list), px. "
                                                                    "Default: 1.45 × label_size.")
    notes_size: int | None = Field(None, ge=6, le=400, description="Notes text, px. Default: 0.92 × label_size.")
    sharpen: float = Field(0.5, ge=0, le=2, description="Sharpening after shrinking each picture (0 = none).")

    @field_validator("font", "font_bold")
    @classmethod
    def _fonts(cls, v: str | None, info: ValidationInfo) -> str | None:
        return _font_check(v, info)


def _safe_name(v: str | None) -> str | None:
    if v is not None and (not v.strip() or re.search(r'[/\\:*?"<>|]', v)):
        raise ValueError(f'"{v}" is not a valid file name (no folders and none of / \\ : * ? " < > |)')
    return v


FileName = Annotated[str, AfterValidator(_safe_name)]


class StoryboardOutput(Model):
    """Where and how the pages are written."""
    dir: str | None = Field(None, description="Folder for every file (created if missing). Default: "
                                              "<system temp>/storyboard-mcp/<input name>/. The answer has "
                                              "the full paths.")
    name: FileName | None = Field(None, max_length=120, description="Base file name, no extension. Default: "
                                  "<input name>_<template>. Several pages add _p1, _p2...")
    format: OutFormat = Field("png", description='"png" (lossless; keeps a transparent background), "jpg", or '
                              '"pdf" (one file, one PDF page per page). Any case. "webp", "gif", "mp4" and "webm" '
                              "are planned for animated output and not available yet.")
    quality: int = Field(90, ge=1, le=100, description="JPEG quality (jpg, and the pictures inside a pdf).")
    dpi: int = Field(150, ge=50, le=600, description="Resolution of fixed pages (A4, Letter, 16:9) and of the pdf.")
    shot_list: list[_ci(Literal["json", "csv"])] = Field([], description="Also write the shot list, one row per "
                                                         "panel with its position and notes: <name>_shots.json "
                                                         "and/or <name>_shots.csv (UTF-8 with BOM).")
    csv_style: _ci(Literal["standard", "excel_es"]) = Field(
        "standard", description='"standard": comma separator, dot decimals, English column names, every column. '
                                '"excel_es": for Excel in Spanish (and other languages with decimal commas): '
                                "semicolon separator, decimal commas, Spanish column names, no always-empty "
                                "columns, and panel numbers kept as text (3.1 is not turned into a date).")
    frames: bool = Field(False, description="Also save each panel's picture at full size in <dir>/<name>_frames/ "
                                            "(video frames as JPEG, images copied unchanged).")
    also_without_labels: bool = Field(False, description="Also write the pages without captions "
                                                         "(<name>_nolabels...).")
    preview: bool = Field(True, description="Include a small image of page 1 in the answer.")


class FramesOutput(Model):
    """Where and how the frames or images are written."""
    dir: str | None = Field(None, description="Folder (created if missing). Default: <system temp>/"
                                              "storyboard-mcp/<input name>/frames/.")
    format: _ci(Literal["jpg", "png", "original"]) | None = Field(
        None, description='Video frames: "jpg" (default) or "png". Images: "original" (default: the file is '
                          'copied byte for byte) or converted to "jpg" / "png".')
    names: _ci(Literal["numbered", "original"]) = Field(
        "numbered", description='Images: "numbered" (default: 001_<name>, 002_<name>... in panel order) or '
                                '"original" (the original file name; a repeated name gets _2, _3...). Video '
                                "frames are always f<frame, 5 digits>.<ext>.")
    quality: int = Field(95, ge=1, le=100, description="JPEG quality.")


class MediaInfoArgs(Model):
    input: InputSpec


class StoryboardArgs(Model):
    input: InputSpec
    select: SelectSpec = Field(default_factory=SelectSpec)
    panels: list[Panel] = Field([], max_length=MAX_PANELS, description="Notes per panel, in panel order (or "
                                "with positions, to choose the panels yourself).")
    layout: LayoutSpec = Field(default_factory=LayoutSpec)
    style: StyleSpec = Field(default_factory=StyleSpec)
    output: StoryboardOutput = Field(default_factory=StoryboardOutput)


class FramesArgs(Model):
    input: InputSpec
    select: SelectSpec = Field(default_factory=SelectSpec)
    output: FramesOutput = Field(default_factory=FramesOutput)


class SchemaArgs(Model):
    tool: Literal["media_info", "make_storyboard", "extract_frames"] | None = Field(
        None, description="Only this tool. Default: every tool.")


# ---------------------------------------------------------------------------- answers
class VideoInfo(Model):
    path: str = Field(description="Absolute path of the video.")
    duration_s: float | None = Field(None, description="Length in seconds.")
    fps: float | None = Field(None, description="Frames per second.")
    frames: int | None = Field(None, description="Number of frames; frame numbers go from 0 to frames - 1.")
    size: list[int] = Field(description="[width, height] as stored in the file.")
    rotation_deg: int = Field(0, description="0, 90, 180 or 270: clockwise turn applied to every picture "
                                             "(phones film sideways).")
    displayed_size: list[int] = Field(description="[width, height] after that turn: size of the full frames.")
    codec: str | None = None


class ImageInfo(Model):
    index: int = Field(description="Position in the input, 0 = first.")
    path: str
    name: str
    format: str | None = Field(None, description='"JPEG", "PNG", "HEIF"...')
    size: list[int] = Field(description="[width, height] as stored.")
    displayed_size: list[int] = Field(description="[width, height] after the EXIF orientation.")
    bytes: int
    duplicate_of: int | None = Field(None, description="Index of an earlier image with exactly the same bytes.")


class MediaInfoResult(Model):
    kind: Literal["video", "images"]
    video: VideoInfo | None = None
    count: int | None = Field(None, description="Images: how many.")
    folder: str | None = None
    formats: dict[str, int] | None = Field(None, description='Images: count per format, e.g. {"JPEG": 10}.')
    sizes: list[list[int]] | None = Field(None, description="Images: the different displayed sizes "
                                                            "[width, height], most common first.")
    images: list[ImageInfo] | None = None


class Dropped(Model):
    frame: int | None = None
    image: int | None = None
    name: str | None = None
    reason: str = Field(description='"min_gap_s", "min_difference" or "min_sharpness" (mode "all"). Mode '
                                    '"best" lists only the repeats it skipped before reaching count, not the '
                                    'pictures below the threshold: use mode "all" to list those.')
    similar_to: int | None = Field(None, description="min_difference / min_gap_s: frame or image index of the "
                                                     "kept panel it was compared with.")
    difference: float | None = Field(None, description="min_difference: the measured difference, 0-255.")


class PanelResult(Model):
    n: int = Field(description="Order of the panel, 1 = first.")
    number: str = Field(description="Number printed on the panel.")
    page: int | None = Field(None, description="Page where it is.")
    frame: int | None = Field(None, description="Video: frame number.")
    time_s: float | None = Field(None, description="Video: seconds from the first frame.")
    timecode: str | None = Field(None, description="Video: h:mm:ss.s")
    pct: float | None = Field(None, description="Position, 0 = first frame or image, 100 = last.")
    image: int | None = Field(None, description="Images: index in the input.")
    name: str | None = Field(None, description="Images: file name.")
    source: str | None = Field(None, description="Images: path of the original image.")
    sharpness: float | None = Field(None, description="Higher = sharper (variance of the edges).")
    sharpness_pct: int | None = Field(None, description="Sharpness as a % of the sharpest panel of this answer "
                                                        "(not a percentile).")
    anchor: str | None = Field(None, description='"first_frame" / "last_frame" on the first / last frame '
                                                 "of the video.")
    file: str | None = Field(None, description="Full-size frame or image copy (output.frames / extract_frames), "
                                               "upright (rotation already applied).")
    size: list[int] | None = Field(None, description="[width, height] of file, px.")
    notes: dict[str, Any] | None = Field(None, description="The panel's notes, as given in panels[].")


class PageResult(Model):
    page: int
    path: str | None = Field(None, description="Image of the page; with format pdf, the PDF file (this is page "
                                               "number `page` of it).")
    size: list[int] = Field(description="[width, height] px.")
    panels: list[int] = Field(description="n of the panels on this page.")


class SelectionResult(Model):
    mode: str = Field(description='"sharpest", "even", "all", "positions" or "panels".')
    range_s: list[float] | None = Field(None, description="Video: [start, end] in seconds.")
    range_pct: list[float]
    measured: int = Field(description="Frames or images whose sharpness was measured (only these values are "
                                      "comparable with each other).")
    threshold: float = Field(description="Frames or images below this sharpness were never picked.")
    sharpness_stats: dict[str, float] | None = Field(None, description="min, p25, median, p75, max of the "
                                                                       "measured ones (nearest measured value, "
                                                                       "not interpolated): use it for min_sharpness.")


class StoryboardResult(Model):
    pages: list[PageResult]
    pdf: str | None = None
    pages_without_labels: list[str] | None = None
    shot_list: dict[str, str] | None = Field(None, description='{"json": path, "csv": path}')
    frames_dir: str | None = None
    panels: list[PanelResult]
    dropped: list[Dropped]
    selection: SelectionResult
    layout: dict[str, Any] = Field(description="Template, columns, rows per page, picture size and page size used.")
    input: MediaInfoResult
    warnings: list[str] = []


class FramesResult(Model):
    dir: str
    files: list[PanelResult]
    dropped: list[Dropped]
    selection: SelectionResult
    input: MediaInfoResult
    warnings: list[str] = []


# ============================================================================ errors
class ToolError(Exception):
    """A problem the caller can fix; the message says what and how."""


_UNION_TAGS = {"int", "str", "float", "bool", "list", "dict", "none"}


def _loc_str(loc) -> str:
    out = ""
    for p in loc:
        if isinstance(p, int):
            out += f"[{p}]"
        else:
            out += ("." if out else "") + str(p)
    return out or "(arguments)"


def _inner_model(ann):
    origin = typing.get_origin(ann)
    if origin is Annotated:
        return _inner_model(typing.get_args(ann)[0])
    if origin in (list, typing.Union, pytypes.UnionType):
        for a in typing.get_args(ann):
            m = _inner_model(a)
            if m:
                return m
        return None
    return ann if isinstance(ann, type) and issubclass(ann, BaseModel) else None


def _model_at(model, loc):
    cur = model
    for part in loc:
        if isinstance(part, int):
            continue
        f = cur.model_fields.get(part) if cur else None
        if f is None:
            return None
        cur = _inner_model(f.annotation)
    return cur


def _got(v) -> str:
    try:
        s = json.dumps(v, ensure_ascii=False, default=str)
    except Exception:
        s = repr(v)
    return f" (got {s[:120]}{'…' if len(s) > 120 else ''})"


def _problems_text(tool: str, problems: list[str]) -> str:
    head = f"{tool}: {len(problems)} problem{'s' if len(problems) != 1 else ''} in the arguments:"
    tail = f'What each field accepts: call get_schema with {{"tool": "{tool}"}}.' if tool != "get_schema" else ""
    return "\n".join([head, *[f"- {p}" for p in problems], tail]).strip()


OBJECT_HINTS = {"InputSpec": '{"video": "/abs/clip.mp4"}', "SelectSpec": '{"count": 8}',
                "LayoutSpec": '{"template": "storyboard"}', "StyleSpec": '{"theme": "light"}',
                "StoryboardOutput": '{"dir": "/abs/folder", "format": "pdf"}', "FramesOutput": '{"dir": "/abs/folder"}',
                "Panel": '{"shot": "PG", "action": "..."}', "LabelSpec": '{"fields": ["number", "time"]}',
                "NumberingSpec": '{"format": "5.{n}"}', "NotesSpec": '{"fields": ["action", "dialogue"]}',
                "GuidesSpec": '{"aspects": ["2.39:1"], "thirds": true}'}


def format_validation_error(tool: str, model: type[BaseModel], err: ValidationError) -> str:
    problems, seen = [], set()
    for e in err.errors(include_url=False):
        loc = tuple(p for p in e["loc"] if not (isinstance(p, str) and ("[" in p or p in _UNION_TAGS)))
        path, typ = _loc_str(loc), e["type"]
        if typ == "extra_forbidden":
            parent = _model_at(model, loc[:-1])
            name = str(loc[-1])
            allowed = list(parent.model_fields) if parent else []
            close = difflib.get_close_matches(name, allowed, n=1, cutoff=0.6)
            msg = f'unknown field "{name}".' + (f' Did you mean "{close[0]}"?' if close else "") + \
                  (f" Allowed here: {', '.join(allowed)}." if allowed else "")
        elif typ == "missing":
            msg = "required, but missing."
        elif typ in ("model_type", "model_attributes_type", "dict_type"):
            sub = _model_at(model, loc)
            fields = ", ".join(sub.model_fields) if sub else ""
            hint = OBJECT_HINTS.get(sub.__name__, "") if sub else ""
            msg = "expected an object" + (f" like {hint}" if hint else "") + \
                  (f" (fields: {fields})" if fields else "") + _got(e.get("input"))
        elif typ == "list_type":
            msg = "expected a list, e.g. [" + json.dumps(e.get("input"), ensure_ascii=False) + "]" + _got(e.get("input"))
        elif typ in ("value_error", "assertion_error"):
            msg = re.sub(r"^(Value error|Assertion failed), ", "", e["msg"])
        else:
            msg = e["msg"] + _got(e.get("input"))
        if (path, msg) not in seen:
            seen.add((path, msg))
            problems.append(f"{path}: {msg}")
    return _problems_text(tool, problems)


# ============================================================================ fonts
FONT_DIRS = [Path("/System/Library/Fonts"), Path("/Library/Fonts"), Path.home() / "Library/Fonts",
             Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), Path.home() / ".fonts",
             Path.home() / ".local/share/fonts", Path("C:/Windows/Fonts")]
DEFAULT_FONTS = [("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
                 ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                  "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
                 ("/usr/share/fonts/TTF/DejaVuSans.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"),
                 ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf")]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


@lru_cache(maxsize=1)
def _font_index() -> dict[str, str]:
    idx: dict[str, str] = {}
    for d in FONT_DIRS:
        if d.is_dir():
            for p in sorted(d.rglob("*")):
                if p.suffix.lower() in (".ttf", ".otf", ".ttc"):
                    idx.setdefault(_norm(p.stem), str(p))
    return idx


def resolve_font(spec: str) -> str:
    """Path of a font given as a file or as an installed font's name."""
    p = Path(spec).expanduser()
    if p.is_file():
        try:
            ImageFont.truetype(str(p), 12)
            return str(p)
        except OSError:
            raise ValueError(f'"{spec}" is not a font file this server can read (.ttf, .otf, .ttc)')
    hit = _font_index().get(_norm(spec))
    if hit:
        return hit
    close = difflib.get_close_matches(_norm(spec), list(_font_index()), n=3, cutoff=0.6)
    raise ValueError(f'font "{spec}" not found; give the path of a .ttf, .otf or .ttc file or the name of an '
                     "installed font" + (f" (similar: {', '.join(Path(_font_index()[c]).stem for c in close)})"
                                         if close else ""))


def _bold_of(path: str) -> str | None:
    stem = Path(path).stem
    for cand in (re.sub(r"(?i)regular|book|roman", "Bold", stem), stem + " Bold", stem + "-Bold", stem + "bd"):
        if cand != stem:
            hit = _font_index().get(_norm(cand))
            if hit:
                return hit
    return None


@lru_cache(maxsize=256)
def _ft(path: str | None, size: int):
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default(size)


# ============================================================================ media
def _tenths(t: float) -> float:
    """Seconds rounded to tenths, as every caption shows them (time_s in the JSON keeps 3 decimals)."""
    return round(t + 1e-9, 1)


def _hms(t: float) -> str:
    t = _tenths(t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:04.1f}"


def _rotation(st) -> int:
    """Clockwise degrees to turn the decoded pictures so they look the way they were
    filmed. Phones film sideways and store that turn in the file, as a display matrix
    (current FFmpeg) or as an old "rotate" tag."""
    try:
        r = st.metadata.get("rotate") if st.metadata else None
        if r:
            return int(r) % 360
    except Exception:
        pass
    try:  # display matrix (PyAV >= 14): int32 3x3 matrix in 16.16 fixed point, read like
        # FFmpeg's av_display_rotation_get(), which gives the counter-clockwise angle
        m = (st.codec_context.coded_side_data or {}).get("display_matrix")
        if m and len(m) >= 20:
            a, b, _, c, d = (v / 65536 for v in struct.unpack("=5i", m[:20]))
            ccw = -math.degrees(math.atan2(b / (math.hypot(b, d) or 1), a / (math.hypot(a, c) or 1)))
            return round(-ccw) % 360
    except Exception:
        pass
    for sd in getattr(st, "side_data", []) or []:  # display matrix (older PyAV)
        rot = getattr(sd, "rotation", None)
        if rot is not None:
            return int(-rot) % 360
    return 0


def _offset(st) -> float:
    """Time of the stream start (containers often start at a small non-zero pts):
    every time reported is measured from it, so the first frame is 0.0 s."""
    try:
        return float(st.start_time * st.time_base) if st.start_time is not None and st.time_base else 0.0
    except Exception:
        return 0.0


def _sharpness(im: Image.Image, analysis_width: int) -> float:
    g = im.convert("L")
    if g.width > analysis_width:
        g = g.resize((analysis_width, max(1, round(g.height * analysis_width / g.width))))
    return ImageStat.Stat(g.filter(ImageFilter.FIND_EDGES)).var[0]


def _tiny(im: Image.Image) -> Image.Image:
    return im.convert("RGB").resize((64, 36), Image.BILINEAR)


def _difference(a: Image.Image, b: Image.Image) -> float:
    """Mean absolute difference, 0-255, of two tiny colour copies."""
    return sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3


def _percentile(values: list[float], pct: float) -> float:
    v = sorted(values)
    if not v:
        return 0.0
    k = max(0, min(len(v) - 1, round(pct / 100 * (len(v) - 1))))
    return v[k]


def _natural(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


class VideoSource:
    kind = "video"

    def __init__(self, path: str):
        self.path = path
        self.name = Path(path).stem
        c, st = self._open()
        try:
            fps = float(st.average_rate) if st.average_rate else None
            dur = float(st.duration * st.time_base) if st.duration else (c.duration / 1e6 if c.duration else None)
            n = st.frames or (round(dur * fps) if dur and fps else None)
            self.rot = _rotation(st)
            w, h = st.codec_context.width, st.codec_context.height
            self.info = VideoInfo(path=path, duration_s=round(dur, 3) if dur else None, fps=fps, frames=n,
                                  size=[w, h], rotation_deg=self.rot,
                                  displayed_size=[h, w] if self.rot in (90, 270) else [w, h],
                                  codec=st.codec_context.name)
        finally:
            c.close()
        self.fps = fps or 30.0
        self.frames = n
        self.duration = dur or ((n or 1) / self.fps)
        self.span = (n - 1) / self.fps if n else self.duration      # time of the last frame
        self.count = n or 0
        self.aspect = self.info.displayed_size[0] / max(1, self.info.displayed_size[1])
        self.measured: list[float] = []          # every sharpness measured in this call (one scale)

    def _open(self):
        try:
            c = av.open(self.path)
        except Exception as e:
            raise ToolError(f"input.video: cannot open {self.path} as a video ({e})")
        if not c.streams.video:
            c.close()
            raise ToolError(f"input.video: {self.path} has no video stream")
        return c, c.streams.video[0]

    def result(self, with_items: bool = True) -> MediaInfoResult:
        return MediaInfoResult(kind="video", video=self.info)

    def _image(self, fr, width: int | None = None) -> Image.Image:
        if width and fr.width > width:
            im = fr.reformat(width=width, height=max(2, round(fr.height * width / fr.width)),
                             format="rgb24").to_image()
        else:
            im = fr.to_image()
        return im.rotate(-self.rot, expand=True) if self.rot else im

    def decode(self, start_s: float = 0.0, end_s: float | None = None):
        """(index, time_s, frame) for the frames in [start_s, end_s], in order."""
        c, st = self._open()
        tb = float(st.time_base) if st.time_base else 0.0
        off = _offset(st)
        try:
            for i, fr in enumerate(c.decode(st)):
                t = float(fr.pts * tb) - off if fr.pts is not None else i / self.fps
                if t < start_s - 1e-6:
                    continue
                if end_s is not None and t > end_s + 1e-6:
                    break
                yield i, t, fr
        finally:
            c.close()

    def around(self, t: float, window_s: float):
        """Seek near t and yield (index, time_s, frame) for the frames in [t - window, t + window].
        Frame index is estimated from the time (seeking does not give it)."""
        c, st = self._open()
        tb = float(st.time_base) if st.time_base else 0.0
        off = _offset(st)
        lo, hi = max(0.0, t - window_s), t + max(window_s, 0.5 / self.fps)
        try:
            if tb:
                c.seek(int((max(0.0, lo - 1.0) + off) / tb), stream=st, backward=True, any_frame=False)
            last, given = None, False
            for fr in c.decode(st):
                ft = float(fr.pts * tb) - off if fr.pts is not None else 0.0
                if ft < lo - 0.5 / self.fps:
                    last = (ft, fr)
                    continue
                if ft > hi:
                    if not given:                 # nothing inside the window: give the next frame
                        yield round(ft * self.fps), ft, fr
                        given = True
                    break
                yield round(ft * self.fps), ft, fr
                given, last = True, None
            if not given and last is not None:    # t beyond the last frame: give the last one
                yield round(last[0] * self.fps), last[0], last[1]
        finally:
            c.close()

    def near(self, t: float, snap_s: float, analysis_width: int):
        """The frame at t (or the sharpest within ± snap_s): (index, time, sharpness, full image)."""
        last_i = (self.frames - 1) if self.frames else None
        if snap_s <= 0 and t <= 0:
            for i, ft, fr in self.decode():
                return 0, 0.0, self.sharpness(fr, analysis_width), self._image(fr)
        if snap_s <= 0 and last_i is not None and t >= self.span - 0.5 / self.fps:
            found = None
            for i, ft, fr in self.around(self.span, 1.0 / self.fps):
                found = fr
            if found is not None:
                return last_i, self.span, self.sharpness(found, analysis_width), self._image(found)
        cands = [(i, ft, self.sharpness(fr, analysis_width) if snap_s > 0 else 0.0, fr)
                 for i, ft, fr in self.around(t, snap_s)]
        if not cands:
            raise ToolError(f"no frame found near {t:.2f} s")
        exact = min(cands, key=lambda c: abs(c[1] - t))
        sharpest = max(cands, key=lambda c: c[2])
        i, ft, sc, fr = sharpest if (snap_s > 0 and exact[2] < 0.8 * sharpest[2]) else exact
        if snap_s <= 0:
            sc = self.sharpness(fr, analysis_width)
        if last_i is not None:
            i = min(i, last_i)
        return i, max(0.0, ft), sc, self._image(fr)

    def sharpness(self, fr, analysis_width: int) -> float:
        sc = _sharpness(self._image(fr, analysis_width), analysis_width)
        self.measured.append(sc)
        return sc

    def measure(self, t0: float, t1: float, analysis_width: int, tiny: bool):
        """Every frame in [t0, t1]: (index, time, sharpness, tiny grey copy or None)."""
        out = []
        for i, t, fr in self.decode(t0, t1):
            im = self._image(fr, analysis_width)
            sc = _sharpness(im, analysis_width)
            self.measured.append(sc)
            out.append((i, t, sc, _tiny(im) if tiny else None))
        return out

    def fetch(self, picks, sink):
        """Full-size image of each pick, given to sink(pick, image)."""
        ready = [p for p in picks if p.image is not None]
        for p in ready:
            sink(p, p.image)
            p.image = None
        rest = [p for p in picks if p not in ready]
        if not rest:
            return
        if len(rest) > 24 and all(p.exact_index for p in rest):   # many picks: one pass, not many seeks
            want = {p.key: p for p in rest}
            last = max(want)
            for i, t, fr in self.decode(max(0.0, min(p.t for p in rest) - 0.5)):
                if i in want:
                    sink(want[i], self._image(fr))
                if i >= last:
                    break
            return
        for p in rest:
            *_, im = self.near(p.t, 0, 64)
            sink(p, im)


class ImageSource:
    kind = "images"

    def __init__(self, paths: list[str], name: str, folder: str | None = None):
        self.paths = [Path(p) for p in paths]
        self.name = name
        self.folder = folder
        self.count = len(paths)
        self._info: list[ImageInfo] | None = None
        self.measured: list[float] = []
        first = self.infos()[0].displayed_size
        self.aspect = first[0] / max(1, first[1])

    def infos(self) -> list[ImageInfo]:
        if self._info is None:
            out = []
            for i, p in enumerate(self.paths):
                try:
                    with Image.open(p) as im:
                        w, h = im.size
                        orient = im.getexif().get(0x0112, 1)
                        fmt = im.format
                except Exception as e:
                    raise ToolError(f"input: cannot read image {p} ({e})")
                out.append(ImageInfo(index=i, path=str(p), name=p.name, format=fmt, size=[w, h],
                                     displayed_size=[h, w] if orient in (5, 6, 7, 8) else [w, h],
                                     bytes=p.stat().st_size))
            by_size: dict[int, list[ImageInfo]] = {}          # identical files: hash only same-size ones
            for x in out:
                by_size.setdefault(x.bytes, []).append(x)
            for group in (g for g in by_size.values() if len(g) > 1):
                seen: dict[str, int] = {}
                for x in group:
                    digest = hashlib.sha1(Path(x.path).read_bytes()).hexdigest()
                    if digest in seen:
                        x.duplicate_of = seen[digest]
                    else:
                        seen[digest] = x.index
            self._info = out
        return self._info

    def result(self, with_items: bool = True) -> MediaInfoResult:
        infos = self.infos()
        formats: dict[str, int] = {}
        for x in infos:
            formats[x.format or "?"] = formats.get(x.format or "?", 0) + 1
        counts: dict[tuple[int, int], int] = {}
        for x in infos:
            key = (x.displayed_size[0], x.displayed_size[1])
            counts[key] = counts.get(key, 0) + 1
        sizes = [list(k) for k, _ in sorted(counts.items(), key=lambda kv: -kv[1])]
        return MediaInfoResult(kind="images", count=len(infos), folder=self.folder, formats=formats,
                               sizes=sizes, images=infos if with_items else None)

    def open(self, i: int, draft: int | None = None) -> Image.Image:
        try:
            with Image.open(self.paths[i]) as im:
                if draft and im.format == "JPEG":
                    im.draft("RGB", (draft, draft))
                im = ImageOps.exif_transpose(im)
                im.load()
                if im.mode not in ("RGB", "RGBA"):
                    im = im.convert("RGBA" if ("A" in im.getbands() or "transparency" in im.info) else "RGB")
                return im
        except Exception as e:
            raise ToolError(f"cannot read image {self.paths[i]} ({e})")

    def measure(self, i0: int, i1: int, analysis_width: int, tiny: bool, only: list[int] | None = None):
        out = []
        for i in (only if only is not None else range(i0, i1 + 1)):
            im = self.open(i, draft=analysis_width)
            sc = _sharpness(im, analysis_width)
            self.measured.append(sc)
            out.append((i, None, sc, _tiny(im) if tiny else None))
        return out

    def fetch(self, picks, sink):
        for p in picks:
            sink(p, self.open(p.key))


def make_source(inp: InputSpec):
    if inp.video:
        return VideoSource(inp.video)
    if inp.images:
        parents = {str(Path(p).parent) for p in inp.images}
        folder = parents.pop() if len(parents) == 1 else None      # all in one folder: name it after it
        return ImageSource(list(inp.images), name=Path(folder).name if folder else "imagenes", folder=folder)
    folder = Path(inp.folder)
    files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS
             and not p.name.startswith(".")
             and (inp.pattern is None or fnmatch.fnmatch(p.name.lower(), inp.pattern.lower()))]
    if not files:
        raise ToolError(f"input.folder: no images ({SUPPORTED_IMAGES}) in {folder}"
                        + (f' matching "{inp.pattern}"' if inp.pattern else ""))
    if len(files) > MAX_IMAGES:
        raise ToolError(f"input.folder: {len(files)} images in {folder}; the limit is {MAX_IMAGES}. "
                        "Narrow it with input.pattern or split the folder")
    files.sort(key=lambda p: _natural(p.name))
    return ImageSource([str(p) for p in files], name=folder.name or "imagenes", folder=str(folder))


# ============================================================================ selection
@dataclass(eq=False)
class Pick:
    key: int                       # frame number (video) or image index
    t: float | None = None         # seconds (video)
    sharpness: float | None = None
    tiny: Any = None
    note: Panel | None = None
    image: Image.Image | None = None
    exact_index: bool = True       # key is the decoded frame index (not estimated from time)


@dataclass
class Chosen:
    picks: list[Pick]
    dropped: list[Dropped]
    mode: str
    range_s: list[float] | None
    range_pct: list[float]
    measured: int
    threshold: float
    stats: dict[str, float] | None
    pct_of: Callable
    warnings: list[str] = field(default_factory=list)


def _given(sel: SelectSpec, name: str) -> bool:
    v = getattr(sel, name)
    return v not in (None, [], 0, 0.0) if name in ("snap_s", "min_gap_s") else v not in (None, [])


EXACT_FIELDS = ("step_pct", "step_s", "at_pct", "at_s", "at_frames", "at_images", "at_names")
VIDEO_ONLY = ("every_s", "step_s", "at_s", "at_frames", "snap_s", "start_s", "end_s", "min_gap_s")
IMAGE_ONLY = ("at_images", "at_names")
PANEL_POS = ("frame", "at_s", "at_pct", "image", "image_name")


def check_selection(kind: str, sel: SelectSpec, panels: list[Panel]) -> list[str]:
    """Problems that depend on the input kind or on several fields at once."""
    p = []
    if kind == "images":
        for f in VIDEO_ONLY:
            if _given(sel, f):
                p.append(f"select.{f}: only for video input; with images use count, every_pct, step_pct, "
                         "at_pct, at_images, at_names, start_pct or end_pct")
    else:
        for f in IMAGE_ONLY:
            if _given(sel, f):
                p.append(f"select.{f}: only for image input; with a video use at_frames, at_s or at_pct")
    if sel.every_s and sel.every_pct:
        p.append("select.every_s: give every_s or every_pct, not both")
    if sel.start_s is not None and sel.start_pct is not None:
        p.append("select.start_s: give start_s or start_pct, not both")
    if sel.end_s is not None and sel.end_pct is not None:
        p.append("select.end_s: give end_s or end_pct, not both")
    positioned = []
    for i, pn in enumerate(panels):
        given = [k for k in PANEL_POS if getattr(pn, k) is not None]
        if len(given) > 1:
            p.append(f"panels[{i}]: give only one position ({', '.join(given)} given)")
        if kind == "images" and set(given) & {"frame", "at_s"}:
            p.append(f"panels[{i}].{given[0]}: only for video input; with images use image, image_name or at_pct")
        if kind == "video" and set(given) & {"image", "image_name"}:
            p.append(f"panels[{i}].{given[0]}: only for image input; with a video use frame, at_s or at_pct")
        positioned.append(bool(given))
    if any(positioned) and not all(positioned):
        missing = [f"panels[{i}]" for i, x in enumerate(positioned) if not x]
        p.append("panels: either every panel has a position (frame, at_s, at_pct, image or image_name) or none "
                 f"has; without one: {', '.join(missing[:10])}{' …' if len(missing) > 10 else ''}")
    if any(positioned) and any(_given(sel, f) for f in EXACT_FIELDS):
        p.append("select: exact positions in select and positions in panels[] at the same time; use one of them")
    return p


def _stats(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    return {"min": round(min(values), 1), "p25": round(_percentile(values, 25), 1),
            "median": round(_percentile(values, 50), 1), "p75": round(_percentile(values, 75), 1),
            "max": round(max(values), 1)}


def choose(src, sel: SelectSpec, panels: list[Panel]) -> Chosen:
    video = src.kind == "video"
    warnings: list[str] = []
    n_items = src.count
    if n_items <= 0:
        raise ToolError("input: the video has no frames")
    if video:
        span = src.span
        t0 = sel.start_s if sel.start_s is not None else (span * sel.start_pct / 100 if sel.start_pct is not None else 0.0)
        t1 = sel.end_s if sel.end_s is not None else (span * sel.end_pct / 100 if sel.end_pct is not None else span)
        t0, t1 = max(0.0, t0), min(span, t1)
        if t1 < t0 or (t1 == t0 and span > 0 and (sel.start_s is not None or sel.start_pct is not None)):
            raise ToolError(f"select: empty range, start {t0:.2f} s is not before end {t1:.2f} s "
                            f"(the last frame is at {span:.2f} s)")
        rng_pct = [round(100 * t0 / span, 1) if span else 0.0, round(100 * t1 / span, 1) if span else 100.0]
        rng_s = [round(t0, 2), round(t1, 2)]
    else:
        last = n_items - 1
        i0 = round(last * sel.start_pct / 100) if sel.start_pct is not None else 0
        i1 = round(last * sel.end_pct / 100) if sel.end_pct is not None else last
        if i1 < i0:
            raise ToolError(f"select: empty range, start_pct gives image {i0} and end_pct image {i1}")
        rng_pct = [round(100 * i0 / last, 1) if last else 0.0, round(100 * i1 / last, 1) if last else 100.0]
        rng_s = None

    def pct_of(p: Pick) -> float:
        if video:
            return round(min(100.0, 100 * (p.t or 0) / src.span), 1) if src.span else 0.0
        return round(100 * p.key / (n_items - 1), 1) if n_items > 1 else 0.0

    pos_panels = bool(panels) and all(any(getattr(p, k) is not None for k in PANEL_POS) for p in panels)
    exact = any(_given(sel, f) for f in EXACT_FIELDS)
    ignored = [f for f in ("count", "every_s", "every_pct", "mode", "min_sharpness", "drop_blurriest_pct",
                           "min_gap_s", "min_difference") if f in sel.model_fields_set]
    if (pos_panels or exact) and ignored:
        warnings.append(f"select.{', select.'.join(ignored)}: not used with exact positions")

    # ---- panels choose themselves / exact positions
    if pos_panels or exact:
        picks: list[Pick] = []
        if video:
            if pos_panels:
                targets = [(p.frame / src.fps if p.frame is not None else p.at_s if p.at_s is not None
                            else src.span * p.at_pct / 100, p) for p in panels]
            else:
                ts: list[float] = []
                if sel.step_pct:
                    k = int(math.floor(100 / sel.step_pct + 1e-9))
                    ts += [t0 + (t1 - t0) * min(100.0, j * sel.step_pct) / 100 for j in range(k + 1)]
                if sel.step_s:
                    k = int(math.floor((t1 - t0) / sel.step_s + 1e-9))
                    ts += [t0 + j * sel.step_s for j in range(k + 1)]
                ts += [src.span * x / 100 for x in sel.at_pct or []]
                ts += list(sel.at_s or [])
                ts += [f / src.fps for f in sel.at_frames or []]
                late = [x for x in ts if x > src.span + 1.0 / src.fps]
                if late:
                    warnings.append(f"select: {len(late)} position(s) after the last frame ({src.span:.2f} s) "
                                    "were moved to the last frame")
                targets = [(x, None) for x in sorted({round(min(max(x, 0.0), src.span), 4) for x in ts})]
            late = [x for x, note in targets if note is not None and x > src.span + 1.0 / src.fps]
            if late:
                warnings.append(f"panels: {len(late)} position(s) after the last frame ({src.span:.2f} s) were "
                                "moved to the last frame")
            seen = set()
            for tg, note in targets:
                i, t, sc, im = src.near(min(max(tg, 0.0), src.span), sel.snap_s, sel.analysis_width)
                if note is None and i in seen:
                    continue
                seen.add(i)
                picks.append(Pick(key=i, t=t, sharpness=sc, note=note, image=im, exact_index=False))
        else:
            names = {p.name: k for k, p in enumerate(src.paths)}
            lower = {p.name.lower(): k for k, p in enumerate(src.paths)}

            def by_name(nm: str, where: str) -> int:
                k = names.get(nm, lower.get(nm.lower()))
                if k is None:
                    close = difflib.get_close_matches(nm, list(names), n=1, cutoff=0.8)
                    raise ToolError(f'{where}: no image named "{nm}" in the input'
                                    + (f' (did you mean "{close[0]}"?)' if close else ""))
                return k

            def by_index(k: int, where: str) -> int:
                if k >= n_items:
                    raise ToolError(f"{where}: image {k} does not exist; the input has {n_items} images "
                                    f"(indexes 0 to {n_items - 1})")
                return k

            if pos_panels:
                idx = []
                for j, p in enumerate(panels):
                    where = f"panels[{j}]"
                    k = (by_index(p.image, where + ".image") if p.image is not None else
                         by_name(p.image_name, where + ".image_name") if p.image_name is not None else
                         round((n_items - 1) * p.at_pct / 100))
                    idx.append((k, p))
            else:
                ks: set[int] = set()
                if sel.step_pct:
                    k = int(math.floor(100 / sel.step_pct + 1e-9))
                    ks |= {i0 + round((i1 - i0) * min(100.0, j * sel.step_pct) / 100) for j in range(k + 1)}
                ks |= {round((n_items - 1) * x / 100) for x in sel.at_pct or []}
                ks |= {by_index(k, f"select.at_images[{j}]") for j, k in enumerate(sel.at_images or [])}
                ks |= {by_name(nm, f"select.at_names[{j}]") for j, nm in enumerate(sel.at_names or [])}
                idx = [(k, None) for k in sorted(ks)]
            picks = [Pick(key=k, note=note) for k, note in idx]
            sharp = {m[0]: m[2] for m in src.measure(0, 0, sel.analysis_width, False,
                                                     only=sorted({k for k, _ in idx}))}
            for p in picks:
                p.sharpness = sharp[p.key]
        if len(picks) > MAX_PANELS:
            raise ToolError(f"select: {len(picks)} panels; the limit is {MAX_PANELS}")
        if not pos_panels:
            _notes_by_order(picks, panels, warnings)
        if video and len(picks) > 2:
            top = max(p.sharpness for p in picks)
            blurry = [str(k + 1) for k, p in enumerate(picks) if p.sharpness < 0.5 * top]
            if blurry:
                warnings.append(f"select: panel(s) {', '.join(blurry)} look blurred (sharpness under half the "
                                "sharpest panel's); "
                                + ("raise snap_s" if sel.snap_s else "add snap_s (e.g. 0.5)")
                                + " to use a sharp frame nearby, or move the positions (start_s)")
        return Chosen(picks, [], "panels" if pos_panels else "positions", rng_s, rng_pct, len(src.measured), 0.0,
                      _stats(src.measured), pct_of, warnings)

    # ---- stretches / best / all
    mode = sel.mode
    if mode == "auto":                          # images: every one, unless a number of panels is asked for
        mode = "sharpest" if (video or "count" in sel.model_fields_set or sel.every_pct) else "all"
    if mode in ("best", "all") and (sel.every_s or sel.every_pct):
        warnings.append(f'select.every_s / every_pct: not used with mode "{mode}"')
    need_tiny = sel.min_difference > 0 or mode == "best"
    meta = src.measure(t0, t1, sel.analysis_width, need_tiny) if video else \
        src.measure(i0, i1, sel.analysis_width, need_tiny)
    if not meta:
        raise ToolError("select: no frames in the requested range")
    sh = [m[2] for m in meta]
    thr = max(sel.min_sharpness, _percentile(sh, sel.drop_blurriest_pct) if sel.drop_blurriest_pct > 0 else 0.0)

    def pos(m) -> float:
        return m[1] if video else float(m[0])

    def ref(m) -> dict:
        return {"frame": m[0]} if video else {"image": m[0], "name": src.paths[m[0]].name}

    def clash(m, other, same: float | None = None) -> dict | None:
        """Why m cannot be kept next to another kept pick (as Dropped fields), or None."""
        if video and sel.min_gap_s > 0 and abs(m[1] - other[1]) < sel.min_gap_s:
            return {"reason": "min_gap_s", "similar_to": other[0]}
        limit = sel.min_difference if same is None else same
        if m[3] is not None and other[3] is not None and (limit > 0 or same is not None):
            d = _difference(m[3], other[3])
            if d < limit or (limit == 0 and d == 0):
                return {"reason": "min_difference", "similar_to": other[0], "difference": round(d, 2)}
        return None

    dropped: list[Dropped] = []
    kept: list = []
    asked, empty = 0, 0
    if mode == "all":
        for m in meta:
            why = {"reason": "min_sharpness"} if m[2] < thr else (clash(m, kept[-1]) if kept else None)
            if why:
                dropped.append(Dropped(**ref(m), **why))
            else:
                kept.append(m)
    elif mode == "best":
        asked = sel.count
        same = sel.min_difference if "min_difference" in sel.model_fields_set else 1.0
        repeats = 0
        for m in sorted((m for m in meta if m[2] >= thr), key=lambda m: -m[2]):
            if len(kept) >= sel.count:
                break
            why = next((w for w in (clash(m, k, same) for k in kept) if w), None)
            if why:
                repeats += 1
                if len(dropped) < 50:
                    dropped.append(Dropped(**ref(m), **why))
                continue
            kept.append(m)
        kept.sort(key=pos)
        if repeats:
            warnings.append(f"select: {repeats} picture(s) sharper than some chosen ones were left out as repeats "
                            f"(see dropped: similar_to, difference); min_difference {same:g} decides what counts "
                            "as a repeat (lower it to keep more, 0 = only exact repeats)")
    else:
        lo, hi = (t0, t1) if video else (float(i0), float(i1))
        width = hi - lo
        if sel.every_s or sel.every_pct:
            step = sel.every_s if sel.every_s else (width * sel.every_pct / 100 if width else 1.0)
            nseg = max(1, math.ceil(width / step - 1e-9)) if width > 0 else 1
            bounds = [lo + k * step for k in range(nseg)] + [hi + 1e-6]
        else:
            nseg = max(1, min(sel.count, len(meta)))
            bounds = [lo + k * width / nseg for k in range(nseg)] + [hi + 1e-6]
        asked = nseg
        seg: list[list] = [[] for _ in range(nseg)]
        k = 0
        for m in meta:
            while k < nseg - 1 and pos(m) >= bounds[k + 1] - 1e-9:
                k += 1
            if m[2] >= thr:
                seg[k].append(m)
        for k, cand in enumerate(seg):
            if not cand:
                empty += 1
                continue
            if mode == "sharpest":
                order = sorted(cand, key=lambda m: -m[2])
            else:
                mid = (bounds[k] + min(bounds[k + 1], hi)) / 2
                order = sorted(cand, key=lambda m: abs(pos(m) - mid))
            half = (bounds[k + 1] - bounds[k]) / 2
            pick, first_why = None, None
            # sharpest: keep picks at least half a stretch apart when possible; filters: take the
            # next best frame of the stretch instead of leaving it empty
            for spaced in ((True, False) if mode == "sharpest" else (False,)):
                for m in order:
                    if kept and spaced and pos(m) - pos(kept[-1]) < half:
                        continue
                    why = clash(m, kept[-1]) if kept else None
                    if why:
                        first_why = first_why or why
                        continue
                    pick = m
                    break
                if pick:
                    break
            if pick:
                kept.append(pick)
            else:
                dropped.append(Dropped(**ref(order[0]), **(first_why or {"reason": "min_difference"})))
    if not kept:
        raise ToolError("select: every frame was filtered out; lower min_sharpness, drop_blurriest_pct, "
                        "min_difference or min_gap_s")
    if len(kept) > MAX_PANELS:
        raise ToolError(f'select: {len(kept)} panels; the limit is {MAX_PANELS}. Use mode "sharpest", "even" '
                        'or "best" with count, or a shorter range')
    if asked and len(kept) < asked:
        why = []
        if dropped:
            reasons = {d.reason for d in dropped}
            fix = []
            if "min_difference" in reasons:
                fix.append(f"lower min_difference (now {sel.min_difference:g}; 2-3 suits a fixed background)")
            if "min_gap_s" in reasons:
                fix.append(f"lower min_gap_s (now {sel.min_gap_s:g})")
            why.append(f"{len(dropped)} removed by the filters (see dropped)"
                       + (f"; to get more, {' or '.join(fix)} or ask for fewer panels" if fix else ""))
        if empty:
            why.append(f"{empty} stretch(es) had nothing sharper than the threshold" if thr > 0 else
                       f"{empty} stretch(es) had no frames")
        if mode == "best" and not why:
            why.append("there are not that many different pictures")
        warnings.append(f"select: {asked} panels asked for, {len(kept)} given: " + "; ".join(why))
    picks = [Pick(key=m[0], t=m[1], sharpness=m[2], tiny=m[3]) for m in kept]
    _notes_by_order(picks, panels, warnings)
    return Chosen(picks, dropped, mode, rng_s, rng_pct, len(sh), round(thr, 1), _stats(sh), pct_of, warnings)


def _notes_by_order(picks: list[Pick], panels: list[Panel], warnings: list[str]):
    for k, p in enumerate(picks):
        p.note = panels[k] if k < len(panels) else None
    if len(panels) > len(picks):
        warnings.append(f"panels: {len(panels)} entries for {len(picks)} panels; panels[{len(picks)}] to "
                        f"panels[{len(panels) - 1}] were not used")


# ============================================================================ drawing
@dataclass
class Look:
    """Resolved style: colours as RGBA, fonts as paths, sizes in px."""
    bg: tuple
    text: tuple
    text2: tuple
    accent: tuple
    label_bg: tuple
    label_fg: tuple
    border: tuple
    notes_bg: tuple
    notes_fg: tuple
    rule: tuple
    guides: tuple
    border_width: int
    regular: str | None
    bold: str | None
    sharpen: float
    user_sizes: dict = field(default_factory=dict)
    title_size: int = 0
    text_size: int = 0
    label_size: int = 0
    number_size: int = 0
    notes_size: int = 0

    def font(self, size: float, bold: bool = False):
        return _ft(self.bold if bold else self.regular, max(6, int(round(size))))

    def set_sizes(self, panel_w: int, page_w: int):
        u = self.user_sizes
        self.text_size = u.get("text_size") or max(18, round(page_w / 64))
        self.title_size = u.get("title_size") or round(self.text_size * 1.3)
        self.label_size = u.get("label_size") or max(12, round(panel_w / 22))
        self.number_size = u.get("number_size") or round(self.label_size * 1.45)
        self.notes_size = u.get("notes_size") or max(11, round(self.label_size * 0.92))


SIZE_FIELDS = ("title_size", "text_size", "label_size", "number_size", "notes_size")


def resolve_look(style: StyleSpec, template: str) -> Look:
    base = THEMES[style.theme]
    col = {k: _rgba(getattr(style, k) or v) for k, v in base.items()}
    regular, bold = style.font, style.font_bold
    if regular:
        bold = bold or _bold_of(regular) or regular
    else:
        for r, b in DEFAULT_FONTS:
            if Path(r).is_file():
                regular, bold = r, (bold or (b if Path(b).is_file() else r))
                break
    bw = style.border_width if style.border_width is not None else TEMPLATES[template]["border"]
    return Look(bg=col["background"], text=col["text"], text2=col["text_secondary"], accent=col["accent"],
                label_bg=col["label_background"], label_fg=col["label_text"], border=col["panel_border"],
                notes_bg=col["notes_background"], notes_fg=col["notes_text"], rule=col["notes_rule"],
                guides=col["guides"], border_width=bw, regular=regular, bold=bold, sharpen=style.sharpen,
                user_sizes={k: getattr(style, k) for k in SIZE_FIELDS if getattr(style, k)})


def _fit_picture(im: Image.Image, w: int, h: int, fit: str, bg: tuple, sharpen: float) -> Image.Image:
    shrunk = im.width > w or im.height > h
    if fit == "cover":
        pic = Image.alpha_composite(Image.new("RGBA", (w, h), bg),
                                    ImageOps.fit(im.convert("RGBA"), (w, h), Image.LANCZOS))
    else:
        c = ImageOps.contain(im.convert("RGBA"), (w, h), Image.LANCZOS)
        pic = Image.new("RGBA", (w, h), bg)
        pic.alpha_composite(c, ((w - c.width) // 2, (h - c.height) // 2))
    if sharpen > 0 and shrunk:
        rgb = pic.convert("RGB").filter(ImageFilter.UnsharpMask(radius=1.0, percent=int(120 * sharpen), threshold=2))
        rgb.putalpha(pic.getchannel("A"))
        pic = rgb
    return pic


def _ellipsize(d: ImageDraw.ImageDraw, text: str, font, maxw: float) -> str:
    if d.textlength(text, font=font) <= maxw:
        return text
    while text and d.textlength(text + "…", font=font) > maxw:
        text = text[:-1]
    return (text.rstrip() + "…") if text else ""


def _wrap(d: ImageDraw.ImageDraw, text: str, font, width: float, first_width: float | None = None) -> list[str]:
    lines: list[str] = []
    for para in str(text).split("\n"):
        line = ""
        for word in para.split():
            avail = first_width if (first_width is not None and not lines) else width
            cand = f"{line} {word}".strip()
            if line and d.textlength(cand, font=font) > avail:
                lines.append(line)
                line = word
            else:
                line = cand
        lines.append(line)
    return lines


def _dashed(d: ImageDraw.ImageDraw, p0, p1, fill, width: int, dash: int):
    (x0, y0), (x1, y1) = p0, p1
    length = math.hypot(x1 - x0, y1 - y0)
    k = 0.0
    while k < length:
        e = min(length, k + dash)
        d.line((x0 + (x1 - x0) * k / length, y0 + (y1 - y0) * k / length,
                x0 + (x1 - x0) * e / length, y0 + (y1 - y0) * e / length), fill=fill, width=width)
        k += dash * 2


@dataclass
class View:
    """One panel ready to draw."""
    n: int
    number: str
    picture: Image.Image                 # RGBA, exactly panel_w × panel_h
    caption: str                         # caption text (overlay band, or next to the number)
    notes: list[tuple[str, str]]         # (title, text)
    notes_lines: int = 0


@dataclass
class Geo:
    template: str
    cols: int
    panel_w: int
    panel_h: int
    margin: int
    gap: int
    pad: int
    caption_h: int
    notes: bool
    notes_gap: int
    text_w: int
    page_w: int
    page_h: int | None
    header_h: int
    footer_h: int
    line_h: int
    lines: list[int]
    rows_per_page: int = 1

    def notes_height(self, lines: int) -> int:
        return 2 * self.pad + lines * self.line_h if (self.notes and lines) else 0

    def row_height(self, lines: int) -> int:
        nh = self.notes_height(lines)
        if self.template == "list":
            return max(self.panel_h, self.caption_h + (self.notes_gap + nh if nh else 0))
        return self.caption_h + self.panel_h + (self.notes_gap + nh if nh else 0)


def _notes_layout(d, look: Look, notes: list[tuple[str, str]], width: int, max_lines: int):
    """Lines of a notes box: each a list of (kind, text) with kind "title" or "text"."""
    ft, fb = look.font(look.notes_size), look.font(look.notes_size * 0.82, bold=True)
    lines: list[list[tuple[str, str]]] = []
    for title, text in notes:
        tw = d.textlength(title + "  ", font=fb)
        parts = _wrap(d, text, ft, width, first_width=max(10.0, width - tw))
        lines.append([("title", title), ("text", parts[0] if parts else "")])
        lines += [[("text", x)] for x in parts[1:]]
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        kind, txt = lines[-1][-1]
        lines[-1][-1] = (kind, txt.rstrip() + " …")
    return lines


def _plan(L: LayoutSpec, out: StoryboardOutput, look: Look, n: int, aspect: float, notes_on: bool,
          notes_texts: list[list[tuple[str, str]]]) -> Geo:
    t = TEMPLATES[L.template]
    cols = 1 if L.template == "list" else max(1, min(L.columns or t["columns"], n))
    gap = L.gap if L.gap is not None else t["gap"]
    margin = L.margin if L.margin is not None else t["margin"]
    fixed = L.page != "auto"
    page_w = page_h = None
    if fixed:
        short, long_ = PAGE_INCHES[L.page]
        w_in, h_in = (long_, short) if L.orientation == "landscape" else (short, long_)
        page_w, page_h = round(w_in * out.dpi), round(h_in * out.dpi)
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    scale = 1.0
    for _ in range(16):
        if fixed:
            avail = page_w - 2 * margin
            base = avail * 0.45 if L.template == "list" else (avail - (cols - 1) * gap) / cols
            panel_w = max(64, int(base * scale))
        else:
            panel_w = L.panel_width
        panel_h = max(16, round(panel_w / aspect))
        text_w = 0
        if L.template == "list":
            text_w = (page_w - 2 * margin - panel_w - gap) if fixed else max(360, panel_w)
        content_w = cols * (panel_w + (gap + text_w if L.template == "list" else 0)) + (cols - 1) * gap
        look.set_sizes(panel_w, page_w if fixed else content_w + 2 * margin)
        pad = L.padding if L.padding is not None else max(6, round(look.text_size * 0.5))
        line_h = round(look.notes_size * 1.4)
        header_h = pad * 2 + round(look.title_size * 1.2) + pad // 2 + round(look.text_size * 1.3) if L.header else 0
        footer_h = pad * 2 + round(look.text_size * 1.3) if L.footer else 0
        caption_h = round(look.number_size * 1.25) + pad // 2 if t["caption"] == "line" else 0
        nw = (text_w if L.template == "list" else panel_w) - 2 * pad
        lines = [len(_notes_layout(probe, look, nt, nw, L.notes.max_lines)) if nt else
                 (L.notes.blank_lines if notes_on else 0) for nt in notes_texts]
        geo = Geo(L.template, cols, panel_w, panel_h, margin, gap, pad, caption_h, notes_on, max(4, pad // 2),
                  text_w, page_w if fixed else content_w + 2 * margin, page_h, header_h, footer_h, line_h, lines)
        if not fixed:
            return geo
        target = L.rows or min(DEFAULT_ROWS[L.template][0 if L.orientation == "landscape" else 1],
                               math.ceil(n / cols))
        need = target * geo.row_height(max(lines or [0])) + (target - 1) * gap
        avail_h = page_h - 2 * margin - (header_h + gap if header_h else 0) - (footer_h + gap if footer_h else 0)
        if need <= avail_h or panel_w <= 64:
            return geo
        scale *= max(0.3, min(0.98, avail_h / need))
    return geo


def _draw_notes(layer: Image.Image, box, v: View, look: Look, geo: Geo, max_lines: int, blank_lines: int):
    x, y, w, h = box
    d = ImageDraw.Draw(layer)
    d.rectangle((x, y, x + w - 1, y + h - 1), fill=look.notes_bg)
    if look.border_width:
        d.rectangle((x, y, x + w - 1, y + h - 1), outline=look.border, width=look.border_width)
    pad = geo.pad
    ft, fb = look.font(look.notes_size), look.font(look.notes_size * 0.82, bold=True)
    if v.notes:
        yy = y + pad
        for line in _notes_layout(d, look, v.notes, w - 2 * pad, max_lines):
            xx = x + pad
            for kind, txt in line:
                if kind == "title":
                    d.text((xx, yy + geo.line_h * 0.5), txt, font=fb, fill=look.accent, anchor="lm")
                    xx += d.textlength(txt + "  ", font=fb)
                else:
                    d.text((xx, yy + geo.line_h * 0.5), txt, font=ft, fill=look.notes_fg, anchor="lm")
            yy += geo.line_h
    else:                                     # ruled lines down the whole box, to write by hand
        for k in range(1, max(blank_lines, (h - 2 * pad) // geo.line_h) + 1):
            yy = y + pad + k * geo.line_h - 2
            if yy < y + h - pad // 2:
                d.line((x + pad, yy, x + w - pad, yy), fill=look.rule, width=1)


def _draw_guides(pic: Image.Image, g: GuidesSpec, look: Look, label_font) -> Image.Image:
    if not (g.aspects or g.safe_zone or g.thirds):
        return pic
    w, h = pic.size
    over = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    lw = max(1, round(w / 400))
    for k, a in enumerate(g.aspects):
        r = ASPECT_RATIOS[a]
        if r > w / h + 1e-6:
            gh = w / r
            box = (0, (h - gh) / 2, w - 1, (h + gh) / 2)
        else:
            gw = h * r
            box = ((w - gw) / 2, 0, (w + gw) / 2, h - 1)
        if k == 0 and g.shade:
            for rect in ((0, 0, w, box[1]), (0, box[3], w, h), (0, 0, box[0], h), (box[2], 0, w, h)):
                if rect[2] - rect[0] > 0.5 and rect[3] - rect[1] > 0.5:
                    d.rectangle(rect, fill=(0, 0, 0, 120))
        d.rectangle(box, outline=look.guides, width=lw)
        d.text((box[0] + 4 * lw, box[1] + 3 * lw), a, font=label_font, fill=look.guides, anchor="la")
    if g.safe_zone:
        inset = (100 - g.safe_zone_pct) / 200
        x0, y0, x1, y1 = w * inset, h * inset, w * (1 - inset), h * (1 - inset)
        dash = max(4, round(w / 60))
        for p0, p1 in (((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)), ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))):
            _dashed(d, p0, p1, look.guides, lw, dash)
    if g.thirds:
        for k in (1, 2):
            d.line((w * k / 3, 0, w * k / 3, h), fill=look.guides, width=lw)
            d.line((0, h * k / 3, w, h * k / 3), fill=look.guides, width=lw)
    return Image.alpha_composite(pic, over)


def _overlay_label(pic: Image.Image, text: str, look: Look, position: str) -> Image.Image:
    w, h = pic.size
    fs = look.label_size
    band = fs + max(8, fs // 2)
    over = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    y0 = 0 if position == "top" else h - band
    d.rectangle((0, y0, w, y0 + band - 1), fill=look.label_bg)
    size, f = fs, look.font(fs, bold=True)
    while d.textlength(text, font=f) > w - 12 and size > max(8, fs * 0.6):
        size -= 1
        f = look.font(size, bold=True)
    d.text((6, y0 + band / 2), _ellipsize(d, text, f, w - 12), font=f, fill=look.label_fg, anchor="lm")
    return Image.alpha_composite(pic, over)


def _caption_line(d, x, y, w, v: View, look: Look, show_caption: bool):
    fn = look.font(look.number_size, bold=True)
    base = y + round(look.number_size * 0.95)
    d.text((x, base), v.number, font=fn, fill=look.text, anchor="ls")
    if show_caption and v.caption:
        fc = look.font(look.label_size)
        nw = d.textlength(v.number + "   ", font=fn)
        d.text((x + w, base), _ellipsize(d, v.caption, fc, w - nw), font=fc, fill=look.text2, anchor="rs")


def _draw_cell(layer, x, y, v: View, geo: Geo, look: Look, L: LayoutSpec, with_labels: bool, row_h: int):
    d = ImageDraw.Draw(layer)
    bw = look.border_width
    pic = _draw_guides(v.picture, L.guides, look, look.font(max(9, look.label_size * 0.75), bold=True))
    if TEMPLATES[geo.template]["caption"] == "overlay" and with_labels and L.label.show and v.caption:
        pic = _overlay_label(pic, v.caption, look, L.label.position)
    if geo.template == "list":
        px, py = x, y
        tx = x + geo.panel_w + geo.gap
        _caption_line(d, tx, y, geo.text_w, v, look, with_labels and L.label.show)
        nh = row_h - geo.caption_h - geo.notes_gap
        if geo.notes and v.notes_lines and nh > 0:
            _draw_notes(layer, (tx, y + geo.caption_h + geo.notes_gap, geo.text_w, nh), v, look, geo,
                        L.notes.max_lines, L.notes.blank_lines)
    else:
        if geo.caption_h:
            _caption_line(d, x, y, geo.panel_w, v, look, with_labels and L.label.show)
        px, py = x, y + geo.caption_h
        nh = row_h - geo.caption_h - geo.panel_h - geo.notes_gap
        if geo.notes and v.notes_lines and nh > 0:
            _draw_notes(layer, (x, py + geo.panel_h + geo.notes_gap, geo.panel_w, nh), v, look, geo,
                        L.notes.max_lines, L.notes.blank_lines)
    if bw:
        d.rectangle((px - bw, py - bw, px + geo.panel_w + bw - 1, py + geo.panel_h + bw - 1), outline=look.border,
                    width=bw)
    return pic, (px, py)


def render_pages(views: list[View], geo: Geo, look: Look, L: LayoutSpec, header: tuple[str, str],
                 footer: str, strings: dict, with_labels: bool, cut: set | None = None) -> list[Image.Image]:
    per_page = geo.cols * geo.rows_per_page
    chunks = [views[k:k + per_page] for k in range(0, len(views), per_page)]
    pages = []
    cell_w = geo.panel_w + (geo.gap + geo.text_w if geo.template == "list" else 0)
    content_w = geo.cols * cell_w + (geo.cols - 1) * geo.gap
    title, subtitle = header
    inset = geo.pad if geo.margin < geo.pad else 0          # text never touches the page edge
    for pno, chunk in enumerate(chunks, 1):
        rows = [chunk[k:k + geo.cols] for k in range(0, len(chunk), geo.cols)]
        row_hs = [max(geo.row_height(v.notes_lines) for v in r) for r in rows]
        body_h = sum(row_hs) + geo.gap * (len(rows) - 1)
        W = geo.page_w
        H = geo.page_h or (geo.margin * 2 + (geo.header_h + geo.gap if geo.header_h else 0) + body_h
                           + (geo.gap + geo.footer_h if geo.footer_h else 0))
        if W * H > MAX_PAGE_PIXELS:
            raise ToolError(f"a page would be {W}×{H} px; the limit is {MAX_PAGE_PIXELS // 1_000_000} Mpx. "
                            "Use layout.rows to split the panels into pages, or a smaller layout.panel_width")
        page = Image.new("RGBA", (W, H), look.bg)
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        x0 = (W - content_w) // 2
        tx, right = x0 + inset, x0 + content_w - inset
        y = geo.margin
        page_txt = strings["page"].format(p=pno, n=len(chunks)) if len(chunks) > 1 else ""
        fs = look.font(look.text_size)
        if geo.header_h:
            ft = look.font(look.title_size, bold=True)
            yy = y + geo.pad
            if page_txt and not geo.footer_h:
                d.text((right, yy), page_txt, font=fs, fill=look.text2, anchor="ra")
            room = right - tx - (d.textlength(page_txt + "   ", font=fs) if page_txt and not geo.footer_h else 0)
            if title:
                shown = _ellipsize(d, title, ft, room)
                if shown != title and cut is not None:
                    cut.add("layout.title")
                d.text((tx, yy), shown, font=ft, fill=look.text, anchor="la")
                yy += round(look.title_size * 1.2) + geo.pad // 2
            if subtitle:
                shown = _ellipsize(d, subtitle, fs, room)
                if shown != subtitle and cut is not None:
                    cut.add("layout.subtitle")
                d.text((tx, yy), shown, font=fs, fill=look.text2, anchor="la")
            y += geo.header_h + geo.gap
        pastes = []
        for r, rh in zip(rows, row_hs):
            x = x0
            for v in r:
                pastes.append(_draw_cell(layer, x, y, v, geo, look, L, with_labels, rh))
                x += cell_w + geo.gap
            y += rh + geo.gap
        if geo.footer_h:
            fy = (H - geo.margin - geo.footer_h) if geo.page_h else y
            pw = d.textlength(page_txt + "   ", font=fs) if page_txt else 0
            txt = footer
            while txt and d.textlength(txt, font=fs) > right - tx - pw and " · " in txt:
                txt = txt.rsplit(" · ", 1)[0]
                if d.textlength(txt + " …", font=fs) <= right - tx - pw:
                    txt += " …"
                    break
            shown = _ellipsize(d, txt, fs, right - tx - pw)
            if shown != footer and cut is not None and L.footer_text is not None:
                cut.add("layout.footer_text")
            d.text((tx, fy + geo.pad), shown, font=fs, fill=look.text2, anchor="la")
            if page_txt:
                d.text((right, fy + geo.pad), page_txt, font=fs, fill=look.text2, anchor="ra")
        page = Image.alpha_composite(page, layer)
        for pic, (px, py) in pastes:
            page.alpha_composite(pic, (px, py))
        pages.append(page)
    return pages


# ============================================================================ tools
def _default_dir(src, sub: str = "") -> Path:
    safe = re.sub(r"[^\w.-]+", "_", src.name).strip("._") or "input"
    return Path(tempfile.gettempdir()) / "storyboard-mcp" / safe / sub


def _out_dir(given: str | None, default: Path) -> Path:
    d = _abs(given) if given else default
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ToolError(f"output.dir: cannot create {d} ({e.strerror})")
    return d


def tool_media_info(args: MediaInfoArgs):
    return make_source(args.input).result(), []


def _anchor(i: int, src) -> str | None:
    if src.kind != "video":
        return None
    if i == 0:
        return "first_frame"
    if src.frames and i == src.frames - 1:
        return "last_frame"
    return None


def _label_text(fields, template, src, number: str, p: Pick, pct: float, strings, sharp_pct) -> str:
    anchor = _anchor(p.key, src)
    anchor_txt = strings[anchor] if anchor else ""
    if template is not None:
        vals = {"number": number, "n": number, "pct": f"{pct:.0f}", "pct1": f"{pct:.1f}",
                "sharpness": f"{p.sharpness:.0f}" if p.sharpness is not None else "",
                "sharpness_pct": sharp_pct}
        if src.kind == "video":
            vals |= {"frame": p.key, "frames_total": src.frames, "time": f"{_tenths(p.t):.1f}", "timecode": _hms(p.t),
                     "anchor": anchor_txt}
        else:
            info = src.infos()[p.key]
            vals |= {"image": p.key, "name": info.name, "images_total": src.count,
                     "size": f"{info.displayed_size[0]}×{info.displayed_size[1]}"}
        return template.format(**vals).strip()
    parts = []
    for f in fields:
        if f == "number":
            parts.append(number)
        elif f in ("frame", "image"):
            parts.append(f"#{p.key}")
        elif f == "time":
            parts.append(f"{_tenths(p.t):.1f}s")
        elif f == "timecode":
            parts.append(_hms(p.t))
        elif f == "pct":
            parts.append(f"{pct:.0f}%")
        elif f == "sharpness" and p.sharpness is not None:
            parts.append(f"{strings['sharpness']} {p.sharpness:.0f}")
        elif f == "sharpness_pct":
            parts.append(f"{strings['sharpness']} {sharp_pct}%")
        elif f == "anchor" and anchor_txt:
            parts.append(anchor_txt)
        elif f == "name":
            parts.append(src.infos()[p.key].name)
        elif f == "size":
            s = src.infos()[p.key].displayed_size
            parts.append(f"{s[0]}×{s[1]}")
    return " · ".join(parts)


def check_layout(kind: str, L: LayoutSpec, out: StoryboardOutput | None) -> list[str]:
    p = []
    for k, f in enumerate(L.label.fields or []):
        if kind == "images" and f in VIDEO_LABELS:
            p.append(f'layout.label.fields[{k}]: "{f}" is only for video input; with images use number, image, '
                     "name, size, pct, sharpness or sharpness_pct")
        if kind == "video" and f in IMAGE_LABELS:
            p.append(f'layout.label.fields[{k}]: "{f}" is only for image input; with a video use number, frame, '
                     "time, timecode, pct, sharpness, sharpness_pct or anchor")
    if L.label.template is not None:
        keys: set[str] = set()
        try:
            keys = {fn.split(".")[0].split("[")[0].split("!")[0].split(":")[0]
                    for _, fn, _, _ in string.Formatter().parse(L.label.template) if fn}
        except ValueError as e:
            p.append(f"layout.label.template: {e} (use {{placeholder}}; write {{{{ and }}}} for braces)")
        for k in sorted(keys - TEMPLATE_KEYS):
            p.append(f"layout.label.template: unknown placeholder {{{k}}}; available: "
                     + " ".join("{" + x + "}" for x in sorted(TEMPLATE_KEYS)))
        for k in sorted(keys & (VIDEO_KEYS if kind == "images" else IMAGE_KEYS)):
            p.append(f"layout.label.template: {{{k}}} is only for {'video' if kind == 'images' else 'image'} input")
    try:
        L.numbering.format.format(n=1)
        if "{n" not in L.numbering.format:
            p.append('layout.numbering.format: must contain {n}, e.g. "{n}" or "5.{n}"')
    except (KeyError, IndexError, ValueError) as e:
        p.append(f'layout.numbering.format: "{L.numbering.format}" is not valid ({e}); use {{n}}, e.g. '
                 '"SH{n:03d}"')
    return p


CSV_ES = {"n": "n", "number": "viñeta", "page": "página", "frame": "fotograma", "time_s": "segundos",
          "timecode": "tiempo", "pct": "%", "image": "imagen", "name": "nombre", "shot": "plano",
          "camera": "cámara", "action": "acción", "dialogue": "diálogo", "sound": "sonido",
          "transition": "transición", "duration_s": "duración (s)", "sharpness": "nitidez", "file": "archivo",
          "size": "tamaño", "source": "original"}


def _flatten(im: Image.Image) -> Image.Image:
    if im.mode == "RGBA":
        return Image.alpha_composite(Image.new("RGBA", im.size, (255, 255, 255, 255)), im).convert("RGB")
    return im.convert("RGB")


def _preview(page: Image.Image, longest: int = 1200) -> mt.ImageContent:
    im = _flatten(page)
    im.thumbnail((longest, longest), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=82)
    return mt.ImageContent(type="image", data=base64.b64encode(buf.getvalue()).decode(), mimeType="image/jpeg")


def _selection_result(c: Chosen) -> SelectionResult:
    return SelectionResult(mode=c.mode, range_s=c.range_s, range_pct=c.range_pct, measured=c.measured,
                           threshold=c.threshold, sharpness_stats=c.stats)


def tool_make_storyboard(args: StoryboardArgs):
    src = make_source(args.input)
    L, S, O = args.layout, args.style, args.output
    problems = check_selection(src.kind, args.select, args.panels) + check_layout(src.kind, L, O)
    if problems:
        raise ToolError(_problems_text("make_storyboard", problems))
    chosen = choose(src, args.select, args.panels)
    picks = chosen.picks
    strings = STRINGS[L.language]
    look = resolve_look(S, L.template)
    t = TEMPLATES[L.template]
    aspect = ASPECT_RATIOS[L.aspect] if L.aspect != "source" else src.aspect
    has_notes = any(p.note and any(getattr(p.note, f) not in (None, "") for f in
                                   ("shot", "camera", "action", "dialogue", "sound", "transition", "duration_s"))
                    for p in picks)
    notes_on = L.notes.show if L.notes.show is not None else (has_notes if L.template == "strip" else t["notes"])

    def note_items(p: Pick) -> list[tuple[str, str]]:
        if not (notes_on and p.note):
            return []
        out = []
        for f in L.notes.fields:
            val = getattr(p.note, f)
            if val not in (None, ""):
                out.append((strings["notes"][f], f"{val:g} s" if f == "duration_s" else str(val)))
        return out

    notes_texts = [note_items(p) for p in picks]
    geo = _plan(L, O, look, len(picks), aspect, notes_on, notes_texts)
    if geo.page_h:
        avail_h = geo.page_h - 2 * geo.margin - (geo.header_h + geo.gap if geo.header_h else 0) - \
            (geo.footer_h + geo.gap if geo.footer_h else 0)
        row_h = geo.row_height(max(geo.lines or [0]))
        fit_rows = max(1, (avail_h + geo.gap) // (row_h + geo.gap))
        geo.rows_per_page = min(L.rows, fit_rows) if L.rows else min(fit_rows, math.ceil(len(picks) / geo.cols))
        if L.rows and L.rows > fit_rows:     # only when the panels cannot shrink any more
            chosen.warnings.append(f"layout.rows: only {fit_rows} row(s) fit on a {L.page} page; using {fit_rows}")
    else:
        geo.rows_per_page = L.rows or (1 if L.template == "strip" else math.ceil(len(picks) / geo.cols))
    per_page = geo.cols * geo.rows_per_page
    pages_n = math.ceil(len(picks) / per_page)
    if pages_n > MAX_PAGES:
        raise ToolError(f"{pages_n} pages; the limit is {MAX_PAGES}. Use more layout.rows or columns, or fewer panels")

    out_dir = _out_dir(O.dir, _default_dir(src))
    name = O.name or f"{src.name}_{L.template}"
    frames_dir = None
    if O.frames:
        frames_dir = out_dir / f"{name}_frames"
        frames_dir.mkdir(parents=True, exist_ok=True)
    order = {id(p): k for k, p in enumerate(picks)}
    files: dict[int, str] = {}
    pictures: dict[int, Image.Image] = {}

    def sink(p: Pick, im: Image.Image):
        k = order[id(p)]
        if p.sharpness is None:
            p.sharpness = _sharpness(im, args.select.analysis_width)
        if frames_dir is not None:
            if src.kind == "video":
                dest = frames_dir / f"f{p.key:05d}.jpg"
                im.convert("RGB").save(dest, quality=95)
            else:
                dest = frames_dir / f"{k + 1:03d}_{src.paths[p.key].name}"
                shutil.copy2(src.paths[p.key], dest)
            files[k] = (str(dest), [im.width, im.height])
        pictures[k] = _fit_picture(im, geo.panel_w, geo.panel_h, L.fit, look.bg, look.sharpen)

    src.fetch(picks, sink)
    top = max((p.sharpness or 0) for p in picks) or 1.0
    fields = L.label.fields or (["number", "time", "frame", "anchor"] if src.kind == "video" else ["number", "name"])
    if t["caption"] == "line":
        fields = [f for f in fields if f != "number"]
    views, results = [], []
    for k, p in enumerate(picks):
        number = p.note.number if (p.note and p.note.number) else \
            L.numbering.format.format(n=L.numbering.start + k * L.numbering.step)
        pct = chosen.pct_of(p)
        sp = round(100 * (p.sharpness or 0) / top)
        caption = _label_text(fields, L.label.template, src, number, p, pct, strings, sp)
        views.append(View(k + 1, number, pictures.pop(k), caption, notes_texts[k], geo.lines[k]))
        notes = {kk: vv for kk, vv in p.note.model_dump(exclude_none=True).items()
                 if kk not in PANEL_POS and kk != "number"} if p.note else {}
        r = PanelResult(n=k + 1, number=number, page=k // per_page + 1, pct=pct,
                        sharpness=round(p.sharpness, 1) if p.sharpness is not None else None, sharpness_pct=sp,
                        file=files[k][0] if k in files else None, size=files[k][1] if k in files else None,
                        notes=notes or None)
        if src.kind == "video":
            r.frame, r.time_s, r.timecode, r.anchor = p.key, round(p.t, 3), _hms(p.t), _anchor(p.key, src)
        else:
            r.image, r.name, r.source = p.key, src.paths[p.key].name, str(src.paths[p.key])
        results.append(r)

    if src.kind == "video":
        info = src.info
        title = Path(src.path).name
        sub = [f"{info.displayed_size[0]}×{info.displayed_size[1]}"]
        if info.fps:
            sub.append(f"{info.fps:.3g} fps")
        sub.append(_hms(src.duration))
        if info.frames:
            sub.append(f"{info.frames:,} {strings['frames']}".replace(",", "."))
        if info.codec:
            sub.append(info.codec)
    else:
        res = src.result(with_items=False)
        title = Path(src.folder).name if src.folder else f"{src.count} {strings['images']}"
        shown = (f"{len(picks)} {strings['of']} " if len(picks) < src.count else "")
        sub = [f"{shown}{src.count} {strings['images']}" if (src.folder or shown) else "",
               ", ".join(sorted(res.formats)),
               ", ".join(f"{w}×{h}" for w, h in res.sizes[:3]) + (" …" if len(res.sizes) > 3 else "")]
    title = L.title if L.title is not None else title
    subtitle = L.subtitle if L.subtitle is not None else " · ".join(x for x in sub if x)
    if not (title or subtitle):
        geo.header_h = 0
    word = strings["frames"] if src.kind == "video" else strings["images"]
    modes = strings.get("mode_images", strings["mode"]) if src.kind == "images" else strings["mode"]
    foot = [f"{len(picks)} {word} ({modes[chosen.mode]})"]
    if chosen.range_s:
        foot.append(f"{strings['range']} {_hms(chosen.range_s[0])}–{_hms(chosen.range_s[1])} "
                    f"({chosen.range_pct[0]:.0f}–{chosen.range_pct[1]:.0f} %)")
    elif chosen.range_pct != [0.0, 100.0]:
        foot.append(f"{strings['range']} {chosen.range_pct[0]:.0f}–{chosen.range_pct[1]:.0f} %")
    if chosen.measured:
        foot.append(f"{strings['measured']} {chosen.measured:,}".replace(",", "."))
    if chosen.stats:
        s = chosen.stats
        foot.append(f"{strings['sharpness']} {strings['min']} {s['min']:.0f} · {strings['median']} "
                    f"{s['median']:.0f} · {strings['max']} {s['max']:.0f}")
    if chosen.dropped:
        foot.append(f"{strings['dropped']} {len(chosen.dropped)}")
    if chosen.threshold > 0:
        foot.append(f"{strings['threshold']} {chosen.threshold:.0f}")
    footer = L.footer_text if L.footer_text is not None else " · ".join(foot)

    def write(imgs: list[Image.Image], base: str) -> tuple[str | None, list[str]]:
        if O.format == "pdf":
            path = out_dir / f"{base}.pdf"
            flat = [_flatten(p) for p in imgs]
            flat[0].save(path, "PDF", save_all=True, append_images=flat[1:], resolution=float(O.dpi),
                         quality=O.quality)
            return str(path), []
        paths = []
        for k, im in enumerate(imgs, 1):
            path = out_dir / f"{base}{f'_p{k}' if len(imgs) > 1 else ''}.{O.format}"
            if O.format == "png":
                (im if look.bg[3] < 255 else im.convert("RGB")).save(path, optimize=True, dpi=(O.dpi, O.dpi))
            else:
                _flatten(im).save(path, quality=O.quality, dpi=(O.dpi, O.dpi))
            paths.append(str(path))
        return None, paths

    cut: set[str] = set()
    pages = render_pages(views, geo, look, L, (title, subtitle), footer, strings, with_labels=True, cut=cut)
    if cut:
        chosen.warnings.append(f"{', '.join(sorted(cut))}: shortened with … to fit the page; use a shorter text, a "
                               "smaller style.title_size / text_size or a wider page")
    pdf, paths = write(pages, name)
    page_results = [PageResult(page=k, path=paths[k - 1] if paths else pdf, size=list(pg.size),
                               panels=[v.n for v in views[(k - 1) * per_page:k * per_page]])
                    for k, pg in enumerate(pages, 1)]
    nolabels = None
    if O.also_without_labels:
        bare = render_pages(views, geo, look, L, (title, subtitle), footer, strings, with_labels=False)
        p2, paths2 = write(bare, f"{name}_nolabels")
        nolabels = [p2] if p2 else paths2
    shot = None
    if O.shot_list:
        shot = {}
        rows = [r.model_dump(exclude_none=True) for r in results]
        if "json" in O.shot_list:
            pj = out_dir / f"{name}_shots.json"
            pj.write_text(json.dumps({"input": src.result(with_items=False).model_dump(exclude_none=True),
                                      "panels": rows}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
            shot["json"] = str(pj)
        if "csv" in O.shot_list:
            pc = out_dir / f"{name}_shots.csv"
            note_cols = ("shot", "camera", "action", "dialogue", "sound", "transition", "duration_s")
            cols = ["n", "number", "page", "frame", "time_s", "timecode", "pct", "image", "name", *note_cols,
                    "sharpness", "file", "size", "source"]
            excel = O.csv_style == "excel_es"

            def cell(c, v):
                if v in (None, ""):
                    return ""
                if c == "size":
                    return "×".join(map(str, v))
                if excel and c == "number":
                    return f'="{v}"'                    # stays text: "3.1" is not read as a date
                if excel and isinstance(v, float):
                    return f"{v:g}".replace(".", ",")
                return v

            table = [[cell(c, (r.get("notes") or {}).get(c) if c in note_cols else r.get(c)) for c in cols]
                     for r in rows]
            if excel:                               # for people: Spanish headers, no always-empty columns
                keep = [j for j in range(len(cols)) if any(row[j] != "" for row in table)]
                cols = [CSV_ES.get(cols[j], cols[j]) for j in keep]
                table = [[row[j] for j in keep] for row in table]
            with open(pc, "w", newline="", encoding="utf-8-sig") as fh:   # BOM: Excel opens the accents right
                w = csv.writer(fh, delimiter=";" if excel else ",")
                w.writerow(cols)
                w.writerows(table)
            shot["csv"] = str(pc)
    result = StoryboardResult(
        pages=page_results, pdf=pdf, pages_without_labels=nolabels, shot_list=shot,
        frames_dir=str(frames_dir) if frames_dir else None, panels=results, dropped=chosen.dropped,
        selection=_selection_result(chosen),
        layout={"template": L.template, "columns": geo.cols, "rows_per_page": geo.rows_per_page,
                "picture_size": [geo.panel_w, geo.panel_h], "page": L.page, "page_size": list(pages[0].size),
                "theme": S.theme},
        input=src.result(with_items=False), warnings=chosen.warnings)
    return result, ([_preview(pages[0])] if O.preview else [])


def _save_still(im: Image.Image, dest: Path, fmt: str, quality: int):
    if fmt == "png":
        im.save(dest, optimize=True)
    else:
        _flatten(im).save(dest, quality=quality)


def tool_extract_frames(args: FramesArgs):
    src = make_source(args.input)
    problems = check_selection(src.kind, args.select, [])
    fmt = args.output.format or ("jpg" if src.kind == "video" else "original")
    if src.kind == "video" and fmt == "original":
        problems.append('output.format: "original" is only for images; video frames are written as "jpg" or "png"')
    if problems:
        raise ToolError(_problems_text("extract_frames", problems))
    chosen = choose(src, args.select, [])
    out_dir = _out_dir(args.output.dir, _default_dir(src, "frames"))
    results: dict[int, PanelResult] = {}
    order = {id(p): k for k, p in enumerate(chosen.picks)}
    used: set[str] = set()

    def dest_name(k: int, orig: Path, ext: str) -> str:
        if args.output.names == "numbered":
            return f"{k + 1:03d}_{orig.stem}{ext}"
        base, n = f"{orig.stem}{ext}", 2
        while base.lower() in used:
            base, n = f"{orig.stem}_{n}{ext}", n + 1
        used.add(base.lower())
        return base

    def sink(p: Pick, im: Image.Image):
        k = order[id(p)]
        if p.sharpness is None:
            p.sharpness = _sharpness(im, args.select.analysis_width)
        if src.kind == "video":
            dest = out_dir / f"f{p.key:05d}.{fmt}"
            _save_still(im, dest, fmt, args.output.quality)
            r = PanelResult(n=k + 1, number=str(k + 1), frame=p.key, time_s=round(p.t, 3), timecode=_hms(p.t),
                            pct=chosen.pct_of(p), anchor=_anchor(p.key, src), file=str(dest))
        else:
            orig = src.paths[p.key]
            if fmt == "original":
                dest = out_dir / dest_name(k, orig, orig.suffix)
                shutil.copy2(orig, dest)
            else:
                dest = out_dir / dest_name(k, orig, f".{fmt}")
                _save_still(im, dest, fmt, args.output.quality)
            r = PanelResult(n=k + 1, number=str(k + 1), image=p.key, name=orig.name, source=str(orig),
                            pct=chosen.pct_of(p), file=str(dest))
        r.sharpness, r.size = round(p.sharpness, 1), [im.width, im.height]
        results[k] = r

    src.fetch(chosen.picks, sink)
    top = max((r.sharpness or 0) for r in results.values()) or 1.0
    for r in results.values():
        r.sharpness_pct = round(100 * (r.sharpness or 0) / top)
    return FramesResult(dir=str(out_dir), files=[results[k] for k in sorted(results)], dropped=chosen.dropped,
                        selection=_selection_result(chosen), input=src.result(with_items=False),
                        warnings=chosen.warnings), []


# ============================================================================ registry
EXAMPLES: dict[str, dict[str, Any]] = {
    "media_info": {
        "minimal": {"input": {"video": "/Users/ana/Movies/clip.mp4"}},
        "full": {"input": {"folder": "/Users/ana/Pictures/rodaje", "pattern": "*.jpg"}},
    },
    "make_storyboard": {
        "minimal": {"input": {"video": "/Users/ana/Movies/clip.mp4"}},
        "full": {
            "input": {"video": "/Users/ana/Movies/clip.mp4"},
            "select": {"at_s": [1.5, 6, 12.5, 20], "snap_s": 0.5},
            "panels": [
                {"shot": "PG", "camera": "fijo", "action": "Amanece sobre la ciudad.", "sound": "tráfico lejano"},
                {"shot": "PM", "camera": "travelling lateral", "action": "Ana cruza la calle.",
                 "dialogue": "ANA: Llego tarde.", "transition": "corte"},
                {"number": "2A", "shot": "PP", "action": "Mira el reloj.", "duration_s": 2.5},
                {"shot": "PG", "camera": "grúa ascendente", "transition": "fundido a negro"},
            ],
            "layout": {"template": "storyboard", "columns": 2, "page": "A4", "orientation": "landscape",
                       "numbering": {"start": 1, "format": "5.{n}"},
                       "label": {"fields": ["timecode", "frame"]},
                       "guides": {"aspects": ["2.39:1"], "thirds": False}, "title": "Escena 5 — La calle",
                       "language": "es"},
            "style": {"theme": "light", "accent": "#c2410c", "font": "Helvetica"},
            "output": {"dir": "/Users/ana/Desktop/storyboard", "name": "escena5", "format": "pdf", "dpi": 150,
                       "shot_list": ["json", "csv"]},
        },
    },
    "extract_frames": {
        "minimal": {"input": {"video": "/Users/ana/Movies/clip.mp4"}, "select": {"at_pct": [0, 50, 100]}},
        "full": {"input": {"images": ["/Users/ana/burst/IMG_1.jpg", "/Users/ana/burst/IMG_2.jpg",
                                      "/Users/ana/burst/IMG_3.jpg"]},
                 "select": {"mode": "sharpest", "count": 1},
                 "output": {"dir": "/Users/ana/Desktop/mejor", "format": "original"}},
    },
}


@dataclass
class ToolSpec:
    args: type[BaseModel]
    result: type[BaseModel] | None
    fn: Callable
    description: str
    read_only: bool


TOOLS: dict[str, ToolSpec] = {
    "media_info": ToolSpec(MediaInfoArgs, MediaInfoResult, tool_media_info, (
        "What the input is. Video: duration, fps, number of frames, size, rotation and codec (fast: it does "
        "not decode the video). Images: how many, and each one's index, name, format and size. Call it first "
        "to know the limits for positions (frames 0 to frames - 1). An image with duplicate_of has exactly the "
        "same bytes as the earlier image of that index.\n"
        'Example: {"input": {"video": "/abs/clip.mp4"}} or {"input": {"folder": "/abs/photos"}}.'), True),
    "make_storyboard": ToolSpec(StoryboardArgs, StoryboardResult, tool_make_storyboard, (
        "Turn a video or a list of images into pages of panels you can look at, plus one JSON entry per "
        "panel. Templates (layout.template): grid (contact sheet, the default), storyboard (number above each "
        "picture, notes box below), list (picture left, notes right), strip (one row). Pages as PNG or JPG "
        "files, or one multi-page PDF (output.format); optional shot list (JSON / CSV) and full-size frames.\n"
        'Only input is required: {"input": {"video": "/abs/clip.mp4"}} gives a grid of the 12 sharpest '
        "frames (one per stretch of the video). The answer lists the page files (open them to see the "
        "panels; page 1 also comes as an image) and, per panel, its number, page, frame and time (video) or "
        "image index and name (images), sharpness and notes.\n"
        "To write a storyboard: look at a first result, then call again with the same select plus panels[] "
        "(one object per panel, in order: shot, camera, action, dialogue, sound, transition, duration_s) and "
        'layout.template "storyboard"; or give each panel its own position (frame, at_s, at_pct, image, '
        "image_name).\n"
        "Recipes: 8 sharp, different frames: "
        '{"select": {"count": 8, "min_difference": 3}}. One every 10 %: {"select": {"step_pct": 10}}. '
        'The 4 sharpest photos, no repeats: {"input": {"folder": "/abs/photos"}, "select": {"mode": "best", '
        '"count": 4}}. Printable storyboard: {"layout": {"template": "storyboard", "page": "A4"}, '
        '"style": {"theme": "light"}, "output": {"format": "pdf", "shot_list": ["csv"]}} (add "csv_style": '
        '"excel_es" for Excel in Spanish). If fewer panels than asked come out, warnings says why and what to '
        'change. get_schema {"tool": "make_storyboard"} has a full example.'), False),
    "extract_frames": ToolSpec(FramesArgs, FramesResult, tool_extract_frames, (
        "Save chosen video frames at full size (JPEG or PNG), or copies of chosen images, to look at details "
        "the panels are too small to show. Same input and select as make_storyboard, e.g. "
        '{"input": {"video": "/abs/clip.mp4"}, "select": {"at_frames": [120, 480]}} or '
        '{"select": {"at_s": [12.5], "snap_s": 0.5}} (the sharpest frame within ± 0.5 s). Frames are saved '
        "upright (rotation applied), at displayed_size, as f<frame, 5 digits>.jpg (frame 42 -> f00042.jpg). "
        'Images are copied byte for byte as 001_<name>, 002_<name>... (output.names "original" keeps the '
        "names). Each entry of files gives its path (file) and size in px."), False),
    "get_schema": ToolSpec(SchemaArgs, None, lambda a: (schema_document(a.tool), []), (
        "The JSON Schema of the tools' arguments and answers, with a minimal and a full example of each, "
        "and the conventions (paths, positions, defaults). The same information the tools' input schemas "
        'give, in one document. {"tool": "make_storyboard"} returns only that tool.'), True),
}


def _clean_schema(s):
    """Drop pydantic's "title" keys: they repeat the field names."""
    if isinstance(s, dict):
        return {k: _clean_schema(v) for k, v in s.items() if not (k == "title" and isinstance(v, str))}
    if isinstance(s, list):
        return [_clean_schema(x) for x in s]
    return s


def input_schema(name: str) -> dict:
    return _clean_schema(TOOLS[name].args.model_json_schema())


def output_schema(name: str) -> dict | None:
    r = TOOLS[name].result
    return _clean_schema(r.model_json_schema(mode="serialization")) if r else None


CONVENTIONS = [
    "Every tool takes the same sections: input (required), select, panels, layout, style, output. "
    "Unknown keys are rejected with the list of allowed ones.",
    "input: exactly one of video (path), images (list of paths) or folder (path, with optional pattern).",
    "Paths: absolute or ~/... A relative path is resolved against the server's working directory.",
    "Video positions: frame numbers 0..frames-1; seconds from the first frame (0.0); % from the first "
    "frame (0) to the last (100). Images: index 0..count-1, file name, or % of the list.",
    "Colours: #RRGGBB, #RRGGBBAA, #RGB, #RGBA or a CSS name. Sizes in px.",
    "Defaults: video -> the sharpest frame of each of 12 stretches; images -> all; layout grid with 4 "
    "columns; theme dark (black page); output png in a temporary folder named in the answer.",
    "Answers are JSON (also as structured content). make_storyboard also returns a small image of page 1.",
]


def schema_document(tool: str | None = None) -> dict:
    doc = {"server": "storyboard", "version": VERSION, "conventions": CONVENTIONS, "tools": {}}
    for name, spec in TOOLS.items():
        if name == "get_schema" or (tool and name != tool):
            continue
        doc["tools"][name] = {"description": spec.description, "input_schema": input_schema(name),
                              "output_schema": output_schema(name), "examples": EXAMPLES[name]}
    return doc


def run_tool(name: str, arguments: dict | None) -> tuple[dict, list]:
    """Validate the arguments and run a tool; returns (JSON answer, extra content).
    Raises ToolError with a message meant for the caller."""
    if name not in TOOLS:
        close = difflib.get_close_matches(name, list(TOOLS), n=1)
        raise ToolError(f'unknown tool "{name}"' + (f'; did you mean "{close[0]}"?' if close else "")
                        + f". Tools: {', '.join(TOOLS)}")
    spec = TOOLS[name]
    try:
        args = spec.args.model_validate(arguments or {})
    except ValidationError as e:
        raise ToolError(format_validation_error(name, spec.args, e)) from None
    result, extra = spec.fn(args)
    data = result.model_dump(mode="json", exclude_none=True) if isinstance(result, BaseModel) else result
    return data, extra


# ============================================================================ MCP
server = Server("storyboard", version=VERSION, instructions=INSTRUCTIONS)


@server.list_tools()
async def _list_tools() -> list[mt.Tool]:
    return [mt.Tool(name=name, description=spec.description, inputSchema=input_schema(name),
                    outputSchema=output_schema(name),
                    annotations=mt.ToolAnnotations(readOnlyHint=spec.read_only, destructiveHint=False,
                                                   idempotentHint=True, openWorldHint=False))
            for name, spec in TOOLS.items()]


@server.call_tool(validate_input=False)
async def _call_tool(name: str, arguments: dict) -> mt.CallToolResult:
    try:
        data, extra = await anyio.to_thread.run_sync(lambda: run_tool(name, arguments))
    except ToolError as e:
        return mt.CallToolResult(content=[mt.TextContent(type="text", text=str(e))], isError=True)
    except Exception as e:  # a bug or an unreadable file: say what failed
        return mt.CallToolResult(content=[mt.TextContent(type="text", text=f"{name} failed: "
                                                                           f"{type(e).__name__}: {e}")],
                                 isError=True)
    text = mt.TextContent(type="text", text=json.dumps(data, ensure_ascii=False, indent=1))
    return mt.CallToolResult(content=[text, *extra], structuredContent=data if TOOLS[name].result else None,
                             isError=False)


@server.list_resources()
async def _list_resources() -> list[mt.Resource]:
    return [mt.Resource(uri="storyboard://schema", name="schema", title="storyboard JSON Schema",
                        mimeType="application/json",
                        description="JSON Schema of every tool's arguments and answers, with examples.")]


@server.read_resource()
async def _read_resource(uri) -> list[ReadResourceContents]:
    if str(uri) != "storyboard://schema":
        raise ValueError(f"unknown resource {uri}; the only one is storyboard://schema")
    return [ReadResourceContents(content=json.dumps(schema_document(), ensure_ascii=False, indent=1),
                                 mime_type="application/json")]


async def _serve():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


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
    if "--schema" in sys.argv[1:]:
        print(json.dumps(schema_document(), ensure_ascii=False, indent=1))
        sys.exit(0)
    anyio.run(_serve)
