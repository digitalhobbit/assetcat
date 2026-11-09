"""Per-file metadata extraction. Runs in worker processes; only reads files."""

from __future__ import annotations

import html
import json
import posixpath
import re
import warnings
import xml.etree.ElementTree as ET

# Stamped on every file read. Bumping it alone re-reads nothing: also raise META_MIN to re-read every
# file, or set the category in META_MIN_BY_CATEGORY to re-read only those (2: alpha kind, 3: 3D models).
META_VERSION = 3
META_MIN = 2
META_MIN_BY_CATEGORY = {"model": 3}

MAX_DECODE_PIXELS = 4_000_000  # palette counting only below this
MAX_TEXT = 60_000
MAX_DATA_BYTES = 4_000_000
PALETTE_CAP = 256


def extract(job: tuple[str, str, str, str]) -> tuple[str, dict]:
    """job = (rel, fs_path, ext, category) -> (rel, columns)."""
    rel, path, ext, cat = job
    out: dict = {}
    try:
        if cat == "source" and ext in ("psd", "psb"):
            out.update(_psd(path))
        elif cat == "image":
            out.update(_image(path, True))
        elif cat == "audio":
            out.update(_audio(path))
        elif cat == "font":
            out.update(_font(path))
        elif cat == "doc":
            t = _doc_text(path, ext)
            if t:
                out["text"] = t[:MAX_TEXT]
        elif cat == "model":
            info = _model(rel, path, ext)
            if info:
                out["info"] = json.dumps(info)
        elif cat == "data":
            info = _data(path, ext)
            if info:
                out["info"] = json.dumps(info)
    except Exception as e:  # noqa: BLE001 - record, never crash the index
        out["error"] = f"{type(e).__name__}: {e}"[:300]
    return rel, out


def _image(path: str, full: bool) -> dict:
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with Image.open(path) as im:
            w, h = im.size
            out = {"width": w, "height": h}
            frames = getattr(im, "n_frames", 1)
            if frames and frames > 1:
                out["frames"] = frames
            if full and w * h <= MAX_DECODE_PIXELS:
                im.seek(0)
                rgba = im.convert("RGBA")
                colors = rgba.getcolors(PALETTE_CAP)
                # opaque colors only; fully transparent pixels don't count
                if colors is None:
                    out["palette"] = PALETTE_CAP + 1
                else:
                    out["palette"] = len({c for n, c in colors if c[3] > 0})
                # 0 opaque, 1 hard edges only (alpha 0 or 255: typical of pixel art), 2 semi-transparent pixels
                hist = rgba.getchannel("A").histogram()
                out["alpha"] = 2 if sum(hist[1:255]) else (1 if hist[0] else 0)
            return out


def _psd(path: str) -> dict:
    """Size from the fixed PSD/PSB header; Pillow's PSD loader chokes on some layer data."""
    import struct

    with open(path, "rb") as f:
        head = f.read(26)
    if len(head) < 26 or head[:4] != b"8BPS":
        return {"error": "not a PSD file"}
    h, w = struct.unpack(">II", head[14:22])
    return {"width": w, "height": h}


def _audio(path: str) -> dict:
    import mutagen

    f = mutagen.File(path)
    if f is None or f.info is None:
        return {"error": "unrecognized audio"}
    info = f.info
    out = {"duration": round(float(getattr(info, "length", 0) or 0), 3)}
    ch = getattr(info, "channels", None)
    sr = getattr(info, "sample_rate", None)
    if ch:
        out["channels"] = int(ch)
    if sr:
        out["sample_rate"] = int(sr)
    return out


def _font(path: str) -> dict:
    from fontTools.ttLib import TTFont

    with TTFont(path, lazy=True, fontNumber=0) as f:
        name = f["name"].getBestFullName() or f["name"].getBestFamilyName()
        return {"font_family": str(name)} if name else {}


def _read_text(path: str) -> str:
    raw = open(path, "rb").read(2_000_000)
    for enc in ("utf-8-sig", "utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8-sig",):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("cp1252", errors="replace")


_TAG = re.compile(r"<[^>]+>")


def _doc_text(path: str, ext: str) -> str | None:
    if ext in ("txt", "md"):
        t = _read_text(path)
    elif ext == "rtf":
        from striprtf.striprtf import rtf_to_text

        t = rtf_to_text(_read_text(path), errors="ignore")
    elif ext in ("html", "htm"):
        t = _read_text(path)
        t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", t)
        t = html.unescape(_TAG.sub(" ", t))
    elif ext == "pdf":
        from pypdf import PdfReader

        r = PdfReader(path)
        t = "\n".join((p.extract_text() or "") for p in r.pages[:12])
    elif ext in ("url", "webloc"):
        t = _read_text(path)
        m = re.search(r"(https?://[^\s<\"]+)", t)
        return m.group(1) if m else None
    else:
        return None
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip() or None


def _data(path: str, ext: str) -> dict | None:
    import os

    if os.path.getsize(path) > MAX_DATA_BYTES:
        return None
    if ext in ("tsx", "tmx"):
        root = ET.parse(path).getroot()
        sets = []
        for ts in ([root] if root.tag == "tileset" else root.findall("tileset")):
            img = ts.find("image")
            if img is None or "tilewidth" not in ts.attrib:
                continue
            sets.append({
                "image": img.get("source"),
                "w": int(ts.get("tilewidth")), "h": int(ts.get("tileheight")),
                "spacing": int(ts.get("spacing", 0)), "margin": int(ts.get("margin", 0)),
            })
        return {"kind": "tiled", "tilesets": sets} if sets else None
    if ext == "xml":
        root = ET.parse(path).getroot()
        if root.tag == "TextureAtlas":
            subs = root.findall("SubTexture")
            return _atlas(root.get("imagePath"), [(int(float(s.get("width", 0))), int(float(s.get("height", 0)))) for s in subs])
        return None
    if ext == "json":
        d = json.loads(_read_text(path))
        if not isinstance(d, dict) or "frames" not in d:
            return None
        meta = d.get("meta") or {}
        fr = d["frames"]
        items = fr.values() if isinstance(fr, dict) else fr
        sizes = []
        for it in items:
            if isinstance(it, dict) and isinstance(it.get("frame"), dict):
                sizes.append((int(it["frame"].get("w", 0)), int(it["frame"].get("h", 0))))
        return _atlas(meta.get("image"), sizes) if sizes else None
    return None


MODEL_EXTS = {"glb", "gltf", "obj"}
MAX_OBJ_BYTES = 200_000_000


def _ref(rel: str, uri: str) -> str:
    """A file a model refers to, as a library path (`..` left in when it points outside)."""
    from urllib.parse import unquote

    uri = unquote(uri).replace("\\", "/")
    if re.match(r"^([a-zA-Z]:)?/", uri):  # absolute: the artist's machine
        return uri
    return posixpath.normpath(posixpath.join(posixpath.dirname(rel), uri))


def _model(rel: str, path: str, ext: str) -> dict | None:
    """Model facts from the file's structure only (no geometry decoding): triangles, meshes,
    animation clips, skins, required extensions and the files it loads (buffers, textures, .mtl)."""
    if ext in ("glb", "gltf"):
        return _gltf(rel, path, ext)
    if ext == "obj":
        return _obj(rel, path)
    return None


def _gltf(rel: str, path: str, ext: str) -> dict:
    import struct

    with open(path, "rb") as f:
        if ext == "glb":
            head = f.read(20)
            if len(head) < 20 or head[:4] != b"glTF":
                return {"kind": "model", "error": "not a GLB file"}
            n, typ = struct.unpack("<II", head[12:20])
            if typ != 0x4E4F534A:  # "JSON"
                return {"kind": "model", "error": "GLB without a JSON chunk"}
            j = json.loads(f.read(n))
        else:
            j = json.loads(f.read().decode("utf-8-sig"))
    acc = j.get("accessors") or []

    def count(i):
        return acc[i].get("count", 0) if isinstance(i, int) and 0 <= i < len(acc) else 0

    mesh_tris = []
    for m in j.get("meshes") or []:
        t = 0
        for pr in m.get("primitives") or []:
            n = count(pr["indices"]) if "indices" in pr else count((pr.get("attributes") or {}).get("POSITION"))
            mode = pr.get("mode", 4)
            t += n // 3 if mode == 4 else max(0, n - 2) if mode in (5, 6) else 0
        mesh_tris.append(t)
    used = [nd["mesh"] for nd in j.get("nodes") or [] if isinstance(nd.get("mesh"), int) and nd["mesh"] < len(mesh_tris)]
    out: dict = {"kind": "model", "tris": sum(mesh_tris[i] for i in used) if used else sum(mesh_tris),
                 "meshes": len(mesh_tris)}
    clips = [a.get("name") or f"clip {i + 1}" for i, a in enumerate(j.get("animations") or [])]
    if clips:
        out["clips"] = clips
    if j.get("skins"):
        out["skinned"] = True
    if j.get("extensionsRequired"):
        out["requires"] = j["extensionsRequired"]
    refs = [x["uri"] for k in ("buffers", "images") for x in j.get(k) or []
            if isinstance(x.get("uri"), str) and not x["uri"].startswith("data:")]
    if refs:
        out["refs"] = list(dict.fromkeys(_ref(rel, u) for u in refs))
    return out


_MTL_MAP = re.compile(r"^\s*(map_\w+|bump|disp|decal|norm|refl)\s+(.+?)\s*$", re.I)


def _obj(rel: str, path: str) -> dict:
    import os

    if os.path.getsize(path) > MAX_OBJ_BYTES:
        return {"kind": "model"}
    tris = groups = 0
    mtls: list[str] = []
    with open(path, "rb") as f:
        for line in f:
            if line[:2] == b"f ":
                tris += max(0, len(line.split()) - 3)
            elif line[:2] in (b"o ", b"g "):
                groups += 1
            elif line[:7] == b"mtllib ":
                mtls.append(line[7:].decode("utf-8", "replace").strip())
    out: dict = {"kind": "model", "tris": tris, "meshes": max(1, groups)}
    refs = []
    for m in mtls:
        mrel = _ref(rel, m)
        refs.append(mrel)
        mfs = os.path.join(os.path.dirname(path), *m.replace("\\", "/").split("/"))
        if not os.path.isfile(mfs):
            continue
        for line in _read_text(mfs).splitlines():
            mm = _MTL_MAP.match(line)
            if mm:
                rest = mm.group(2)
                name = rest.split()[-1] if rest.startswith("-") else rest  # options like -bm 1 come first
                refs.append(_ref(mrel, name))
    if refs:
        out["refs"] = list(dict.fromkeys(refs))
    return out


def _atlas(image: str | None, sizes: list[tuple[int, int]]) -> dict | None:
    if not image or not sizes:
        return None
    uniform = len(set(sizes)) == 1
    out = {"kind": "atlas", "image": image, "frames": len(sizes)}
    if uniform:
        out["w"], out["h"] = sizes[0]
    return out
