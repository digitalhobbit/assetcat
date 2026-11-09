"""`assetcat serve`: the browse UI. A FastAPI app on localhost that serves the JSON API, the
library files (read-only, only paths in the index), lazy thumbnails, contact sheets, and the
static frontend in web/ (inside the package). Edits go to the curation files through edit.py."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import os
import posixpath
import random
import re
import sqlite3
import subprocess
import sys
import threading
import warnings
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import ai as A
from . import classify as C
from . import embed as M
from . import edit as E
from . import scan as S
from . import sheets
from .cli import fts_query, license_info, pack_detail
from .config import WEB_DIR, Config, eprint, read_user_config
from .curation import load_curation
from .indexer import descendants, run_index, sheet_items, sheet_job

THUMB_SIZES = (96, 128, 192, 256, 384, 512)
MAX_RENDER_BYTES = 4_000_000
MIN_PREVIEW = 128  # a model's bundled render is its thumbnail from this size; smaller ones get rendered
TEXTURE_DIR = re.compile(r"/(skins?|textures?|materials?|maps)/", re.I)
INLINE_IMAGE = {"png", "gif", "jpg", "jpeg", "webp", "svg"}
ASSET_COLS = """a.id, a.pack_id, a.name, a.folder, a.path, a.media, a.type, a.formats, a.variants, a.available,
    a.displayable, a.width, a.height, a.frames, a.palette, a.duration, a.grid_w, a.grid_h, a.grid_spacing,
    a.grid_margin, a.grid_cols, a.grid_rows, a.grid_source, a.atlas, a.preview, a.tris, a.clips, a.rigged, a.missing"""

_edit_lock = threading.Lock()


class SheetReq(BaseModel):
    pack: str
    folder: str = ""
    limit: int = 100


class PackEdit(BaseModel):
    id: str
    fields: dict


class GroupEdit(BaseModel):
    id: str
    fields: dict


class GridEdit(BaseModel):
    asset_id: int
    grid: dict | None = None


class ShortlistItem(BaseModel):
    name: str
    kind: str = ""
    ref: str = ""
    note: str = ""


class Reveal(BaseModel):
    path: str


class TagsReq(BaseModel):
    id: str
    add: list[str] = []
    remove: list[str] = []


class RolesReq(BaseModel):
    group: str
    roles: dict[str, str | None] = {}


def create_app(cfg: Config) -> FastAPI:
    app = FastAPI(title="assetcat", docs_url=None, redoc_url=None, lifespan=_lifespan)
    thumbs_dir = cfg.cache / "thumbs"
    _local = threading.local()  # one connection per server thread
    _cache: dict = {}  # per-index lookups, see derived()
    _scans: dict = {}  # group id -> (index stamp, section, scanned folder), for the layout page
    _vec: dict = {}  # image vectors for search by meaning, reloaded when the index or the shards change
    _vec_lock = threading.Lock()
    embed_model = M.model_name(read_user_config())

    def vectors(con) -> M.Vectors | None:
        key = (indexed_at(con), tuple((p.name, p.stat().st_mtime) for p in M.shards(cfg, embed_model)))
        if not key[1]:
            return None
        with _vec_lock:
            if _vec.get("key") != key:
                _vec.update(key=key, v=M.Vectors(cfg, con, embed_model))
            return _vec["v"]

    def text_vector(text: str):
        with _vec_lock:
            if "enc" not in _vec:
                try:
                    _vec["enc"] = M.Encoder(embed_model)
                except M.EmbedError as e:
                    raise HTTPException(400, f"Search by meaning {e}") from e
            return _vec["enc"].texts([text])[0]

    def db() -> sqlite3.Connection:
        con = getattr(_local, "con", None)
        if con is None:
            con = sqlite3.connect(cfg.db_path, check_same_thread=False)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA busy_timeout=5000")
            _local.con = con
        return con

    def indexed_at(con) -> str:
        r = con.execute("SELECT value FROM meta WHERE key='indexed_at'").fetchone()
        return r[0] if r else ""

    def derived(con) -> dict:
        """Per-index lookups: newest file per pack (recent sort) and 'also in' duplicates."""
        stamp = indexed_at(con)
        if _cache.get("stamp") != stamp:
            parents = {r["id"]: r["parent_id"] for r in con.execute("SELECT id, parent_id FROM packs")}
            newest: dict[str, float] = {}
            for r in con.execute("SELECT pack_id, MAX(mtime) m FROM files WHERE pack_id IS NOT NULL GROUP BY pack_id"):
                pid = r["pack_id"]
                while pid:
                    if r["m"] > newest.get(pid, 0):
                        newest[pid] = r["m"]
                    pid = parents.get(pid)
            also: dict[str, list] = {}
            for r in con.execute("SELECT * FROM pack_dupes WHERE frac_a >= 0.9 OR frac_b >= 0.9 ORDER BY shared DESC"):
                if r["frac_a"] >= 0.9:
                    also.setdefault(r["pack_a"], []).append(r["pack_b"])
                if r["frac_b"] >= 0.9:
                    also.setdefault(r["pack_b"], []).append(r["pack_a"])
            # pixel art: most of a pack's images are small with few colors (rolled up into collections)
            # Pixel art: few colors and hard alpha edges (anti-aliased art has semi-transparent pixels).
            px: dict[str, list[int]] = {}
            for r in con.execute("""SELECT a.pack_id, COUNT(*) n,
                                      SUM(f.palette <= 64 AND (f.alpha = 1 OR (f.alpha = 0 AND f.palette <= 32))) px
                                    FROM assets a JOIN files f ON f.path = a.path
                                    WHERE a.media='visual' AND a.type != 'preview' AND f.palette IS NOT NULL GROUP BY a.pack_id"""):
                pid = r["pack_id"]
                while pid:
                    t = px.setdefault(pid, [0, 0])
                    t[0] += r["n"]; t[1] += r["px"] or 0
                    pid = parents.get(pid)
            pixel = {k for k, (n, sm) in px.items() if n >= 3 and sm / n >= 0.6}
            named = re.compile(r"pixel|\b(1|8|16)[- ]?bit|pico-?8|roguelike", re.I)
            for r in con.execute("SELECT id, name, tags FROM packs"):
                if named.search(r["name"]) or (r["tags"] and "style:pixel-art" in json.loads(r["tags"])):
                    pixel.add(r["id"])
            # copies: A is (almost) contained in B; hide A when B is shown too
            copy_of: dict[str, list[str]] = {}
            for r in con.execute("SELECT * FROM pack_dupes WHERE frac_a >= 0.9 OR frac_b >= 0.9"):
                for a, b, fa, fb in ((r["pack_a"], r["pack_b"], r["frac_a"], r["frac_b"]), (r["pack_b"], r["pack_a"], r["frac_b"], r["frac_a"])):
                    if fa >= 0.9 and (fb < 0.9 or a > b):
                        copy_of.setdefault(a, []).append(b)
            _cache.update(stamp=stamp, newest=newest, also=also, pixel=pixel, copy_of=copy_of)
        return _cache

    def file_row(con, rel: str):
        r = con.execute("SELECT path, ext, size, mtime, width, height FROM files WHERE path=?", (rel,)).fetchone()
        if not r or r["size"] == 0:
            raise HTTPException(404, "not in the index (or not synced yet)")
        return r

    def card(p: dict, groups: dict, d: dict) -> dict:
        tc = json.loads(p["type_counts"] or "{}")
        return {
            "id": p["id"], "name": p["name"], "group_id": p["group_id"], "group": groups[p["group_id"]]["name"],
            "section": p["section"], "kind": p["kind"], "role": p["role"], "parent_id": p["parent_id"],
            "asset_count": p["asset_count"], "file_count": p["file_count"], "total_size": p["total_size"],
            "pending": p["pending"], "license": p["license"], "ai_generated": p["ai_generated"],
            "engines": p["engines"], "types": list(tc.items())[:3], "has_cover": _show_cover(p),
            "tags": json.loads(p["tags"]) if p["tags"] else [], "also_in": d["also"].get(p["id"], [])[:3],
            "children": p.get("children", 0), "newest": d["newest"].get(p["id"]), "pixel": p["id"] in d["pixel"],
        }

    # --- meta ------------------------------------------------------------------------------

    @app.get("/api/meta")
    def meta():
        con = db()
        cur = load_curation(cfg)
        groups = [dict(r) for r in con.execute("""SELECT g.*, (SELECT COUNT(*) FROM packs p WHERE p.group_id=g.id AND p.parent_id IS NULL) AS packs
                                                  FROM groups g ORDER BY g.section, g.name""")]
        return {
            "indexed_at": indexed_at(con),
            "library": str(cfg.library),
            "groups": groups,
            "kinds": {r[0]: r[1] for r in con.execute("SELECT kind, COUNT(*) FROM packs WHERE parent_id IS NULL GROUP BY kind ORDER BY 2 DESC")},
            "ai": {r[0]: r[1] for r in con.execute("SELECT ai_generated, COUNT(*) FROM packs WHERE parent_id IS NULL GROUP BY 1")},
            "licenses": {k: {"key": k, **v} for k, v in cur.licenses.items()},
            "license_counts": {r[0]: r[1] for r in con.execute("SELECT license, COUNT(*) FROM packs WHERE parent_id IS NULL GROUP BY 1")},
            "types": {r[0]: r[1] for r in con.execute("SELECT type, COUNT(*) FROM assets GROUP BY type ORDER BY 2 DESC")},
            "stats": {
                "files": con.execute("SELECT COUNT(*) FROM files").fetchone()[0],
                "bytes": con.execute("SELECT SUM(size) FROM files").fetchone()[0],
                "packs": con.execute("SELECT COUNT(*) FROM packs").fetchone()[0],
                "assets": con.execute("SELECT COUNT(*) FROM assets").fetchone()[0],
            },
            "sections": [s.name for s in cfg.sections],
            "shortlists": E.list_shortlists(cfg),
            "tag_facets": tag_facets(con),
            "embed": {"model": embed_model, "ready": bool(M.shards(cfg, embed_model))},
            "tag_values": [f"{ns}:{v}" for ns, sp in A.load_vocab(cfg).pack_namespaces() for v in sp["values"]],
        }

    def tag_facets(con) -> list[dict]:
        """Facet namespaces with their values and top-level pack counts, for the pack wall."""
        vocab = A.load_vocab(cfg)
        counts: dict[str, int] = {}
        for (t,) in con.execute("SELECT tags FROM packs WHERE parent_id IS NULL AND tags IS NOT NULL"):
            for tag in json.loads(t):
                counts[tag] = counts.get(tag, 0) + 1
        out = []
        for ns in vocab.facets():
            vals = {v: counts[f"{ns}:{v}"] for v in vocab.namespaces[ns].get("values", []) if counts.get(f"{ns}:{v}")}
            if vals:
                out.append({"ns": ns, "counts": vals})
        return out

    # --- packs -----------------------------------------------------------------------------

    @app.get("/api/packs")
    def packs(q: str = "", section: str = "", group: str = "", kind: str = "", license: str = "", ai: str = "",
              tag: str = "", sub: bool = False, parent: str = "", sort: str = "name", seed: int = 0,
              style: str = "", copies: bool = True):
        con = db()
        d = derived(con)
        where, args = [], []
        sql = "SELECT p.*, (SELECT COUNT(*) FROM packs c WHERE c.parent_id=p.id) AS children"
        sql += ", f.score FROM packs p" if q.strip() else " FROM packs p"
        if q.strip():
            sql += " JOIN (SELECT rowid, bm25(packs_fts, 10, 2, 3, 1, 3, 4) AS score FROM packs_fts WHERE packs_fts MATCH ?) f ON f.rowid=p.rid"
            args.append(fts_query([q]))
        if parent:
            where.append("p.parent_id=?"); args.append(parent)
        elif not sub and not q.strip():
            where.append("p.parent_id IS NULL")
        for col, val in (("section", section), ("group_id", group), ("kind", kind), ("license", license), ("ai_generated", ai)):
            if val:
                where.append(f"p.{col}=?"); args.append(val)
        for t in filter(None, tag.split(",")):
            where.append("p.tags LIKE ?"); args.append(f'%"{t}"%')
        if where:
            sql += " WHERE " + " AND ".join(where)
        rows = [dict(r) for r in con.execute(sql, args)]
        groups = {r["id"]: dict(r) for r in con.execute("SELECT * FROM groups")}
        items = [card(r, groups, d) for r in rows]
        if style:
            items = [c for c in items if c["pixel"] == (style == "pixel")]
        hidden = 0
        if not copies:
            shown = {c["id"] for c in items}
            keep = [c for c in items if not any(o in shown for o in d["copy_of"].get(c["id"], []))]
            hidden = len(items) - len(keep)
            items = keep
        if sort == "random":
            random.Random(seed).shuffle(items)
        elif sort == "recent":
            items.sort(key=lambda c: -(c["newest"] or 0))
        elif sort == "size":
            items.sort(key=lambda c: -(c["total_size"] or 0))
        elif sort == "assets":
            items.sort(key=lambda c: -(c["asset_count"] or 0))
        elif sort == "group":
            items.sort(key=lambda c: (c["group"].lower(), c["name"].lower()))
        elif q.strip() and sort == "relevance":
            order = {r["id"]: r["score"] for r in rows}
            items.sort(key=lambda c: order[c["id"]])
        else:
            items.sort(key=lambda c: c["name"].lower())
        return {"total": len(items), "hidden_copies": hidden, "items": items}

    @app.get("/api/pack")
    def pack(id: str):
        con = db()
        r = con.execute("SELECT * FROM packs WHERE id=?", (id,)).fetchone()
        if not r:
            raise HTTPException(404, f"no pack {id}")
        p = dict(r)
        out = pack_detail(con, cfg, p, full_docs=True)
        d = derived(con)
        groups = {g["id"]: dict(g) for g in con.execute("SELECT * FROM groups")}
        subs = [dict(x) for x in con.execute("SELECT p.*, (SELECT COUNT(*) FROM packs c WHERE c.parent_id=p.id) AS children FROM packs p WHERE parent_id=? ORDER BY name COLLATE NOCASE", (id,))]
        out["sub_packs"] = [card(s, groups, d) for s in subs]
        crumbs, pid = [], p["parent_id"]
        while pid:
            x = con.execute("SELECT id, name, parent_id FROM packs WHERE id=?", (pid,)).fetchone()
            crumbs.insert(0, {"id": x["id"], "name": x["name"]})
            pid = x["parent_id"]
        out["breadcrumbs"] = crumbs
        prevs = [dict(x) for x in con.execute(f"SELECT {ASSET_COLS} FROM assets a WHERE a.pack_id=? AND a.type='preview' AND a.preview_of IS NULL AND a.available=1 ORDER BY a.path", (id,))]
        per_folder: dict[str, int] = {}
        for x in prevs:
            per_folder[x["folder"]] = per_folder.get(x["folder"], 0) + 1
        out["previews"] = [x for x in prevs if per_folder[x["folder"]] <= 12][:16]
        out["cover_url"] = f"/cover?pack={_q(id)}&s=512" if _show_cover(p) else None
        out["sheet_url"] = f"/sheets/{Path(p['sheet']).name}" if p["sheet"] and Path(p["sheet"]).exists() else None
        out["also_in"] = d["also"].get(id, [])
        out["newest"] = d["newest"].get(id)
        gc = load_curation(cfg).group(p["group_id"])
        out["curation_values"] = {k: v for k, v in gc.packs.get(p["key"], {}).items() if k != "grid"}
        out["group_curation"] = {k: gc.data.get(k) for k in E.GROUP_FIELDS}
        out["license_info"] = license_info(cfg, p["license"])
        out["ai_tags"] = json.loads(p["ai_tags"]) if p["ai_tags"] else []
        for x in out["docs"]:
            x.pop("abs_path", None)
        return out

    @app.get("/api/folders")
    def folders(pack: str):
        con = db()
        p = con.execute("SELECT * FROM packs WHERE id=?", (pack,)).fetchone()
        if not p:
            raise HTTPException(404)
        ids = descendants(con, pack)
        counts: dict[str, int] = {}
        base = len(p["path"]) + 1
        for r in con.execute(f"SELECT path FROM assets WHERE pack_id IN ({','.join('?' * len(ids))})", ids):
            parts = r["path"][base:].split("/")[:-1]
            for i in range(1, len(parts) + 1):
                k = "/".join(parts[:i])
                counts[k] = counts.get(k, 0) + 1
        return [{"folder": k, "count": n} for k, n in sorted(counts.items(), key=lambda kv: kv[0].lower())]

    # --- assets ----------------------------------------------------------------------------

    @app.get("/api/assets")
    def assets(pack: str = "", folder: str = "", q: str = "", type: str = "", media: str = "", group: str = "",
               license: str = "", ai: str = "", grid: bool = False, animated: bool = False, rigged: bool = False,
               offset: int = 0, limit: int = Query(200, le=2000), meaning: bool = False, similar: int = 0):
        con = db()
        where, args = ["a.preview_of IS NULL"], []  # a model's bundled render shows as the model
        join = ""
        semantic = similar or (meaning and q.strip())
        if q.strip() and not semantic:
            join = " JOIN (SELECT rowid, bm25(assets_fts, 10, 4, 2, 1, 3, 3) AS score FROM assets_fts WHERE assets_fts MATCH ?) f ON f.rowid=a.id"
            args.append(fts_query([q]))
        if pack:
            p = con.execute("SELECT path FROM packs WHERE id=?", (pack,)).fetchone()
            if not p:
                raise HTTPException(404)
            ids = descendants(con, pack)
            where.append(f"a.pack_id IN ({','.join('?' * len(ids))})"); args += ids
            if folder:
                prefix = f"{p['path']}/{folder.strip('/')}/"
                where.append("substr(a.path, 1, ?) = ?"); args += [len(prefix), prefix]
        if type:
            ts = type.split(",")
            where.append(f"a.type IN ({','.join('?' * len(ts))})"); args += ts
        if media:
            where.append("a.media=?"); args.append(media)
        for col, val in (("group_id", group), ("license", license), ("ai_generated", ai)):
            if val:
                where.append(f"p.{col}=?"); args.append(val)
        if grid:
            where.append("a.grid_w IS NOT NULL")
        if animated:
            where.append("a.clips > 0")
        if rigged:
            where.append("a.rigged = 1")
        w = " WHERE " + " AND ".join(where)
        if semantic:
            return semantic_assets(con, q, similar, w, args, offset, limit)
        base = f" FROM assets a JOIN packs p ON p.id=a.pack_id{join}{w}"
        total = con.execute("SELECT COUNT(*)" + base, args).fetchone()[0]
        order = " ORDER BY a.available DESC, a.displayable DESC, f.score" if q.strip() else " ORDER BY a.path COLLATE NOCASE"
        rows = con.execute(f"SELECT {ASSET_COLS}, p.name AS pack_name, p.path AS pack_path" + base + order + " LIMIT ? OFFSET ?",
                           args + [limit, offset])
        return {"total": total, "offset": offset, "items": [_asset_item(dict(r)) for r in rows]}

    SEMANTIC_MAX = 1000  # results by meaning are a ranking of everything; show the best ones

    def semantic_assets(con, q: str, similar: int, w: str, args: list, offset: int, limit: int) -> dict:
        v = vectors(con)
        if v is None:
            raise HTTPException(400, "No image vectors yet: run `assetcat classify --kind embed`")
        # Bundled renders of models carry the vectors; rank them and answer with the model.
        render_of = {r[0]: r[1] for r in con.execute("SELECT id, preview_of FROM assets WHERE preview_of IS NOT NULL")}
        if similar:
            qv = v.vector(similar)
            if qv is None:
                r = con.execute("SELECT id FROM assets WHERE preview_of=?", (similar,)).fetchone()
                qv = v.vector(r[0]) if r else None
            if qv is None:
                raise HTTPException(404, "this image has no vector (not embedded yet)")
        else:
            qv = text_vector(q.strip())
        only = {r[0] for r in con.execute(f"SELECT a.id FROM assets a JOIN packs p ON p.id=a.pack_id{w}", args)}
        only |= {rid for rid, mid in render_of.items() if mid in only}
        ranked, seen = [], set()
        for i, sc in v.rank(qv, only, SEMANTIC_MAX):
            i = render_of.get(i, i)
            if i not in seen:
                seen.add(i)
                ranked.append((i, sc))
        page = ranked[offset:offset + limit]
        rows = {r["id"]: dict(r) for r in con.execute(
            f"SELECT {ASSET_COLS}, p.name AS pack_name, p.path AS pack_path FROM assets a JOIN packs p ON p.id=a.pack_id "
            f"WHERE a.id IN ({','.join('?' * len(page))})", [i for i, _ in page])} if page else {}
        return {"total": len(ranked), "offset": offset, "items": [{**_asset_item(rows[i]), "score": round(s, 4)} for i, s in page]}

    @app.get("/api/asset")
    def asset(id: int):
        con = db()
        r = con.execute(f"SELECT {ASSET_COLS}, a.grid_confidence, p.name AS pack_name, p.path AS pack_path, p.license, p.ai_generated, p.ai_reason, p.license_file FROM assets a JOIN packs p ON p.id=a.pack_id WHERE a.id=?", (id,)).fetchone()
        if not r:
            raise HTTPException(404)
        out = _asset_item(dict(r))
        out["files"] = [dict(f) for f in con.execute("""SELECT f.path, f.ext, f.size, f.width, f.height, f.channels, f.sample_rate, f.font_family
                                                         FROM files f JOIN asset_files af ON af.file_id=f.id WHERE af.asset_id=? ORDER BY f.path""", (id,))]
        out["abs_path"] = str(cfg.abs(r["path"]))
        out["license_info"] = license_info(cfg, r["license"])
        out["license"], out["ai_generated"], out["ai_reason"], out["license_file"] = r["license"], r["ai_generated"], r["ai_reason"], r["license_file"]
        out["subjects"] = [dict(x) for x in con.execute("SELECT subject, score FROM asset_subjects WHERE asset_id=? ORDER BY score DESC", (id,))]
        if r["media"] == "model":
            models = {}
            for f in con.execute("SELECT f.path, f.info FROM files f JOIN asset_files af ON af.file_id=f.id WHERE af.asset_id=?", (id,)):
                info = json.loads(f["info"]) if f["info"] else {}
                refs = info.get("refs", [])
                have = {x[0] for x in con.execute(f"SELECT path FROM files WHERE path IN ({','.join('?' * len(refs))})", refs)} if refs else set()
                info["missing"] = [x for x in refs if x not in have]
                models[f["path"]] = info
            out["model"] = models
            # FBX files often name textures by the artist's own paths: offer the pack's textures instead
            if any(f["ext"] == "fbx" for f in out["files"]):
                out["textures"] = [x[0] for x in con.execute(
                    "SELECT path FROM files WHERE pack_id=? AND category='image' AND size > 0 ORDER BY path", (r["pack_id"],))
                    if TEXTURE_DIR.search(x[0])][:100]
        return out

    def _asset_item(r: dict) -> dict:
        r["ext"] = r["path"].rsplit(".", 1)[-1].lower()
        r["inner"] = r["path"][len(r["pack_path"]) + 1:] if r.get("pack_path") else r["path"]
        if r.get("font_family") is None and r["media"] == "font":
            fam = db().execute("SELECT font_family FROM files WHERE path=?", (r["path"],)).fetchone()
            r["font_family"] = fam[0] if fam else None
        return {k: v for k, v in r.items() if v is not None}

    # --- files, thumbnails, covers, sheets -------------------------------------------------

    @app.get("/lib/{rel:path}")
    def lib(rel: str):
        r = file_row(db(), rel)
        return FileResponse(cfg.fs(r["path"]), headers={"Cache-Control": "max-age=3600"})

    def thumb_response(src_fs: str, key: str, s: int, trim: bool = False):
        if trim:
            key += "|trim"
        out = thumbs_dir / str(s) / (hashlib.blake2b(key.encode(), digest_size=10).hexdigest() + ".webp")
        headers = {"Cache-Control": "max-age=604800"}
        cached = read_cached(out)
        if cached is not None:
            return Response(cached, media_type="image/webp", headers=headers)
        try:
            data = make_thumb(src_fs, s, trim)
        except Exception as e:  # noqa: BLE001
            eprint(f"thumbnail failed: {src_fs}: {type(e).__name__}: {e}")
            raise HTTPException(415, f"can't make a thumbnail: {type(e).__name__}") from e
        if not cache_file(out, data):
            eprint(f"thumbnail not cached (served anyway): {out}")
        return Response(data, media_type="image/webp", headers=headers)

    @app.get("/wave")
    def wave(path: str, n: int = Query(96, ge=8, le=400)):
        """Peaks of an audio file (n values, 0-1, loudest = 1) for waveform tiles; cached on disk."""
        con = db()
        r = file_row(con, path)
        key = hashlib.blake2b(f"{path}|{r['size']}|{r['mtime']}|{n}".encode(), digest_size=10).hexdigest()
        out = cfg.cache / "waves" / f"{key}.json"
        headers = {"cache-control": "max-age=86400"}
        cached = read_cached(out)
        if cached is not None:
            return Response(cached, media_type="application/json", headers=headers)
        peaks = audio_peaks(cfg.fs(path), n)
        if peaks is None:
            raise HTTPException(415, "can't decode this audio file")
        data = json.dumps(peaks).encode()
        cache_file(out, data)
        return Response(data, media_type="application/json", headers=headers)

    def render_path(r) -> Path:
        key = hashlib.blake2b(f"{r['path']}|{r['mtime']}|{r['size']}".encode(), digest_size=10).hexdigest()
        return thumbs_dir / "model" / f"{key}.png"

    @app.get("/thumb")
    def thumb(path: str, s: int = 192):
        s = min(THUMB_SIZES, key=lambda x: abs(x - s))
        con = db()
        r = file_row(con, path)
        if C.category(r["ext"]) == "model":
            out = render_path(r)  # rendered by the browser (POST /api/render), kept in the cache
            if out.exists():
                return thumb_response(str(out), f"{out.name}|{out.stat().st_mtime}", s, trim=True)
            a = con.execute("SELECT preview FROM assets WHERE path=? AND preview IS NOT NULL", (path,)).fetchone()
            pr = file_row(con, a["preview"]) if a else None
            if pr and max(pr["width"] or 0, pr["height"] or 0) >= MIN_PREVIEW:  # Kenney's are 64 px: too small
                return thumb_response(cfg.fs(pr["path"]), f"{pr['path']}|{pr['mtime']}|{pr['size']}", s, trim=True)
            raise HTTPException(404, "no render yet")
        if r["ext"] == "svg" or (r["ext"] in INLINE_IMAGE and r["width"] and max(r["width"], r["height"]) <= s):
            return RedirectResponse(f"/lib/{_q(r['path'], safe='/')}")
        return thumb_response(cfg.fs(r["path"]), f"{r['path']}|{r['mtime']}|{r['size']}", s)

    @app.post("/api/render")
    async def save_render(request: Request, path: str):
        """A model's thumbnail, rendered in the browser; stored in the cache, never the library."""
        r = file_row(db(), path)
        if C.category(r["ext"]) != "model":
            raise HTTPException(400, "not a 3D model")
        data = await request.body()
        if len(data) > MAX_RENDER_BYTES or not data.startswith(b"\x89PNG"):
            raise HTTPException(400, "expected a PNG up to 4 MB")
        from PIL import Image

        try:
            with Image.open(io.BytesIO(data)) as im:
                if max(im.size) > 1024:
                    raise HTTPException(400, "render too large")
                im.verify()
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"not a PNG: {type(e).__name__}") from e
        cache_file(render_path(r), data)
        return {"ok": True}

    @app.get("/cover")
    def cover(pack: str, s: int = 256):
        s = min(THUMB_SIZES, key=lambda x: abs(x - s))
        con = db()
        p = con.execute("SELECT cover, cover_kind FROM packs WHERE id=?", (pack,)).fetchone()
        if not p or not p["cover"]:
            raise HTTPException(404)
        if p["cover_kind"] == "sheet":
            if not os.path.exists(p["cover"]):
                raise HTTPException(404)
            return thumb_response(p["cover"], f"{p['cover']}|{os.path.getmtime(p['cover'])}", s)
        r = file_row(con, p["cover"])
        if r["ext"] == "gif" and r["width"] and max(r["width"], r["height"]) <= s * 2:
            return RedirectResponse(f"/lib/{_q(r['path'], safe='/')}")  # keep it animated
        return thumb_response(cfg.fs(r["path"]), f"{r['path']}|{r['mtime']}|{r['size']}", s)

    @app.get("/sheets/{name}")
    def sheet_file(name: str):
        f = cfg.sheets_dir / Path(name).name
        if not f.exists():
            raise HTTPException(404)
        return FileResponse(f, headers={"Cache-Control": "max-age=86400"})


    @app.post("/api/sheet")
    def make_sheet(req: SheetReq):
        con = db()
        r = con.execute("SELECT * FROM packs WHERE id=?", (req.pack,)).fetchone()
        if not r:
            raise HTTPException(404)
        p = dict(r)
        folder = req.folder.strip("/")
        base = f"{p['path']}/{folder}" if folder else p["path"]
        items = sheet_items(con, cfg, descendants(con, p["id"]), base if folder else None)
        if not items:
            raise HTTPException(404, "no viewable images here")
        for x in items:
            x["folder"] = posixpath.dirname(x["path"][len(base) + 1:])
        title = f"{p['name']} / {folder}" if folder else p["name"]
        out, job = sheet_job(cfg, f"{title}  ·  {p['id']}", items, base, C.slugify(f"{p['id']}-{folder}"), min(req.limit, 400), 10)
        if not os.path.exists(out):
            _, err = sheets.render(job)
            if err:
                raise HTTPException(500, err)
        return {"url": f"/sheets/{Path(out).name}", "path": out, "images": len(items)}

    # --- editing ---------------------------------------------------------------------------




    def guarded(fn, *a):
        with _edit_lock:
            _cache["stamp"] = None  # tags feed the pixel-art facet
            try:
                return fn(cfg, db(), *a)
            except E.EditError as e:
                raise HTTPException(400, str(e)) from e

    @app.post("/api/edit/pack")
    def edit_pack(req: PackEdit):
        guarded(E.edit_pack, req.id, req.fields)
        return pack(req.id)

    @app.post("/api/edit/tags")
    def edit_tags(req: TagsReq):
        guarded(E.edit_tags, req.id, req.add, req.remove)
        return pack(req.id)

    @app.post("/api/edit/group")
    def edit_group(req: GroupEdit):
        return guarded(E.edit_group, req.id, req.fields)

    @app.post("/api/edit/grid")
    def edit_grid(req: GridEdit):
        guarded(E.edit_grid, req.asset_id, req.grid)
        return asset(req.asset_id)

    # --- layout: pack roles ----------------------------------------------------------------

    def group_scan(con, gid: str):
        stamp = indexed_at(con)
        hit = _scans.get(gid)
        if not hit or hit[0] != stamp:
            found = S.find_group(cfg, gid)
            if not found:
                raise HTTPException(404, f"no group {gid}")
            _scans[gid] = hit = (stamp, *found)
        return hit[1], hit[2]

    def with_roles(packs: dict, roles: dict) -> dict:
        out = {k: dict(v) for k, v in packs.items()}
        for k, r in roles.items():
            t = out.setdefault(k, {})
            if r is None:
                t.pop("role", None)
            else:
                t["role"] = r
        return out

    def layout_view(gid: str, roles: dict) -> dict:
        """The group's folder tree with `roles` (pending, not saved) applied over the curation,
        and how it compares with what's saved and what's indexed."""
        con = db()
        g = con.execute("SELECT * FROM groups WHERE id=?", (gid,)).fetchone()
        if not g:
            raise HTTPException(404, f"no group {gid}")
        sec, gdir = group_scan(con, gid)
        packs = load_curation(cfg).group(gid).packs
        saved = S.group_tree(cfg, sec, gdir, gid, packs)
        view = S.group_tree(cfg, sec, gdir, gid, with_roles(packs, roles)) if roles else saved
        indexed = {r["id"]: r["role"] for r in con.execute("SELECT id, role FROM packs WHERE group_id=?", (gid,))}

        def count(ps: dict) -> dict:
            return {"packs": len(ps), "collections": sum(1 for r in ps.values() if r == "collection")}

        return {"group": {"id": gid, "name": g["name"], "section": g["section"], "file": g["curation_file"]},
                "tree": view["tree"], "summary": count(view["packs"]), "indexed": count(indexed),
                "stale": saved["packs"] != indexed}

    @app.post("/api/layout")
    def layout(req: RolesReq):
        return layout_view(req.group, req.roles)

    @app.post("/api/edit/roles")
    def edit_roles(req: RolesReq):
        """Save roles. Keys must be folders that can take a role once all of `roles` apply."""
        sec, gdir = group_scan(db(), req.group)
        packs = with_roles(load_curation(cfg).group(req.group).packs, req.roles)
        known: set[str] = set()

        def walk(nodes):
            for n in nodes:
                if n["role"] != "content":
                    known.add(n["key"])
                walk(n["kids"])

        walk(S.group_tree(cfg, sec, gdir, req.group, packs)["tree"])
        with _edit_lock:
            try:
                E.edit_roles(cfg, req.group, req.roles, known)
            except E.EditError as e:
                raise HTTPException(400, str(e)) from e
        return layout_view(req.group, {})

    @app.post("/api/reindex")
    def reindex():
        with _edit_lock:
            s = run_index(cfg)
            _cache["stamp"] = None
            _scans.clear()
        return {k: s[k] for k in ("groups", "packs", "assets", "seconds", "new", "changed", "removed")}

    # --- shortlists ------------------------------------------------------------------------


    def sl(fn, *a):
        with _edit_lock:
            try:
                return fn(cfg, *a)
            except E.EditError as e:
                raise HTTPException(400, str(e)) from e

    @app.get("/api/shortlists")
    def shortlists():
        return E.list_shortlists(cfg)

    @app.get("/api/shortlists/marks")
    def shortlist_marks():
        """Which shortlists each starred item is in: {"kind:ref": [names]}."""
        out: dict[str, list[str]] = {}
        for sl_ in E.list_shortlists(cfg):
            for i in E.read_shortlist(cfg, sl_["name"])["items"]:
                out.setdefault(f"{i['kind']}:{i['ref']}", []).append(sl_["name"])
        return out

    @app.get("/api/shortlist")
    def shortlist(name: str):
        data = sl(E.read_shortlist, name)
        con = db()
        d = derived(con)
        groups = {g["id"]: dict(g) for g in con.execute("SELECT * FROM groups")}
        for it in data["items"]:
            if it["kind"] == "pack":
                r = con.execute("SELECT p.*, 0 AS children FROM packs p WHERE id=?", (it["ref"],)).fetchone()
                it["pack"] = card(dict(r), groups, d) if r else None
            elif it["kind"] == "asset":
                r = con.execute(f"SELECT {ASSET_COLS}, p.name AS pack_name, p.path AS pack_path FROM assets a JOIN packs p ON p.id=a.pack_id WHERE a.path=?", (it["ref"],)).fetchone()
                it["asset"] = _asset_item(dict(r)) if r else None
            else:
                r = con.execute("SELECT id, name, path FROM packs WHERE ? LIKE path || '/%' OR path=? ORDER BY length(path) DESC LIMIT 1", (it["ref"], it["ref"])).fetchone()
                it["pack"] = {"id": r["id"], "name": r["name"], "folder": it["ref"][len(r["path"]) + 1:]} if r else None
        return data

    @app.post("/api/shortlist")
    def shortlist_create(req: ShortlistItem):
        return sl(E.create_shortlist, req.name, req.note)

    @app.post("/api/shortlist/add")
    def shortlist_add(req: ShortlistItem):
        return sl(E.shortlist_add, req.name, req.kind, req.ref, req.note)

    @app.post("/api/shortlist/remove")
    def shortlist_remove(req: ShortlistItem):
        return sl(E.shortlist_remove, req.name, req.kind, req.ref)

    # --- misc ------------------------------------------------------------------------------


    @app.post("/api/reveal")
    def reveal(req: Reveal):
        con = db()
        known = con.execute("SELECT 1 FROM files WHERE path=? UNION SELECT 1 FROM packs WHERE path=?", (req.path, req.path)).fetchone()
        if not known:
            raise HTTPException(404)
        target = str(cfg.abs(req.path))
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", target])
        elif os.name == "nt":
            subprocess.Popen(["explorer", "/select,", target])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(target)])
        return {"ok": True}

    @app.exception_handler(sqlite3.OperationalError)
    def db_error(_, exc):
        return JSONResponse({"detail": f"database busy or missing ({exc}); is `assetcat index` running?"}, status_code=503)

    @app.get("/")
    def index_html():
        return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")
    return app


def _show_cover(p) -> bool:
    """Generated contact sheets make poor covers for audio packs (often one 'thank you' image)."""
    return bool(p["cover"]) and not (p["cover_kind"] == "sheet" and p["kind"] in ("music", "sfx", "audio", "fonts"))


def _q(s: str, safe: str = "") -> str:
    from urllib.parse import quote

    return quote(s, safe=safe)


@contextlib.asynccontextmanager
async def _lifespan(app):
    if sys.platform == "win32":
        asyncio.get_running_loop().set_exception_handler(_ignore_connection_resets)
    yield


def _ignore_connection_resets(loop, context):
    """Windows' proactor event loop reports a browser dropping a connection as an unhandled
    ConnectionResetError (with a traceback) after the connection was already handled. Noise."""
    if isinstance(context.get("exception"), ConnectionResetError):
        return
    loop.default_exception_handler(context)


def make_thumb(src_fs: str, s: int, trim: bool = False) -> bytes:
    """WebP bytes of an image scaled to fit s×s (never scaled up). `trim` crops away transparent
    margins first (renders of 3D models often leave the model small in the middle)."""
    import io

    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with Image.open(src_fs) as im:
            im.seek(0)
            im.draft("RGBA", (s, s))
            im = im.convert("RGBA")
            box = im.getchannel("A").getbbox() if trim else None
            if box:
                pad = max(box[2] - box[0], box[3] - box[1]) // 16
                im = im.crop((max(0, box[0] - pad), max(0, box[1] - pad), min(im.width, box[2] + pad), min(im.height, box[3] + pad)))
            if max(im.size) > s:
                im.thumbnail((s, s), Image.BOX)
            buf = io.BytesIO()
            im.save(buf, "WEBP", quality=88, method=4)
            return buf.getvalue()


def audio_peaks(path: str, n: int) -> list[float] | None:
    """The loudest sample in each of n slices, scaled so the loudest slice is 1."""
    import numpy as np
    import soundfile as sf

    try:
        with sf.SoundFile(path) as f:
            frames = f.frames
            if frames <= 0:
                return None
            step = max(1, -(-frames // n))
            peaks = [float(np.abs(b).max()) if len(b) else 0.0 for b in f.blocks(blocksize=step, always_2d=True)]
    except Exception:
        return None
    top = max(peaks, default=0) or 1.0
    return [round(p / top, 3) for p in peaks[:n]]


def read_cached(f: Path, tries: int = 4) -> bytes | None:
    """A cache file's bytes, or None if it isn't there. On Windows, opening a file while another
    request renames a fresh copy over it fails for a moment (PermissionError): retry, then give
    up and let the caller make it again."""
    import time

    for n in range(tries):
        try:
            return f.read_bytes()
        except FileNotFoundError:
            return None
        except PermissionError:
            time.sleep(0.02 * (n + 1))
    return None


def cache_file(out: Path, data: bytes, tries: int = 5) -> bool:
    """Write a cache file atomically. Each writer uses its own temp name, so concurrent requests
    for the same thumbnail don't collide (Windows refuses a second writer on one file). The rename
    is retried because Windows briefly locks fresh files (virus scanners, a reader of the old
    file). False if it couldn't be cached; the caller still has the bytes."""
    import time

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f"{out.stem}.{os.getpid()}-{threading.get_ident()}.tmp")
    try:
        tmp.write_bytes(data)
        for n in range(tries):
            try:
                os.replace(tmp, out)
                return True
            except PermissionError:
                if out.exists():  # another request cached it meanwhile
                    break
                time.sleep(0.05 * (n + 1))
    except OSError:
        pass
    try:
        tmp.unlink(missing_ok=True)
    except OSError:
        pass
    return out.exists()


def serve(cfg: Config, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = False, verbose: bool = False):
    import uvicorn

    if not cfg.db_path.exists():
        sys.exit("assetcat: no index yet; run `assetcat index` first")
    app = create_app(cfg)
    url = f"http://{host}:{port}/"
    print(f"assetcat browser at {url}  (Ctrl+C to stop)")
    if open_browser:
        import webbrowser

        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    # Graceful shutdown waits for open browser connections; don't let one keep Ctrl+C hanging.
    uvicorn.run(app, host=host, port=port, log_level="info" if verbose else "warning", timeout_graceful_shutdown=3)
    # The server has stopped and every request finished or timed out. A worker thread still stuck
    # (say, on a cloud file that isn't downloaded) would keep Python alive, so exit regardless.
    exit_timer = threading.Timer(2.0, lambda: os._exit(0))
    exit_timer.daemon = True
    exit_timer.start()
