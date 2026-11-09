"""`assetcat pull` on the synthetic library."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from assetcat import edit as E
from assetcat.cli import main
from assetcat.pull import PullError, file_hash, run_pull
from conftest import snapshot

HERO = "Bundles/Mega Fantasy Bundle/Graphics/Hero Sprites"
TINY = "kenney/2d-assets/tiny-dungeon"


def pull(env, con, targets, to, **kw):
    return run_pull(env["cfg"], con, targets, str(to), **kw)


def files_in(d: Path) -> list[str]:
    return sorted(p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file())


def test_pack_with_license_and_manifest(env, con, tmp_path):
    out, man = tmp_path / "game" / "assets", tmp_path / "game" / "assets" / "sources.toml"
    r = pull(env, con, [TINY], out, manifest=str(man))
    assert r["new"] == 8 and r["unchanged"] == 0
    assert "Tiny Dungeon/Tiles/tile_0002.png" in files_in(out)
    assert "License-Tiny-Dungeon.txt" in files_in(out)
    src = env["cfg"].abs("Kenney Game Assets All-in-1/2D assets/Tiny Dungeon/Tiles/tile_0002.png")
    assert (out / "Tiny Dungeon" / "Tiles" / "tile_0002.png").read_bytes() == src.read_bytes()

    m = tomllib.loads(man.read_text(encoding="utf-8"))
    assert m["format"] == 1 and len(m["files"]) == 8
    f = m["files"]["Tiny Dungeon/Tiles/tile_0002.png"]
    assert f["pack"] == TINY and f["source"].endswith("Tiles/tile_0002.png")
    assert f["hash"] == "blake2b-128:" + file_hash(str(src))
    p = m["packs"][TINY]
    assert p["license_file"] == "License-Tiny-Dungeon.txt" and p["credit"].startswith("Tiny Dungeon (Kenney")

    # pulling again changes nothing, not even the manifest's dates
    before = man.read_bytes()
    r = pull(env, con, [TINY], out, manifest=str(man))
    assert (r["new"], r["unchanged"]) == (0, 8)
    assert man.read_bytes() == before


def test_file_brings_its_atlas(env, con, tmp_path):
    r = pull(env, con, [f"{HERO}/hero_sheet.png"], tmp_path)
    assert {f["dest"] for f in r["files"]} == {"hero_sheet.png", "hero_sheet.xml"}


def test_file_brings_its_atlas_from_a_sibling_folder(env, con, tmp_path):
    # Kenney keeps sheets in Tilemap/ and their Tiled files in Tiled/: both land side by side
    r = pull(env, con, ["Bundles/Mega Fantasy Bundle/Graphics/Forest Tileset/Tilemap/forest_packed.png"], tmp_path)
    assert {f["dest"] for f in r["files"]} == {"forest_packed.png", "forest_packed.tsx"}


def test_folder_and_absolute_paths(env, con, tmp_path):
    folder = env["cfg"].abs("Kenney Game Assets All-in-1/2D assets/Tiny Dungeon/Tiles")
    r = pull(env, con, [str(folder)], tmp_path)
    assert sorted(f["dest"] for f in r["files"]) == [f"Tiles/tile_000{i}.png" for i in range(4)]


def test_flatten_and_clashes(env, con, tmp_path):
    r = pull(env, con, [f"{HERO}/PNG", "Kenney Game Assets All-in-1/2D assets/Tiny Dungeon/Tiles"], tmp_path / "a", flatten=True)
    assert sorted(f["dest"] for f in r["files"]) == ["hero_idle.png"] + [f"tile_000{i}.png" for i in range(4)]
    with pytest.raises(PullError, match="same place"):  # both packs have a Preview.png
        pull(env, con, [TINY, "space-ships"], tmp_path / "b", flatten=True)
    assert not (tmp_path / "b").exists()


def test_shared_license_is_named_after_its_owner(env, con, tmp_path):
    r = pull(env, con, ["hero-sprites", "battle-sounds"], tmp_path)
    assert [x["dest"] for x in r["licenses"]] == ["License-Mega-Fantasy-Bundle.txt"]
    assert {p["license_file"] for p in r["packs"]} == {"License-Mega-Fantasy-Bundle.txt"}


def test_license_name_template_and_lf(env, con, tmp_path):
    r = pull(env, con, ["ui-kit"], tmp_path, license_name="LICENSE-{slug}{ext}", license_lf=True)
    lic = tmp_path / "LICENSE-ui-kit.txt"
    assert r["licenses"][0]["dest"] == lic.name
    assert lic.read_bytes() == b"Licensed for use in games.\nAttribution appreciated.\n"
    assert b"\r\n" in (tmp_path / "UI Kit" / "license.txt").read_bytes()  # pack files are copied as they are
    r = pull(env, con, ["ui-kit"], tmp_path, license_name="LICENSE-{slug}{ext}", license_lf=True)
    assert r["licenses"][0]["status"] == "unchanged"
    r = pull(env, con, ["ui-kit"], tmp_path, license_name="LICENSE-{slug}{ext}")  # without --license-lf: same license
    assert r["licenses"][0]["status"] == "unchanged"
    with pytest.raises(PullError, match="plain file name"):
        pull(env, con, ["ui-kit"], tmp_path / "x", license_name="../{pack}.txt")


def test_conflicts_are_all_or_nothing(env, con, tmp_path):
    pull(env, con, ["space-ships"], tmp_path)
    changed = tmp_path / "Space Ships" / "ships" / "ship_01.png"
    changed.write_bytes(b"edited in the game")
    with pytest.raises(PullError, match="different content"):
        pull(env, con, ["space-ships", TINY], tmp_path)
    assert not (tmp_path / "Tiny Dungeon").exists()  # nothing copied
    assert changed.read_bytes() == b"edited in the game"
    r = pull(env, con, ["space-ships"], tmp_path, overwrite=True)
    assert r["replaced"] == 1 and r["unchanged"] == 3
    assert changed.read_bytes() != b"edited in the game"


@pytest.fixture
def human_made_space_ships(env, con):
    cfg = env["cfg"]
    p = cfg.curation_dir / "bundles" / "space-bundle.toml"
    stub = p.read_bytes()
    E.edit_pack(cfg, con, "bundles/space-bundle/space-ships", {"ai_generated": "no", "ai_reason": "Drawn by hand in 2015"})
    yield
    p.write_bytes(stub)
    E.refresh(cfg, con)


def test_require_ai(env, con, tmp_path, human_made_space_ships):
    r = pull(env, con, ["space-ships"], tmp_path / "a", require_ai="no")
    assert r["packs"][0]["ai_generated"] == "no" and r["packs"][0]["ai_reason"] == "Drawn by hand in 2015"
    with pytest.raises(PullError, match="tiny-dungeon: ai_generated = unknown"):
        pull(env, con, ["space-ships", TINY], tmp_path / "b", require_ai="no")
    assert not (tmp_path / "b").exists()
    r = pull(env, con, ["space-ships", TINY], tmp_path / "b", require_ai="no", force=True)
    assert r["new"] == 12


def test_dry_run_writes_nothing(env, con, tmp_path):
    r = pull(env, con, [TINY], tmp_path / "out", manifest=str(tmp_path / "m.toml"), dry_run=True)
    assert r["new"] == 8 and r["dry_run"]
    assert not (tmp_path / "out").exists() and not (tmp_path / "m.toml").exists()


def test_never_writes_into_the_library(env, con, tmp_path):
    lib = env["cfg"].library
    with pytest.raises(PullError, match="inside the library"):
        pull(env, con, [TINY], lib / "Packs" / "copy")
    with pytest.raises(PullError, match="inside the library"):
        pull(env, con, [TINY], tmp_path, manifest=str(lib / "sources.toml"))
    with pytest.raises(PullError, match="no pack, folder or file"):
        pull(env, con, ["nothing-like-this"], tmp_path)
    with pytest.raises(PullError, match="not inside the library"):
        pull(env, con, [str(tmp_path)], tmp_path)
    assert snapshot(lib) == env["before"]


def test_unsynced_files_are_refused(tmp_path, monkeypatch):
    import make_library
    from assetcat import db as DB
    from assetcat.config import load_config
    from assetcat.indexer import run_index

    lib = make_library.build(tmp_path / "lib")
    (lib / "Packs" / "Pixel Store" / "UI Kit" / "buttons" / "button_9.png").write_bytes(b"")  # a cloud placeholder
    monkeypatch.setenv("ASSETCAT_CONFIG", str(tmp_path / "c.toml"))
    monkeypatch.setenv("ASSETCAT_CACHE", str(tmp_path / "cache"))
    main(["init", str(lib)])
    cfg = load_config()
    run_index(cfg, workers=1, make_sheets=False)
    con = DB.connect(cfg.db_path)
    with pytest.raises(PullError, match="aren't synced yet"):
        run_pull(cfg, con, ["ui-kit"], str(tmp_path / "out"))
    run_pull(cfg, con, ["Packs/Pixel Store/UI Kit/buttons/button_1.png"], str(tmp_path / "out"))  # synced files are fine
    con.close()


def test_cli(env, tmp_path, capsys):
    main(["pull", "battle-sounds", "--to", str(tmp_path), "--manifest", str(tmp_path / "sources.toml")])
    out = capsys.readouterr().out
    assert "copied 4 files" in out and "License-Mega-Fantasy-Bundle.txt" in out
    assert "Battle Sounds (Mega Fantasy Bundle): license unknown" in out
    main(["pull", "battle-sounds", "--to", str(tmp_path), "--json"])
    r = json.loads(capsys.readouterr().out)
    assert r["unchanged"] == 4
    with pytest.raises(SystemExit, match="ai_generated"):
        main(["pull", "battle-sounds", "--to", str(tmp_path / "x"), "--require-ai", "no"])
