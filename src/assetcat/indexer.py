"""`assetcat index`: scan, extract metadata (incremental), hash size collisions, rebuild the
derived tables, render pack contact sheets."""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

from . import ai as A
from . import classify as C
from . import db as DB
from . import grids, sheets
from .config import Config, eprint
from .curation import Curation, ensure_stub, load_curation
from .extract import META_MIN, META_MIN_BY_CATEGORY, META_VERSION, MODEL_EXTS, extract
from .scan import Layout, Pack, build_layout

EXTRACT_CATS = {"image", "audio", "font", "doc", "data", "model"}
DATA_EXTS = {"tsx", "tmx", "xml", "json"}
HASH_CATS = ("image", "vector", "source", "audio", "model", "font")
META_COLS = ("width", "height", "frames", "palette", "alpha", "duration", "channels", "sample_rate", "font_family", "text", "info", "error")
SHEET_LIMIT = 64
SHEET_COLS = 10


def run_index(cfg: Config, *, extract_meta=True, hash_dupes=True, make_sheets=True, workers=None) -> dict:
    t0 = time.time()
    cur = load_curation(cfg)
    lay = build_layout(cfg, cur)
    created = [str(p) for g in lay.groups.values() if (p := ensure_stub(cfg, g.id, g.name))]
    if created:
        cur = load_curation(cfg)
        lay = build_layout(cfg, cur)
    eprint(f"scan: {len(lay.groups)} groups, {len(lay.packs)} packs ({time.time() - t0:.1f}s)")

    con = DB.connect(cfg.db_path)
    changed = _sync_files(con, lay)
    eprint(f"files: {changed['total']} total, {changed['new']} new, {changed['changed']} changed, {changed['removed']} removed")
    if extract_meta:
        _extract(con, cfg, workers)
    if hash_dupes:
        _hash(con, cfg)
    stats = _rebuild(con, cfg, cur, lay)
    if make_sheets:
        _sheets(con, cfg, workers)
    con.execute("INSERT OR REPLACE INTO meta VALUES ('indexed_at', ?)", (time.strftime("%Y-%m-%d %H:%M:%S"),))
    con.execute("INSERT OR REPLACE INTO meta VALUES ('derived', ?)", (str(DB.DERIVED_VERSION),))
    con.commit()
    con.close()
    stats.update(changed, created=created, seconds=round(time.time() - t0, 1))
    return stats


# --- files -----------------------------------------------------------------------------------

def _owners(lay: Layout):
    for g in lay.groups.values():
        for f in g.loose:
            yield f, g.id, None
    for p in lay.packs.values():
        for f in p.files:
            yield f, p.group_id, p.id


def _sync_files(con, lay: Layout) -> dict:
    existing = {r[0]: (r[1], r[2]) for r in con.execute("SELECT path, size, mtime FROM files")}
    seen: set[str] = set()
    ins, upd_meta, upd_owner = [], [], []
    for f, gid, pid in _owners(lay):
        seen.add(f.rel)
        old = existing.get(f.rel)
        if old is None:
            ext = C.ext_of(f.name)
            ins.append((f.rel, ext, C.category(ext), f.size, f.mtime, gid, pid))
        elif old[0] != f.size or abs(old[1] - f.mtime) > 1e-3:
            upd_meta.append((f.size, f.mtime, gid, pid, f.rel))
        else:
            upd_owner.append((gid, pid, f.rel))
    removed = [p for p in existing if p not in seen]
    con.executemany("INSERT INTO files (path, ext, category, size, mtime, group_id, pack_id) VALUES (?,?,?,?,?,?,?)", ins)
    nulls = ", ".join(f"{c}=NULL" for c in META_COLS)
    con.executemany(f"UPDATE files SET size=?, mtime=?, group_id=?, pack_id=?, meta_version=0, hash=NULL, {nulls} WHERE path=?", upd_meta)
    con.executemany("UPDATE files SET group_id=?, pack_id=? WHERE path=?", upd_owner)
    con.executemany("DELETE FROM files WHERE path=?", [(p,) for p in removed])
    con.commit()
    return {"total": len(seen), "new": len(ins), "changed": len(upd_meta), "removed": len(removed)}


def _wants_extract(cat: str, ext: str) -> bool:
    if cat == "data":
        return ext in DATA_EXTS
    if cat == "source":
        return ext == "psd"
    if cat == "model":
        return ext in MODEL_EXTS
    return cat in EXTRACT_CATS


def _extract(con, cfg: Config, workers):
    stale = " OR ".join(["meta_version < ?"] + ["(category = ? AND meta_version < ?)"] * len(META_MIN_BY_CATEGORY))
    args = [META_MIN] + [x for kv in META_MIN_BY_CATEGORY.items() for x in kv]
    rows = con.execute(f"SELECT path, ext, category, size FROM files WHERE ({stale} OR error IS NOT NULL) AND size > 0", args).fetchall()
    skip = [(META_VERSION, r["path"]) for r in rows if not _wants_extract(r["category"], r["ext"])]
    con.executemany("UPDATE files SET meta_version=? WHERE path=?", skip)
    con.commit()
    jobs = [(r["path"], cfg.fs(r["path"]), r["ext"], r["category"]) for r in rows if _wants_extract(r["category"], r["ext"])]
    if not jobs:
        return
    eprint(f"extract: {len(jobs)} files")
    t0 = time.time()
    done = 0
    cols = ", ".join(f"{c}=?" for c in META_COLS)
    batch = []
    with ProcessPoolExecutor(max_workers=workers or os.cpu_count()) as ex:
        for rel, out in ex.map(extract, jobs, chunksize=64):
            batch.append(tuple(out.get(c) for c in META_COLS) + (META_VERSION, rel))
            done += 1
            if len(batch) >= 2000:
                con.executemany(f"UPDATE files SET {cols}, meta_version=? WHERE path=?", batch)
                con.commit()
                batch.clear()
                rate = done / max(0.1, time.time() - t0)
                eprint(f"  {done}/{len(jobs)}  ({rate:.0f}/s, ~{(len(jobs) - done) / rate:.0f}s left)")
    con.executemany(f"UPDATE files SET {cols}, meta_version=? WHERE path=?", batch)
    con.commit()
    eprint(f"extract: done in {time.time() - t0:.1f}s")


def _hash_file(job):
    fid, path = job
    h = hashlib.blake2b(digest_size=16)
    try:
        with open(path, "rb") as f:
            while chunk := f.read(1 << 20):
                h.update(chunk)
        return fid, h.hexdigest()
    except OSError:
        return fid, None


def _hash(con, cfg: Config):
    q = ",".join("?" * len(HASH_CATS))
    rows = con.execute(
        f"""SELECT id, path FROM files WHERE hash IS NULL AND size >= 64 AND category IN ({q})
            AND size IN (SELECT size FROM files WHERE size >= 64 AND category IN ({q})
                         GROUP BY size HAVING COUNT(*) > 1)""",
        HASH_CATS + HASH_CATS,
    ).fetchall()
    if not rows:
        return
    eprint(f"hash: {len(rows)} files with size collisions")
    with ThreadPoolExecutor(max_workers=8) as ex:
        res = list(ex.map(_hash_file, [(r["id"], cfg.fs(r["path"])) for r in rows]))
    con.executemany("UPDATE files SET hash=? WHERE id=?", [(h, fid) for fid, h in res if h])
    con.commit()


# --- derived tables --------------------------------------------------------------------------

DISPLAYABLE = {
    "visual": C.BROWSER_IMAGE,
    "audio": {"ogg", "wav", "mp3", "flac", "m4a"},
    "model": {"glb", "gltf"},
    "font": {"ttf", "otf", "woff", "woff2"},
}


def _rebuild(con, cfg: Config, cur: Curation, lay: Layout) -> dict:
    t0 = time.time()
    con.executescript(DB.DERIVED)
    rows = {r["path"]: dict(r) for r in con.execute(
        "SELECT id, path, ext, category, size, width, height, frames, palette, duration, hash, text, info, error, pack_id, group_id FROM files")}

    # groups
    for g in lay.groups.values():
        gc = cur.group(g.id)
        con.execute("INSERT INTO groups VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
            g.id, g.section, g.rel, g.name, gc.get("store"), gc.get("purchased"), gc.get("license", "unknown"),
            gc.get("ai_generated", "unknown"), gc.get("ai_reason"), gc.get("notes"),
            gc.path.relative_to(cfg.profile).as_posix() if gc.path.exists() else None))

    # docs (license/readme near the pack root, or at group level)
    doc_rows = []
    for g in lay.groups.values():
        for f in g.loose:
            r = rows.get(f.rel)
            if r and r["category"] == "doc" and r["text"] and (role := C.doc_role(f.name)):
                doc_rows.append((r["id"], f.rel, role, None, g.id, r["text"]))
    pack_docs: dict[str, list[dict]] = defaultdict(list)
    for p in lay.packs.values():
        for f in p.files:
            r = rows.get(f.rel)
            if not r or r["category"] != "doc" or not r["text"]:
                continue
            depth = f.rel[len(p.rel) + 1:].count("/")
            role = C.doc_role(f.name)
            if role and depth <= 2:
                doc_rows.append((r["id"], f.rel, role, p.id, p.group_id, r["text"]))
                pack_docs[p.id].append({"path": f.rel, "role": role, "depth": depth, "text": r["text"]})
    con.executemany("INSERT INTO docs VALUES (?,?,?,?,?,?)", doc_rows)
    group_docs = defaultdict(list)
    for d in doc_rows:
        if d[3] is None:
            group_docs[d[4]].append({"path": d[1], "role": d[2]})

    # assets + grids, per pack
    all_assets: list[tuple] = []
    asset_files: list[tuple] = []
    pack_assets: dict[str, list[dict]] = {}
    next_id = 1
    for p in lay.packs.values():
        gc = cur.group(p.group_id)
        pc = gc.packs.get(p.key, {})
        assets, fmap = _assets_for(p, rows)
        pfiles = [rows[f.rel] for f in p.files if f.rel in rows]
        grids.assign_grids(assets, pfiles, fmap, pc.get("grid", {}), p.rel)
        for a in assets:
            a["id"] = next_id
            next_id += 1
        _model_previews(assets)
        for a in assets:
            if a["media"] == "visual" and a.get("grid_cols") and a["type"] in ("sprite", "character"):
                a["type"] = "sheet"
            all_assets.append((
                a["id"], p.id, a["name"], a["folder"], a["path"], a["media"], a["type"], a["formats"], a["variants"],
                a["available"], a["displayable"], a.get("width"), a.get("height"), a.get("frames"), a.get("palette"),
                a.get("duration"), a.get("grid_w"), a.get("grid_h"), a.get("grid_spacing"), a.get("grid_margin"),
                a.get("grid_cols"), a.get("grid_rows"), a.get("grid_source"), a.get("grid_confidence"), a.get("atlas"),
                a.get("preview"), a.get("preview_of"), a.get("tris"), a.get("clips"), a.get("rigged"), a.get("missing")))
            asset_files.extend((a["id"], fid) for fid in a["file_ids"])
        pack_assets[p.id] = assets
    con.executemany(f"INSERT INTO assets VALUES ({','.join('?' * 31)})", all_assets)
    con.executemany("INSERT INTO asset_files VALUES (?,?)", asset_files)
    subjects = _subjects(con, cfg)
    for assets in pack_assets.values():  # what a model's bundled render shows, the model shows
        for a in assets:
            if a.get("preview_of") and a["id"] in subjects:
                subjects[a["preview_of"]] = subjects.pop(a["id"])
    con.executemany("INSERT INTO asset_subjects VALUES (?,?,?)", [(aid, sub, sc) for aid, hits in subjects.items() for sub, sc in hits])

    # packs
    resolved: dict[str, dict] = {}
    order = sorted(lay.packs.values(), key=lambda p: p.depth)
    for p in order:
        resolved[p.id] = _pack_row(p, lay, cur, rows, pack_assets[p.id], pack_docs, group_docs, resolved)
        resolved[p.id]["_contains"] = Counter(sub for a in pack_assets[p.id] for sub, _ in subjects.get(a["id"], []))
    # roll sub-pack totals up into collections (deepest first)
    for p in sorted(lay.packs.values(), key=lambda p: -p.depth):
        if p.parent_id:
            a, b = resolved[p.parent_id], resolved[p.id]
            for k in ("file_count", "total_size", "pending", "asset_count"):
                a[k] += b[k]
            for t, n in b["_types"].items():
                a["_types"][t] += n
            for m, n in b["_media"].items():
                a["_media"][m] += n
            a["_contains"].update(b["_contains"])
            a["_engines"] |= b["_engines"]
    for p in lay.packs.values():
        r = resolved[p.id]
        r["kind"], r["media"] = _kind(r["_media"], r["_types"], r["_engines"])
        r["engines"] = ",".join(sorted(r["_engines"] - {"_archive"})) or None
        r["type_counts"] = json.dumps(dict(r["_types"].most_common()))
        top = [(k, n) for k, n in r["_contains"].most_common(24) if n >= 2]
        r["contains"] = json.dumps(dict(top)) if top else None
    _ai_tags(cfg, resolved)
    cols = ["id", "key", "group_id", "section", "path", "name", "role", "parent_id", "depth", "kind", "media", "engines",
            "file_count", "total_size", "pending", "asset_count", "type_counts", "license", "license_from", "license_file",
            "ai_generated", "ai_reason", "cover", "cover_kind", "sheet", "tags", "notes", "description", "description_source",
            "ai_tags", "ai_model", "ai_at", "ai_stale", "ai_description", "contains"]
    con.executemany(f"INSERT INTO packs ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                    [tuple(r.get(c) for c in cols) for r in resolved.values()])

    _dupes(con, rows)
    _fts(con, lay, pack_assets, pack_docs, resolved, cur, subjects)
    con.commit()
    eprint(f"rebuild: {len(all_assets)} assets ({time.time() - t0:.1f}s)")
    return {"groups": len(lay.groups), "packs": len(lay.packs), "assets": len(all_assets),
            "unused_curation": lay.unused_curation, "missing_sections": lay.missing_sections,
            "key_collisions": lay.key_collisions, "curation_problems": cur.problems}


def _assets_for(p: Pack, rows: dict) -> tuple[list[dict], dict[str, int]]:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for f in p.files:
        r = rows.get(f.rel)
        if not r:
            continue
        media = C.MEDIA.get(r["category"])
        if not media:
            continue
        inner = f.rel[len(p.rel) + 1:]
        folder = posixpath.dirname(inner)
        stem = C.stem_of(posixpath.basename(inner))
        groups[(C.variant_folder(folder), stem.lower(), media)].append(r)
    assets, fmap = [], {}
    for (_, _, media), frs in groups.items():
        rank = C.PREF_RANK[media]
        frs.sort(key=lambda r: (r["size"] == 0, rank.get(r["ext"], 99), r["path"]))
        pr = frs[0]
        inner = pr["path"][len(p.rel) + 1:]
        folder = posixpath.dirname(inner)
        stem = C.stem_of(posixpath.basename(inner))
        if media == "visual":
            typ = C.visual_type(stem, folder, p.name)
        elif media == "audio":
            typ = C.audio_type(stem, folder, p.name, pr["duration"])
        else:
            typ = media
        a = {
            "name": stem, "folder": folder, "path": pr["path"], "media": media, "type": typ,
            "formats": ",".join(sorted({r["ext"] for r in frs}, key=lambda e: rank.get(e, 99))),
            "variants": len(frs), "available": int(pr["size"] > 0),
            "displayable": int(pr["ext"] in DISPLAYABLE[media]),
            "width": pr["width"], "height": pr["height"], "frames": pr["frames"], "palette": pr["palette"],
            "duration": pr["duration"], "file_ids": [r["id"] for r in frs],
        }
        if media == "model" and pr["info"]:
            info = json.loads(pr["info"])
            a["tris"] = info.get("tris")
            a["clips"] = len(info.get("clips", [])) or None
            a["rigged"] = 1 if info.get("skinned") else None
            a["missing"] = sum(1 for x in info.get("refs", []) if x not in rows) or None
        idx = len(assets)
        assets.append(a)
        for r in frs:
            fmap[r["path"]] = idx
    return assets, fmap


def _model_previews(assets: list[dict]):
    """Pair each model with a bundled render of the same name in the pack (Kenney's
    `Previews/x.png` for `Models/GLB format/x.glb`): the render becomes the model's thumbnail and
    leaves the asset lists (`preview_of`)."""
    renders: dict[str, dict] = {}
    for a in assets:
        if a["media"] == "visual" and a["type"] == "preview" and a["displayable"]:
            renders.setdefault(a["name"].lower(), a)
    if not renders:
        return
    for a in assets:
        if a["media"] == "model" and (r := renders.get(a["name"].lower())) and not r.get("preview_of"):
            a["preview"], r["preview_of"] = r["path"], a["id"]


def _cover(p: Pack, rows: dict, override: str | None) -> str | None:
    if override:
        rel = f"{p.rel}/{override}"
        return rel if rel in rows else None
    best, best_s = None, -1e9
    for f in p.files:
        r = rows.get(f.rel)
        if not r or r["category"] != "image" or r["size"] == 0 or r["ext"] not in ("png", "gif", "jpg", "jpeg", "webp"):
            continue
        inner = f.rel[len(p.rel) + 1:]
        parts = inner.split("/")
        depth = len(parts) - 1
        if depth > 2:
            continue
        stem = C.stem_of(parts[-1]).lower()
        in_prev_folder = any(C.PREVIEW_FOLDER.match(x) for x in parts[:-1])
        if not (C.is_preview_name(stem) or in_prev_folder):
            continue
        if stem.startswith("preview") or stem.endswith("preview"):
            s = 50
        elif stem.startswith(("sample", "cover", "showcase", "banner", "promo", "poster")):
            s = 45
        elif stem.startswith(("mockup", "mock-up", "screenshot", "screen")):
            s = 40
        elif C.is_preview_name(stem):
            s = 25
        else:
            s = 12
        s += 20 - 8 * depth
        if r["ext"] == "gif":
            s += 3
        w, h = r["width"] or 0, r["height"] or 0
        s += min(10, w * h / 150_000)
        if w and w < 64:
            s -= 30
        if s > best_s:
            best, best_s = f.rel, s
    return best


def _pack_row(p, lay, cur, rows, assets, pack_docs, group_docs, resolved) -> dict:
    gc = cur.group(p.group_id)
    pc = gc.packs.get(p.key, {})
    parent = resolved.get(p.parent_id) if p.parent_id else None

    def pick(field, default):
        if field in pc:
            return pc[field], "pack"
        if parent and parent.get(field) is not None and parent.get(f"_{field}_from") != "default":
            return parent[field], "parent"
        if field in gc.data:
            return gc.data[field], "group"
        return default, "default"

    lic, lic_from = pick("license", "unknown")
    ai, ai_from = pick("ai_generated", "unknown")
    ai_reason = pc.get("ai_reason") or (parent or {}).get("ai_reason") or gc.get("ai_reason")
    lic_file = None
    if pc.get("license_file"):
        lic_file = f"{p.rel}/{pc['license_file']}"
    else:
        lics = sorted((d for d in pack_docs.get(p.id, []) if d["role"] == "license"), key=lambda d: d["depth"])
        if lics:
            lic_file = lics[0]["path"]
        elif parent and parent.get("license_file"):
            lic_file = parent["license_file"]
        elif gc.get("license_file"):
            lic_file = f"{lay.groups[p.group_id].rel}/{gc.get('license_file')}"
        else:
            gl = [d for d in group_docs.get(p.group_id, []) if d["role"] == "license"]
            lic_file = gl[0]["path"] if gl else None

    file_rows = [rows[f.rel] for f in p.files if f.rel in rows]
    engines = {C.EXT_ENGINE[r["ext"]] for r in file_rows if r["ext"] in C.EXT_ENGINE}
    if any(r["category"] == "archive" for r in file_rows):
        engines.add("_archive")
    types = Counter(a["type"] for a in assets)
    media = Counter(a["media"] for a in assets if a["type"] != "preview")
    music = types.get("music", 0)
    sfx = types.get("sfx", 0) + types.get("voice", 0)
    media["_music"], media["_sfx"] = music, sfx

    cover = _cover(p, rows, pc.get("cover"))
    desc = pc.get("description")
    return {
        "id": p.id, "key": p.key, "group_id": p.group_id, "section": p.section, "path": p.rel, "name": p.name,
        "role": p.role, "parent_id": p.parent_id, "depth": p.depth,
        "file_count": len(file_rows), "total_size": sum(r["size"] for r in file_rows),
        "pending": sum(1 for r in file_rows if r["size"] == 0), "asset_count": len(assets),
        "license": lic, "license_from": lic_from, "_license_from": lic_from, "license_file": lic_file,
        "ai_generated": ai, "_ai_generated_from": ai_from, "ai_reason": ai_reason,
        "cover": cover, "cover_kind": "bundled" if cover else None, "sheet": None,
        "notes": pc.get("notes"), "description": desc, "description_source": "curation" if desc else None,
        "_tag_layers": ((parent or {}).get("_tag_layers") or [gc.data]) + [pc],
        "_types": types, "_media": media, "_engines": engines,
    }


def _subjects(con, cfg: Config) -> dict[int, list[tuple[str, float]]]:
    """Asset subjects scored from the stored image vectors (no model needed); {} without them."""
    from . import embed as M
    from .config import read_user_config

    model = M.model_name(read_user_config())
    texts, spec = M.load_subject_texts(cfg, model), M.subject_spec(A.load_vocab(cfg))
    if not texts or not spec:
        return {}
    t = time.time()
    out = M.assign_subjects(M.Vectors(cfg, con, model), texts, spec)
    eprint(f"subjects: {len(out)} images with a subject ({time.time() - t:.1f}s)")
    return out


def _ai_tags(cfg: Config, resolved: dict[str, dict]):
    """AI results from the profile, then the curation layers on top: effective `tags`."""
    vocab = A.load_vocab(cfg)
    _, _, version = A.build_prompt(vocab)
    results = A.load_results(cfg)
    for pid, r in resolved.items():
        rec = results.get(pid)
        ai = rec["tags"] if rec else []
        tags = A.apply_layers(ai, r["_tag_layers"], vocab)
        r["tags"] = json.dumps(tags) if tags else None
        if rec:
            r.update(ai_tags=json.dumps(ai), ai_model=rec.get("model"), ai_at=rec.get("at"),
                     ai_stale=0 if A.is_fresh(rec, r, version) else 1, ai_description=rec.get("description"))
            if not r["description"] and rec.get("description"):
                r["description"], r["description_source"] = rec["description"], "ai"


def _kind(media: Counter, types: Counter, engines: set) -> tuple[str, str]:
    vis, aud, mod, fon = media.get("visual", 0), media.get("audio", 0), media.get("model", 0), media.get("font", 0)
    total = vis + aud + mod + fon
    present = [m for m, n in (("graphics", vis), ("audio", aud), ("3d", mod), ("fonts", fon)) if total and n / total >= 0.05]
    if total == 0:
        return ("archived" if "_archive" in engines else "engine" if engines - {"_archive"} else "other"), ""
    if mod and mod >= 0.2 * total:
        kind = "3d"
    elif aud >= 0.5 * total:
        m, s = media.get("_music", 0), media.get("_sfx", 0)
        kind = "music" if m >= 0.6 * aud else "sfx" if s >= 0.6 * aud else "audio"
    elif fon >= 0.5 * total:
        kind = "fonts"
    elif aud >= 0.2 * total:
        kind = "mixed"
    else:
        kind = "graphics"
    return kind, ",".join(present)


def _dupes(con, rows: dict):
    by_hash: dict[str, set[str]] = defaultdict(set)
    media_count: Counter = Counter()
    for r in rows.values():
        if r["pack_id"] and r["category"] in HASH_CATS and r["size"] > 0:
            media_count[r["pack_id"]] += 1
            if r["hash"]:
                by_hash[r["hash"]].add(r["pack_id"])
    pairs: Counter = Counter()
    for packs in by_hash.values():
        if 2 <= len(packs) <= 20:
            ps = sorted(packs)
            for i in range(len(ps)):
                for j in range(i + 1, len(ps)):
                    pairs[(ps[i], ps[j])] += 1
    out = []
    for (a, b), n in pairs.items():
        if n >= 3:
            out.append((a, b, n, round(n / max(1, media_count[a]), 3), round(n / max(1, media_count[b]), 3)))
    con.executemany("INSERT INTO pack_dupes VALUES (?,?,?,?,?)", out)


def _w(s: str) -> str:
    ws = C.words(s)
    raw = "".join(ch for ch in s.lower() if ch.isalnum())
    return " ".join(ws + ([raw] if raw and raw not in ws else []))


def _fts(con, lay: Layout, pack_assets, pack_docs, resolved, cur: Curation, subjects: dict):
    arows = []
    prow = []
    rid = {r["id"]: r for r in con.execute("SELECT rid, id FROM packs")}
    for p in lay.packs.values():
        r = resolved[p.id]
        g = lay.groups[p.group_id]
        tags = " ".join(json.loads(r["tags"])) if r["tags"] else ""
        pw = _w(p.name)
        gw = _w(g.name)
        vocab: dict[str, None] = {}
        for a in pack_assets[p.id]:
            subj = " ".join(sub.replace("-", " ") for sub, _ in subjects.get(a["id"], []))
            arows.append((a["id"], _w(a["name"]), _w(a["folder"].replace("/", " ")), pw, gw, a["type"], f"{tags} {subj}".strip()))
            for w in C.words(a["name"]) + C.words(a["folder"]):
                if not w.isdigit():
                    vocab[w] = None
        docs = "\n".join(d["text"][:5000] for d in pack_docs.get(p.id, []))[:20000]
        contains = " ".join(k.replace("-", " ") for k in json.loads(r["contains"] or "{}"))  # what's in it: low weight
        prow.append((rid[p.id]["rid"], pw, gw, " ".join(list(vocab)[:4000]), f"{docs}\n{contains}".strip(), r["description"] or "", tags))
    con.executemany("INSERT INTO assets_fts (rowid, name, folder, pack, grp, type, tags) VALUES (?,?,?,?,?,?,?)", arows)
    con.executemany("INSERT INTO packs_fts (rowid, name, grp, words, docs, description, tags) VALUES (?,?,?,?,?,?,?)", prow)


# --- contact sheets --------------------------------------------------------------------------

def sheet_items(con, cfg: Config, pack_ids: list[str], folder_prefix: str | None = None) -> list[dict]:
    q = ",".join("?" * len(pack_ids))
    sql = f"""SELECT a.*, p.path AS pack_path FROM assets a JOIN packs p ON p.id = a.pack_id
              WHERE a.pack_id IN ({q}) AND a.media='visual' AND a.available=1 AND a.displayable=1
              AND (a.type != 'preview' OR (p.kind = '3d' AND a.folder LIKE '%review%'))
              AND a.path NOT LIKE '%.svg'"""
    args = list(pack_ids)
    if folder_prefix:
        sql += " AND a.path LIKE ? ESCAPE '\\'"
        args.append(folder_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%")
    return [dict(r) for r in con.execute(sql, args)]


def descendants(con, pack_id: str) -> list[str]:
    out, todo = [pack_id], [pack_id]
    while todo:
        kids = [r[0] for r in con.execute("SELECT id FROM packs WHERE parent_id=?", (todo.pop(),))]
        out += kids
        todo += kids
    return out


def sheet_job(cfg: Config, title: str, assets: list[dict], base_rel: str, slug: str, limit=SHEET_LIMIT, cols=SHEET_COLS):
    chosen = sheets.pick(assets, limit)
    sig = sheets.signature([(a["path"], a["width"], a["height"]) for a in chosen] + [title, limit, cols])
    out = cfg.sheets_dir / f"{slug[-60:]}-{sig}.png"
    items = [(cfg.fs(a["path"]), a["path"][len(base_rel) + 1:]) for a in chosen]
    return str(out), (str(out), title, items, len(assets), cols)


def _sheets(con, cfg: Config, workers):
    jobs, updates = [], []
    for p in con.execute("SELECT id, path, name, group_id FROM packs").fetchall():
        assets = sheet_items(con, cfg, descendants(con, p["id"]))
        if not assets:
            continue
        for a in assets:  # folder relative to this pack (sub-pack assets included)
            a["folder"] = posixpath.dirname(a["path"][len(p["path"]) + 1:])
        out, job = sheet_job(cfg, f"{p['name']}  ·  {p['id']}", assets, p["path"], C.slugify(p["id"]))
        updates.append((out, p["id"]))
        if not os.path.exists(out):
            jobs.append(job)
    if jobs:
        eprint(f"sheets: rendering {len(jobs)} pack contact sheets")
        t0 = time.time()
        with ProcessPoolExecutor(max_workers=workers or os.cpu_count()) as ex:
            for out, err in ex.map(sheets.render, jobs, chunksize=2):
                if err:
                    eprint(f"  sheet failed: {out}: {err}")
        eprint(f"sheets: done in {time.time() - t0:.1f}s")
    con.executemany("UPDATE packs SET sheet=? WHERE id=?", updates)
    con.execute("UPDATE packs SET cover=sheet, cover_kind='sheet' WHERE cover IS NULL AND sheet IS NOT NULL")
    con.commit()
