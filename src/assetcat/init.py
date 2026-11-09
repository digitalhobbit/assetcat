"""`assetcat init <library>`: record the library in this machine's user config, and create a
starter profile (<library>/assetcat/) if the library has none yet.

A second machine finds the existing profile and only records the path. Nothing in the library is
written except the profile folder.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from . import classify as C
from .config import (CATALOG_FILE, PROFILE_DIRNAME, STARTER_DIR, ConfigError, ensure_inside, fs_path,
                     resolve_paths, write_user_config)

JUNK_FILES = [".DS_Store", "Thumbs.db", "desktop.ini", "Icon\r"]
JUNK_DIRS = ["__MACOSX", ".git", ".svn", "__pycache__"]
GROUPING_NAMES = ["graphics", "audio", "sounds", "sound", "music", "sfx", "art", "2d", "3d"]
_KENNEY_ALL_IN_ONE = re.compile(r"kenney.*(all[ -]?in[ -]?1|all[ -]?in[ -]?one)", re.I)


# --- layout detection --------------------------------------------------------------------------

def _listdir(p: Path) -> tuple[list[str], list[str]]:
    """(dirs, files) directly in p, junk and hidden names dropped."""
    dirs, files = [], []
    try:
        entries = list(os.scandir(fs_path(p)))
    except OSError:
        return dirs, files
    for e in sorted(entries, key=lambda e: e.name.lower()):
        if e.name.startswith(".") or e.name in JUNK_FILES or e.name in JUNK_DIRS:
            continue
        try:
            (dirs if e.is_dir(follow_symlinks=False) else files).append(e.name)
        except OSError:
            continue
    return dirs, files


def _looks_like_pack_root(files: list[str]) -> bool:
    """A pack's own root usually has a preview image or a license/readme next to it."""
    for f in files:
        cat = C.category(C.ext_of(f))
        if cat == "doc" and C.doc_role(f):
            return True
        if cat == "image" and C.is_preview_name(C.stem_of(f)):
            return True
    return False


def _pack_children(p: Path, depth: int = 0) -> int:
    """How many folders below p (through grouping folders like Graphics/) look like packs."""
    dirs, _ = _listdir(p)
    n = 0
    for d in dirs:
        if d.strip().lstrip("_ ").lower() in GROUPING_NAMES and depth < 2:
            n += _pack_children(p / d, depth + 1)
        elif _looks_like_pack_root(_listdir(p / d)[1]):
            n += 1
    return n


def _is_container(p: Path) -> bool:
    """Does this folder hold packs (a bundle, a store) rather than being a pack? Yes if several
    folders below it look like packs, or if it's bare: no media, previews, docs or format folders
    of its own, only sub-folders."""
    if _pack_children(p) >= 2:
        return True
    dirs, files = _listdir(p)
    if _looks_like_pack_root(files) or any(C.MEDIA.get(C.category(C.ext_of(f))) for f in files):
        return False
    return bool(dirs) and not any(C.FORMAT_FOLDER.match(d.strip()) for d in dirs)


def detect_sections(library: Path, profile: Path) -> list[dict]:
    """One section per top-level folder: `groups` when its folders hold packs of their own (bundles,
    stores), `single-group` when its folders are the packs. Kenney's all-in-1 is one group with
    packs at <category>/<pack>."""
    dirs, _ = _listdir(library)
    prof_name = profile.name if profile.parent == library else None
    out, used = [], set()
    for d in dirs:
        if d == prof_name or d == PROFILE_DIRNAME:
            continue
        sub, _ = _listdir(library / d)
        if not sub:
            continue
        name = C.slugify(d)
        if _KENNEY_ALL_IN_ONE.search(d):
            name = "kenney"
        base, k = name, 2
        while name in used:
            name, k = f"{base}-{k}", k + 1
        used.add(name)
        sec = {"name": name, "folder": d}
        if _KENNEY_ALL_IN_ONE.search(d):
            sec.update(layout="single-group", pack_depth=2, why="Kenney all-in-1: packs at <category>/<pack>")
            if "Archive" in sub:
                sec["ignore"] = ["Archive"]
        else:
            containers = sum(1 for s in sub if _is_container(library / d / s))
            if containers >= max(1, len(sub) / 2):
                sec.update(layout="groups", why=f"{containers} of {len(sub)} folders hold packs")
            else:
                sec.update(layout="single-group", why=f"{len(sub) - containers} of {len(sub)} folders look like packs")
        out.append(sec)
    return out


def _toml_str(s: str) -> str:
    import tomlkit

    return tomlkit.string(s).as_string()


def render_catalog(sections: list[dict]) -> str:
    lines = [
        "# Library layout for assetcat. Shared by every machine that uses this library.",
        "# Folders in the library root that aren't listed as a section are not indexed.",
        "# layout = \"groups\": each folder in the section is a group (a bundle, a store) holding packs.",
        "# layout = \"single-group\": the section is one group; pack_depth = N puts packs N folders down.",
        "# ignore = [...]: folder names inside the section to skip.",
        "format = 1",
        "",
    ]
    for s in sections:
        lines.append(f"# {s['why']}" if s.get("why") else "")
        lines.append("[[sections]]")
        lines.append(f"name = {_toml_str(s['name'])}")
        lines.append(f"folder = {_toml_str(s['folder'])}")
        lines.append(f"layout = {_toml_str(s['layout'])}")
        if s.get("pack_depth"):
            lines.append(f"pack_depth = {s['pack_depth']}")
        if s.get("ignore"):
            lines.append("ignore = [" + ", ".join(_toml_str(x) for x in s["ignore"]) + "]")
        lines.append("")
    lines += [
        "[scan]",
        "# Never indexed.",
        "junk_files = [" + ", ".join(_toml_str(x) for x in JUNK_FILES) + "]",
        "junk_dirs = [" + ", ".join(_toml_str(x) for x in JUNK_DIRS) + "]",
        "# Folders directly inside a group (or another grouping folder) with these names hold packs",
        "# rather than being packs. Compared lowercase, ignoring leading \"_\" and spaces.",
        "grouping_names = [" + ", ".join(_toml_str(x) for x in GROUPING_NAMES) + "]",
        "",
    ]
    return "\n".join(lines)


# --- init --------------------------------------------------------------------------------------

def placeholders(profile: Path) -> list[str]:
    """Profile files that are 0 bytes: Dropbox (or similar) online-only placeholders."""
    out = []
    if profile.is_dir():
        for root, _, files in os.walk(profile):
            for f in files:
                p = Path(root) / f
                if p.suffix == ".toml" and p.stat().st_size == 0:
                    out.append(p.relative_to(profile).as_posix())
    return sorted(out)


def run_init(library: str | None, profile: str | None, dry_run: bool = False):
    lib, prof, uc = resolve_paths(library, profile)
    if lib is None:
        raise ConfigError("usage: assetcat init <library folder>")
    if not lib.is_dir():
        raise ConfigError(f"Library folder not found: {lib}")
    default_prof = prof == lib / PROFILE_DIRNAME
    catalog = prof / CATALOG_FILE

    if catalog.exists():
        print(f"profile found: {prof}")
        created = False
    else:
        sections = detect_sections(lib, prof)
        if not sections:
            raise ConfigError(f"no folders with content in {lib}")
        print(f"new profile for {lib}" + (" (dry run, nothing written)" if dry_run else ""))
        for s in sections:
            extra = f", pack_depth {s['pack_depth']}" if s.get("pack_depth") else ""
            print(f"  section {s['name']}: {s['folder']}/  ({s['layout']}{extra}; {s['why']})")
        if dry_run:
            return
        _write_profile(prof, sections)
        created = True
        print(f"wrote {prof}: {CATALOG_FILE}, licenses.toml, tags.toml, curation/, shortlists/")

    if dry_run:
        return
    ph = placeholders(prof)
    if ph:
        print(f"warning: {len(ph)} profile files are 0 bytes, probably online-only placeholders "
              f"(e.g. {ph[0]}). Make the profile folder available offline, then run init again.")
    cfg_path = write_user_config({"library": str(lib), "profile": None if default_prof else str(prof)})
    print(f"recorded in {cfg_path}")
    if uc.get("library") and uc.get("library") != str(lib):
        print(f"  (replaces the previous library {uc['library']})")
    print("next: `assetcat index`" + ("; review the sections in catalog.toml first" if created else ""))


def _write_profile(prof: Path, sections: list[dict]):
    def put(rel: str, text: str):
        p = ensure_inside(prof, prof / rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_text(text, encoding="utf-8", newline="\n")

    prof.mkdir(parents=True, exist_ok=True)
    put(CATALOG_FILE, render_catalog(sections))
    for f in ("licenses.toml", "tags.toml"):
        put(f, (STARTER_DIR / f).read_text(encoding="utf-8"))
    for d in ("curation", "shortlists"):
        ensure_inside(prof, prof / d).mkdir(exist_ok=True)

