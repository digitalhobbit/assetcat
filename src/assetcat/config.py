"""Configuration: where the library, the profile and the cache are, and the profile's layout file.

- **User config** (per machine): `platformdirs.user_config_dir("assetcat")/config.toml`, written by
  `assetcat init`. Keys: `library`, optional `profile`, `cache`. `ASSETCAT_CONFIG` points elsewhere.
- **Library**: `--library` > `ASSETCAT_LIBRARY` > `library` in the user config.
- **Profile**: `--profile` > `ASSETCAT_PROFILE` > `profile` in the user config > `<library>/assetcat`.
  It holds `catalog.toml` (layout), `licenses.toml`, `tags.toml`, `curation/`, `shortlists/`.
- **Cache**: `ASSETCAT_CACHE` > `cache` in the user config > `user_cache_dir("assetcat")/<profile id>/`.
  SQLite database, contact sheets, thumbnails. Never synced; rebuilt from library + profile.

The library is read-only: the only folder the tool writes in is the profile (see `Config.writable`).
"""

from __future__ import annotations

import hashlib
import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import platformdirs

PKG_DIR = Path(__file__).resolve().parent
WEB_DIR = PKG_DIR / "web"
STARTER_DIR = PKG_DIR / "starter"
APP = "assetcat"
PROFILE_DIRNAME = "assetcat"
CATALOG_FILE = "catalog.toml"
FORMAT = 1  # profile file format this version reads and writes


class ConfigError(Exception):
    pass


@dataclass
class Section:
    name: str
    folder: str
    layout: str  # "groups" | "single-group"
    pack_depth: int | None = None
    ignore: list[str] = field(default_factory=list)


@dataclass
class Config:
    library: Path
    profile: Path
    cache: Path
    sections: list[Section]
    junk_files: set[str]
    junk_dirs: set[str]
    grouping_names: set[str]

    @property
    def db_path(self) -> Path:
        return self.cache / "catalog.sqlite"

    @property
    def sheets_dir(self) -> Path:
        return self.cache / "sheets"

    @property
    def curation_dir(self) -> Path:
        return self.profile / "curation"

    @property
    def shortlist_dir(self) -> Path:
        return self.profile / "shortlists"

    @property
    def licenses_path(self) -> Path:
        return self.profile / "licenses.toml"

    def abs(self, rel: str) -> Path:
        """Absolute path of a library-relative (posix) path."""
        return self.library.joinpath(*rel.split("/"))

    def fs(self, rel: str) -> str:
        """Path string for file system calls; long-path safe on Windows."""
        return fs_path(self.abs(rel))

    def writable(self, p: Path) -> Path:
        """The guard for every write the tool makes outside its cache: only inside the profile."""
        return ensure_inside(self.profile, p)


def ensure_inside(profile: Path, p: Path) -> Path:
    rp, prof = Path(os.path.abspath(p)), Path(os.path.abspath(profile))
    if rp != prof and prof not in rp.parents:
        raise ConfigError(f"refusing to write outside the profile: {p}")
    return p


def fs_path(p: Path) -> str:
    s = str(p)
    if os.name == "nt" and not s.startswith("\\\\?\\"):
        s = "\\\\?\\" + str(p.resolve())
    return s


# --- user config -------------------------------------------------------------------------------

def user_config_path() -> Path:
    env = os.environ.get("ASSETCAT_CONFIG")
    if env:
        return Path(env).expanduser()
    return Path(platformdirs.user_config_dir(APP, appauthor=False)) / "config.toml"


def read_user_config() -> dict:
    p = user_config_path()
    if not p.exists():
        return {}
    try:
        return tomllib.loads(p.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{p}: {e}") from e


def write_user_config(values: dict) -> Path:
    """Merge `values` into the user config (None removes a key). Keeps the file simple: flat keys."""
    import tomlkit

    p = user_config_path()
    doc = tomlkit.parse(p.read_text(encoding="utf-8")) if p.exists() else tomlkit.document()
    if not p.exists():
        doc.add(tomlkit.comment("assetcat user config for this machine (written by `assetcat init`)."))
    for k, v in values.items():
        if v is None:
            doc.pop(k, None)
        else:
            doc[k] = v
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(tomlkit.dumps(doc), encoding="utf-8", newline="\n")
    return p


# --- resolution --------------------------------------------------------------------------------

def _path(s: str) -> Path:
    return Path(os.path.abspath(Path(s).expanduser()))


def resolve_paths(library: str | None = None, profile: str | None = None) -> tuple[Path | None, Path | None, dict]:
    """(library, profile, user config) from flags, environment and the user config."""
    uc = read_user_config()
    lib_s = library or os.environ.get("ASSETCAT_LIBRARY")
    # the user config's profile belongs to the user config's library, not to one given otherwise
    prof_s = profile or os.environ.get("ASSETCAT_PROFILE") or (None if lib_s else uc.get("profile"))
    lib_s = lib_s or uc.get("library")
    lib = _path(lib_s) if lib_s else None
    prof = _path(prof_s) if prof_s else (lib / PROFILE_DIRNAME if lib else None)
    return lib, prof, uc


def profile_id(profile: Path) -> str:
    """Cache folder name for a profile: readable and stable on this machine."""
    lib_name = profile.parent.name if profile.name == PROFILE_DIRNAME else profile.name
    from .classify import slugify

    h = hashlib.blake2b(str(profile).encode("utf-8"), digest_size=4).hexdigest()
    return f"{slugify(lib_name)[:40]}-{h}"


def load_config(library: str | None = None, profile: str | None = None) -> Config:
    lib, prof, uc = resolve_paths(library, profile)
    if lib is None:
        raise ConfigError("No library yet. Run `assetcat init <library folder>` once on this machine, "
                          "or pass --library / set ASSETCAT_LIBRARY.")
    if not lib.is_dir():
        raise ConfigError(f"Library folder not found: {lib}")
    catalog = prof / CATALOG_FILE
    if not catalog.exists():
        raise ConfigError(f"No profile at {prof} ({CATALOG_FILE} missing). Run `assetcat init \"{lib}\"`.")
    try:
        shared = tomllib.loads(catalog.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{catalog}: {e}") from e
    check_format(shared, catalog)

    cache_s = os.environ.get("ASSETCAT_CACHE") or uc.get("cache")
    cache = _path(cache_s) if cache_s else Path(platformdirs.user_cache_dir(APP, appauthor=False)) / profile_id(prof)
    cache.mkdir(parents=True, exist_ok=True)

    sections = [
        Section(
            name=s["name"],
            folder=s["folder"],
            layout=s.get("layout", "groups"),
            pack_depth=s.get("pack_depth"),
            ignore=list(s.get("ignore", [])),
        )
        for s in shared.get("sections", [])
    ]
    for s in sections:
        if s.layout not in ("groups", "single-group"):
            raise ConfigError(f"{catalog}: section '{s.name}': layout must be groups or single-group")
        sec_abs = _path(str(lib.joinpath(*s.folder.split("/"))))
        if sec_abs == prof or sec_abs in prof.parents:
            raise ConfigError(f"{catalog}: section '{s.name}' contains the profile folder; the profile is never indexed")
    scan = shared.get("scan", {})
    return Config(
        library=lib,
        profile=prof,
        cache=cache,
        sections=sections,
        junk_files=set(scan.get("junk_files", [])),
        junk_dirs=set(scan.get("junk_dirs", [])),
        grouping_names={n.lower() for n in scan.get("grouping_names", [])},
    )


def check_format(data: dict, path: Path):
    f = data.get("format", 1)
    if not isinstance(f, int) or f > FORMAT:
        raise ConfigError(f"{path}: format {f} is newer than this assetcat understands ({FORMAT}); upgrade assetcat")


def eprint(*a, **k):
    print(*a, file=sys.stderr, **k)
