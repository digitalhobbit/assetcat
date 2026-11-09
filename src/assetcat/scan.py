"""Walk the library and turn folders into groups and a tree of packs."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from . import classify as C
from .config import Config, Section, fs_path
from .curation import Curation


@dataclass
class FileEntry:
    rel: str
    name: str
    size: int
    mtime: float


@dataclass
class Dir:
    rel: str
    name: str
    files: list[FileEntry] = field(default_factory=list)
    dirs: list["Dir"] = field(default_factory=list)
    _count: int = -1

    def file_count(self) -> int:
        if self._count < 0:
            self._count = len(self.files) + sum(d.file_count() for d in self.dirs)
        return self._count

    def iter_files(self):
        yield from self.files
        for d in self.dirs:
            yield from d.iter_files()


def scan_dir(cfg: Config, rel: str, name: str, ignore: set[str] = frozenset()) -> Dir:
    node = Dir(rel=rel, name=name)
    try:
        it = list(os.scandir(fs_path(cfg.abs(rel))))
    except OSError:
        return node
    for e in sorted(it, key=lambda e: e.name.lower()):
        if e.name in cfg.junk_files or e.name.startswith("._"):
            continue
        child_rel = f"{rel}/{e.name}"
        try:
            if e.is_dir(follow_symlinks=False):
                if e.name in cfg.junk_dirs or e.name in ignore:
                    continue
                node.dirs.append(scan_dir(cfg, child_rel, e.name))
            elif e.is_file(follow_symlinks=False):
                st = e.stat(follow_symlinks=False)
                node.files.append(FileEntry(child_rel, e.name, st.st_size, st.st_mtime))
        except OSError:
            continue
    return node


@dataclass
class Group:
    id: str
    section: str
    rel: str
    name: str
    loose: list[FileEntry] = field(default_factory=list)  # files not inside any pack


@dataclass
class Pack:
    id: str
    key: str  # curation key: slug path relative to the group
    group_id: str
    section: str
    rel: str
    name: str
    role: str  # "pack" | "collection"
    parent_id: str | None
    depth: int  # 0 = top-level pack in its group
    files: list[FileEntry] = field(default_factory=list)  # files owned directly (not by sub-packs)
    dir: Dir | None = None


@dataclass
class Layout:
    groups: dict[str, Group] = field(default_factory=dict)
    packs: dict[str, Pack] = field(default_factory=dict)
    unused_curation: list[str] = field(default_factory=list)  # "group: key" with no matching folder
    missing_sections: list[str] = field(default_factory=list)


def _norm(name: str) -> str:
    return name.strip().lstrip("_ ").lower()


_COLLECTION_NAME = re.compile(r"(bundle|mega ?pack|collection|\bkit\b|-kit\b|packs\b)", re.I)


def _pack_signal(d: Dir, weak: bool) -> bool:
    """Does this folder look like a pack of its own? Strong signals: its own preview images or
    license/readme. Weak (only for folders named like collections): format sub-folders
    (AI/EPS/PNG, Icons_png) or a name with pack/kit/set/bundle in it."""
    for f in d.files:
        cat = C.category(C.ext_of(f.name))
        if cat == "doc" and C.doc_role(f.name):
            return True
        if cat == "image" and C.is_preview_name(C.stem_of(f.name)):
            return True
    if not weak:
        return False
    if any(C.FORMAT_FOLDER.match(c.name.strip()) for c in d.dirs):
        return True
    return bool(set(C.words(d.name)) & {"pack", "bundle", "kit", "set"})


def _is_collection(d: Dir) -> bool:
    subs = [c for c in d.dirs if c.file_count() > 0]
    if len(subs) < 2 or d.file_count() < 150:
        return False
    if _COLLECTION_NAME.search(d.name):
        return sum(1 for c in subs if _pack_signal(c, True)) >= max(2, 0.5 * len(subs))
    return len(subs) >= 3 and sum(1 for c in subs if _pack_signal(c, False)) >= 0.7 * len(subs)


def build_layout(cfg: Config, cur: Curation) -> Layout:
    lay = Layout()
    used: set[tuple[str, str]] = set()
    for sec in cfg.sections:
        root = cfg.abs(sec.folder)
        if not root.is_dir():
            lay.missing_sections.append(sec.name)
            continue
        top = scan_dir(cfg, sec.folder, sec.folder, set(sec.ignore))
        if sec.layout == "single-group":
            _add_group(cfg, lay, cur.group(sec.name).packs, sec, top, sec.name, used)
        else:
            for gdir in top.dirs:
                if gdir.file_count() == 0:
                    continue
                gid = f"{sec.name}/{C.slugify(gdir.name)}"
                _add_group(cfg, lay, cur.group(gid).packs, sec, gdir, gid, used)
            # loose files directly in the section folder are ignored
    for gid, gc in cur.groups.items():
        for key in gc.packs:
            if (gid, key) not in used:
                lay.unused_curation.append(f"{gid}: {key}")
    return lay


def _add_group(cfg, lay: Layout, packs: dict[str, dict], sec: Section, gdir: Dir, gid: str, used,
               trace: dict | None = None):
    """Add one group and its pack tree. `packs` is the group's [packs."<key>"] curation. With
    `trace`, records every folder that got a role: key -> {ctx, auto, set, role}, where ctx is
    "top" (outside any pack) or "collection" (inside one), auto the detected role and set the
    curated one."""
    g = Group(id=gid, section=sec.name, rel=gdir.rel, name=gdir.name)
    lay.groups[gid] = g
    g.loose.extend(gdir.files)

    def child_key(parent_key: str, d: Dir) -> str:
        s = C.slugify(d.name)
        return f"{parent_key}/{s}" if parent_key else s

    def detect(c: Dir, depth_left: int | None) -> str:
        if depth_left is not None:
            return "grouping" if depth_left > 1 else "pack"
        if _norm(c.name) in cfg.grouping_names:
            return "grouping"
        return "collection" if _is_collection(c) else "pack"

    def container(d: Dir, key: str, depth_left: int | None):
        for c in d.dirs:
            if c.file_count() == 0:
                continue
            k = child_key(key, c)
            ov = packs.get(k, {})
            if ov:
                used.add((gid, k))
            role = ov.get("role")
            if trace is not None:
                trace[k] = {"ctx": "top", "auto": detect(c, depth_left), "set": role}
                trace[k]["role"] = role or trace[k]["auto"]
            if role == "ignore":
                continue
            if role is None:
                role = trace[k]["auto"] if trace is not None else detect(c, depth_left)
            if role == "grouping":
                g.loose.extend(c.files)
                container(c, k, depth_left - 1 if depth_left is not None else None)
            else:
                make_pack(c, k, role, None, 0)

    def make_pack(d: Dir, key: str, role: str, parent: Pack | None, depth: int):
        pid = f"{gid}/{key}"
        p = Pack(id=pid, key=key, group_id=gid, section=sec.name, rel=d.rel, name=d.name,
                 role=role, parent_id=parent.id if parent else None, depth=depth, dir=d)
        lay.packs[pid] = p
        p.files.extend(d.files)
        if role != "collection":
            p.files.extend(f for sub in d.dirs for f in sub.iter_files())
            return
        for c in d.dirs:
            if c.file_count() == 0:
                continue
            k = child_key(key, c)
            ov = packs.get(k, {})
            if ov:
                used.add((gid, k))
            r = ov.get("role")
            if trace is not None:
                auto = "collection" if _is_collection(c) and _COLLECTION_NAME.search(c.name) else "pack"
                trace[k] = {"ctx": "collection", "auto": auto, "set": r,
                            "role": "pack" if r == "grouping" else r or auto}
            if r == "ignore":
                continue
            if r in (None, "grouping"):
                r = "collection" if r is None and _is_collection(c) and _COLLECTION_NAME.search(c.name) else "pack"
            make_pack(c, k, r, p, depth + 1)

    container(gdir, "", sec.pack_depth)


# --- one group's folder tree, for the layout editor ------------------------------------------

def find_group(cfg: Config, gid: str) -> tuple[Section, Dir] | None:
    """The section and scanned folder of a group, scanning only that group."""
    sec_name, _, slug = gid.partition("/")
    sec = next((s for s in cfg.sections if s.name == sec_name), None)
    if sec is None or not cfg.abs(sec.folder).is_dir():
        return None
    if sec.layout == "single-group":
        return (sec, scan_dir(cfg, sec.folder, sec.folder, set(sec.ignore))) if not slug else None
    try:
        names = sorted(e.name for e in os.scandir(fs_path(cfg.abs(sec.folder))) if e.is_dir(follow_symlinks=False))
    except OSError:
        return None
    for n in names:
        if n not in cfg.junk_dirs and n not in sec.ignore and C.slugify(n) == slug:
            return sec, scan_dir(cfg, f"{sec.folder}/{n}", n)
    return None


def group_tree(cfg: Config, sec: Section, gdir: Dir, gid: str, packs: dict[str, dict]) -> dict:
    """The group's folders as a tree, each with the role it gets under `packs` (curation):
    grouping | pack | collection | ignore, or "content" for a folder inside a pack (listed one
    level deep). Returns {"tree": [...], "packs": {pack id: role}}."""
    lay, trace = Layout(), {}
    _add_group(cfg, lay, packs, sec, gdir, gid, set(), trace)
    owner = {p.rel: p for p in lay.packs.values()}

    def node(d: Dir, key: str) -> dict:
        n = {"name": d.name, "key": key, "files": d.file_count(), "role": "content"}
        if key in trace:
            n.update(trace[key])
            n["pack"] = owner[d.rel].id if d.rel in owner else None
        kids = [c for c in d.dirs if c.file_count() > 0]
        if n["role"] in ("grouping", "collection", "pack"):
            n["kids"] = [node(c, f"{key}/{C.slugify(c.name)}") for c in kids]
        else:  # inside a pack, or ignored: not expanded further
            n["kids"], n["more"] = [], len(kids)
        return n

    tree = [node(c, C.slugify(c.name)) for c in gdir.dirs if c.file_count() > 0]
    return {"tree": tree, "packs": {p.id: p.role for p in lay.packs.values()}}
