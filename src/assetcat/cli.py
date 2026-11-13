"""assetcat command line. Every read command takes --json (paths in JSON are absolute)."""

from __future__ import annotations

import argparse
import json
import posixpath
import re
import sys
import textwrap

from . import __version__
from . import ai as A
from . import classify as C
from . import db as DB
from .config import ConfigError, load_config, read_user_config, user_config_path, write_user_config
from .curation import load_curation, load_licenses


def _location_args(p: argparse.ArgumentParser, top: bool):
    # On the main parser and on every subcommand, so they work before or after the command.
    d = None if top else argparse.SUPPRESS
    p.add_argument("--library", default=d, help="library folder (default: from `assetcat init`)")
    p.add_argument("--profile", default=d, help="profile folder (default: <library>/assetcat)")


def unmangle_windows_args(argv: list[str]) -> list[str]:
    """Undo the trailing-backslash quoting bug of Windows shells.

    PowerShell tab completion writes 'D:\\Lib\\My Assets\\'. The shell passes it on as
    "D:\\Lib\\My Assets\\", and the C runtime reads the final \\" as an escaped quote: the path
    absorbs the closing quote and every argument after it ('D:\\Lib\\My Assets" --dry-run').
    A double quote can't occur in a Windows file name, so an argument containing one is always
    this: the part before it is the path (without the eaten trailing backslash, which doesn't
    matter), the rest is the remaining arguments.
    """
    import shlex

    out: list[str] = []
    for arg in argv:
        if '"' not in arg:
            out.append(arg)
            continue
        head, _, tail = arg.partition('"')
        if head:
            out.append(head)
        try:
            toks = shlex.split(tail, posix=False)
        except ValueError:  # an unbalanced quote from a second mangled path
            toks = tail.split()
        for tok in toks:
            # posix=False keeps backslashes (paths) intact but leaves quotes on the tokens
            tok = tok[1:-1] if len(tok) >= 2 and tok[0] == tok[-1] == '"' else tok
            out.extend(unmangle_windows_args([tok]) if '"' in tok else [tok])
    return out


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
        if sys.platform == "win32":
            argv = unmangle_windows_args(argv)
    ap = argparse.ArgumentParser(prog="assetcat", description="Index, search and browse a game asset library.")
    ap.add_argument("--version", action="version", version=f"assetcat {__version__}")
    _location_args(ap, True)
    sub = ap.add_subparsers(dest="cmd", required=True)
    _add = sub.add_parser

    def add_parser(*a, **k):
        p = _add(*a, **k)
        _location_args(p, False)
        return p

    sub.add_parser = add_parser

    p = sub.add_parser("init", help="set up this machine for a library (and create its profile if it has none)")
    p.add_argument("path", nargs="?", help="the library folder (or use --library)")
    p.add_argument("--dry-run", action="store_true", help="show the detected layout, write nothing")

    p = sub.add_parser("index", help="scan the library and (re)build the index")
    p.add_argument("--no-extract", action="store_true", help="skip reading file metadata")
    p.add_argument("--no-hash", action="store_true", help="skip duplicate hashing")
    p.add_argument("--no-sheets", action="store_true", help="skip pack contact sheets")
    p.add_argument("--workers", type=int)

    p = sub.add_parser("packs", help="list packs")
    p.add_argument("--tree", action="store_true", help="groups and nested packs, with curation keys")
    p.add_argument("--section")
    p.add_argument("--group", help="group id or part of its name")
    p.add_argument("--kind", help="graphics | 3d | music | sfx | audio | mixed | fonts | engine | archived | other")
    p.add_argument("--license")
    p.add_argument("--ai", choices=["yes", "no", "unknown"])
    p.add_argument("--tag", action="append", help="only packs with this tag, e.g. style:pixel-art (repeat for several)")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("search", help="full-text search over assets (or packs with --packs)")
    p.add_argument("query", nargs="*")
    p.add_argument("--packs", action="store_true", help="search packs instead of assets")
    p.add_argument("--meaning", action="store_true",
                   help="search images by what they show (needs `assetcat classify --kind embed` and the AI extra)")
    p.add_argument("--similar", metavar="FILE", help="images that look like this one (a library path)")
    p.add_argument("--type", help="sprite, sheet, tileset, ui, icon, background, portrait, vfx, character, preview, sfx, music, voice, model, font (comma list)")
    p.add_argument("--media", help="visual | audio | model | font")
    p.add_argument("--pack", help="limit to a pack (id, key or name part) and its sub-packs")
    p.add_argument("--section")
    p.add_argument("--kind", help="pack kind (with --packs, or to filter assets by their pack's kind)")
    p.add_argument("--license")
    p.add_argument("--ai", choices=["yes", "no", "unknown"])
    p.add_argument("--tag", action="append", help="only packs (or assets in packs) with this tag, e.g. perspective:top-down")
    p.add_argument("--min-size", help="WxH, e.g. 16x16")
    p.add_argument("--max-size", help="WxH")
    p.add_argument("--grid", action="store_true", help="only sheets/tilesets with a known grid")
    p.add_argument("--animated", action="store_true", help="only 3D models with animation clips (glTF)")
    p.add_argument("--rigged", action="store_true", help="only 3D models with a skeleton (glTF)")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("show", help="details of a pack (id, key or name) or an asset (library path)")
    p.add_argument("target")
    p.add_argument("--docs", action="store_true", help="print license/readme text in full")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("sheet", help="contact sheet of a pack or a folder in it; prints the PNG path")
    p.add_argument("target", help="pack, or a library path to a folder")
    p.add_argument("folder", nargs="?", help="folder inside the pack")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--cols", type=int, default=10)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("pull", help="copy packs, folders or files into a project, with licenses and a manifest")
    p.add_argument("targets", nargs="+", help="pack (id, key or name), or a library path of a folder or file in a pack")
    p.add_argument("--to", required=True, help="destination folder (outside the library)")
    p.add_argument("--manifest", help="TOML file recording where each pulled file came from (created or updated)")
    p.add_argument("--license-name", default="License-{pack}{ext}",
                   help="file name for each pack's license; {pack}, {slug} and {ext} are filled in (default: %(default)s)")
    p.add_argument("--license-lf", action="store_true", help="convert text license files to LF line endings")
    p.add_argument("--require-ai", choices=["no"], help="refuse packs not marked ai_generated = \"no\" (human-made)")
    p.add_argument("--force", action="store_true", help="pull anyway when --require-ai refuses")
    p.add_argument("--overwrite", action="store_true", help="replace existing files that differ")
    p.add_argument("--flatten", action="store_true", help="put every file directly in the destination folder")
    p.add_argument("--dry-run", action="store_true", help="show what would be copied, write nothing")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("classify", help="tag packs with a vision-language model (LM Studio, Ollama or another OpenAI-compatible server)")
    p.add_argument("--kind", dest="ai_kind", choices=["tags", "embed"], default="tags",
                   help="tags: pack tags from a vision-language model (default); embed: image vectors for search by meaning")
    p.add_argument("--pack", action="append", help="only this pack and its sub-packs (id, key or name; repeat for several)")
    p.add_argument("--section", help="only packs in this section")
    p.add_argument("--group", help="only packs in this group (id or part of its name)")
    p.add_argument("--model", help="tags: model id on the server (default: ai_model); embed: Hugging Face model (default: embed_model)")
    p.add_argument("--url", help=f"server's OpenAI-compatible base URL (default: ai_url, or {A.DEFAULT_URL})")
    p.add_argument("--sample", type=int, metavar="N", help="tag N random packs and print the tags, saving nothing: try a model first")
    p.add_argument("--redo", action="store_true", help="also packs whose tags are up to date (for this model)")
    p.add_argument("--limit", type=int, help="stop after N packs")
    p.add_argument("--no-index", action="store_true", help="don't run `assetcat index` at the end (search picks up new tags then)")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("config", help="show or change this machine's settings (the user config file)")
    p.add_argument("key", nargs="?", choices=sorted(CONFIG_KEYS))
    p.add_argument("value", nargs="?")
    p.add_argument("--unset", action="store_true", help="remove the key")

    p = sub.add_parser("doctor", help="what needs attention: licenses, AI status, pending files, curation")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("stats", help="library totals")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("serve", help="start the browse UI on localhost")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="127.0.0.1", help="keep it on 127.0.0.1: the UI can edit curation files")
    p.add_argument("--open", action="store_true", help="open it in the browser")
    p.add_argument("--verbose", action="store_true", help="log every request with its status")

    a = ap.parse_args(argv)
    if a.cmd == "config":
        return cmd_config(a)
    if a.cmd == "init":
        from .init import run_init

        try:
            return run_init(a.path or a.library, a.profile, dry_run=a.dry_run)
        except ConfigError as e:
            sys.exit(f"assetcat: {e}")
    try:
        cfg = load_config(a.library, a.profile)
    except ConfigError as e:
        sys.exit(f"assetcat: {e}")
    if a.cmd == "index":
        from .indexer import run_index

        s = run_index(cfg, extract_meta=not a.no_extract, hash_dupes=not a.no_hash, make_sheets=not a.no_sheets, workers=a.workers)
        print(f"indexed {s['groups']} groups, {s['packs']} packs, {s['assets']} assets from {s['total']} files in {s['seconds']}s")
        for c in s["created"]:
            print(f"new curation stub: {c}")
        for m in s["missing_sections"]:
            print(f"section folder not found (skipped): {m}")
        for k in s.get("key_collisions", []):
            print(f"folders with the same key (the later ones get a suffix): {k}")
        if s["unused_curation"] or s["curation_problems"]:
            print("curation issues: run `assetcat doctor`")
        return
    if a.cmd == "serve":
        from .server import serve

        con = DB.connect(cfg.db_path)
        if DB.has_derived(con) and not DB.derived_current(con):
            sys.exit("assetcat: the index was built by an older assetcat; run `assetcat index` to update it")
        con.close()

        return serve(cfg, a.host, a.port, a.open, a.verbose)
    con = DB.connect(cfg.db_path)
    if not DB.has_derived(con):
        sys.exit("assetcat: no index yet; run `assetcat index` first")
    if not DB.derived_current(con):
        sys.exit("assetcat: the index was built by an older assetcat; run `assetcat index` to update it")
    a._cfg = cfg
    ctx = Ctx(cfg, con, a)
    {"packs": cmd_packs, "search": cmd_search, "show": cmd_show, "sheet": cmd_sheet,
     "doctor": cmd_doctor, "stats": cmd_stats, "pull": cmd_pull, "classify": cmd_classify}[a.cmd](ctx)


CONFIG_KEYS = {
    "library": "the library folder (set by `assetcat init`)",
    "profile": "the profile folder, if not <library>/assetcat",
    "cache": "where the index, thumbnails and contact sheets go",
    "machine": "this machine's name in AI result files (default: the host name)",
    "ai_url": f"OpenAI-compatible server for `classify` (default: {A.DEFAULT_URL}, LM Studio)",
    "ai_model": f"model id on that server for `classify` (recommended: {A.RECOMMENDED_MODEL})",
    "embed_model": "image-text model for search by meaning (default: google/siglip2-base-patch16-256)",
}


def cmd_config(a):
    uc = read_user_config()
    if a.key is None:
        print(f"user config: {user_config_path()}")
        for k, about in CONFIG_KEYS.items():
            print(f"  {k:9} = {uc[k]!r}" if k in uc else f"  {k:9}   (not set: {about})")
        return
    if a.unset:
        write_user_config({a.key: None})
    elif a.value is None:
        print(uc.get(a.key, ""))
        return
    else:
        write_user_config({a.key: a.value})
    print(f"{a.key} {'removed' if a.unset else '= ' + repr(a.value)}  ({user_config_path()})")


class Ctx:
    def __init__(self, cfg, con, args):
        self.cfg, self.con, self.args = cfg, con, args

    def abs(self, rel):
        return str(self.cfg.abs(rel)) if rel else None

    def out(self, data, text_fn):
        if getattr(self.args, "json", False):
            json.dump(data, sys.stdout, indent=2, ensure_ascii=False, default=str)
            print()
        else:
            text_fn(data)


# --- helpers ---------------------------------------------------------------------------------

def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def human(n: int | None) -> str:
    n = n or 0
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return str(n)


def resolve_pack(con, q: str) -> dict:
    q = q.strip().strip("/")
    r = con.execute("SELECT * FROM packs WHERE id=?", (q,)).fetchone()
    if r:
        return dict(r)
    ql = q.lower()
    cands = [dict(r) for r in con.execute("SELECT * FROM packs WHERE lower(id) LIKE ? OR lower(name)=? OR key=?", (f"%/{ql}", ql, ql))]
    if not cands:
        cands = [dict(r) for r in con.execute("SELECT * FROM packs WHERE lower(id) LIKE ? OR lower(name) LIKE ?", (f"%{ql}%", f"%{ql}%"))]
    if len(cands) == 1:
        return cands[0]
    if not cands:
        sys.exit(f"assetcat: no pack matches '{q}'")
    exact = [c for c in cands if c["name"].lower() == ql or c["id"].endswith("/" + ql)]
    if len(exact) == 1:
        return exact[0]
    lines = "\n".join(f"  {c['id']}" for c in cands[:15])
    sys.exit(f"assetcat: '{q}' matches {len(cands)} packs; be more specific:\n{lines}")


def pack_scope(con, q: str) -> list[str]:
    from .indexer import descendants

    return descendants(con, resolve_pack(con, q)["id"])


def parse_wh(s: str | None):
    if not s:
        return None
    m = re.fullmatch(r"(\d+)\s*[xX×]\s*(\d+)", s.strip())
    if not m:
        sys.exit(f"assetcat: size must look like 16x16, got '{s}'")
    return int(m.group(1)), int(m.group(2))


def fts_query(terms: list[str]) -> str:
    out = []
    for t in terms:
        for w in re.split(r"[^\w]+", t):
            if w:
                out.append(f'"{w}"*')
    return " ".join(out)


def _abs(cfg, rel):
    return str(cfg.abs(rel)) if rel else None


def license_info(cfg, key):
    lic = load_licenses(cfg).get(key or "unknown", {})
    return {"key": key, **lic}


# --- packs -----------------------------------------------------------------------------------

def _pack_filters(a, alias="p"):
    where, args = [], []
    if getattr(a, "section", None):
        where.append(f"{alias}.section=?"); args.append(a.section)
    if getattr(a, "kind", None):
        where.append(f"{alias}.kind=?"); args.append(a.kind)
    if getattr(a, "license", None):
        where.append(f"{alias}.license=?"); args.append(a.license)
    if getattr(a, "ai", None):
        where.append(f"{alias}.ai_generated=?"); args.append(a.ai)
    if getattr(a, "group", None):
        where.append(f"({alias}.group_id=? OR lower({alias}.group_id) LIKE ?)"); args += [a.group, f"%{a.group.lower()}%"]
    if getattr(a, "tag", None):
        from .ai import load_vocab

        vocab = load_vocab(a._cfg)
        for t in a.tag:
            where.append(f"{alias}.tags LIKE ?"); args.append(f'%"{vocab.norm(t)}"%')
    return where, args


def cmd_packs(ctx: Ctx):
    a, con = ctx.args, ctx.con
    where, args = _pack_filters(a)
    sql = "SELECT p.* FROM packs p" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY p.group_id, p.path"
    packs = [dict(r) for r in con.execute(sql, args)]
    groups = {r["id"]: dict(r) for r in con.execute("SELECT * FROM groups")}
    data = [{**p, "type_counts": json.loads(p["type_counts"] or "{}"), "abs_path": ctx.abs(p["path"]),
             "cover_abs": p["cover"] if p["cover_kind"] == "sheet" else ctx.abs(p["cover"])} for p in packs]

    def text(_):
        cur_g = None
        for p in data:
            if p["group_id"] != cur_g:
                cur_g = p["group_id"]
                g = groups[cur_g]
                print(f"\n== {g['name']}  [{cur_g}]  license={g['license']} ai={g['ai_generated']}")
            ind = "  " * (p["depth"] + 1)
            pend = f"  ({p['pending']} pending)" if p["pending"] else ""
            if a.tree:
                coll = "  [collection]" if p["role"] == "collection" else ""
                print(f"{ind}{p['key']}{coll}  {p['kind']}  {p['file_count']} files{pend}")
            else:
                lic = "" if p["license_from"] in ("group", "parent") else f"  license={p['license']}"
                print(f"{ind}{p['name']}  ·  {p['kind']}, {p['asset_count']} assets, {human(p['total_size'])}{pend}{lic}")
        print(f"\n{len(data)} packs")

    ctx.out(data, text)


# --- search ----------------------------------------------------------------------------------

def cmd_search(ctx: Ctx):
    a, con = ctx.args, ctx.con
    q = "" if (a.meaning or a.similar) else fts_query(a.query)
    if a.packs:
        return _search_packs(ctx, q)
    where, args = ["a.preview_of IS NULL"], []  # a model's bundled render is listed as the model
    sql = "SELECT a.*, p.name AS pack_name, p.license, p.ai_generated, p.kind FROM assets a JOIN packs p ON p.id=a.pack_id"
    if q:
        sql += " JOIN (SELECT rowid, bm25(assets_fts, 10, 4, 2, 1, 3, 3) AS score FROM assets_fts WHERE assets_fts MATCH ?) f ON f.rowid=a.id"
        args.append(q)
    if a.type:
        ts = a.type.split(",")
        where.append(f"a.type IN ({','.join('?' * len(ts))})"); args += ts
    if a.media:
        where.append("a.media=?"); args.append(a.media)
    if a.pack:
        ids = pack_scope(con, a.pack)
        where.append(f"a.pack_id IN ({','.join('?' * len(ids))})"); args += ids
    pw, pa = _pack_filters(a)
    where += pw; args += pa
    mn, mx = parse_wh(a.min_size), parse_wh(a.max_size)
    if mn:
        where.append("COALESCE(a.grid_w, a.width) >= ? AND COALESCE(a.grid_h, a.height) >= ?"); args += list(mn)
    if mx:
        where.append("COALESCE(a.grid_w, a.width) <= ? AND COALESCE(a.grid_h, a.height) <= ?"); args += list(mx)
    if a.grid:
        where.append("a.grid_w IS NOT NULL")
    if a.animated:
        where.append("a.clips > 0")
    if a.rigged:
        where.append("a.rigged = 1")
    if a.meaning or a.similar:
        ranked = _semantic(ctx, where, args)
        by_id = {r["id"]: dict(r) for r in con.execute(
            f"SELECT a.*, p.name AS pack_name, p.license, p.ai_generated, p.kind FROM assets a JOIN packs p ON p.id=a.pack_id "
            f"WHERE a.id IN ({','.join('?' * len(ranked))})", [i for i, _ in ranked])} if ranked else {}
        rows = [{**by_id[i], "score": round(s, 4)} for i, s in ranked]
    else:
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY a.available DESC, " + ("f.score, " if q else "") + "a.pack_id, a.path LIMIT ?"
        args.append(a.limit)
        rows = [dict(r) for r in con.execute(sql, args)]
    data = [_asset_out(ctx, r) for r in rows]

    def text(_):
        for r in data:
            print((f"{r['score']:.3f} " if "score" in r else "") + f"[{r['type']}] {r['path']}{_asset_bits(r)}")
        if not data:
            print("no matches")
        elif len(data) == a.limit:
            print(f"(first {a.limit}; use --limit or narrow the search)")

    ctx.out(data, text)


def _asset_out(ctx, r):
    out = {k: v for k, v in r.items() if v is not None}
    out["abs_path"] = ctx.abs(r["path"])
    if r.get("atlas"):
        out["atlas_abs"] = ctx.abs(r["atlas"])
    return out


def _asset_bits(r) -> str:
    bits = []
    if r.get("width"):
        bits.append(f"{r['width']}x{r['height']}")
    if r.get("grid_w"):
        bits.append(f"grid {r['grid_w']}x{r['grid_h']}" + (f"+{r['grid_spacing']}" if r.get("grid_spacing") else "") + f" ({r['grid_cols']}x{r['grid_rows']})")
    elif r.get("frames") and r.get("atlas"):
        bits.append(f"atlas {r['frames']} frames")
    elif r.get("frames"):
        bits.append(f"{r['frames']} frames")
    if r.get("duration"):
        bits.append(f"{r['duration']:.1f}s")
    if r.get("palette") is not None and r.get("media") == "visual":
        bits.append(f"{r['palette']}+ colors" if r["palette"] > 256 else f"{r['palette']} colors")
    if r.get("tris"):
        bits.append(f"{r['tris']:,} tris")
    if r.get("clips"):
        bits.append(plural(r["clips"], "clip"))
    if r.get("rigged"):
        bits.append("rigged")
    if r.get("missing"):
        bits.append(plural(r["missing"], "missing file"))
    if r.get("variants", 1) > 1:
        bits.append(r["formats"] if "," in r["formats"] else f"{r['variants']} variants")
    if not r.get("available", 1):
        bits.append("PENDING")
    return ("  (" + ", ".join(bits) + ")") if bits else ""


def _search_packs(ctx, q):
    a, con = ctx.args, ctx.con
    where, args = _pack_filters(a)
    sql = "SELECT p.* FROM packs p"
    if q:
        sql = "SELECT p.*, f.score FROM packs p JOIN (SELECT rowid, bm25(packs_fts, 10, 2, 3, 1, 3, 4) AS score FROM packs_fts WHERE packs_fts MATCH ?) f ON f.rowid=p.rid"
        args = [q] + args
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += (" ORDER BY f.score" if q else " ORDER BY p.id") + " LIMIT ?"
    args.append(a.limit)
    data = [{**dict(r), "type_counts": json.loads(r["type_counts"] or "{}"), "abs_path": ctx.abs(r["path"])} for r in con.execute(sql, args)]

    def text(_):
        for p in data:
            tc = ", ".join(f"{n} {t}" for t, n in list(p["type_counts"].items())[:4])
            print(f"{p['id']}  ·  {p['kind']}  ·  {tc}")
        if not data:
            print("no matches")

    ctx.out(data, text)


# --- show ------------------------------------------------------------------------------------

def cmd_show(ctx: Ctx):
    t = ctx.args.target
    rel = t
    lib = str(ctx.cfg.library)
    if t.startswith(lib):
        rel = t[len(lib):].lstrip("/\\").replace("\\", "/")
    r = ctx.con.execute("SELECT a.* FROM assets a JOIN asset_files af ON af.asset_id=a.id JOIN files f ON f.id=af.file_id WHERE f.path=?", (rel,)).fetchone()
    if r:
        return _show_asset(ctx, dict(r))
    if ctx.con.execute("SELECT 1 FROM packs WHERE path=?", (rel,)).fetchone():
        t = ctx.con.execute("SELECT id FROM packs WHERE path=?", (rel,)).fetchone()[0]
    _show_pack(ctx, resolve_pack(ctx.con, t))


def _show_asset(ctx, a):
    con = ctx.con
    files = [dict(r) for r in con.execute("SELECT f.path, f.ext, f.size, f.width, f.height, f.error, f.info FROM files f JOIN asset_files af ON af.file_id=f.id WHERE af.asset_id=?", (a["id"],))]
    for f in files:
        info = json.loads(f.pop("info") or "null")
        if a["media"] == "model" and info:
            refs = info.get("refs", [])
            have = {x[0] for x in con.execute(f"SELECT path FROM files WHERE path IN ({','.join('?' * len(refs))})", refs)}
            info["missing"] = [x for x in refs if x not in have]
            f["model"] = info
    p = dict(con.execute("SELECT * FROM packs WHERE id=?", (a["pack_id"],)).fetchone())
    out = _asset_out(ctx, a)
    out["files"] = [{**f, "abs_path": ctx.abs(f["path"])} for f in files]
    out["subjects"] = [dict(x) for x in con.execute("SELECT subject, score FROM asset_subjects WHERE asset_id=? ORDER BY score DESC", (a["id"],))]
    out["pack"] = {"id": p["id"], "name": p["name"], "license": license_info(ctx.cfg, p["license"]),
                   "license_file": ctx.abs(p["license_file"]), "ai_generated": p["ai_generated"], "ai_reason": p["ai_reason"]}

    def text(o):
        print(f"{o['name']}  [{o['type']}]  in {p['id']}")
        print(f"  {o['abs_path']}{_asset_bits(a)}")
        for f in o["files"]:
            print(f"  variant: {f['path']}  ({human(f['size'])}){'  ERROR ' + f['error'] if f.get('error') else ''}")
            m = f.get("model") or {}
            if m.get("clips"):
                print(f"    clips: {', '.join(m['clips'])}")
            for x in m.get("refs", []):
                print(f"    uses: {x}" + ("  MISSING" if x in m["missing"] else ""))
        if o["subjects"]:
            print("  shows: " + ", ".join(f"{x['subject']} ({x['score']:.2f})" for x in o["subjects"]) + "  (AI)")
        if a.get("atlas"):
            print(f"  atlas: {o['atlas_abs']}")
        if a.get("grid_source"):
            print(f"  grid from {a['grid_source']} (confidence {a['grid_confidence']})")
        lic = o["pack"]["license"]
        print(f"  license: {lic.get('name', lic['key'])}  ai_generated: {p['ai_generated']}")

    ctx.out(out, text)


def pack_detail(con, cfg, p: dict, full_docs: bool = False) -> dict:
    g = dict(con.execute("SELECT * FROM groups WHERE id=?", (p["group_id"],)).fetchone())
    from .indexer import descendants

    ids = descendants(con, p["id"])
    q = ",".join("?" * len(ids))
    subs = [dict(r) for r in con.execute("SELECT id, key, name, kind, file_count, asset_count FROM packs WHERE parent_id=? ORDER BY path", (p["id"],))]
    folders: dict[str, dict] = {}
    for r in con.execute(f"SELECT path, type, media FROM assets WHERE pack_id IN ({q})", ids):
        inner = r["path"][len(p["path"]) + 1:]
        parts = inner.split("/")[:-1]
        for depth in range(1, min(3, len(parts)) + 1):
            k = "/".join(parts[:depth])
            d = folders.setdefault(k, {"folder": k, "assets": 0, "types": {}})
            d["assets"] += 1
            d["types"][r["type"]] = d["types"].get(r["type"], 0) + 1
    docs = [dict(r) for r in con.execute(f"SELECT path, role, text, pack_id FROM docs WHERE pack_id IN ({q}) OR (pack_id IS NULL AND group_id=?) ORDER BY pack_id IS NULL, path", ids + [p["group_id"]])]
    # group-level docs only from folders above this pack (not from sibling sections)
    docs = [d for d in docs if d["pack_id"] or (p["path"] + "/").startswith(posixpath.dirname(d["path"]) + "/")]
    dupes = [dict(r) for r in con.execute(f"SELECT * FROM pack_dupes WHERE pack_a IN ({q}) OR pack_b IN ({q}) ORDER BY shared DESC LIMIT 10", ids + ids)]
    grids_ = [dict(r) for r in con.execute(f"SELECT path, grid_w, grid_h, grid_spacing, grid_cols, grid_rows, grid_source FROM assets WHERE pack_id IN ({q}) AND grid_w IS NOT NULL ORDER BY path LIMIT 40", ids)]
    pending_examples = [r[0] for r in con.execute(f"SELECT path FROM files WHERE pack_id IN ({q}) AND size=0 LIMIT 3", ids)]
    out = {
        **{k: v for k, v in p.items() if not k.startswith("_")},
        "abs_path": _abs(cfg, p["path"]),
        "group": {k: g[k] for k in ("id", "name", "store", "purchased", "license", "ai_generated", "ai_reason", "notes", "curation_file")},
        "license_info": license_info(cfg, p["license"]),
        "license_file_abs": _abs(cfg, p["license_file"]),
        "cover_abs": p["cover"] if p["cover_kind"] == "sheet" else _abs(cfg, p["cover"]),
        "type_counts": json.loads(p["type_counts"] or "{}"),
        "tags": json.loads(p["tags"]) if p["tags"] else [],
        "ai_tags": json.loads(p["ai_tags"]) if p["ai_tags"] else [],
        "contains": json.loads(p["contains"]) if p["contains"] else {},
        "sub_packs": subs,
        "folders": sorted(folders.values(), key=lambda d: d["folder"]),
        "docs": [{"path": d["path"], "abs_path": _abs(cfg, d["path"]), "role": d["role"], "text": d["text"] if full_docs else d["text"][:800]} for d in docs],
        "duplicates": [{"other": d["pack_b"] if d["pack_a"] in ids else d["pack_a"], "shared_files": d["shared"],
                        "share_of_this": d["frac_a"] if d["pack_a"] in ids else d["frac_b"]} for d in dupes],
        "grids": grids_,
        "pending_examples": pending_examples,
        "curation": {"file": g["curation_file"], "file_abs": str(cfg.profile.joinpath(*(g["curation_file"] or "").split("/"))) if g["curation_file"] else None,
                     "key": f'[packs."{p["key"]}"]'},
    }
    return out


def _show_pack(ctx, p):
    out = pack_detail(ctx.con, ctx.cfg, p, ctx.args.docs)
    g, grids_, pending_examples, subs = out["group"], out["grids"], out["pending_examples"], out["sub_packs"]

    def text(o):
        print(f"{o['name']}   [{o['id']}]")
        print(f"  {o['abs_path']}")
        print(f"  group: {g['name']}" + (f"  (store {g['store']}" + (f", bought {g['purchased']}" if g["purchased"] else "") + ")" if g["store"] else ""))
        print(f"  {o['role']}, kind {o['kind']} ({o['media']}), {o['asset_count']} assets in {o['file_count']} files, {human(o['total_size'])}"
              + (f", engines: {o['engines']}" if o["engines"] else ""))
        if o["pending"]:
            print(f"  PENDING: {o['pending']} files are 0-byte placeholders (not synced yet), e.g. {pending_examples[0] if pending_examples else ''}")
        print("  types: " + ", ".join(f"{n} {t}" for t, n in o["type_counts"].items()))
        li = o["license_info"]
        print(f"  license: {li.get('name', li['key'])} (from {o['license_from']})" + (f"  {li['url']}" if li.get("url") else ""))
        if li.get("summary"):
            print(f"    {li['summary']}")
        if o["license_file_abs"]:
            print(f"    file: {o['license_file_abs']}")
        print(f"  ai_generated: {o['ai_generated']}" + (f"  ({o['ai_reason']})" if o["ai_reason"] else ""))
        if o["cover_abs"]:
            print(f"  cover ({o['cover_kind']}): {o['cover_abs']}")
        if o["sheet"] and o["sheet"] != o["cover_abs"]:
            print(f"  contact sheet: {o['sheet']}")
        if o["description"]:
            print(f"  description: {o['description']}" + ("  (AI)" if o["description_source"] == "ai" else ""))
        if o["tags"] or o["ai_tags"]:
            ai = [t for t in o["tags"] if t in o["ai_tags"]]
            yours = [t for t in o["tags"] if t not in o["ai_tags"]]
            removed = [t for t in o["ai_tags"] if t not in o["tags"]]
            parts = ([", ".join(ai)] if ai else []) + ([f"yours: {', '.join(yours)}"] if yours else []) \
                + ([f"removed: {', '.join(removed)}"] if removed else [])
            print("  tags: " + "  ·  ".join(parts))
            if o["ai_model"]:
                print(f"    AI tags by {o['ai_model']}, {o['ai_at'][:10]}" + ("  (outdated: run `assetcat classify`)" if o["ai_stale"] else ""))
        if o["contains"]:
            print("  contains: " + ", ".join(f"{k} {n}" for k, n in o["contains"].items()) + "  (images, from AI subjects)")
        if o["notes"]:
            print(f"  notes: {o['notes']}")
        if subs:
            print(f"  sub-packs ({len(subs)}):")
            for s in subs[:40]:
                print(f"    {s['name']}  ·  {s['kind']}, {s['asset_count']} assets")
        if o["folders"]:
            print("  folders:")
            for d in o["folders"][:60]:
                ind = "  " * d["folder"].count("/")
                tc = ", ".join(f"{n} {t}" for t, n in sorted(d["types"].items(), key=lambda x: -x[1])[:3])
                print(f"    {ind}{posixpath.basename(d['folder'])}/  {tc}")
            if len(o["folders"]) > 60:
                print(f"    ... {len(o['folders']) - 60} more")
        if grids_:
            print("  grids:")
            for r in grids_[:12]:
                sp = f"+{r['grid_spacing']}" if r["grid_spacing"] else ""
                print(f"    {r['path'][len(p['path']) + 1:]}  {r['grid_w']}x{r['grid_h']}{sp} ({r['grid_cols']}x{r['grid_rows']}, {r['grid_source']})")
        if o["duplicates"]:
            print("  shares files with:")
            for d in o["duplicates"]:
                print(f"    {d['other']}  ({d['shared_files']} files, {d['share_of_this']:.0%} of this pack)")
        for d in o["docs"]:
            print(f"  --- {d['role']}: {d['path']}")
            body = d["text"] if ctx.args.docs else d["text"][:400]
            for line in body.splitlines()[: (None if ctx.args.docs else 8)]:
                print(f"    {line}")
        print(f"  curation: {o['curation']['file_abs'] or '(no curation file yet)'}  {o['curation']['key']}")

    ctx.out(out, text)


# --- sheet -----------------------------------------------------------------------------------

def cmd_sheet(ctx: Ctx):
    from . import sheets
    from .indexer import descendants, sheet_items, sheet_job

    a, con, cfg = ctx.args, ctx.con, ctx.cfg
    folder = a.folder
    lib = str(cfg.library)
    t = a.target
    if t.startswith(lib):
        t = t[len(lib):].lstrip("/\\").replace("\\", "/")
    row = con.execute("SELECT * FROM packs WHERE ? = path OR ? LIKE path || '/%' ORDER BY length(path) DESC LIMIT 1", (t, t)).fetchone()
    if row and t != row["path"]:
        p = dict(row)
        folder = t[len(p["path"]) + 1:] + (f"/{folder}" if folder else "")
    else:
        p = dict(row) if row else resolve_pack(con, t)
    base = f"{p['path']}/{folder.strip('/')}" if folder else p["path"]
    assets = sheet_items(con, cfg, descendants(con, p["id"]), base if folder else None)
    if not assets:
        sys.exit(f"assetcat: no viewable images under {base}" + (" (files may still be syncing)" if p["pending"] else ""))
    for x in assets:
        x["folder"] = posixpath.dirname(x["path"][len(base) + 1:])
    title = f"{p['name']} / {folder}" if folder else p["name"]
    out, job = sheet_job(cfg, f"{title}  ·  {p['id']}", assets, base, C.slugify(f"{p['id']}-{folder or ''}"), a.limit, a.cols)
    import os

    if not os.path.exists(out):
        _, err = sheets.render(job)
        if err:
            sys.exit(f"assetcat: sheet failed: {err}")
    ctx.out({"sheet": out, "pack": p["id"], "folder": folder, "images": len(assets), "shown": min(len(assets), a.limit)},
            lambda d: print(d["sheet"]))


# --- pull ------------------------------------------------------------------------------------

def cmd_pull(ctx: Ctx):
    from .pull import PullError, run_pull

    a = ctx.args
    try:
        r = run_pull(ctx.cfg, ctx.con, a.targets, a.to, manifest=a.manifest, license_name=a.license_name,
                     license_lf=a.license_lf, require_ai=a.require_ai, force=a.force, overwrite=a.overwrite,
                     flatten=a.flatten, dry_run=a.dry_run)
    except PullError as e:
        sys.exit(f"assetcat: {e}")

    def text(o):
        verb = "would copy" if o["dry_run"] else "copied"
        n = o["new"] + o["replaced"]
        print(f"{verb} {n} files to {o['to']}" + (f" ({o['replaced']} replaced)" if o["replaced"] else "")
              + (f", {o['unchanged']} already there" if o["unchanged"] else ""))
        for lic in o["licenses"]:
            print(f"license: {lic['dest']}" + ("  (already there)" if lic["status"] == "unchanged" else ""))
        for w in o["warnings"]:
            print(f"warning: {w}")
        no_file = [p["name"] for p in o["packs"] if not p["license_file"]]
        if no_file:
            print("no license file found for: " + ", ".join(no_file) + " (see the license in curation)")
        if o["manifest"]:
            print(f"manifest: {o['manifest']}" + (" (not written: dry run)" if o["dry_run"] else ""))
        print("credits:")
        for p in o["packs"]:
            ai = {"no": "human-made", "yes": "AI-generated"}.get(p["ai_generated"], "AI status unknown")
            print(f"  {p['credit']}  [{ai}]")

    ctx.out(r, text)


# --- doctor / stats --------------------------------------------------------------------------

FIXES = {
    "profile_placeholders": "They haven't synced yet. Make the profile folder available offline "
        "(Dropbox: right-click, Make available offline) and wait for the sync.",
    "curation_problems": "Edit the named file (plain TOML). A license must be a key in licenses.toml: "
        "add it there or fix the spelling. A file that doesn't parse is ignored until it's fixed.",
    "unused_curation_keys": "The folder was renamed, moved or deleted. Rename the [packs.\"<key>\"] table "
        "to the folder's new key (the Layout page and `assetcat packs --tree` show keys), or delete it.",
    "key_collisions": "These sibling folders' names give the same key (e.g. \"Tiles!\" and \"tiles\"), so all "
        "but the first get a short suffix. Their keys are stable, but a new folder with a clashing name that sorts "
        "first would take the plain key. Rename a folder to keep the keys unambiguous.",
    "missing_sections": "Fix the section's `folder` in catalog.toml (relative to the library), or remove the section.",
    "license_unknown": "Set the license of the whole bundle or store: in the browse UI (`assetcat serve`) open "
        "one of its packs, Edit curation, Bundle defaults; or set `license` in its curation file. Packs that "
        "differ get their own. Licenses missing from the list go in licenses.toml.",
    "ai_unknown": "Set AI-generated where the license goes (Bundle defaults in the pack editor, or "
        "`ai_generated` in the curation file): no = human-made, with the reason in `ai_reason`, e.g. "
        "\"released in 2018, before generative art tools\" or \"made by the store's in-house artists\".",
    "packs_nearly_empty": "Often an incomplete download or extraction: re-download it. If the folder isn't a "
        "pack, set its role to grouping or ignore on the Layout page.",
    "pending": "Files not synced yet (0 bytes, e.g. Dropbox online-only). Make the folders available offline, "
        "wait for the sync, then run `assetcat index`.",
    "ai_tags": "Run `assetcat classify` on the machine with the model server; it only tags these. Outdated "
        "means the pack's files or the tag vocabulary (tags.toml) changed since.",
    "ai_vectors": "Run `assetcat classify --kind embed` (it only embeds new images and re-encodes the subject "
        "list from tags.toml), then search by meaning and subjects cover them.",
    "model_refs": "The model points at files that aren't there (often the artist's own paths, or a texture "
        "folder left out of the download); it shows untextured. `assetcat show <path>` lists them. Find the "
        "textures in the pack or re-download it.",
    "text_encoding": "The text isn't UTF-8 and no likely encoding fits, so it was read as Windows-1252 and may show "
        "wrong characters (in search too). Open the file in an editor that can pick the encoding to read the "
        "license terms, then set the pack's license in its curation.",
    "errors": "Usually a damaged file or an unusual format variant. Re-download the pack if you need them; "
        "`assetcat index` retries them every time.",
}


def cmd_doctor(ctx: Ctx):
    """What needs attention, each problem with how to fix it."""
    con, cfg = ctx.con, ctx.cfg
    cur = load_curation(cfg)
    from .init import placeholders
    from .scan import build_layout

    lay = build_layout(cfg, cur)
    groups = {r["id"]: dict(r) for r in con.execute("SELECT * FROM groups ORDER BY id")}
    counts = con.execute("""SELECT group_id, COUNT(*) n, SUM(license='unknown') lic, SUM(ai_generated='unknown') ai
                            FROM packs WHERE parent_id IS NULL GROUP BY group_id ORDER BY group_id""").fetchall()

    def per_group(col):
        return [f"{r['group_id']}  ({r[col]} of {plural(r['n'], 'pack')})  {groups[r['group_id']]['curation_file'] or ''}".rstrip()
                for r in counts if r[col]]

    pending = con.execute("SELECT group_id, COUNT(*) FROM files WHERE size=0 GROUP BY group_id ORDER BY group_id").fetchall()
    n_err = con.execute("SELECT COUNT(*) FROM files WHERE error IS NOT NULL").fetchone()[0]
    ai_items = []
    if con.execute("SELECT 1 FROM packs WHERE ai_model IS NOT NULL LIMIT 1").fetchone():  # only once tagging has started
        packs = [dict(r) for r in con.execute("SELECT * FROM packs")]
        results, version = A.load_results(cfg), A.build_prompt(A.load_vocab(cfg))[2]  # live: tags.toml may have changed
        missing = sum(1 for p in packs if A.taggable(p) and p["id"] not in results)
        stale = sum(1 for p in packs if A.taggable(p) and p["id"] in results and not A.is_fresh(results[p["id"]], p, version))
        ai_items = ([f"{plural(missing, 'pack')} without AI tags"] if missing else []) + ([f"{plural(stale, 'pack')} with outdated AI tags"] if stale else [])
    found = [
        ("profile_placeholders", "profile files that are 0 bytes", placeholders(cfg.profile)),
        ("curation_problems", "curation file problems", cur.problems),
        ("unused_curation_keys", "curation keys matching no folder", lay.unused_curation),
        ("missing_sections", "section folders not found", lay.missing_sections),
        ("key_collisions", "folders whose names give the same key", lay.key_collisions),
        ("license_unknown", "groups with packs whose license is unknown", per_group("lic")),
        ("ai_unknown", "groups with packs whose AI status is unknown", per_group("ai")),
        ("packs_nearly_empty", "packs with at most 2 files",
         [f"{r[0]}  ({plural(r[1], 'file')})" for r in con.execute("SELECT id, file_count FROM packs WHERE file_count <= 2 ORDER BY id")]),
        ("ai_tags", "packs to tag", ai_items),
        ("ai_vectors", "image vectors and subjects", _vector_items(con, cfg)),
        ("model_refs", "3D models whose textures or buffers aren't in the library",
         [f"{r[0]}  ({r[1]} missing)" for r in con.execute("SELECT path, missing FROM assets WHERE missing > 0 ORDER BY path LIMIT 200")]),
        ("text_encoding", "readmes and licenses whose text encoding was guessed",
         [r[0] for r in con.execute("SELECT path FROM files WHERE category='doc' AND info LIKE '%\"guessed\": true%' ORDER BY path LIMIT 200")]),
        ("pending", "files not synced yet, by group", [f"{n:>7}  {g}" for g, n in pending]),
        ("errors", f"files that failed to read ({n_err})",
         [f"{r[0]}: {r[1]}" for r in con.execute("SELECT path, error FROM files WHERE error IS NOT NULL ORDER BY path LIMIT 50")]),
    ]
    out = {
        "profile": str(cfg.profile),
        "indexed_at": (con.execute("SELECT value FROM meta WHERE key='indexed_at'").fetchone() or [None])[0],
        "issues": [{"id": i, "title": t, "fix": FIXES[i], "items": items} for i, t, items in found if items],
    }

    def text(o):
        print(f"index built {o['indexed_at']}; profile {o['profile']}")
        if not o["issues"]:
            print("\nNothing needs attention.")
        for i in o["issues"]:
            items = i["items"]
            print(f"\n{i['title']} ({len(items)}):" if i["id"] != "errors" else f"\n{i['title']}:")
            for x in items[:20]:
                print(f"  {x}")
            if len(items) > 20:
                print(f"  ... {len(items) - 20} more (--json for all)")
            print(textwrap.fill(i["fix"], 100, initial_indent="  fix: ", subsequent_indent="       "))

    ctx.out(out, text)


def cmd_classify(ctx: Ctx):
    """Tag packs with a vision-language model; results go to the profile's ai/ folder."""
    import random
    import time

    from .edit import refresh

    a, con, cfg = ctx.args, ctx.con, ctx.cfg
    uc = read_user_config()
    if a.ai_kind == "embed":
        return _classify_embed(ctx, uc)
    url = a.url or uc.get("ai_url") or A.DEFAULT_URL
    model = a.model or uc.get("ai_model")
    client = A.Client(url, model or "")
    try:
        if not model:
            try:
                have = "\n  models on the server: " + ", ".join(client.models())
            except A.AIError:
                have = ""
            sys.exit(f"assetcat: no model set. Pass --model, or set one for this machine:\n"
                     f"  assetcat config ai_model {A.RECOMMENDED_MODEL}\n"
                     f"  (the recommended model; see the README for smaller ones){have}")
        client.check()
    except A.AIError as e:
        sys.exit(f"assetcat: {e}")

    vocab = A.load_vocab(cfg)
    prompt, schema, version = A.build_prompt(vocab)
    if a.pack:
        ids = list(dict.fromkeys(i for q in a.pack for i in pack_scope(con, q)))
        packs = [dict(con.execute("SELECT * FROM packs WHERE id=?", (i,)).fetchone()) for i in ids]
    else:
        where, args = _pack_filters(a)
        packs = [dict(r) for r in con.execute("SELECT p.* FROM packs p" + (" WHERE " + " AND ".join(where) if where else "")
                                              + " ORDER BY p.group_id, p.path", args)]
    packs = [p for p in packs if A.taggable(p)]
    save = not a.sample
    if a.sample:
        packs = random.Random().sample(packs, min(a.sample, len(packs)))
    elif not a.redo:
        done = A.load_results(cfg, model)
        packs = [p for p in packs if not A.is_fresh(done.get(p["id"]), p, version)]
    if a.limit:
        packs = packs[: a.limit]
    if not packs:
        print("Nothing to tag: every pack has up-to-date tags from this model (--redo to tag them again).")
        return
    machine = A.machine_name(uc)
    con.execute("PRAGMA busy_timeout=10000")
    print(f"{plural(len(packs), 'pack')} with {model} at {url}" + ("" if save else " (sample: nothing is saved)"), file=sys.stderr)

    results, failed, t_start = [], [], time.time()
    try:
        for n, p in enumerate(packs, 1):
            t = time.time()
            try:
                text, images = A.pack_input(con, cfg, p)
                answer, usage = client.tag(prompt, schema, text, images)
            except A.AIError as e:
                failed.append({"pack": p["id"], "error": str(e)})
                print(f"[{n}/{len(packs)}] {p['id']}: FAILED {e}", file=sys.stderr)
                if n == len(failed) >= 3:
                    sys.exit("assetcat: the first 3 packs failed; stopping")
                continue
            got = A.parse_answer(answer, vocab)
            rec = {"pack": p["id"], "model": model, "at": A.now(), "prompt": version, "files": p["file_count"],
                   "seconds": round(time.time() - t, 1), **got}
            results.append(rec)
            if save:
                A.append_result(cfg, model, machine, rec)
                con.execute("UPDATE packs SET ai_tags=?, ai_model=?, ai_at=?, ai_stale=0, ai_description=? WHERE id=?",
                            (json.dumps(rec["tags"]), model, rec["at"], rec["description"], p["id"]))
                refresh(cfg, con)  # effective tags and description, live in the browse UI
            left = (time.time() - t_start) / n * (len(packs) - n)
            eta = f"  (~{left / 60:.0f} min left)" if n % 10 == 0 and left > 90 else ""
            if not a.json:
                print(f"[{n}/{len(packs)}] {rec['seconds']:4.1f}s  {p['name']}: {' '.join(rec['tags']) or '(no tags)'}{eta}", flush=True)
                if a.sample and rec["description"]:
                    print(f"           {rec['description']}")
    except KeyboardInterrupt:
        print("\nstopped; what's done is saved, and the next run continues from there" if save else "\nstopped", file=sys.stderr)
    if a.json:
        ctx.out({"model": model, "saved": save, "results": results, "failed": failed}, lambda _: None)
    if save and results:
        print(f"tagged {plural(len(results), 'pack')}" + (f", {len(failed)} failed" if failed else "")
              + f"; results in {A.shard_path(cfg, model, machine).relative_to(cfg.profile).as_posix()} (profile)", file=sys.stderr)
        if not a.no_index:
            from .indexer import run_index

            print("updating the index so search finds the new tags...", file=sys.stderr)
            con.close()
            run_index(cfg)


def _classify_embed(ctx: Ctx, uc: dict):
    from . import embed as M

    a, con, cfg = ctx.args, ctx.con, ctx.cfg
    model = M.model_name(uc, a.model)
    where, args = _pack_filters(a)
    if a.pack:
        ids = list(dict.fromkeys(i for q in a.pack for i in pack_scope(con, q)))
        where.append(f"p.id IN ({','.join('?' * len(ids))})"); args += ids
    ext = ",".join(f"'{e}'" for e in sorted(M.LOADABLE))
    sql = (f"SELECT a.path, a.type, a.width, a.height, a.grid_w, a.grid_h, a.grid_spacing, a.grid_margin, a.grid_cols, a.grid_rows, "
           f"f.size, f.id AS file_id, f.hash FROM assets a JOIN files f ON f.path=a.path JOIN packs p ON p.id=a.pack_id "
           f"WHERE a.media='visual' AND a.available=1 AND f.ext IN ({ext})" + "".join(f" AND {w}" for w in where) + " ORDER BY a.path")
    rows = [dict(r) for r in con.execute(sql, args)]
    if a.limit:
        rows = rows[: a.limit]
    say = lambda m: print(m, file=sys.stderr, flush=True)  # noqa: E731
    vocab = A.load_vocab(cfg)
    try:
        st = M.run(cfg, con, model, A.machine_name(uc), rows, progress=say)
        st["subjects_updated"] = False
        if not M.subject_texts_current(cfg, model, vocab):
            say("encoding the subject vocabulary (tags.toml)...")
            st["subjects_updated"] = M.update_subject_texts(cfg, model, vocab, M.Encoder(model))
    except M.EmbedError as e:
        sys.exit(f"assetcat: {e}")
    if a.json:
        ctx.out(st, lambda _: None)
    else:
        print(f"embedded {st['embedded']:,} images" + (f" ({st['unreadable']} unreadable)" if st["unreadable"] else "")
              + f"; {st['already']:,} already had vectors (or are duplicates)")
    if (st["embedded"] or st["subjects_updated"]) and not a.no_index:
        from .indexer import run_index

        say("updating the index (subjects, search)...")
        con.close()
        run_index(cfg)


def _semantic(ctx: Ctx, where: list, args: list) -> list[tuple[int, float]]:
    """(asset id, score) for --meaning / --similar, best first, among assets matching the filters."""
    from . import embed as M

    a, con, cfg = ctx.args, ctx.con, ctx.cfg
    model = M.model_name(read_user_config())
    vecs = M.Vectors(cfg, con, model)
    if not len(vecs):
        sys.exit(f"assetcat: no image vectors for {model} yet; run `assetcat classify --kind embed` first")
    if a.similar:
        rel = a.similar
        lib = str(cfg.library)
        if rel.startswith(lib):
            rel = rel[len(lib):].lstrip("/\\").replace("\\", "/")
        r = con.execute("SELECT id FROM assets WHERE path=? OR path=(SELECT a2.path FROM asset_files af JOIN files f ON f.id=af.file_id "
                        "JOIN assets a2 ON a2.id=af.asset_id WHERE f.path=?)", (rel, rel)).fetchone()
        q = vecs.vector(r[0]) if r else None
        if q is None and r:  # a model: its bundled render has the vector
            x = con.execute("SELECT id FROM assets WHERE preview_of=?", (r[0],)).fetchone()
            q = vecs.vector(x[0]) if x else None
        if q is None:
            sys.exit(f"assetcat: no vector for '{a.similar}' (not an indexed image, or not embedded yet)")
    else:
        if not a.query:
            sys.exit("assetcat: --meaning needs words: what the image should show")
        try:
            q = M.Encoder(model).texts([" ".join(a.query)])[0]
        except M.EmbedError as e:
            sys.exit(f"assetcat: search by meaning {e}")
    only = {r[0] for r in con.execute("SELECT a.id FROM assets a JOIN packs p ON p.id=a.pack_id WHERE " + " AND ".join(where), args)}
    render_of = {r[0]: r[1] for r in con.execute("SELECT id, preview_of FROM assets WHERE preview_of IS NOT NULL")}
    only |= {rid for rid, mid in render_of.items() if mid in only}
    out, seen = [], set()
    for i, sc in vecs.rank(q, only, a.limit * 2):
        i = render_of.get(i, i)
        if i not in seen:
            seen.add(i)
            out.append((i, sc))
    return out[:a.limit]


def _vector_items(con, cfg) -> list[str]:
    """Only once image vectors exist: what a `classify --kind embed` would still do."""
    from . import embed as M

    model = M.model_name(read_user_config())
    if not M.shards(cfg, model):
        return []
    out = []
    if not M.subject_texts_current(cfg, model, A.load_vocab(cfg)):
        out.append("the subject list in tags.toml changed since the subjects were encoded")
    done = M.done_hashes(cfg, model)
    ext = ",".join(f"'{e}'" for e in sorted(M.LOADABLE))
    missing = sum(1 for (h,) in con.execute(f"SELECT f.hash FROM assets a JOIN files f ON f.path=a.path WHERE a.media='visual' "
                                            f"AND a.available=1 AND f.ext IN ({ext})") if not h or h not in done)
    if missing:
        out.append(f"{plural(missing, 'image')} without a vector (new, changed, or not hashed yet)")
    return out


def cmd_stats(ctx: Ctx):
    con = ctx.con
    out = {
        "files": con.execute("SELECT COUNT(*), SUM(size) FROM files").fetchone()[:],
        "by_section": {r[0]: {"packs": r[1], "assets": r[2]} for r in con.execute(
            "SELECT p.section, COUNT(DISTINCT p.id), COUNT(a.id) FROM packs p LEFT JOIN assets a ON a.pack_id=p.id GROUP BY p.section")},
        "packs_by_kind": {r[0]: r[1] for r in con.execute("SELECT kind, COUNT(*) FROM packs WHERE parent_id IS NULL GROUP BY kind ORDER BY 2 DESC")},
        "assets_by_type": {r[0]: r[1] for r in con.execute("SELECT type, COUNT(*) FROM assets GROUP BY type ORDER BY 2 DESC")},
        "files_by_category": {r[0]: r[1] for r in con.execute("SELECT category, COUNT(*) FROM files GROUP BY category ORDER BY 2 DESC")},
        "with_grid": con.execute("SELECT COUNT(*) FROM assets WHERE grid_w IS NOT NULL").fetchone()[0],
        "duplicate_pack_pairs": con.execute("SELECT COUNT(*) FROM pack_dupes").fetchone()[0],
        "db": str(ctx.cfg.db_path),
        "library": str(ctx.cfg.library),
        "profile": str(ctx.cfg.profile),
    }

    def text(o):
        print(f"{o['files'][0]} files, {human(o['files'][1])}   db: {o['db']}")
        for s, d in o["by_section"].items():
            print(f"  {s}: {d['packs']} packs, {d['assets']} assets")
        print("top-level packs by kind: " + ", ".join(f"{k} {n}" for k, n in o["packs_by_kind"].items()))
        print("assets by type: " + ", ".join(f"{k} {n}" for k, n in o["assets_by_type"].items()))
        print("files by category: " + ", ".join(f"{k} {n}" for k, n in o["files_by_category"].items()))
        print(f"assets with a known grid: {o['with_grid']}; pack pairs sharing files: {o['duplicate_pack_pairs']}")

    ctx.out(out, text)


if __name__ == "__main__":
    main()
