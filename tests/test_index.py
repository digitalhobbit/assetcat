"""Index, search, show and sheet on the synthetic library."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from assetcat.cli import main


def run_json(capsys, *argv):
    main([*argv, "--json"])
    return json.loads(capsys.readouterr().out)


def test_init_wrote_profile_with_detected_layout(env):
    prof = env["library"] / "assetcat"
    cat = tomllib.loads((prof / "catalog.toml").read_text(encoding="utf-8"))
    secs = {s["name"]: s for s in cat["sections"]}
    assert set(secs) == {"kenney", "bundles", "packs"}
    assert secs["kenney"]["layout"] == "single-group" and secs["kenney"]["pack_depth"] == 2
    assert secs["kenney"]["ignore"] == ["Archive"]
    assert secs["bundles"]["layout"] == "groups"
    assert secs["packs"]["layout"] == "groups"
    for f in ("licenses.toml", "tags.toml"):
        assert tomllib.loads((prof / f).read_text(encoding="utf-8"))["format"] == 1
    assert (prof / "curation").is_dir() and (prof / "shortlists").is_dir()


def test_totals(env, capsys):
    s = env["stats"]
    assert (s["groups"], s["packs"], s["assets"], s["total"]) == (5, 12, 48, 75)
    st = run_json(capsys, "stats")
    assert st["files"][0] == 75
    assert st["by_section"] == {"bundles": {"packs": 6, "assets": 21}, "kenney": {"packs": 4, "assets": 20},
                                "packs": {"packs": 2, "assets": 7}}
    assert st["with_grid"] == 8
    assert st["duplicate_pack_pairs"] == 1


def test_profile_and_junk_not_indexed(con):
    paths = [r[0] for r in con.execute("SELECT path FROM files")]
    assert not [p for p in paths if p.startswith("assetcat/")]
    assert not [p for p in paths if "/Archive/" in p or "__MACOSX" in p or p.endswith(".DS_Store")]


def test_no_extract_errors(con):
    assert con.execute("SELECT path, error FROM files WHERE error IS NOT NULL").fetchall() == []


def test_grids_from_every_source(con):
    rows = {Path(r["path"]).name: dict(r) for r in con.execute("SELECT * FROM assets WHERE grid_w IS NOT NULL")}
    assert rows["forest.png"]["grid_source"] == "tiled" and (rows["forest.png"]["grid_cols"], rows["forest.png"]["grid_rows"]) == (4, 2)
    assert rows["hero_sheet.png"]["grid_source"] == "atlas" and rows["hero_sheet.png"]["grid_w"] == 32
    assert rows["monsters.png"]["grid_source"] == "atlas" and rows["monsters.png"]["grid_cols"] == 3
    t = rows["tilemap.png"]
    assert t["grid_source"] == "tilesheet.txt" and (t["grid_w"], t["grid_spacing"], t["grid_cols"], t["grid_rows"]) == (16, 1, 4, 3)
    assert rows["cursors_16x16.png"]["grid_source"] == "name"


def test_variants_are_one_asset(con):
    hit = con.execute("SELECT * FROM assets WHERE name='sword_hit'").fetchone()
    assert hit["variants"] == 2 and hit["formats"] == "ogg,wav" and hit["path"].endswith(".ogg")
    idle = con.execute("SELECT * FROM assets WHERE name='hero_idle'").fetchone()
    assert idle["variants"] == 2 and idle["formats"] == "png,psd"


def test_media_metadata(con):
    f = con.execute("SELECT * FROM files WHERE path LIKE '%Pixelade.ttf'").fetchone()
    assert f["font_family"] == "Pixelade"
    o = con.execute("SELECT * FROM files WHERE path LIKE '%battle_theme.ogg'").fetchone()
    assert o["channels"] == 2 and 0.2 < o["duration"] < 0.4
    w = con.execute("SELECT * FROM files WHERE path LIKE '%sword_hit.wav'").fetchone()
    assert w["sample_rate"] == 8000


def test_search_assets(capsys):
    r = run_json(capsys, "search", "sword")
    assert [x["name"] for x in r] == ["sword_hit"]
    r = run_json(capsys, "search", "--type", "music")
    assert [x["name"] for x in r] == ["battle_theme"]
    r = run_json(capsys, "search", "--grid", "--min-size", "32x32")
    assert {x["name"] for x in r} == {"hero_sheet", "monsters"}
    r = run_json(capsys, "search", "ship", "--pack", "pixel-shmup")
    assert len(r) == 3 and all("Pixel Shmup" in x["path"] for x in r)


def test_search_packs_finds_readme_text(capsys):
    r = run_json(capsys, "search", "--packs", "water")  # only in the forest readme
    assert {x["id"] for x in r} == {"bundles/mega-fantasy-bundle/graphics/forest-tileset", "bundles/space-bundle/forest-tileset-copy"}


def test_show_pack(capsys):
    p = run_json(capsys, "show", "hero-sprites")
    assert p["id"] == "bundles/mega-fantasy-bundle/graphics/hero-sprites"
    assert p["license_file"] == "Bundles/Mega Fantasy Bundle/License.txt"  # group-level license file
    assert p["cover"].endswith("Hero Sprites/Preview.png")
    assert p["curation"]["file"] == "curation/bundles/mega-fantasy-bundle.toml"
    assert Path(p["curation"]["file_abs"]).exists()
    p = run_json(capsys, "show", "forest-tileset")
    assert p["duplicates"][0]["other"] == "bundles/space-bundle/forest-tileset-copy"
    assert p["duplicates"][0]["share_of_this"] == 1.0


def test_show_asset_by_library_path(env, capsys):
    rel = "Bundles/Mega Fantasy Bundle/Audio/Battle Sounds/wav/sword_hit.wav"
    a = run_json(capsys, "show", rel)
    assert a["name"] == "sword_hit" and len(a["files"]) == 2
    a = run_json(capsys, "show", str(env["library"] / rel))  # absolute paths work too
    assert a["name"] == "sword_hit"


def test_sheet(capsys):
    r = run_json(capsys, "sheet", "tiny-dungeon", "Tiles")
    assert r["images"] == 4 and Path(r["sheet"]).exists()
    from PIL import Image

    with Image.open(r["sheet"]) as im:
        assert im.width >= 420


def test_text_output_smoke(capsys):
    for argv in (["stats"], ["doctor"], ["packs", "--tree"], ["show", "ui-kit"], ["search", "tile"]):
        main(argv)
    out = capsys.readouterr().out
    assert "assetdb" not in out and "tools/" not in out
    assert "curation:" in out


def test_doctor(capsys):
    d = run_json(capsys, "doctor")
    assert [i["id"] for i in d["issues"]] == ["license_unknown", "ai_unknown", "packs_nearly_empty", "model_refs"]  # fresh stubs
    assert len(d["issues"][0]["items"]) == 5


def test_reindex_is_incremental(env):
    from assetcat.indexer import run_index

    s = run_index(env["cfg"], workers=1)
    assert (s["new"], s["changed"], s["removed"], s["created"]) == (0, 0, 0, [])
    assert s["assets"] == 48


def test_doctor_explains_each_problem(env, capsys):
    cur = env["cfg"].curation_dir / "packs" / "pixel-store.toml"
    original = cur.read_bytes()
    cur.write_text(original.decode() + '\n[packs."renamed-folder"]\nlicense = "nope"\n', encoding="utf-8", newline="\n")
    try:
        d = run_json(capsys, "doctor")
        issues = {i["id"]: i for i in d["issues"]}
        assert issues["unused_curation_keys"]["items"] == ["packs/pixel-store: renamed-folder"]
        assert "licenses.toml" in issues["curation_problems"]["fix"]
        assert "kenney  (4 of 4 packs)  curation/kenney.toml" in issues["license_unknown"]["items"]
        assert all(i["fix"] for i in d["issues"])
        main(["doctor"])
        out = capsys.readouterr().out
        assert out.count("  fix: ") == len(d["issues"])
    finally:
        cur.write_bytes(original)
