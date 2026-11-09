"""Edits from the browse UI: write curation files (tomlkit keeps comments and layout), then
apply the change to the index right away, without a full `assetcat index`.

Applied live: license, AI status, tags, notes, description, cover, grids, group defaults.
Applied by the next index only (the layout page can run one): roles (the pack tree) and the
search text of tags.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

import tomlkit

from . import ai as A
from . import grids
from .config import Config
from .curation import AI_VALUES, ROLES, group_file, load_curation, load_licenses

PACK_FIELDS = {"license", "ai_generated", "ai_reason", "tags", "notes", "description", "cover", "license_file"}
GROUP_FIELDS = {"store", "purchased", "license", "ai_generated", "ai_reason", "notes"}
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}$")


class EditError(ValueError):
    pass


# --- toml helpers ----------------------------------------------------------------------------

def _load(path: Path) -> tomlkit.TOMLDocument:
    return tomlkit.parse(path.read_text(encoding="utf-8")) if path.exists() else tomlkit.document()


def _save(cfg: Config, path: Path, doc: tomlkit.TOMLDocument):
    cfg.writable(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if "packs" in doc and len(doc["packs"]) == 0:
        del doc["packs"]
    text = tomlkit.dumps(doc).rstrip() + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")


def _check(cfg: Config, fields: dict, allowed: set[str]):
    bad = set(fields) - allowed
    if bad:
        raise EditError(f"not editable here: {', '.join(sorted(bad))}")
    licenses = load_licenses(cfg)
    if fields.get("license") not in (None, "") and fields["license"] not in licenses:
        raise EditError(f"unknown license '{fields['license']}' (add it to licenses.toml in the profile)")
    if fields.get("ai_generated") not in (None, "") and fields["ai_generated"] not in AI_VALUES:
        raise EditError("ai_generated must be yes, no or unknown")
    if "tags" in fields and fields["tags"] is not None:
        if not isinstance(fields["tags"], list):
            raise EditError("tags must be a list")
        fields["tags"] = sorted({str(t).strip().lower() for t in fields["tags"] if str(t).strip()})


def _apply(table, fields: dict):
    for k, v in fields.items():
        if v is None or v == "" or v == []:
            if k in table:
                del table[k]
        else:
            table[k] = v


def _pack_table(doc, key: str, create: bool):
    if "packs" not in doc:
        if not create:
            return None
        doc["packs"] = tomlkit.table(is_super_table=True)
    packs = doc["packs"]
    if key not in packs:
        if not create:
            return None
        packs[key] = tomlkit.table()
    return packs[key]


# --- curation writes -------------------------------------------------------------------------

def edit_pack(cfg: Config, con, pack_id: str, fields: dict) -> dict:
    p = con.execute("SELECT * FROM packs WHERE id=?", (pack_id,)).fetchone()
    if not p:
        raise EditError(f"no pack {pack_id}")
    _check(cfg, fields, PACK_FIELDS)
    if fields.get("cover"):
        rel = f"{p['path']}/{fields['cover']}"
        if not con.execute("SELECT 1 FROM files WHERE path=?", (rel,)).fetchone():
            raise EditError(f"cover not found in the pack: {fields['cover']}")
    path = group_file(cfg, p["group_id"])
    doc = _load(path)
    t = _pack_table(doc, p["key"], create=True)
    _apply(t, fields)
    if len(t) == 0:
        del doc["packs"][p["key"]]
    _save(cfg, path, doc)
    refresh(cfg, con)
    return dict(con.execute("SELECT * FROM packs WHERE id=?", (pack_id,)).fetchone())


def edit_group(cfg: Config, con, group_id: str, fields: dict) -> dict:
    if not con.execute("SELECT 1 FROM groups WHERE id=?", (group_id,)).fetchone():
        raise EditError(f"no group {group_id}")
    _check(cfg, fields, GROUP_FIELDS)
    path = group_file(cfg, group_id)
    doc = _load(path)
    _apply(doc, fields)
    _save(cfg, path, doc)
    refresh(cfg, con)
    return dict(con.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone())


def edit_grid(cfg: Config, con, asset_id: int, spec: dict | None) -> dict:
    """spec = {w, h, spacing, margin} sets the grid; None removes the curation entry."""
    a = con.execute("SELECT a.*, p.path AS pack_path, p.key, p.group_id FROM assets a JOIN packs p ON p.id=a.pack_id WHERE a.id=?",
                    (asset_id,)).fetchone()
    if not a:
        raise EditError(f"no asset {asset_id}")
    inner = a["path"][len(a["pack_path"]) + 1:]
    if spec is not None:
        w, h = int(spec["w"]), int(spec.get("h") or spec["w"])
        s, m = int(spec.get("spacing") or 0), int(spec.get("margin") or 0)
        if not grids.fit(a["width"] or 0, a["height"] or 0, w, h, s, m):
            raise EditError(f"{w}x{h} (spacing {s}, margin {m}) doesn't tile a {a['width']}x{a['height']} image")
    path = group_file(cfg, a["group_id"])
    doc = _load(path)
    t = _pack_table(doc, a["key"], create=spec is not None)
    if t is not None:
        g = t.get("grid")
        if spec is None:
            if g is not None and inner in g:
                del g[inner]
                if len(g) == 0:
                    del t["grid"]
        else:
            if g is None:
                t["grid"] = tomlkit.inline_table()
                g = t["grid"]
            if s or m:
                v = tomlkit.inline_table()
                v.update({"w": w, "h": h, "spacing": s, "margin": m})
                g[inner] = v
            else:
                g[inner] = [w, h]
        if len(t) == 0:
            del doc["packs"][a["key"]]
        _save(cfg, path, doc)
    if spec is None:
        con.execute("""UPDATE assets SET grid_w=NULL, grid_h=NULL, grid_spacing=NULL, grid_margin=NULL, grid_cols=NULL,
                       grid_rows=NULL, grid_source=NULL, grid_confidence=NULL WHERE id=? AND grid_source='curation'""", (asset_id,))
    else:
        cols, rows = grids.fit(a["width"], a["height"], w, h, s, m)
        con.execute("""UPDATE assets SET grid_w=?, grid_h=?, grid_spacing=?, grid_margin=?, grid_cols=?, grid_rows=?,
                       grid_source='curation', grid_confidence=1.0,
                       type=CASE WHEN type IN ('sprite','character') THEN 'sheet' ELSE type END WHERE id=?""",
                    (w, h, s, m, cols, rows, asset_id))
    con.commit()
    return dict(con.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone())


def edit_tags(cfg: Config, con, pack_id: str, add: list[str], remove: list[str]) -> dict:
    """Make tags show (add) or not (remove) on a pack, as the smallest change to its curation:
    adding undoes the pack's own `tags_remove` entry first and only lands in `tags` if the AI or
    an inherited layer doesn't already give it; removing works the same way round."""
    p = con.execute("SELECT * FROM packs WHERE id=?", (pack_id,)).fetchone()
    if not p:
        raise EditError(f"no pack {pack_id}")
    vocab = A.load_vocab(cfg)
    gc = load_curation(cfg).group(p["group_id"])
    chain, pid = [], p["parent_id"]
    while pid:
        x = con.execute("SELECT key, parent_id FROM packs WHERE id=?", (pid,)).fetchone()
        chain.insert(0, gc.packs.get(x["key"], {}))
        pid = x["parent_id"]
    base = A.apply_layers(json.loads(p["ai_tags"] or "[]"), [gc.data] + chain, vocab)
    path = group_file(cfg, p["group_id"])
    doc = _load(path)
    t = _pack_table(doc, p["key"], create=True)
    adds, rems = list(t.get("tags", [])), list(t.get("tags_remove", []))
    for raw in add:
        n = vocab.norm(raw)
        if not n or ":" in n and not re.fullmatch(r"[a-z0-9-]+:[a-z0-9][a-z0-9 .-]*", n):
            raise EditError(f"not a tag: {raw!r}")
        rems = [x for x in rems if vocab.norm(x) != n]
        if n not in base and n not in map(vocab.norm, adds):
            adds.append(n)
    for raw in remove:
        n = vocab.norm(raw)
        adds = [x for x in adds if vocab.norm(x) != n]
        if n in base and n not in map(vocab.norm, rems):
            rems.append(n)
    changed = {k: v or None for k, v in (("tags", adds), ("tags_remove", rems)) if v != list(t.get(k, []))}
    if changed:
        _apply(t, changed)
        if len(t) == 0:
            del doc["packs"][p["key"]]
        _save(cfg, path, doc)
    elif len(t) == 0:
        return dict(p)
    refresh(cfg, con)
    return dict(con.execute("SELECT * FROM packs WHERE id=?", (pack_id,)).fetchone())


def edit_roles(cfg: Config, gid: str, roles: dict[str, str | None], known: set[str]) -> Path:
    """Set or clear (None) the role of folders in a group, by curation key. `known` holds the
    keys that can take a role (folders the layout reaches); clearing works for any key. Takes
    effect on the next index."""
    bad = {r for r in roles.values() if r is not None and r not in ROLES}
    if bad:
        raise EditError(f"role must be one of {sorted(ROLES)}")
    if unknown := sorted(k for k, r in roles.items() if r is not None and k not in known):
        raise EditError(f"no folder with a role at: {', '.join(unknown)}")
    path = group_file(cfg, gid)
    doc = _load(path)
    for key, role in roles.items():
        t = _pack_table(doc, key, create=role is not None)
        if t is None:
            continue
        _apply(t, {"role": role})
        if len(t) == 0:
            del doc["packs"][key]
    _save(cfg, path, doc)
    return path


# --- re-resolve curation into the index ------------------------------------------------------

def refresh(cfg: Config, con):
    """Re-apply group and pack curation (license, AI, tags, notes, description, cover) to the
    index, with the same inheritance as the indexer: pack > parent pack > group > default."""
    cur = load_curation(cfg)
    vocab = A.load_vocab(cfg)
    for g in con.execute("SELECT id FROM groups").fetchall():
        gc = cur.group(g["id"])
        con.execute("UPDATE groups SET store=?, purchased=?, license=?, ai_generated=?, ai_reason=?, notes=? WHERE id=?",
                    (gc.get("store"), gc.get("purchased"), gc.get("license", "unknown"), gc.get("ai_generated", "unknown"),
                     gc.get("ai_reason"), gc.get("notes"), g["id"]))
    resolved: dict[str, dict] = {}
    for p in con.execute("SELECT id, key, group_id, parent_id, path, cover, cover_kind, license_file, ai_tags, ai_description "
                         "FROM packs ORDER BY depth").fetchall():
        gc = cur.group(p["group_id"])
        pc = gc.packs.get(p["key"], {})
        parent = resolved.get(p["parent_id"]) if p["parent_id"] else None

        def pick(field, default):
            if field in pc:
                return pc[field], "pack"
            if parent and parent[field][1] != "default":
                return parent[field][0], "parent"
            if field in gc.data:
                return gc.data[field], "group"
            return default, "default"

        r = {"license": pick("license", "unknown"), "ai_generated": pick("ai_generated", "unknown")}
        r["ai_reason"] = pc.get("ai_reason") or (parent or {}).get("ai_reason") or gc.get("ai_reason")
        r["layers"] = ((parent or {}).get("layers") or [gc.data]) + [pc]
        resolved[p["id"]] = r
        tags = A.apply_layers(json.loads(p["ai_tags"] or "[]"), r["layers"], vocab)
        cover, cover_kind = p["cover"], p["cover_kind"]
        if pc.get("cover"):
            cover, cover_kind = f"{p['path']}/{pc['cover']}", "bundled"
        lic_file = f"{p['path']}/{pc['license_file']}" if pc.get("license_file") else p["license_file"]
        desc, desc_from = (pc["description"], "curation") if pc.get("description") else \
            (p["ai_description"], "ai") if p["ai_description"] else (None, None)
        con.execute("""UPDATE packs SET license=?, license_from=?, ai_generated=?, ai_reason=?, tags=?, notes=?,
                       description=?, description_source=?, cover=?, cover_kind=?, license_file=? WHERE id=?""",
                    (r["license"][0], r["license"][1], r["ai_generated"][0], r["ai_reason"],
                     json.dumps(tags) if tags else None, pc.get("notes"), desc, desc_from,
                     cover, cover_kind, lic_file, p["id"]))
    con.commit()


# --- shortlists ------------------------------------------------------------------------------

def shortlist_path(cfg: Config, name: str) -> Path:
    if not _NAME.match(name):
        raise EditError("shortlist names are lowercase letters, digits and dashes")
    return cfg.shortlist_dir / f"{name}.toml"


def list_shortlists(cfg: Config) -> list[dict]:
    out = []
    for p in sorted(cfg.shortlist_dir.glob("*.toml")):
        doc = tomlkit.parse(p.read_text(encoding="utf-8"))
        out.append({"name": p.stem, "notes": doc.get("notes", ""), "count": len(doc.get("items", []))})
    return out


def read_shortlist(cfg: Config, name: str) -> dict:
    p = shortlist_path(cfg, name)
    if not p.exists():
        raise EditError(f"no shortlist '{name}'")
    doc = tomlkit.parse(p.read_text(encoding="utf-8"))
    return {"name": name, "notes": str(doc.get("notes", "")),
            "items": [{k: (str(v) if isinstance(v, (dt.date, tomlkit.items.Date)) else v) for k, v in dict(i).items()}
                      for i in doc.get("items", [])]}


def create_shortlist(cfg: Config, name: str, notes: str = "") -> dict:
    p = shortlist_path(cfg, name)
    if p.exists():
        return read_shortlist(cfg, name)
    doc = tomlkit.document()
    doc.add(tomlkit.comment(f'Shortlist "{name}": packs, folders and assets picked in the asset browser.'))
    doc.add(tomlkit.comment("kind = pack (ref = pack id) | folder | asset (ref = library path). Edit freely."))
    doc["format"] = 1
    doc["created"] = dt.date.today()
    doc["notes"] = notes
    _save(cfg, p, doc)
    return read_shortlist(cfg, name)


def shortlist_add(cfg: Config, name: str, kind: str, ref: str, note: str = "") -> dict:
    if kind not in ("pack", "folder", "asset"):
        raise EditError("kind must be pack, folder or asset")
    p = shortlist_path(cfg, name)
    if not p.exists():
        create_shortlist(cfg, name)
    doc = _load(p)
    items = doc.get("items")
    if items is None:
        doc.add(tomlkit.nl())
        doc.add("items", tomlkit.aot())
        items = doc["items"]
    if not any(i.get("kind") == kind and i.get("ref") == ref for i in items):
        t = tomlkit.table()
        t.update({"kind": kind, "ref": ref, "added": dt.date.today()})
        if note:
            t["note"] = note
        t.add(tomlkit.nl())
        items.append(t)
        _save(cfg, p, doc)
    return read_shortlist(cfg, name)


def shortlist_remove(cfg: Config, name: str, kind: str, ref: str) -> dict:
    p = shortlist_path(cfg, name)
    doc = _load(p)
    items = doc.get("items")
    if items is not None:
        for n in reversed(range(len(items))):
            if items[n].get("kind") == kind and items[n].get("ref") == ref:
                del items[n]
        if len(items) == 0:
            del doc["items"]
        _save(cfg, p, doc)
    return read_shortlist(cfg, name)
