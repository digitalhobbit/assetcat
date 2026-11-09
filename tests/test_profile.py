"""Profile lookup, init on a second machine, the write guard, curation edits and shortlists."""

from __future__ import annotations

import tomllib

import pytest

from assetcat import edit as E
from assetcat.config import ConfigError, load_config, resolve_paths
from conftest import snapshot

CURATED = '''# Hand-written curation, with comments that must survive edits.
store = "Example Store"
purchased = 2021
license = "gamedev-market"   # from the store page
ai_generated = "no"
ai_reason = "Released before generative art tools"

# The tileset pack
[packs."graphics/forest-tileset"]
tags = ["forest", "tiles"]
notes = "Pairs well with the hero sprites"

[packs."graphics/hero-sprites"]
grid = { "PNG/hero_idle.png" = [16, 16] }
'''


@pytest.fixture
def curated(env, con):
    """Give the fantasy bundle a hand-written curation file; restore the stub afterwards."""
    cfg = env["cfg"]
    p = cfg.curation_dir / "bundles" / "mega-fantasy-bundle.toml"
    stub = p.read_bytes()
    p.write_text(CURATED, encoding="utf-8", newline="\n")
    E.refresh(cfg, con)
    yield p
    p.write_bytes(stub)
    E.refresh(cfg, con)


def test_lookup_order(env, monkeypatch, tmp_path):
    lib = env["library"]
    l2, p2, uc = resolve_paths()
    assert l2 == lib and p2 == lib / "assetcat" and uc["library"] == str(lib)
    assert "profile" not in uc  # default profile location isn't written
    other = tmp_path / "elsewhere"
    l2, p2, _ = resolve_paths(profile=str(other))
    assert l2 == lib and p2 == other
    monkeypatch.setenv("ASSETCAT_LIBRARY", str(tmp_path))
    l2, p2, _ = resolve_paths()
    assert l2 == tmp_path and p2 == tmp_path / "assetcat"
    l2, p2, _ = resolve_paths(library=str(lib))  # the flag beats the environment
    assert l2 == lib


def test_missing_profile_and_library_errors(env, tmp_path):
    with pytest.raises(ConfigError, match="No profile"):
        load_config(library=str(tmp_path))
    with pytest.raises(ConfigError, match="not found"):
        load_config(library=str(tmp_path / "nope"))


def test_init_on_second_machine_keeps_profile(env, monkeypatch, tmp_path, capsys):
    from assetcat.cli import main

    prof = env["library"] / "assetcat"
    before = {p.relative_to(prof).as_posix(): p.read_bytes() for p in prof.rglob("*") if p.is_file()}
    monkeypatch.setenv("ASSETCAT_CONFIG", str(tmp_path / "machine2.toml"))
    main(["init", str(env["library"])])
    assert "profile found" in capsys.readouterr().out
    after = {p.relative_to(prof).as_posix(): p.read_bytes() for p in prof.rglob("*") if p.is_file()}
    assert after == before
    assert tomllib.loads((tmp_path / "machine2.toml").read_text(encoding="utf-8"))["library"] == str(env["library"])


def test_init_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    import make_library
    from assetcat.cli import main

    lib = make_library.build(tmp_path / "lib")
    monkeypatch.setenv("ASSETCAT_CONFIG", str(tmp_path / "c.toml"))
    main(["init", str(lib), "--dry-run"])
    out = capsys.readouterr().out
    assert "section kenney" in out and "dry run" in out
    assert not (lib / "assetcat").exists() and not (tmp_path / "c.toml").exists()


def test_write_guard(env):
    cfg = env["cfg"]
    cfg.writable(cfg.profile / "curation" / "x.toml")
    for bad in (cfg.library / "Bundles" / "x.toml", cfg.profile / ".." / "x.toml", cfg.profile.parent):
        with pytest.raises(ConfigError):
            cfg.writable(bad)


def test_section_containing_profile_is_refused(tmp_path, monkeypatch):
    (tmp_path / "lib" / "assetcat").mkdir(parents=True)
    (tmp_path / "lib" / "assetcat" / "catalog.toml").write_text('[[sections]]\nname = "all"\nfolder = "."\n', encoding="utf-8")
    monkeypatch.setenv("ASSETCAT_CONFIG", str(tmp_path / "c.toml"))
    with pytest.raises(ConfigError, match="contains the profile"):
        load_config(library=str(tmp_path / "lib"))


def test_newer_format_is_refused(tmp_path, monkeypatch):
    (tmp_path / "lib" / "assetcat").mkdir(parents=True)
    (tmp_path / "lib" / "assetcat" / "catalog.toml").write_text("format = 99\n", encoding="utf-8")
    monkeypatch.setenv("ASSETCAT_CONFIG", str(tmp_path / "c.toml"))
    with pytest.raises(ConfigError, match="newer"):
        load_config(library=str(tmp_path / "lib"))


def test_pack_edit_round_trip_is_byte_identical(env, con, curated):
    cfg = env["cfg"]
    original = curated.read_bytes()
    pid = "bundles/mega-fantasy-bundle/graphics/forest-tileset"
    E.edit_pack(cfg, con, pid, {"license": "cc0", "tags": ["Forest", "pixel-art"], "notes": "", "description": "Trees"})
    p = con.execute("SELECT * FROM packs WHERE id=?", (pid,)).fetchone()
    assert (p["license"], p["license_from"], p["tags"], p["notes"], p["description"]) == ("cc0", "pack", '["forest", "style:pixel-art"]', None, "Trees")
    text = curated.read_text(encoding="utf-8")
    assert "# The tileset pack" in text and "# from the store page" in text
    E.edit_pack(cfg, con, pid, {"license": None, "tags": ["forest", "tiles"], "notes": "Pairs well with the hero sprites", "description": None})
    assert curated.read_bytes() == original
    p = con.execute("SELECT * FROM packs WHERE id=?", (pid,)).fetchone()
    assert (p["license"], p["license_from"], p["description"]) == ("gamedev-market", "group", None)


def test_new_pack_table_round_trip(env, con, curated):
    cfg = env["cfg"]
    original = curated.read_bytes()
    pid = "bundles/mega-fantasy-bundle/fonts-pack"
    E.edit_pack(cfg, con, pid, {"ai_generated": "yes", "ai_reason": "test"})
    assert tomllib.loads(curated.read_text(encoding="utf-8"))["packs"]["fonts-pack"]["ai_generated"] == "yes"
    E.edit_pack(cfg, con, pid, {"ai_generated": None, "ai_reason": None})
    assert curated.read_bytes() == original


def test_group_and_grid_round_trip(env, con, curated):
    cfg = env["cfg"]
    original = curated.read_bytes()
    E.edit_group(cfg, con, "bundles/mega-fantasy-bundle", {"license": "cc0", "store": "Other"})
    assert con.execute("SELECT license FROM packs WHERE id='bundles/mega-fantasy-bundle/fonts-pack'").fetchone()[0] == "cc0"
    E.edit_group(cfg, con, "bundles/mega-fantasy-bundle", {"license": "gamedev-market", "store": "Example Store"})
    assert curated.read_bytes() == original

    aid = con.execute("SELECT id FROM assets WHERE path LIKE '%Forest Tileset/props/tree.png'").fetchone()[0]
    a = E.edit_grid(cfg, con, aid, {"w": 16, "h": 16})
    assert (a["grid_cols"], a["grid_rows"], a["grid_source"]) == (2, 3, "curation")
    E.edit_grid(cfg, con, aid, None)
    assert curated.read_bytes() == original


def test_edit_validation(env, con):
    cfg = env["cfg"]
    pid = "kenney/2d-assets/tiny-dungeon"
    with pytest.raises(E.EditError, match="unknown license"):
        E.edit_pack(cfg, con, pid, {"license": "nope"})
    with pytest.raises(E.EditError, match="not editable"):
        E.edit_pack(cfg, con, pid, {"role": "ignore"})
    aid = con.execute("SELECT id FROM assets WHERE path LIKE '%Tiny Dungeon/Preview.png'").fetchone()[0]
    with pytest.raises(E.EditError, match="doesn't tile"):
        E.edit_grid(cfg, con, aid, {"w": 7, "h": 7})


def test_shortlists(env):
    cfg = env["cfg"]
    E.create_shortlist(cfg, "space-ideas", "for the shmup")
    E.shortlist_add(cfg, "space-ideas", "pack", "bundles/space-bundle/space-ships")
    E.shortlist_add(cfg, "space-ideas", "asset", "Kenney Game Assets All-in-1/2D assets/Pixel Shmup/Ships/ship_0000.png", "player ship")
    E.shortlist_add(cfg, "space-ideas", "pack", "bundles/space-bundle/space-ships")  # no duplicates
    s = E.read_shortlist(cfg, "space-ideas")
    assert s["notes"] == "for the shmup" and [i["kind"] for i in s["items"]] == ["pack", "asset"]
    assert s["items"][1]["note"] == "player ship"
    assert (cfg.shortlist_dir / "space-ideas.toml").exists()
    assert {x["name"]: x["count"] for x in E.list_shortlists(cfg)}["space-ideas"] == 2
    E.shortlist_remove(cfg, "space-ideas", "pack", "bundles/space-bundle/space-ships")
    assert len(E.read_shortlist(cfg, "space-ideas")["items"]) == 1
    with pytest.raises(E.EditError):
        E.shortlist_path(cfg, "../escape")


def test_library_untouched_except_profile(env, con, curated):
    """Init, index, edits and shortlists never wrote anywhere in the library but the profile."""
    E.edit_pack(env["cfg"], con, "bundles/mega-fantasy-bundle/graphics/forest-tileset", {"notes": "x"})
    assert snapshot(env["library"]) == env["before"]
