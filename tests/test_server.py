# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.10,<2", "av>=14", "pillow>=10.1", "pillow-heif>=0.16", "pydantic>=2.7", "pytest>=8"]
# ///
"""Tests of storyboard-mcp, with generated media only (no real photos or videos).

    uv run --script tests/test_server.py          # or: pytest tests/
"""
import csv
import importlib.util
import json
import sys
from pathlib import Path

import av
import jsonschema
import pytest
from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("storyboard_server", ROOT / "server.py")
srv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(srv)


def _frame(i: int, w: int = 160, h: int = 90) -> Image.Image:
    im = Image.new("RGB", (w, h), (40 + 7 * (i % 30), 80, 160))
    d = ImageDraw.Draw(im)
    for x in range(0, w, 8):
        d.line((x + i, 0, x + i, h), fill=(255, 255, 255))
    d.rectangle((10, 10, 40 + i, 40), fill=(250, 200, 0))
    return im.filter(ImageFilter.GaussianBlur(3)) if i % 10 < 5 else im   # half the frames "blurred"


@pytest.fixture(scope="session")
def video(tmp_path_factory) -> Path:
    """3 s, 10 fps, 160×90: frames 0-4 of every 10 are blurred."""
    path = tmp_path_factory.mktemp("media") / "clip.mp4"
    with av.open(str(path), "w") as c:
        st = c.add_stream("mpeg4", rate=10)
        st.width, st.height, st.pix_fmt = 160, 90, "yuv420p"
        for i in range(30):
            for pkt in st.encode(av.VideoFrame.from_image(_frame(i))):
                c.mux(pkt)
        for pkt in st.encode():
            c.mux(pkt)
    return path


@pytest.fixture(scope="session")
def photos(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("photos")
    for k in range(1, 7):
        im = _frame(k * 10 + 7, 300, 200)                      # sharp
        if k == 3:
            im = im.filter(ImageFilter.GaussianBlur(6))        # blurred
        im.save(d / (f"img{k}.png" if k % 2 else f"img{k}.jpg"))
    rot = _frame(5, 300, 200).transpose(Image.Transpose.ROTATE_90)
    exif = Image.Exif()
    exif[0x0112] = 6                                           # shown turned 90° clockwise
    rot.save(d / "img10_exif.jpg", exif=exif)
    (d / "notes.txt").write_text("not an image")
    return d


def run(name, args):
    return srv.run_tool(name, args)[0]


def err(name, args) -> str:
    with pytest.raises(srv.ToolError) as e:
        srv.run_tool(name, args)
    return str(e.value)


# ----------------------------------------------------------------------------- schema
def test_schema_json_is_up_to_date():
    on_disk = json.loads((ROOT / "schema.json").read_text())
    assert on_disk == json.loads(json.dumps(srv.schema_document(), ensure_ascii=False)), \
        "schema.json is out of date: uv run --script server.py --schema > schema.json"


def test_examples_are_valid():
    for name, ex in srv.EXAMPLES.items():
        for kind, args in ex.items():
            srv.TOOLS[name].args.model_validate(args, context={"skip_path_checks": True})


def test_every_field_is_described():
    def walk(s, path):
        if isinstance(s, dict):
            for k, v in (s.get("properties") or {}).items():
                assert "description" in v or "$ref" in v or "allOf" in v or \
                    any("$ref" in x for x in v.get("anyOf", [])), f"{path}.{k} has no description"
            for k, v in s.items():
                walk(v, f"{path}.{k}")
    for name in ("media_info", "make_storyboard", "extract_frames"):
        walk(srv.input_schema(name).get("$defs", {}), name)


# ----------------------------------------------------------------------------- video
def test_media_info_video(video):
    d = run("media_info", {"input": {"video": str(video)}})
    v = d["video"]
    assert (d["kind"], v["frames"], v["fps"], v["displayed_size"], v["rotation_deg"]) == ("video", 30, 10.0,
                                                                                          [160, 90], 0)


def test_minimal_storyboard(video, tmp_path):
    d, extra = srv.run_tool("make_storyboard", {"input": {"video": str(video)}, "output": {"dir": str(tmp_path)}})
    assert len(d["panels"]) == 12 and d["selection"]["mode"] == "sharpest"
    assert all(p["sharpness"] > 0 for p in d["panels"])
    assert Path(d["pages"][0]["path"]).is_file() and extra and extra[0].type == "image"
    jsonschema.validate(d, srv.output_schema("make_storyboard"))
    frames = [p["frame"] for p in d["panels"]]
    assert frames == sorted(frames)


def test_sharpest_avoids_blur(video, tmp_path):
    d = run("make_storyboard", {"input": {"video": str(video)}, "select": {"count": 3},
                                "output": {"dir": str(tmp_path), "preview": False}})
    assert all(p["frame"] % 10 >= 5 for p in d["panels"]), "sharpest mode picked a blurred frame"
    d = run("make_storyboard", {"input": {"video": str(video)}, "select": {"count": 10, "drop_blurriest_pct": 60},
                                "output": {"dir": str(tmp_path), "preview": False}})
    assert all(p["frame"] % 10 >= 5 for p in d["panels"]) and d["selection"]["threshold"] > 0


def test_exact_positions_and_last_frame(video, tmp_path):
    d = run("make_storyboard", {"input": {"video": str(video)}, "select": {"at_pct": [0, 50, 100]},
                                "output": {"dir": str(tmp_path), "preview": False}})
    p = d["panels"]
    assert [x["frame"] for x in p] == [0, 14, 29] or [x["frame"] for x in p] == [0, 15, 29]
    assert p[0]["anchor"] == "first_frame" and p[-1]["anchor"] == "last_frame" and p[-1]["pct"] == 100.0


def test_storyboard_pdf_notes_numbering_shotlist(video, tmp_path):
    panels = [{"shot": "PG", "action": "Entra"}, {"number": "2A", "dialogue": "Hola"}, {"camera": "paneo"}]
    d = run("make_storyboard", {
        "input": {"video": str(video)}, "select": {"count": 9}, "panels": panels,
        "layout": {"template": "storyboard", "page": "A4", "numbering": {"format": "5.{n}"}},
        "style": {"theme": "light"}, "output": {"dir": str(tmp_path), "name": "sb", "format": "pdf",
                                                "shot_list": ["json", "csv"]}})
    assert Path(d["pdf"]).read_bytes()[:4] == b"%PDF" and len(d["pages"]) == 2
    assert [p["number"] for p in d["panels"][:4]] == ["5.1", "2A", "5.3", "5.4"]
    rows = list(csv.DictReader(open(d["shot_list"]["csv"], encoding="utf-8-sig")))
    assert rows[0]["shot"] == "PG" and rows[1]["dialogue"] == "Hola" and rows[1]["number"] == "2A"
    assert json.loads(Path(d["shot_list"]["json"]).read_text())["panels"][2]["notes"] == {"camera": "paneo"}


def test_panels_choose_their_frames(video, tmp_path):
    d = run("make_storyboard", {"input": {"video": str(video)}, "panels": [{"frame": 20, "action": "b"},
                                                                         {"at_s": 0.5, "action": "a"}],
                                "output": {"dir": str(tmp_path), "preview": False}})
    assert [p["frame"] for p in d["panels"]] == [20, 5] and d["selection"]["mode"] == "panels"


def test_snap_moves_only_blurred_positions(video, tmp_path):
    d = run("make_storyboard", {"input": {"video": str(video)}, "select": {"at_frames": [2, 7, 17], "snap_s": 0.4},
                                "output": {"dir": str(tmp_path), "preview": False}})
    frames = [p["frame"] for p in d["panels"]]
    assert frames[1:] == [7, 17] and frames[0] % 10 >= 5, frames     # 2 is blurred: moved; 7 and 17 stay
    d = run("make_storyboard", {"input": {"video": str(video)}, "select": {"at_frames": [2, 7, 17, 27]},
                                "output": {"dir": str(tmp_path), "preview": False}})
    assert any("look blurred" in w and "snap_s" in w for w in d["warnings"])


def test_fixed_page_uses_only_needed_rows(video, tmp_path):
    d = run("make_storyboard", {"input": {"video": str(video)}, "select": {"count": 3},
                                "layout": {"template": "storyboard", "page": "A4", "orientation": "portrait"},
                                "output": {"dir": str(tmp_path), "preview": False}})
    assert d["layout"]["rows_per_page"] == 1 and len(d["pages"]) == 1


def test_extract_frames_video(video, tmp_path):
    d = run("extract_frames", {"input": {"video": str(video)}, "select": {"at_frames": [7], "snap_s": 0.3},
                               "output": {"dir": str(tmp_path)}})
    f = d["files"][0]
    assert f["frame"] % 10 >= 5 and Path(f["file"]).name == f"f{f['frame']:05d}.jpg"
    assert Image.open(f["file"]).size == (160, 90)


def test_rotation(video, tmp_path):
    out = tmp_path / "rot.mp4"
    with av.open(str(video)) as src, av.open(str(out), "w") as dst:
        ist = src.streams.video[0]
        ost = dst.add_stream_from_template(ist)
        try:
            ost.set_display_rotation(90)          # PyAV >= 15
        except AttributeError:
            pytest.skip("this PyAV cannot write a display matrix")
        for pkt in src.demux(ist):
            if pkt.dts is not None:
                pkt.stream = ost
                dst.mux(pkt)
    info = run("media_info", {"input": {"video": str(out)}})["video"]
    assert info["rotation_deg"] in (90, 270) and info["displayed_size"] == [90, 160]
    f = run("extract_frames", {"input": {"video": str(out)}, "select": {"at_frames": [0]},
                               "output": {"dir": str(tmp_path / "f")}})["files"][0]
    assert Image.open(f["file"]).size == (90, 160)


# ----------------------------------------------------------------------------- images
def test_images_folder(photos, tmp_path):
    info = run("media_info", {"input": {"folder": str(photos)}})
    names = [x["name"] for x in info["images"]]
    assert names == ["img1.png", "img2.jpg", "img3.png", "img4.jpg", "img5.png", "img6.jpg", "img10_exif.jpg"]
    assert info["images"][-1]["displayed_size"] == [300, 200]
    d = run("make_storyboard", {"input": {"folder": str(photos)}, "output": {"dir": str(tmp_path)}})
    assert len(d["panels"]) == 7 and d["selection"]["mode"] == "all" and d["panels"][0]["name"] == "img1.png"


def test_images_sharpest_and_copy(photos, tmp_path):
    imgs = [str(photos / n) for n in ("img1.png", "img2.jpg", "img3.png", "img4.jpg")]
    d = run("extract_frames", {"input": {"images": imgs}, "select": {"mode": "sharpest", "count": 2},
                               "output": {"dir": str(tmp_path)}})
    assert "img3.png" not in [f["name"] for f in d["files"]]
    for f in d["files"]:
        assert Path(f["file"]).read_bytes() == Path(f["source"]).read_bytes()   # copied unchanged
    by_name = run("extract_frames", {"input": {"images": imgs}, "select": {"at_names": ["IMG4.JPG"]},
                                     "output": {"dir": str(tmp_path / "n")}})
    assert by_name["files"][0]["image"] == 3


def test_images_best_duplicates_names(photos, tmp_path):
    import shutil
    d = tmp_path / "burst"
    d.mkdir()
    for n in ("img1.png", "img2.jpg", "img3.png", "img4.jpg"):
        shutil.copy(photos / n, d / n)
    shutil.copy(photos / "img2.jpg", d / "img2_copy.jpg")                 # exact duplicate
    info = run("media_info", {"input": {"folder": str(d)}})
    assert [x.get("duplicate_of") for x in info["images"]] == [None, None, 1, None, None]
    best = run("extract_frames", {"input": {"folder": str(d)}, "select": {"mode": "BEST", "count": 3},
                                  "output": {"dir": str(tmp_path / "o"), "names": "original"}})
    names = [f["name"] for f in best["files"]]
    assert len(names) == 3 and "img3.png" not in names and not {"img2.jpg", "img2_copy.jpg"} <= set(names)
    rep = [x for x in best["dropped"] if x["name"] in ("img2.jpg", "img2_copy.jpg")]
    assert rep and rep[0]["reason"] == "min_difference" and rep[0]["difference"] == 0 and best["warnings"]
    assert all(Path(f["file"]).name == f["name"] for f in best["files"]) and best["files"][0]["size"] == [300, 200]
    counted = run("make_storyboard", {"input": {"folder": str(d)}, "select": {"count": 2},
                                      "output": {"dir": str(tmp_path), "preview": False}})
    assert len(counted["panels"]) == 2 and counted["selection"]["mode"] == "sharpest"


def test_fewer_panels_are_explained(video, tmp_path):
    d = run("make_storyboard", {"input": {"video": str(video)}, "select": {"count": 6, "drop_blurriest_pct": 60},
                                "output": {"dir": str(tmp_path), "preview": False}})
    assert len(d["panels"]) < 6 and any("6 panels asked for" in w for w in d["warnings"])
    frames = [p["frame"] for p in run("make_storyboard", {"input": {"video": str(video)}, "select": {"count": 6},
                                                         "output": {"dir": str(tmp_path), "preview": False}})["panels"]]
    assert all(b - a >= 2 for a, b in zip(frames, frames[1:])), frames      # never two neighbouring frames


def test_csv_delimiter_and_case(video, tmp_path):
    d = run("make_storyboard", {"input": {"video": str(video)}, "select": {"count": 2},
                                "layout": {"template": "Storyboard", "page": "a4"},
                                "output": {"dir": str(tmp_path), "format": "PDF", "shot_list": ["CSV"],
                                           "csv_style": "Excel_ES", "preview": False}})
    assert d["pdf"].endswith(".pdf") and d["layout"]["page"] == "A4" and d["pages"][0]["path"] == d["pdf"]
    rows = list(csv.reader(open(d["shot_list"]["csv"], encoding="utf-8-sig"), delimiter=";"))
    assert rows[0][:5] == ["n", "viñeta", "página", "fotograma", "segundos"] and "imagen" not in rows[0]
    assert rows[1][1] == '="1"' and "," in rows[1][4]                                   # segundos 0,5
    e = err("make_storyboard", {"input": {"video": str(video)}, "style": {"accent": "naranja"}})
    assert '(for "naranja": "orange")' in e


# ----------------------------------------------------------------------------- styles and errors
def test_custom_style_and_transparency(video, tmp_path):
    d = run("make_storyboard", {"input": {"video": str(video)}, "select": {"count": 2},
                                "style": {"background": "#00000000", "label_text": "red", "border_width": 3},
                                "layout": {"guides": {"aspects": ["2.39:1"], "thirds": True, "safe_zone": True}},
                                "output": {"dir": str(tmp_path), "preview": False}})
    im = Image.open(d["pages"][0]["path"])
    assert im.mode == "RGBA" and im.getpixel((0, 0))[3] == 0


def test_errors_are_clear(video, photos):
    e = err("make_storyboard", {"input": {"video": str(video), "folder": str(photos)}})
    assert 'input: give exactly one of "video", "images" or "folder"' in e
    e = err("make_storyboard", {"input": {"video": str(video)}, "layout": {"colums": 3},
                                "style": {"background": "#12"}})
    assert 'layout.colums: unknown field "colums". Did you mean "columns"?' in e
    assert 'style.background: "#12" is not a valid colour' in e
    e = err("make_storyboard", {"input": {"images": [str(photos / "img1.png"), str(photos / "notes.txt")]}})
    assert "input.images[1]: not an image this server can read: notes.txt" in e
    e = err("make_storyboard", {"input": {"folder": str(photos)}, "select": {"at_s": [1]}})
    assert "select.at_s: only for video input" in e
    e = err("make_storyboard", {"input": {"folder": str(photos)}, "output": {"format": "MP4"}})
    assert 'output.format: "mp4" is planned for animated output and not available yet' in e
    e = err("make_storyboard", {"input": {"video": str(video)}, "layout": {"numbering": "3.{n}"}})
    assert 'layout.numbering: expected an object like {"format": "5.{n}"}' in e
    e = err("make_storyboard", {"input": {"video": "no/such.mp4"}})
    assert "input.video: file not found" in e and "relative paths are resolved" in e
    e = err("make_storyboard", {"input": {"video": str(video)}, "panels": [{"frame": 1}, {"action": "x"}]})
    assert "either every panel has a position" in e and "without one: panels[1]" in e
    e = err("make_storyboard", {"input": {"video": str(video)}, "layout": {"label": {"template": "{nope}"}}})
    assert "unknown placeholder {nope}" in e
    assert "unknown tool" in err("storyboard", {})


# ----------------------------------------------------------------------------- MCP protocol
def test_mcp_round_trip(video, tmp_path):
    from mcp.shared.memory import create_connected_server_and_client_session
    import anyio

    async def go():
        async with create_connected_server_and_client_session(srv.server) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            assert set(tools) == {"media_info", "make_storyboard", "extract_frames", "get_schema"}
            assert tools["make_storyboard"].inputSchema["required"] == ["input"]
            ok = await client.call_tool("make_storyboard", {"input": {"video": str(video)},
                                                            "select": {"count": 2},
                                                            "output": {"dir": str(tmp_path)}})
            assert not ok.isError and ok.structuredContent["panels"][0]["n"] == 1
            assert [c.type for c in ok.content] == ["text", "image"]
            bad = await client.call_tool("make_storyboard", {"input": {}})
            assert bad.isError and "give exactly one of" in bad.content[0].text
            res = await client.read_resource("storyboard://schema")
            assert "make_storyboard" in json.loads(res.contents[0].text)["tools"]

    anyio.run(go)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
