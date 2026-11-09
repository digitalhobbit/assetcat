"""Sheet and tileset grids from ground truth the packs ship: curation overrides, Tiled .tsx/.tmx,
texture atlases (XML/JSON), Kenney Tilesheet.txt, and sizes in names ("16x16", "tiles_16px").
No pixel heuristics yet (milestone 6)."""

from __future__ import annotations

import json
import posixpath
import re

_TS_SIZE = re.compile(r"tile size\W*(\d+)\s*px\s*[×xX]\s*(\d+)", re.I)
_TS_SPACE = re.compile(r"space between tiles\W*(\d+)\s*px", re.I)
_NAME_WH = re.compile(r"(?<!\d)(\d{1,3})\s*[x×X]\s*(\d{1,3})(?!\d)")
_NAME_PX = re.compile(r"(?<!\d)(\d{1,3})\s*px(?![a-z])", re.I)
_SHEETISH = re.compile(r"(tilesheet|tilemap|tileset|spritesheet|sprite ?sheet|sheet|atlas|tiles)", re.I)


def parse_kenney_tilesheet(text: str) -> tuple[int, int, int] | None:
    m = _TS_SIZE.search(text or "")
    if not m:
        return None
    s = _TS_SPACE.search(text)
    return int(m.group(1)), int(m.group(2)), int(s.group(1)) if s else 0


def fit(W: int, H: int, w: int, h: int, s: int = 0, m: int = 0) -> tuple[int, int] | None:
    """(cols, rows) if a w×h grid with spacing s and margin m tiles a W×H image exactly."""
    if not (W and H and w > 0 and h > 0):
        return None
    iw, ih = W - 2 * m + s, H - 2 * m + s
    if iw <= 0 or ih <= 0 or iw % (w + s) or ih % (h + s):
        return None
    cols, rows = iw // (w + s), ih // (h + s)
    if cols * rows < 2:
        return None
    return cols, rows


def assign_grids(assets: list[dict], pack_files: list[dict], file_asset: dict[str, int], overrides: dict, pack_rel: str):
    """Fill grid_* fields on raster assets of one pack, in place.

    assets: asset dicts (with 'path', 'width', 'height', 'media', 'displayable', and 'idx').
    pack_files: file rows of the pack. file_asset: library path -> index into assets.
    overrides: curation grid table {path-in-pack: [w, h] | {w, h, spacing, margin}}.
    """
    by_idx = {i: a for i, a in enumerate(assets)}

    def set_grid(a, w, h, s, m, src, conf, atlas=None):
        if a.get("grid_confidence") is not None and a["grid_confidence"] >= conf:
            return
        f = fit(a.get("width") or 0, a.get("height") or 0, w, h, s, m)
        if not f:
            return
        a.update(grid_w=w, grid_h=h, grid_spacing=s, grid_margin=m, grid_cols=f[0], grid_rows=f[1],
                 grid_source=src, grid_confidence=conf, atlas=atlas)

    # 1. curation overrides (trusted even if the fit check fails? no: must fit, else it's a typo)
    for p, v in (overrides or {}).items():
        i = file_asset.get(f"{pack_rel}/{p}")
        if i is None:
            continue
        if isinstance(v, list):
            w, h, s, m = v[0], v[1], 0, 0
        else:
            w, h, s, m = v.get("w"), v.get("h", v.get("w")), v.get("spacing", 0), v.get("margin", 0)
        set_grid(by_idx[i], w, h, s, m, "curation", 1.0)

    kenney: list[tuple[int, int, int]] = []
    for f in pack_files:
        if f["category"] == "data" and f["info"]:
            info = json.loads(f["info"])
            base = posixpath.dirname(f["path"])
            if info.get("kind") == "tiled":
                for ts in info["tilesets"]:
                    i = file_asset.get(posixpath.normpath(posixpath.join(base, ts["image"] or "")))
                    if i is not None:
                        set_grid(by_idx[i], ts["w"], ts["h"], ts["spacing"], ts["margin"], "tiled", 1.0, f["path"])
            elif info.get("kind") == "atlas":
                i = file_asset.get(posixpath.normpath(posixpath.join(base, info["image"])))
                if i is not None:
                    a = by_idx[i]
                    if "w" in info:
                        set_grid(a, info["w"], info["h"], 0, 0, "atlas", 0.9, f["path"])
                    if a.get("grid_source") is None:
                        a.update(frames=info["frames"], atlas=f["path"], grid_source="atlas")
        elif f["ext"] == "txt" and f["text"] and "tilesheet" in f["path"].lower().rsplit("/", 1)[-1]:
            k = parse_kenney_tilesheet(f["text"])
            if k:
                kenney.append(k)

    for a in assets:
        if a["media"] != "visual" or not a.get("width"):
            continue
        name = a["path"].rsplit("/", 1)[-1]
        if kenney and _SHEETISH.search(a["path"][len(pack_rel):]):
            for w, h, s in kenney:
                set_grid(a, w, h, s, 0, "tilesheet.txt", 0.95)
                set_grid(a, w, h, 0, 0, "tilesheet.txt", 0.95)
        if a.get("grid_confidence") is not None or a.get("type") == "preview":
            continue
        # sizes in the file name, then in folder names (innermost first)
        parts = [name] + list(reversed(a["path"][len(pack_rel):].split("/")[:-1]))
        for part in parts:
            m = _NAME_WH.search(part)
            if m:
                w, h = int(m.group(1)), int(m.group(2))
            else:
                m = _NAME_PX.search(part)
                if not m:
                    continue
                w = h = int(m.group(1))
            if 8 <= w <= 256 and 8 <= h <= 256:
                set_grid(a, w, h, 0, 0, "name", 0.75 if part is name else 0.6)
                if a.get("grid_confidence") is not None:
                    break
