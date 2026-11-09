"""Curation files in the profile: the human-maintained knowledge.

<profile>/licenses.toml                 license table
<profile>/curation/<section>.toml       the group of a single-group section (e.g. kenney.toml)
<profile>/curation/<section>/<slug>.toml  one per group of a `groups` section (bundles/, packs/)

`assetcat index` only reads group files (new groups get a stub). The browse UI writes edits back
through edit.py with tomlkit, which keeps comments and layout.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, ConfigError, check_format

AI_VALUES = {"yes", "no", "unknown"}
ROLES = {"pack", "collection", "grouping", "ignore"}


@dataclass
class GroupCuration:
    id: str
    path: Path
    data: dict
    packs: dict[str, dict] = field(default_factory=dict)

    def get(self, k, default=None):
        return self.data.get(k, default)


@dataclass
class Curation:
    cfg: Config
    licenses: dict[str, dict]
    groups: dict[str, GroupCuration]
    problems: list[str]

    def group(self, gid: str) -> GroupCuration:
        if gid not in self.groups:
            self.groups[gid] = GroupCuration(gid, group_file(self.cfg, gid), {}, {})
        return self.groups[gid]


def group_file(cfg: Config, gid: str) -> Path:
    parts = gid.split("/")
    if len(parts) == 1:
        return cfg.curation_dir / f"{parts[0]}.toml"
    return cfg.curation_dir / parts[0] / f"{parts[1]}.toml"


def _gid_for(cfg: Config, path: Path) -> str:
    rel = path.relative_to(cfg.curation_dir).with_suffix("")
    return "/".join(rel.parts)


def group_files(cfg: Config) -> list[Path]:
    """Curation files of the configured sections, in section order."""
    out: list[Path] = []
    for s in cfg.sections:
        if s.layout == "single-group":
            out.append(cfg.curation_dir / f"{s.name}.toml")
        else:
            out.extend(sorted((cfg.curation_dir / s.name).glob("*.toml")))
    return out


def load_licenses(cfg: Config) -> dict[str, dict]:
    p = cfg.licenses_path
    licenses = tomllib.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    licenses.pop("format", None)
    licenses.setdefault("unknown", {"name": "Unknown", "summary": "Not recorded yet."})
    return licenses


def load_curation(cfg: Config) -> Curation:
    problems: list[str] = []
    try:
        licenses = load_licenses(cfg)
    except tomllib.TOMLDecodeError as e:
        problems.append(f"licenses.toml: {e}")
        licenses = {"unknown": {"name": "Unknown", "summary": "Not recorded yet."}}
    groups: dict[str, GroupCuration] = {}
    for p in group_files(cfg):
        if not p.exists():
            continue
        where = p.relative_to(cfg.curation_dir).as_posix()
        try:
            data = tomllib.loads(p.read_text(encoding="utf-8"))
            check_format(data, Path(where))
        except (tomllib.TOMLDecodeError, ConfigError) as e:
            problems.append(f"{where}: {e}" if isinstance(e, tomllib.TOMLDecodeError) else str(e))
            continue
        data.pop("format", None)
        gid = _gid_for(cfg, p)
        packs = data.pop("packs", {})
        gc = GroupCuration(gid, p, data, packs)
        groups[gid] = gc
        _validate(cfg, gc, licenses, problems)
    return Curation(cfg, licenses, groups, problems)


def _validate(cfg: Config, gc: GroupCuration, licenses: dict, problems: list[str]):
    where = gc.path.relative_to(cfg.curation_dir).as_posix()
    entries = [("", gc.data)] + [(f" [packs.\"{k}\"]", v) for k, v in gc.packs.items()]
    for label, d in entries:
        lic = d.get("license")
        if lic is not None and lic not in licenses:
            problems.append(f"{where}{label}: license '{lic}' is not in licenses.toml")
        ai = d.get("ai_generated")
        if ai is not None and ai not in AI_VALUES:
            problems.append(f"{where}{label}: ai_generated must be one of {sorted(AI_VALUES)}")
        role = d.get("role")
        if role is not None and role not in ROLES:
            problems.append(f"{where}{label}: role must be one of {sorted(ROLES)}")


STUB = '''# Curation for "{name}" (created by `assetcat index`). Edit freely; the browse UI also writes here, keeping comments.
# Keys: store, purchased (year), license (key in the profile's licenses.toml), license_file,
# ai_generated ("yes" | "no" | "unknown"), ai_reason, notes.
# tags = [...] adds tags to every pack, tags_remove = [...] takes AI tags away (e.g. "genre:fantasy").
# Per-pack overrides go in [packs."<key>"] tables (keys: the browse UI's Layout page or `assetcat packs --tree`):
# role ("pack" | "collection" | "grouping" | "ignore"), license, ai_generated, ai_reason,
# cover (path in the pack), tags = [...], tags_remove = [...], notes, description,
# grid = {{ "path/in/pack.png" = [16, 16] }}  or  {{ "x.png" = {{ w = 16, h = 16, spacing = 1, margin = 0 }} }}
format = 1

store = ""
# purchased = 2020
license = "unknown"
ai_generated = "unknown"
ai_reason = ""
'''


def ensure_stub(cfg: Config, gid: str, name: str) -> Path | None:
    p = group_file(cfg, gid)
    if p.exists():
        return None
    cfg.writable(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(STUB.format(name=name.replace('"', "'")), encoding="utf-8", newline="\n")
    return p
