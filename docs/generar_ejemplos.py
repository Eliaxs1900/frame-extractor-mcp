# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.10,<2", "av>=14", "pillow>=10.1", "pillow-heif>=0.16", "pydantic>=2.7"]
# ///
"""Regenera las figuras y las respuestas JSON de docs/formatos.md y del README.

    uv run --script docs/generar_ejemplos.py

1. Crea los medios de prueba en docs/formatos/salidas/ (no se suben; salen iguales cada vez):
   - ciudad.mp4: 640×360, 30 fps, 20 s = 600 fotogramas. Cada fotograma lleva escritos su
     número, su tiempo y su %, para comprobar a simple vista que las etiquetas y el JSON
     dicen la verdad. Al fondo se desplaza una ciudad; la primera mitad de cada segundo
     está desenfocada (como un vídeo movido) y los 3 últimos segundos son un plano fijo.
   - fotos/: 10 imágenes (JPEG y PNG, una vertical, una girada por EXIF como las de un
     móvil, dos movidas y una casi repetida).
   - movil.mp4: 3 s grabados «en vertical» como un móvil: la imagen guardada de lado y un
     giro de 90° en la matriz de visualización.
2. Llama a las herramientas de server.py con run_tool(), igual que un asistente por MCP.
   Las respuestas se guardan en docs/formatos/json/, con las rutas cambiadas por
   /Users/ana/ejemplos/ (son rutas de ejemplo).
3. Monta con esas salidas reales las figuras docs/formatos/fig_*.png.
"""
import colorsys
import importlib.util
import json
import os
import random
import shutil
from pathlib import Path

import av
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "formatos"
WORK = OUT / "salidas"
FAKE = "/Users/ana/ejemplos"                 # prefijo de las rutas en los JSON publicados
W, H, FPS, SECONDS, STILL_FROM, SCROLL = 640, 360, 30, 20, 17.0, 48
N = FPS * SECONDS


# ----------------------------------------------------------------------------- fuentes
def font(size: int, bold: bool = False, mono: bool = False) -> ImageFont.FreeTypeFont:
    if mono:
        cands = [("/System/Library/Fonts/Menlo.ttc", 1 if bold else 0),
                 ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono" + ("-Bold" if bold else "") + ".ttf", 0)]
    else:
        cands = [("/System/Library/Fonts/HelveticaNeue.ttc", 1 if bold else 0),
                 ("/usr/share/fonts/truetype/dejavu/DejaVuSans" + ("-Bold" if bold else "") + ".ttf", 0)]
    for path, index in cands:
        try:
            return ImageFont.truetype(path, size, index=index)
        except OSError:
            pass
    return ImageFont.load_default(size)


# ----------------------------------------------------------------------------- 1. medios
def _city():
    rng, out, x = random.Random(7), [], -20
    while x < W + SCROLL * SECONDS + 40:
        w, h = rng.randint(30, 86), rng.randint(64, 168)
        wins = [(wx, wy) for wy in range(H - h + 12, H - 22, 14) for wx in range(x + 7, x + w - 8, 11)
                if rng.random() < 0.38]
        out.append((x, w, h, rng.randint(0, 2), wins))
        x += w + rng.randint(0, 12)
    return out


CITY = _city()


def _mix(a, b, f):
    return tuple(round(a[k] + (b[k] - a[k]) * f) for k in range(3))


def frame_image(i: int) -> Image.Image:
    t = i / FPS
    still = t >= STILL_FROM
    tc = STILL_FROM if still else t
    f = tc / SECONDS
    top, bottom = _mix((37, 99, 235), (234, 88, 12), f), _mix((191, 219, 254), (254, 215, 170), f)
    im = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(im)
    for y in range(H):
        d.line((0, y, W, y), fill=_mix(top, bottom, y / H))
    off = round(tc * SCROLL)
    tones = [(30, 41, 59), (51, 65, 85), (15, 23, 42)]
    for x, w, h, tone, wins in CITY:
        if x - off > W or x + w - off < 0:
            continue
        d.rectangle((x - off, H - h, x - off + w, H), fill=tones[tone])
        for wx, wy in wins:
            d.rectangle((wx - off, wy, wx - off + 4, wy + 6), fill=(253, 224, 71))
    card = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    c = ImageDraw.Draw(card)
    c.rounded_rectangle((112, 22, W - 112, 170), radius=18, fill=(12, 12, 16, 230))
    c.text((W // 2, 80), f"#{i}", font=font(82, bold=True, mono=True), fill=(255, 255, 255), anchor="mm")
    c.text((W // 2, 142), f"{t:.2f} s · {100 * i / (N - 1):.1f} %", font=font(30, mono=True),
           fill=(253, 224, 71), anchor="mm")
    if still:
        c.rounded_rectangle((W // 2 - 84, 184, W // 2 + 84, 218), radius=17, fill=(255, 255, 255, 240))
        c.text((W // 2, 201), "PLANO FIJO", font=font(19, bold=True), fill=(12, 12, 16), anchor="mm")
    c.rounded_rectangle((24, H - 24, W - 24, H - 18), radius=3, fill=(255, 255, 255, 200))
    xm = 24 + (W - 48) * i / (N - 1)
    c.ellipse((xm - 9, H - 30, xm + 9, H - 12), fill=(255, 255, 255, 255), outline=(12, 12, 16, 255), width=3)
    im = Image.alpha_composite(im.convert("RGBA"), card).convert("RGB")
    if not still and (t % 1.0) < 0.5:
        im = im.filter(ImageFilter.GaussianBlur(4))
    return im


def make_video(path: Path) -> None:
    with av.open(str(path), "w") as c:
        st = c.add_stream("mpeg4", rate=FPS)
        st.width, st.height, st.pix_fmt, st.bit_rate = W, H, "yuv420p", 3_000_000
        for i in range(N):
            for pkt in st.encode(av.VideoFrame.from_image(frame_image(i))):
                c.mux(pkt)
        for pkt in st.encode():
            c.mux(pkt)


def make_phone_video(path: Path) -> None:
    """Portrait scene stored sideways plus a display matrix, the way a phone records it."""
    big, small = font(64, bold=True), font(28)
    with av.open(str(path), "w") as c:
        st = c.add_stream("mpeg4", rate=FPS)
        st.width, st.height, st.pix_fmt, st.bit_rate = 640, 360, "yuv420p", 2_000_000
        st.set_display_rotation(-90)                    # show it turned 90° clockwise
        for i in range(90):
            im = Image.new("RGB", (360, 640))
            d = ImageDraw.Draw(im)
            for y in range(640):
                d.line((0, y, 360, y), fill=_mix((37, 99, 235), (234, 88, 12), y / 640))
            bx = 60 + (240 * i / 89)
            d.ellipse((bx - 34, 420, bx + 34, 488), fill=(253, 224, 71))
            d.polygon([(180, 60), (115, 150), (245, 150)], fill=(255, 255, 255))
            d.rectangle((160, 150, 200, 230), fill=(255, 255, 255))
            d.text((180, 300), "ARRIBA", font=big, fill="white", anchor="mm")
            d.text((180, 360), "vídeo de móvil", font=small, fill="white", anchor="mm")
            d.rounded_rectangle((90, 540, 270, 610), radius=14, fill=(12, 12, 16))
            d.text((180, 575), f"#{i}", font=font(44, bold=True, mono=True), fill=(253, 224, 71), anchor="mm")
            for pkt in st.encode(av.VideoFrame.from_image(im.transpose(Image.Transpose.ROTATE_90))):
                c.mux(pkt)
        for pkt in st.encode():
            c.mux(pkt)


def make_photos(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    big = font(96, bold=True)

    def scene(k: int, hue: float, w: int = 1200, h: int = 800) -> Image.Image:
        sky = tuple(round(255 * v) for v in colorsys.hsv_to_rgb(hue, 0.35, 0.95))
        im = Image.new("RGB", (w, h), sky)
        d = ImageDraw.Draw(im)
        rng = random.Random(k)
        for j in range(9):                                     # montañas
            x = rng.randint(-200, w)
            d.polygon([(x, h), (x + 260, h * 0.35 + rng.randint(0, 200)), (x + 520, h)],
                      fill=tuple(round(255 * v) for v in colorsys.hsv_to_rgb(hue, 0.5, 0.35 + 0.05 * j)))
        d.ellipse((w * 0.72, h * 0.12, w * 0.72 + 120, h * 0.12 + 120), fill=(255, 244, 214))
        d.rounded_rectangle((w / 2 - 230, h / 2 - 85, w / 2 + 230, h / 2 + 85), radius=26, fill=(12, 12, 16))
        d.text((w / 2, h / 2), f"FOTO {k}", font=big, fill=(255, 255, 255), anchor="mm")
        return im

    for k in range(1, 9):
        im = scene(k, k / 9)
        if k in (3, 6):
            im = im.filter(ImageFilter.GaussianBlur(10))       # movidas
        im.save(folder / (f"foto_{k:02d}.png" if k % 4 == 0 else f"foto_{k:02d}.jpg"), quality=90)
    scene(5, 5 / 9).save(folder / "foto_05b.jpg", quality=88)   # casi igual que la 5
    vertical = scene(9, 0.95, 800, 1200).transpose(Image.Transpose.ROTATE_90)
    exif = Image.Exif()
    exif[0x0112] = 6                                          # el móvil la guardó girada
    vertical.save(folder / "foto_09_movil.jpg", exif=exif, quality=90)


# ----------------------------------------------------------------------------- 2. herramientas
def load_server():
    spec = importlib.util.spec_from_file_location("server", ROOT / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SRV = None


def run(name: str, tool: str, args: dict) -> dict:
    """Llama a una herramienta y guarda la respuesta (con rutas de ejemplo) en json/<name>.json."""
    data, _ = SRV.run_tool(tool, args)
    text = json.dumps(data, ensure_ascii=False, indent=1).replace(str(WORK.resolve()), FAKE)
    (OUT / "json").mkdir(exist_ok=True)
    (OUT / "json" / f"{name}.json").write_text(text + "\n", encoding="utf-8")
    print(f"  {name}: {tool}")
    return data


# ----------------------------------------------------------------------------- 3. figuras
INK, MUTED, RULE, PAPER, CODE_BG, ACCENT = (24, 24, 27), (82, 82, 91), (228, 228, 231), (255, 255, 255), \
    (244, 244, 245), (37, 99, 235)
FIG_W, PAD, GAP = 1440, 44, 36
F_TITLE, F_SUB, F_HEAD = font(36, bold=True), font(21), font(24, bold=True)
F_TEXT, F_CODE, F_BADGE = font(21), font(18, mono=True), font(19, bold=True)


def wrap(d, text, f, width):
    out = []
    for para in text.split("\n"):
        line = ""
        for word in para.split():
            cand = f"{line} {word}".strip()
            if line and d.textlength(cand, font=f) > width:
                out.append(line)
                line = word
            else:
                line = cand
        out.append(line)
    return out


def badge(d, cx, cy, n, r=17):
    d.ellipse((cx - r - 3, cy - r - 3, cx + r + 3, cy + r + 3), fill=PAPER)
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=ACCENT)
    d.text((cx, cy + 1), str(n), font=F_BADGE, fill=PAPER, anchor="mm")


def fit(path, width):
    im = Image.open(path).convert("RGB")
    return im if im.width <= width else im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)


def heading(d, title, subtitle):
    d.text((PAD, PAD), title, font=F_TITLE, fill=INK)
    y = PAD + 50
    for ln in wrap(d, subtitle, F_SUB, FIG_W - 2 * PAD):
        d.text((PAD, y), ln, font=F_SUB, fill=MUTED)
        y += 30
    return y + 22


def side_text(d, x, y, width, code, text, draw=True):
    lh, h = 27, 0
    if code:
        box = 14 + lh * len(code)
        if draw:
            d.rounded_rectangle((x, y, x + width, y + box), radius=8, fill=CODE_BG)
            for n, ln in enumerate(code):
                d.text((x + 12, y + 8 + n * lh), ln, font=F_CODE, fill=INK)
        h += box + 14
    lines = wrap(d, text, F_TEXT, width)
    if draw:
        for n, ln in enumerate(lines):
            d.text((x, y + h + n * 30), ln, font=F_TEXT, fill=MUTED)
    return h + 30 * len(lines)


def save_fig(fig: Image.Image, out: Path):
    fig.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.FLOYDSTEINBERG).save(
        out, optimize=True)
    print(f"  figura {out.name}  {fig.width}×{fig.height}  {out.stat().st_size // 1024} KB")


def rows_figure(title, subtitle, rows, out, img_w=860):
    """Una fila por variante: número y título, la salida real a la izquierda y, a la derecha,
    la llamada que la produce y qué es."""
    txt_w = FIG_W - 2 * PAD - img_w - GAP
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    imgs = [fit(p, img_w) for _, p, _, _ in rows]
    heights = [44 + max(im.height, side_text(probe, 0, 0, txt_w, code, text, False))
               for im, (_, _, code, text) in zip(imgs, rows)]
    head_h = heading(probe, title, subtitle)
    fig = Image.new("RGB", (FIG_W, head_h + sum(heights) + GAP * (len(rows) - 1) + PAD), PAPER)
    d = ImageDraw.Draw(fig)
    y = heading(d, title, subtitle)
    for n, (im, (head, _, code, text), h) in enumerate(zip(imgs, rows, heights), 1):
        badge(d, PAD + 17, y + 15, n)
        d.text((PAD + 46, y + 15), head, font=F_HEAD, fill=INK, anchor="lm")
        fig.paste(im, (PAD, y + 44))
        d.rectangle((PAD - 1, y + 43, PAD + im.width, y + 44 + im.height), outline=RULE)
        side_text(d, PAD + img_w + GAP, y + 44, txt_w, code, text)
        y += h + GAP
        if n < len(rows):
            d.line((PAD, y - GAP // 2, FIG_W - PAD, y - GAP // 2), fill=RULE, width=2)
    save_fig(fig, out)


def grid_figure(title, subtitle, items, out, cols):
    cell = (FIG_W - 2 * PAD - GAP * (cols - 1)) // cols
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    imgs = [fit(p, cell) for _, p, _, _ in items]
    rows = [list(range(k, min(k + cols, len(items)))) for k in range(0, len(items), cols)]
    row_h = [max(44 + imgs[k].height + 14 + side_text(probe, 0, 0, cell, items[k][2], items[k][3], False)
                 for k in r) for r in rows]
    head_h = heading(probe, title, subtitle)
    fig = Image.new("RGB", (FIG_W, head_h + sum(row_h) + GAP * (len(rows) - 1) + PAD), PAPER)
    d = ImageDraw.Draw(fig)
    y = heading(d, title, subtitle)
    for r, h in zip(rows, row_h):
        for j, k in enumerate(r):
            head, _, code, text = items[k]
            x = PAD + j * (cell + GAP)
            badge(d, x + 17, y + 15, k + 1)
            d.text((x + 46, y + 15), head, font=F_HEAD, fill=INK, anchor="lm")
            fig.paste(imgs[k], (x, y + 44))
            d.rectangle((x - 1, y + 43, x + imgs[k].width, y + 44 + imgs[k].height), outline=RULE)
            side_text(d, x, y + 44 + imgs[k].height + 14, cell, code, text)
        y += h + GAP
    save_fig(fig, out)


def anatomy_figure(sheet: Path, data: dict, out: Path):
    """La hoja real con sus partes numeradas y la leyenda debajo."""
    src = Image.open(sheet).convert("RGB")
    lay = data["layout"]
    pw, ph = lay["picture_size"]
    # geometría con las mismas fórmulas que server.py (Look.set_sizes y _plan)
    text_size = max(18, round(src.width / 64))
    title_size = round(text_size * 1.3)
    pad = max(6, round(text_size * 0.5))
    head_h = pad * 2 + round(title_size * 1.2) + pad // 2 + round(text_size * 1.3)
    foot_h = pad * 2 + round(text_size * 1.3)
    label = max(12, round(pw / 22))
    band = label + max(8, label // 2)
    k = (FIG_W - 2 * PAD) / src.width
    im = src.resize((FIG_W - 2 * PAD, round(src.height * k)), Image.LANCZOS)
    legend = [
        ("Cabecera", "Datos del vídeo: nombre, tamaño, fps, duración, nº de fotogramas y códec.",
         ['"layout.title", "subtitle"', '"header": false la quita']),
        ("Etiqueta", "Número de viñeta, segundos y nº de fotograma; en el primero y el último, además, "
         "«primer / último fotograma».", ['"layout.label"', '"fields" o "template"']),
        ("Viñetas", f"{len(data['panels'])} fotogramas en orden de tiempo, por filas. La viñeta n es el "
         "elemento n de «panels» en el JSON.", ['"select": {"count": 12}', '"layout": {"columns": 4}']),
        ("Pie", "Cómo se eligieron: modo, rango, fotogramas medidos y nitidez mín / mediana / máx.",
         ['"footer": false lo quita']),
    ]
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    cell = (FIG_W - 2 * PAD - GAP * 3) // 4
    leg_h = max(40 + side_text(probe, 0, 0, cell, code, text, False) for _, text, code in legend)
    title = "La respuesta más simple: una hoja de contactos"
    sub = ('make_storyboard con solo {"input": {"video": ".../ciudad.mp4"}}: los 12 fotogramas más nítidos, '
           f"uno por tramo, en 4 columnas y tema oscuro. La imagen real mide {src.width}×{src.height} px.")
    head = heading(probe, title, sub)
    fig = Image.new("RGB", (FIG_W, head + im.height + 30 + leg_h + PAD), PAPER)
    d = ImageDraw.Draw(fig)
    y0 = heading(d, title, sub)
    fig.paste(im, (PAD, y0))
    for n, yy in enumerate((head_h / 2, head_h + band / 2, head_h + ph * 0.6, src.height - foot_h / 2), 1):
        cy = y0 + round(yy * k)                 # badges in the left margin, with a short line into the part
        d.line((PAD // 2, cy, PAD + 26, cy), fill=ACCENT, width=3)
        badge(d, PAD // 2, cy, n, r=15)
    y = y0 + im.height + 30
    for n, (name, text, code) in enumerate(legend, 1):
        x = PAD + (n - 1) * (cell + GAP)
        badge(d, x + 17, y + 15, n)
        d.text((x + 46, y + 15), name, font=F_HEAD, fill=INK, anchor="lm")
        side_text(d, x, y + 44, cell, code, text)
    save_fig(fig, out)


# ----------------------------------------------------------------------------- main
def main() -> None:
    global SRV
    WORK.mkdir(parents=True, exist_ok=True)
    video, photos, phone = WORK / "ciudad.mp4", WORK / "fotos", WORK / "movil.mp4"
    if not video.exists():
        print("creando ciudad.mp4 …")
        make_video(video)
    if not phone.exists():
        make_phone_video(phone)
    if not photos.exists():
        make_photos(photos)
    SRV = load_server()
    V, F, O = str(video), str(photos), str(WORK)
    out = lambda name, **kw: {"dir": O, "name": name, "preview": False, **kw}   # noqa: E731
    print("llamadas:")
    run("media_info_video", "media_info", {"input": {"video": V}})
    run("media_info_fotos", "media_info", {"input": {"folder": F}})
    minimo = run("minimo", "make_storyboard", {"input": {"video": V}, "output": out("minimo")})

    # plantillas
    notas = [{"shot": "PG", "action": "Amanece sobre la ciudad.", "sound": "Tráfico lejano"},
             {"shot": "PM", "camera": "Travelling lateral", "action": "La luz cambia.", "transition": "Corte"},
             {"shot": "PP", "dialogue": "VOZ EN OFF: Cada día igual.", "duration_s": 2.5}]
    sel3 = {"at_s": [2.5, 9.5, 15.5]}
    run("plantilla_grid", "make_storyboard", {"input": {"video": V}, "select": {"count": 6},
                                              "layout": {"panel_width": 320, "columns": 3},
                                              "output": out("plantilla_grid")})
    run("plantilla_storyboard", "make_storyboard", {"input": {"video": V}, "select": sel3, "panels": notas,
                                                    "layout": {"template": "storyboard", "panel_width": 320},
                                                    "output": out("plantilla_storyboard")})
    run("plantilla_list", "make_storyboard", {"input": {"video": V}, "select": {"at_s": [2.5, 9.5]},
                                              "panels": notas, "layout": {"template": "list", "panel_width": 320},
                                              "output": out("plantilla_list")})
    run("plantilla_strip", "make_storyboard", {"input": {"video": V}, "select": {"count": 4}, "panels": notas,
                                               "layout": {"template": "strip", "panel_width": 280},
                                               "output": out("plantilla_strip")})
    # temas
    base = {"input": {"video": V}, "select": sel3, "panels": notas, "layout": {"template": "storyboard",
                                                                                "panel_width": 320}}
    run("tema_oscuro", "make_storyboard", {**base, "output": out("tema_oscuro")})
    run("tema_claro", "make_storyboard", {**base, "style": {"theme": "light"}, "output": out("tema_claro")})
    run("tema_propio", "make_storyboard", {**base, "style": {
        "background": "#1e293b", "text": "#f8fafc", "text_secondary": "#94a3b8", "accent": "#f472b6",
        "panel_border": "#334155", "notes_background": "#0f172a", "notes_text": "#e2e8f0",
        "font": "Georgia", "font_bold": "Georgia Bold"}, "output": out("tema_propio")})
    # selección
    sel = {"input": {"video": V}, "layout": {"panel_width": 256, "columns": 6,
                                             "label": {"fields": ["number", "frame", "sharpness"]}}}
    run("sel_nitido", "make_storyboard", {**sel, "select": {"count": 6}, "output": out("sel_nitido")})
    run("sel_centro", "make_storyboard", {**sel, "select": {"count": 6, "mode": "even"},
                                          "output": out("sel_centro")})
    run("sel_exacto", "make_storyboard", {**sel, "select": {"step_pct": 20}, "output": out("sel_exacto")})
    run("sel_exacto_snap", "make_storyboard", {**sel, "select": {"step_pct": 20, "snap_s": 0.5},
                                               "output": out("sel_exacto_snap")})
    run("filtros_sin", "make_storyboard", {**sel, "select": {"count": 10, "mode": "even"},
                                           "layout": {**sel["layout"], "columns": 5},
                                           "output": out("filtros_sin")})
    run("filtros_con", "make_storyboard", {**sel, "select": {"count": 10, "mode": "even", "drop_blurriest_pct": 50,
                                                             "min_difference": 6},
                                           "layout": {**sel["layout"], "columns": 5},
                                           "output": out("filtros_con")})
    # etiquetas y numeración
    lab = {"input": {"video": V}, "select": {"at_pct": [0, 100]}}
    lw = {"panel_width": 400, "columns": 2}
    run("etiqueta_defecto", "make_storyboard", {**lab, "layout": lw, "output": out("etiqueta_defecto")})
    run("etiqueta_campos", "make_storyboard", {**lab, "layout": {**lw, "label": {
        "fields": ["number", "timecode", "pct"], "position": "bottom"}}, "output": out("etiqueta_campos")})
    run("etiqueta_plantilla", "make_storyboard", {**lab, "layout": {**lw, "numbering": {"format": "SH{n:03d}"},
                                                                    "label": {"template": "{number} · fotograma "
                                                                              "{frame} de {frames_total}"}},
                                                  "output": out("etiqueta_plantilla")})
    run("etiqueta_escena", "make_storyboard", {**lab, "panels": [{"action": "Primer plano"},
                                                                 {"number": "5.2A", "action": "Inserto añadido"}],
                                               "layout": {**lw, "template": "storyboard",
                                                          "numbering": {"format": "5.{n}"}},
                                               "output": out("etiqueta_escena")})
    # guías
    g = {"input": {"video": V}, "select": {"at_s": [8.6]}, "layout": {"panel_width": 480, "header": False,
                                                                      "footer": False, "label": {"show": False}}}
    run("guia_239", "make_storyboard", {**g, "layout": {**g["layout"], "guides": {"aspects": ["2.39:1"]}},
                                        "output": out("guia_239")})
    run("guia_916", "make_storyboard", {**g, "layout": {**g["layout"], "guides": {"aspects": ["9:16", "1:1"]}},
                                        "output": out("guia_916")})
    run("guia_segura", "make_storyboard", {**g, "layout": {**g["layout"], "guides": {"safe_zone": True,
                                                                                    "thirds": True}},
                                           "output": out("guia_segura")})
    # imágenes
    run("fotos_todas", "make_storyboard", {"input": {"folder": F}, "layout": {"panel_width": 300, "columns": 5},
                                           "output": out("fotos_todas")})
    run("fotos_mejores", "make_storyboard", {"input": {"folder": F}, "select": {"drop_blurriest_pct": 20,
                                                                                "min_difference": 4},
                                             "layout": {"panel_width": 300, "columns": 5,
                                                        "label": {"fields": ["number", "name", "sharpness"]}},
                                             "output": out("fotos_mejores")})
    run("fotos_best", "make_storyboard", {"input": {"folder": F}, "select": {"mode": "best", "count": 4},
                                          "layout": {"panel_width": 300, "columns": 4,
                                                     "label": {"fields": ["number", "name", "sharpness"]}},
                                          "output": out("fotos_best")})
    run("fotos_lista", "make_storyboard", {"input": {"images": [f"{F}/foto_01.jpg", f"{F}/foto_09_movil.jpg"]},
                                           "panels": [{"action": "Paisaje del día 1."},
                                                      {"action": "Foto vertical del móvil: se gira sola (EXIF)."}],
                                           "layout": {"template": "list", "panel_width": 300,
                                                      "label": {"fields": ["name", "size"]}},
                                           "style": {"theme": "light"}, "output": out("fotos_lista")})
    # PDF y lista de planos (y las mismas páginas en PNG para enseñarlas)
    pdf_args = {"input": {"video": V}, "select": {"count": 8},
                "panels": [{"shot": "PG", "action": "Amanece."}, {"shot": "PM", "camera": "Paneo"},
                           {"shot": "PP", "dialogue": "ANA: Llego tarde."}],
                "layout": {"template": "storyboard", "page": "A4", "title": "Escena 3 — Amanecer",
                           "numbering": {"format": "3.{n}"}},
                "style": {"theme": "light"}}
    run("pdf", "make_storyboard", {**pdf_args, "output": out("pdf", format="pdf", shot_list=["json", "csv"])})
    run("pdf_png", "make_storyboard", {**pdf_args, "output": out("pdf_png")})
    # usos (para el README)
    run("uso_tramo", "make_storyboard", {"input": {"video": V}, "select": {"start_s": 5, "end_s": 10,
                                                                          "every_s": 1},
                                         "layout": {"panel_width": 256, "columns": 5},
                                         "output": out("uso_tramo")})
    run("uso_movil", "make_storyboard", {"input": {"video": str(phone)}, "select": {"count": 4},
                                         "layout": {"template": "strip", "panel_width": 200},
                                         "output": out("uso_movil")})
    run("uso_rafaga", "extract_frames", {"input": {"folder": F}, "select": {"mode": "best", "count": 3},
                                         "output": {"dir": f"{O}/mejores", "names": "original"}})
    # extract_frames
    run("extraer_pct", "extract_frames", {"input": {"video": V}, "select": {"at_pct": [0, 50, 100]},
                                          "output": {"dir": f"{O}/extraer"}})
    run("extraer_snap", "extract_frames", {"input": {"video": V}, "select": {"at_frames": [90], "snap_s": 0.5},
                                           "output": {"dir": f"{O}/extraer_snap"}})
    run("extraer_fotos", "extract_frames", {"input": {"folder": F}, "select": {"mode": "sharpest", "count": 3},
                                            "output": {"dir": f"{O}/extraer_fotos"}})
    print("figuras:")
    figures(minimo)


def figures(minimo: dict) -> None:
    S = WORK
    anatomy_figure(S / "minimo.png", minimo, OUT / "fig_minimo.png")
    rows_figure("Plantillas de página (layout.template)",
                "Las mismas entradas con las cuatro plantillas. Las notas salen de panels[] (una por viñeta, en "
                "orden); sin notas, storyboard, list y strip dejan líneas para escribir a mano.", [
        ("grid: hoja de contactos (por defecto)", S / "plantilla_grid.png", ['"template": "grid"'],
         "Etiqueta encima de cada imagen, sin márgenes. Para ver un vídeo de un vistazo."),
        ("storyboard: número arriba, notas abajo", S / "plantilla_storyboard.png", ['"template": "storyboard"'],
         "Para planificar o contar un rodaje: plano, cámara, acción, diálogo, sonido, transición y duración."),
        ("list: imagen a la izquierda, notas a la derecha", S / "plantilla_list.png", ['"template": "list"'],
         "Una viñeta por fila, con sitio para notas largas."),
        ("strip: una sola fila", S / "plantilla_strip.png", ['"template": "strip"'],
         "Tira de viñetas con las notas debajo; con más de 8, una tira por página."),
    ], OUT / "fig_plantillas.png", img_w=900)
    rows_figure("Temas y colores (style)",
                "Tema oscuro (negro puro) o claro (blanco puro); cualquier color, fuente o tamaño se cambia "
                "con su campo, y lo que no se da sale del tema.", [
        ("Oscuro (por defecto)", S / "tema_oscuro.png", ['"style": {"theme": "dark"}'], "Fondo #000000."),
        ("Claro", S / "tema_claro.png", ['"style": {"theme": "light"}'], "Fondo #ffffff, para imprimir."),
        ("Colores y fuente propios", S / "tema_propio.png", ['"background": "#1e293b",', '"accent": "#f472b6",',
                                                               '"notes_background": "#0f172a",',
                                                               '"font": "Georgia" …'],
         "Cada color admite #RRGGBB, #RRGGBBAA (con transparencia) o un nombre CSS."),
    ], OUT / "fig_temas.png", img_w=900)
    rows_figure("Qué fotogramas se eligen (select)",
                "El vídeo de muestra se desenfoca en la primera mitad de cada segundo, como uno movido.", [
        ("El más nítido de cada tramo (defecto)", S / "sel_nitido.png", ['"count": 6'],
         "Divide el vídeo en 6 tramos y toma el menos movido de cada uno."),
        ("El del centro de cada tramo", S / "sel_centro.png", ['"count": 6, "mode": "even"'],
         "Reparto regular en el tiempo, aunque salga movido."),
        ("Posiciones exactas", S / "sel_exacto.png", ['"step_pct": 20'],
         "0, 20, 40, 60, 80 y 100 %: exactas, sin decodificar todo el vídeo. También at_pct, at_s, at_frames, "
         "step_s."),
        ("Exactas, el más nítido cerca", S / "sel_exacto_snap.png", ['"step_pct": 20, "snap_s": 0.5'],
         "En cada posición, el más nítido a ± 0,5 s."),
    ], OUT / "fig_seleccion.png")
    rows_figure("Filtros: sin movidos ni repetidos",
                "Lo que quitan los filtros sale en «dropped» del JSON y el umbral, en «selection.threshold».", [
        ("Sin filtros", S / "filtros_sin.png", ['"count": 10, "mode": "even"'],
         "La mitad sale movida y las dos últimas son el mismo plano fijo."),
        ("Con filtros", S / "filtros_con.png", ['"count": 10, "mode": "even",', '"drop_blurriest_pct": 50,',
                                                '"min_difference": 6'],
         "Ignora el 50 % más borroso y descarta la viñeta casi igual a la anterior."),
    ], OUT / "fig_filtros.png")
    rows_figure("Etiquetas y numeración (layout.label, layout.numbering)",
                "Los mismos dos fotogramas, el primero y el último del vídeo.", [
        ("Por defecto", S / "etiqueta_defecto.png", ["(sin parámetros)"],
         "Número · segundos · nº de fotograma; «primer / último fotograma» en los extremos."),
        ("Campos elegidos, abajo", S / "etiqueta_campos.png", ['"label": {"fields": ["number",', '  "timecode", '
                                                                                                  '"pct"],',
                                                                '  "position": "bottom"}'],
         "Los campos en el orden dado. Vídeo: number, frame, time, timecode, pct, sharpness, anchor. Imágenes: "
         "number, image, name, size, pct, sharpness."),
        ("Plantilla y numeración propia", S / "etiqueta_plantilla.png",
         ['"numbering": {"format":', '  "SH{n:03d}"},', '"label": {"template":', '  "{number} · fotograma',
          '  {frame} de {frames_total}"}'], "Texto libre con variables; números con formato de Python."),
        ("Escenas e insertos", S / "etiqueta_escena.png", ['"numbering": {"format": "5.{n}"}', 'panels[1].number:',
                                                          '  "5.2A"'],
         "En storyboard el número va grande encima; una viñeta puede llevar su propio número."),
    ], OUT / "fig_etiquetas.png", img_w=820)
    grid_figure("Guías sobre la imagen (layout.guides)",
                "Para ver qué quedaría al recortar a otro formato, la zona segura o la regla de los tercios.", [
        ("2.39:1 (cine)", S / "guia_239.png", ['"aspects": ["2.39:1"]'], "Oscurece lo que se perdería."),
        ("9:16 y 1:1", S / "guia_916.png", ['"aspects": ["9:16", "1:1"]'], "Varias a la vez; la primera sombrea."),
        ("Zona segura y tercios", S / "guia_segura.png", ['"safe_zone": true,', '"thirds": true'],
         "Zona segura del 90 % (safe_zone_pct)."),
    ], OUT / "fig_guias.png", cols=3)
    rows_figure("Entrada de imágenes (input.images o input.folder)",
                "Lo mismo que con vídeo, con el orden de la lista en vez del tiempo: nombre en vez de segundos.", [
        ("Una carpeta, todas", S / "fotos_todas.png", ['"input": {"folder": ".../fotos"}'],
         "Ordenadas por nombre (foto_2 antes que foto_10). La vertical del móvil sale derecha (EXIF)."),
        ("Las 4 más nítidas, sin repetir", S / "fotos_best.png", ['"mode": "best", "count": 4'],
         "Las más nítidas de toda la carpeta, en su orden; nunca la misma imagen dos veces."),
        ("Sin movidas ni repetidas", S / "fotos_mejores.png", ['"drop_blurriest_pct": 20,',
                                                               '"min_difference": 4'],
         "Todas menos las movidas (3 y 6) y la casi igual a la anterior (5b); salen en «dropped» del JSON."),
        ("Lista con notas", S / "fotos_lista.png", ['"input": {"images": [...]},', '"template": "list"'],
         "Una lista de rutas en el orden que quieras."),
    ], OUT / "fig_imagenes.png")
    rows_figure("Usos: lo que pides y lo que sale",
                "Peticiones típicas a un asistente, la llamada que hace y la imagen real que recibe (además del "
                "JSON con una entrada por viñeta).", [
        ("«Resume este vídeo»", S / "minimo.png", ['{"input": {"video": "…/ciudad.mp4"}}'],
         "Hoja de contactos con los 12 fotogramas más nítidos, uno por tramo."),
        ("«Del segundo 5 al 10, el mejor de cada segundo»", S / "uso_tramo.png",
         ['"select": {"start_s": 5,', '  "end_s": 10, "every_s": 1}'],
         "Solo esa parte del vídeo, el más nítido de cada segundo."),
        ("«Mira este vídeo del móvil»", S / "uso_movil.png", ['{"input": {"video": "…/movil.mp4"},',
                                                             ' "layout": {"template": "strip"}}'],
         "Grabado en vertical: sale derecho, con rotation_deg 90 en el JSON."),
        ("«Hazme un storyboard para imprimir»", S / "pdf_png_p1.png",
         ['"panels": [{"shot": "PG", …}],', '"layout": {"template":', '  "storyboard", "page": "A4"},',
          '"output": {"format": "pdf",', '  "shot_list": ["csv"]}'],
         "PDF A4 con notas por viñeta y la lista de planos en CSV."),
        ("«Quédate con las 4 mejores fotos»", S / "fotos_best.png", ['{"input": {"folder": "…/fotos"},',
                                                                     ' "select": {"mode": "best",',
                                                                     '   "count": 4}}'],
         "Las más nítidas y sin repetir. Con extract_frames se copian tal cual a otra carpeta."),
    ], OUT / "fig_usos.png")
    pages = sorted(S.glob("pdf_png_p*.png"))
    grid_figure("PDF de varias páginas y lista de planos",
                'storyboard en A4 apaisado ("page": "A4"), 8 viñetas en 2 páginas, con '
                '"format": "pdf" y "shot_list": ["json", "csv"]. Aquí, las mismas páginas en PNG.',
                [(f"Página {k}", p, [], "") for k, p in enumerate(pages, 1)], OUT / "fig_pdf.png", cols=2)
    ex = json.loads((OUT / "json" / "extraer_pct.json").read_text())["files"]
    sn = json.loads((OUT / "json" / "extraer_snap.json").read_text())["files"][0]
    items = [(Path(f["file"]).name, S / "extraer" / Path(f["file"]).name, [], f"Fotograma {f['frame']}, "
              f"{f['time_s']} s, {f['pct']} %.") for f in ex]
    items.append((Path(sn["file"]).name, S / "extraer_snap" / Path(sn["file"]).name, ['"at_frames": [90],',
                                                                                     '"snap_s": 0.5'],
                  f"Se pidió el 90 (movido): da el {sn['frame']}, el más nítido a ± 0,5 s."))
    grid_figure("extract_frames: fotogramas a tamaño completo",
                'Con "at_pct": [0, 50, 100] y, en el último, la búsqueda del más nítido. Cada uno se guarda '
                "como f<número de 5 cifras>.jpg (640×360 aquí).", items, OUT / "fig_extract_frames.png", cols=4)


if __name__ == "__main__":
    main()
