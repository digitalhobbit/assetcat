"""`assetcat pull`: copy assets out of the library into a project, with their licenses and a record
of where every file came from.

Targets are packs (id, key or name), folders inside a pack, or single files (library paths or
absolute paths). Each target is copied under its own name, like `cp -r`: a pack lands in
DIR/<pack folder>/, a folder in DIR/<folder>/, a file in DIR/<file> (a sheet also brings the
atlas or Tiled file that describes it, a 3D model the textures, buffers and .mtl files it loads,
at the same path relative to the model). `flatten` puts every file directly in DIR, except a
model's files, which keep their place next to it.

All-or-nothing: unsynced files, the AI policy and conflicting existing files are checked before
anything is copied. Identical existing files count as unchanged; different ones are only replaced
with `overwrite`. Nothing is ever written inside the library.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .curation import load_curation

TEXT_LICENSE = {"txt", "md"}
DEFAULT_LICENSE_NAME = "License-{pack}{ext}"


class PullError(Exception):
    pass


@dataclass
class Item:
    src: str          # library path
    dest: str         # posix path relative to the destination folder
    pack_id: str
    size: int
    status: str = ""  # new | unchanged | replaced
    hash: str = ""


@dataclass
class Plan:
    items: list[Item] = field(default_factory=list)
    licenses: list[Item] = field(default_factory=list)
    packs: dict[str, dict] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


# --- targets -----------------------------------------------------------------------------------

def _lib_rel(cfg: Config, t: str) -> str:
    """A target as a library-relative posix path (absolute paths inside the library work too)."""
    t = t.strip()
    p = Path(t)
    if p.is_absolute():
        try:
            return p.resolve().relative_to(cfg.library.resolve()).as_posix()
        except ValueError:
            try:
                return Path(os.path.abspath(p)).relative_to(cfg.library).as_posix()
            except ValueError:
                raise PullError(f"not inside the library: {t}") from None
    if os.sep == "\\":
        t = t.replace("\\", "/")
    return t.strip("/")


def _pack_by_name(con, q: str) -> dict | None:
    from .cli import resolve_pack

    try:
        return resolve_pack(con, q)
    except SystemExit as e:  # resolve_pack reports ambiguity / no match by exiting
        msg = str(e.code or "").replace("assetcat: ", "")
        if msg.startswith("no pack matches"):
            return None
        raise PullError(msg) from None


def _pack_of(con, rel: str) -> dict | None:
    r = con.execute("SELECT * FROM packs WHERE ? = path OR ? LIKE replace(replace(path, '%', '\\%'), '_', '\\_') || '/%' ESCAPE '\\' "
                    "ORDER BY length(path) DESC LIMIT 1", (rel, rel)).fetchone()
    return dict(r) if r else None


def _files_under(con, prefix: str) -> list[dict]:
    rows = con.execute("SELECT path, size, pack_id FROM files WHERE substr(path, 1, ?) = ? ORDER BY path",
                       (len(prefix) + 1, prefix + "/")).fetchall()
    return [dict(r) for r in rows]


def model_refs(con, rows: list[dict]) -> tuple[dict[str, str], list[dict], list[str]]:
    """Files the models among `rows` load (textures, buffers, .mtl): {ref: model}, rows for the
    refs not already in `rows`, and the refs that aren't in the library."""
    have = {r["path"] for r in rows}
    ref_of: dict[str, str] = {}
    extra, missing = [], []
    from .classify import category, ext_of

    for r in list(rows):
        if category(ext_of(r["path"])) != "model":
            continue
        info = con.execute("SELECT info FROM files WHERE path=?", (r["path"],)).fetchone()
        for ref in json.loads(info[0]).get("refs", []) if info and info[0] else []:
            if ref in ref_of:
                continue
            x = con.execute("SELECT path, size, pack_id FROM files WHERE path=?", (ref,)).fetchone()
            if not x:
                missing.append(f"{r['path']}: {ref}")
                continue
            ref_of[ref] = r["path"]
            if ref not in have:
                have.add(ref)
                extra.append({**dict(x), "pack_id": x["pack_id"] or r["pack_id"]})
    return ref_of, extra, missing


def resolve_target(cfg: Config, con, target: str) -> tuple[str, dict, list[dict], str]:
    """(kind, pack, file rows, base) where each file lands at its path relative to `base`."""
    rel = _lib_rel(cfg, target)
    f = con.execute("SELECT path, size, pack_id FROM files WHERE path=?", (rel,)).fetchone()
    if f:
        if not f["pack_id"]:
            raise PullError(f"not inside a pack: {rel}")
        pack = dict(con.execute("SELECT * FROM packs WHERE id=?", (f["pack_id"],)).fetchone())
        rows = [dict(f)]
        a = con.execute("SELECT a.atlas FROM assets a JOIN asset_files af ON af.asset_id=a.id JOIN files fi ON fi.id=af.file_id "
                        "WHERE fi.path=? AND a.atlas IS NOT NULL", (rel,)).fetchone()
        if a and a["atlas"] != rel:
            x = con.execute("SELECT path, size, pack_id FROM files WHERE path=?", (a["atlas"],)).fetchone()
            if x:
                rows.append(dict(x))
        return "file", pack, rows, posixpath.dirname(rel)
    files = _files_under(con, rel)
    if files:
        pack = _pack_of(con, rel)
        if pack is None:
            raise PullError(f"not inside a pack: {rel} (pull the packs in it instead)")
        kind = "pack" if pack["path"] == rel else "folder"
        return kind, pack, files, posixpath.dirname(rel)
    pack = _pack_by_name(con, target)
    if pack is None:
        raise PullError(f"no pack, folder or file matches '{target}'")
    return "pack", pack, _files_under(con, pack["path"]), posixpath.dirname(pack["path"])


# --- planning ----------------------------------------------------------------------------------

def file_hash(path: str) -> str:
    h = hashlib.blake2b(digest_size=16)
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _safe_name(s: str) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "", s).strip().strip(".")
    return re.sub(r"\s+", "-", s) or "pack"


def license_owner(con, pack: dict, src: str) -> str:
    """Whose license file this is, for its name: the pack it sits in, else the parent pack or
    group above it (a bundle-wide License.txt is named after the bundle, not one of its packs)."""
    if src.startswith(pack["path"] + "/"):
        return pack["name"]
    owner = _pack_of(con, src)
    if owner:
        return owner["name"]
    return con.execute("SELECT name FROM groups WHERE id=?", (pack["group_id"],)).fetchone()[0]


def license_dest(template: str, owner: str, src: str) -> str:
    ext = posixpath.splitext(src)[1]
    from .classify import slugify

    name = template.format(pack=_safe_name(owner), slug=slugify(owner), ext=ext)
    if "/" in name or "\\" in name or name in ("", ".", ".."):
        raise PullError(f"--license-name must give a plain file name, got '{name}'")
    return name


def _same_file(cfg: Config, a: Item, r: dict) -> bool:
    """Two sources for one destination are fine when they are the same bytes (Kenney ships one
    texture per format folder, so a GLB and an OBJ of one model both bring Textures/colormap.png)."""
    if a.size != r["size"] or not a.size:
        return False
    a.hash = a.hash or file_hash(cfg.fs(a.src))
    return file_hash(cfg.fs(r["path"])) == a.hash


def make_plan(cfg: Config, con, targets: list[str], *, flatten=False, license_name=DEFAULT_LICENSE_NAME) -> Plan:
    plan = Plan()
    seen: dict[str, Item] = {}  # by dest
    clashes = []
    for t in targets:
        _, top, rows, base = resolve_target(cfg, con, t)
        ref_of, extra, missing = model_refs(con, rows)
        plan.warnings += [f"not in the library (the model will miss it): {m}" for m in missing]
        dest_of: dict[str, str] = {}
        both = rows + extra  # models before the files they load, so those can be placed next to them
        for r in [r for r in both if r["path"] not in ref_of] + [r for r in both if r["path"] in ref_of]:
            under = not base or r["path"].startswith(base + "/")
            # a sheet's atlas can sit in a sibling folder (Kenney: Tilemap/ and Tiled/): it goes next to the sheet
            inner = (r["path"][len(base) + 1:] if base else r["path"]) if under else posixpath.basename(r["path"])
            dest = posixpath.basename(r["path"]) if flatten else inner
            model = ref_of.get(r["path"])
            if model in dest_of and (flatten or not under):  # keep it where the model looks for it
                rel = posixpath.relpath(r["path"], posixpath.dirname(model))
                dest = posixpath.normpath(posixpath.join(posixpath.dirname(dest_of[model]), rel))
                if dest == ".." or dest.startswith("../"):
                    dest = posixpath.basename(r["path"])
                    plan.warnings.append(f"{r['path']} sits above {model}; copied as {dest}, so the model won't find it")
            dest_of[r["path"]] = dest
            if dest in seen:
                if seen[dest].src != r["path"] and not _same_file(cfg, seen[dest], r):
                    clashes.append(f"{dest}: {seen[dest].src}  and  {r['path']}")
                continue
            seen[dest] = Item(r["path"], dest, r["pack_id"] or top["id"], r["size"])
            plan.items.append(seen[dest])
    if clashes:
        raise PullError("two files would land at the same place" + (" (drop --flatten?)" if flatten else "") + ":\n  "
                        + "\n  ".join(clashes[:20]))
    if not plan.items:
        raise PullError("nothing to pull")

    cur = load_curation(cfg)
    lic_srcs: dict[str, Item] = {}
    for pid in dict.fromkeys(i.pack_id for i in plan.items):
        p = dict(con.execute("SELECT * FROM packs WHERE id=?", (pid,)).fetchone())
        g = dict(con.execute("SELECT * FROM groups WHERE id=?", (p["group_id"],)).fetchone())
        lic = cur.licenses.get(p["license"] or "unknown", {})
        info = {
            "id": pid, "name": p["name"], "group": g["name"], "store": g["store"] or None,
            "license": p["license"], "license_name": lic.get("name", p["license"]), "license_url": lic.get("url"),
            "attribution_required": lic.get("attribution_required"),
            "ai_generated": p["ai_generated"], "ai_reason": p["ai_reason"], "license_file": None,
        }
        src = p["license_file"]
        if src:
            if src in lic_srcs:  # sub-packs sharing their parent's license file
                info["license_file"] = lic_srcs[src].dest
            else:
                row = con.execute("SELECT size FROM files WHERE path=?", (src,)).fetchone()
                if row:
                    item = Item(src, license_dest(license_name, license_owner(con, p, src), src), pid, row["size"])
                    if item.dest in seen and seen[item.dest].src != src:
                        raise PullError(f"the license file name {item.dest} clashes with a pulled file; use --license-name")
                    lic_srcs[src] = item
                    plan.licenses.append(item)
                    info["license_file"] = item.dest
        info["credit"] = credit_line(info)
        plan.packs[pid] = info
    return plan


def credit_line(p: dict) -> str:
    who = p.get("store") or p["group"]
    lic = "license unknown (check it before shipping)" if p.get("license") in (None, "unknown") else (p["license_name"] or p["license"])
    tail = " (attribution required)" if p.get("attribution_required") else ""
    url = f" {p['license_url']}" if p.get("license_url") else ""
    return f"{p['name']} ({who}): {lic}{tail}{url}"


# --- running -----------------------------------------------------------------------------------

def run_pull(cfg: Config, con, targets: list[str], to: str, *, manifest: str | None = None,
             license_name: str = DEFAULT_LICENSE_NAME, license_lf: bool = False, require_ai: str | None = None,
             force: bool = False, overwrite: bool = False, flatten: bool = False, dry_run: bool = False) -> dict:
    dest_root = Path(os.path.abspath(Path(to).expanduser()))
    lib = Path(os.path.abspath(cfg.library))
    if dest_root == lib or lib in dest_root.parents:
        raise PullError(f"the destination is inside the library, which assetcat never writes to: {dest_root}")
    if manifest:
        m = Path(os.path.abspath(Path(manifest).expanduser()))
        if m == lib or lib in m.parents:
            raise PullError(f"the manifest is inside the library, which assetcat never writes to: {m}")

    plan = make_plan(cfg, con, targets, flatten=flatten, license_name=license_name)

    pending = [i.src for i in plan.items + plan.licenses if i.size == 0]
    if pending:
        raise PullError(f"{len(pending)} files aren't synced yet (0 bytes); make them available offline and run "
                        "`assetcat index`:\n  " + "\n  ".join(pending[:10]))
    if require_ai:
        blocked = [p for p in plan.packs.values() if p["ai_generated"] != require_ai]
        if blocked and not force:
            lines = [f"{p['id']}: ai_generated = {p['ai_generated']}" + (f" ({p['ai_reason']})" if p["ai_reason"] else "")
                     for p in blocked]
            raise PullError(f"--require-ai {require_ai}: these packs aren't marked ai_generated = \"{require_ai}\" "
                            "(record it in curation, or --force):\n  " + "\n  ".join(lines))

    conflicts = []
    for i in plan.items + plan.licenses:
        d = dest_root.joinpath(*i.dest.split("/"))
        if not d.exists():
            i.status = "new"  # hashed while copying; a dry run never reads the file
            continue
        if d.is_dir():
            conflicts.append(f"{i.dest} (a folder)")
            continue
        i.hash = file_hash(cfg.fs(i.src))
        same = d.stat().st_size == i.size and file_hash(str(d)) == i.hash
        if i in plan.licenses and not same:  # an earlier pull may have normalized line endings
            same = d.read_bytes() == _lf(Path(cfg.fs(i.src)).read_bytes(), i.src)
        if same:
            i.status = "unchanged"
        elif overwrite:
            i.status = "replaced"
        else:
            conflicts.append(i.dest)
    if conflicts:
        raise PullError(f"{len(conflicts)} files already exist in {dest_root} with different content "
                        "(--overwrite to replace them):\n  " + "\n  ".join(conflicts[:20]))

    if not dry_run:
        for i in plan.items + plan.licenses:
            if i.status == "unchanged":
                continue
            d = dest_root.joinpath(*i.dest.split("/"))
            d.parent.mkdir(parents=True, exist_ok=True)
            data = Path(cfg.fs(i.src)).read_bytes()
            i.hash = hashlib.blake2b(data, digest_size=16).hexdigest()
            if i in plan.licenses and license_lf:
                data = _lf(data, i.src)
            d.write_bytes(data)
        if manifest:
            write_manifest(Path(manifest).expanduser(), dest_root, plan)

    count = lambda s: sum(1 for i in plan.items if i.status == s)  # noqa: E731
    return {
        "to": str(dest_root), "dry_run": dry_run,
        "new": count("new"), "replaced": count("replaced"), "unchanged": count("unchanged"),
        "files": [{"dest": i.dest, "source": i.src, "pack": i.pack_id, "status": i.status, "hash": i.hash} for i in plan.items],
        "licenses": [{"dest": i.dest, "source": i.src, "pack": i.pack_id, "status": i.status} for i in plan.licenses],
        "packs": list(plan.packs.values()),
        "warnings": plan.warnings,
        "manifest": str(Path(os.path.abspath(Path(manifest).expanduser()))) if manifest else None,
    }


def _lf(data: bytes, src: str) -> bytes:
    if posixpath.splitext(src)[1].lstrip(".").lower() not in TEXT_LICENSE:
        return data
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


# --- manifest ----------------------------------------------------------------------------------

MANIFEST_HEADER = [
    "Files pulled from an asset library with `assetcat pull`: where each file came from, its pack's",
    "license and AI status. Updated by every pull; edit freely. Paths are relative to this file.",
]


def write_manifest(path: Path, dest_root: Path, plan: Plan):
    import tomlkit

    path = Path(os.path.abspath(path))
    doc = tomlkit.parse(path.read_text(encoding="utf-8")) if path.exists() else tomlkit.document()
    if not path.exists():
        for line in MANIFEST_HEADER:
            doc.add(tomlkit.comment(line))
        doc["format"] = 1
    base = path.parent

    def rel(dest: str) -> str:
        target = dest_root.joinpath(*dest.split("/"))
        try:
            return Path(os.path.relpath(target, base)).as_posix()
        except ValueError:  # Windows: another drive
            return target.as_posix()

    if "packs" not in doc:
        doc.add(tomlkit.nl())
        doc["packs"] = tomlkit.table(is_super_table=True)
    for pid, p in plan.packs.items():
        t = tomlkit.table()
        for k in ("name", "group", "store", "license", "license_name", "license_url", "attribution_required",
                  "ai_generated", "ai_reason", "credit"):
            if p.get(k) not in (None, ""):
                t[k] = p[k]
        if p.get("license_file"):
            t["license_file"] = rel(p["license_file"])
        doc["packs"][pid] = t
    if "files" not in doc:
        doc.add(tomlkit.nl())
        doc["files"] = tomlkit.table()
    today = dt.date.today()
    for i in plan.items:
        old = doc["files"].get(rel(i.dest))
        if old is not None and old.get("hash") == f"blake2b-128:{i.hash}" and old.get("source") == i.src:
            continue  # same file pulled again: keep its entry (and date) as it is
        v = tomlkit.inline_table()
        v.update({"source": i.src, "pack": i.pack_id, "hash": f"blake2b-128:{i.hash}", "pulled": today})
        doc["files"][rel(i.dest)] = v
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomlkit.dumps(doc).rstrip() + "\n", encoding="utf-8", newline="\n")
