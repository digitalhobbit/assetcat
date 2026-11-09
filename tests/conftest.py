from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import make_library  # noqa: E402

PROFILE = "assetcat"


def snapshot(root: Path, skip: str = PROFILE) -> dict[str, tuple[int, int]]:
    """Every file under root (except the profile folder): relative path -> (size, mtime_ns)."""
    out = {}
    for dirpath, dirnames, files in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        if rel_dir.parts and rel_dir.parts[0] == skip:
            continue
        for f in files:
            st = (Path(dirpath) / f).stat()
            out[(rel_dir / f).as_posix()] = (st.st_size, st.st_mtime_ns)
    return out


@pytest.fixture(scope="session")
def env(tmp_path_factory):
    """A synthetic library, its profile (from `assetcat init`) and a built index.

    Returns a dict with library, cache, config paths, the loaded Config and the library snapshot
    taken before init/index ran."""
    base = tmp_path_factory.mktemp("assetcat")
    lib = make_library.build(base / "Gamedev Assets")
    before = snapshot(lib)
    mp = pytest.MonkeyPatch()
    mp.setenv("ASSETCAT_CONFIG", str(base / "config" / "config.toml"))
    mp.setenv("ASSETCAT_CACHE", str(base / "cache"))
    mp.delenv("ASSETCAT_LIBRARY", raising=False)
    mp.delenv("ASSETCAT_PROFILE", raising=False)

    from assetcat.cli import main
    from assetcat.config import load_config
    from assetcat.indexer import run_index

    main(["init", str(lib)])
    cfg = load_config()
    stats = run_index(cfg, workers=2)
    yield {"base": base, "library": lib, "cfg": cfg, "stats": stats, "before": before}
    mp.undo()


@pytest.fixture
def con(env):
    from assetcat import db as DB

    c = DB.connect(env["cfg"].db_path)
    yield c
    c.close()
