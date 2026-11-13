"""Folder names beyond ASCII (keys, collisions) and text files that aren't UTF-8."""

from __future__ import annotations

import json
import re
import unicodedata

import pytest

import make_library
from assetcat import classify as C
from assetcat import edit as E
from assetcat.extract import decode_text
from conftest import snapshot

JP = "packs/ドット工房/ドット絵パック"


def legacy_slugify(s: str) -> str:  # keys up to 1.0.1
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s or "x"


def run_json(capsys, *argv):
    from assetcat.cli import main

    main([*argv, "--json"])
    return json.loads(capsys.readouterr().out)


# --- keys ------------------------------------------------------------------------------------

@pytest.mark.parametrize("name, key", [
    ("Ógma Fadas", "ogma-fadas"),
    ("İstanbul Tiles", "istanbul-tiles"),
    ("ドット絵パック", "ドット絵パック"),
    ("خطوط عربية", "خطوط-عربية"),
    ("Пиксели Йода", "пиксели-йода"),  # й keeps its breve: the base letter isn't ASCII
    ("Yōkai", "yokai"),
    ("ＵＩ　Ｋｉｔ　２", "ui-kit-2"),  # full-width letters and space
    ("हिन्दी पैक", "हिन्दी-पैक"),  # vowel signs stay inside the word
    ("4×4 Pixels", "4-4-pixels"),
    ("★★★", "x"),
])
def test_slugify_beyond_ascii(name, key):
    assert C.slugify(name) == key


def test_slugify_ignores_unicode_normalization():
    # macOS and zip tools can hand back decomposed names; Windows usually composed ones
    for name in ("Café Ógma", "ドット絵パック", "Пиксели Йода", "İstanbul"):
        assert C.slugify(unicodedata.normalize("NFD", name)) == C.slugify(unicodedata.normalize("NFC", name))


def test_ascii_keys_unchanged(tmp_path):
    lib = make_library.build(tmp_path / "lib")
    names = {p.name for p in lib.rglob("*") if p.is_dir() and p.name.isascii()}
    names |= {"UI_Kit v2.0", "  --Pack--  ", "Tiles!", "a__b", "CamelCase99", "...", "x"}
    assert names and all(C.slugify(n) == legacy_slugify(n) for n in names)


def test_unique_keys():
    keys, clashes = C.unique_keys(["tiles", "Tiles!", "Rocks", "Tiles"])
    assert keys["Tiles"] == "tiles" and keys["Rocks"] == "rocks"  # first sorted keeps the plain key
    assert re.fullmatch(r"tiles-[0-9a-f]{4}", keys["tiles"]) and re.fullmatch(r"tiles-[0-9a-f]{4}", keys["Tiles!"])
    assert len(set(keys.values())) == 4 and clashes == [["Tiles", "tiles", "Tiles!"]]
    # a suffix depends only on the folder's own name, not on which other folders exist
    assert C.unique_keys(["Tiles", "Tiles!"])[0]["Tiles!"] == keys["Tiles!"]
    assert C.unique_keys(["ドット", "★", "☆"])[0] == {"ドット": "ドット", "★": "x", "☆": C.unique_keys(["★", "☆"])[0]["☆"]}


# --- text encodings --------------------------------------------------------------------------

SAMPLES = [
    ("shift_jis", make_library.SJIS_README),
    ("shift_jis", "ドット絵素材集\n利用規約\n・商用、非商用問わずご利用いただけます。\n・改変可。再配布は禁止です。\n"),
    ("euc_jp", "ライセンス: 商用利用可能。クレジット表記不要。"),
    ("gbk", "许可证：可商用，无需署名。素材包包含 120 个精灵图。"),
    ("big5", "授權：可商業使用，不需署名。"),
    ("euc_kr", "라이선스: 상업적 사용 가능. 크레딧 표기 불필요."),
    ("cp1251", "Лицензия: можно использовать в коммерческих проектах. Указание автора не требуется."),
    ("cp1252", "Café sprites – résumé\nLicense: CC0. © 2019 Zoë Müller"),
    ("cp1252", "Größe: 16×16 Pixel. Lizenz: frei für kommerzielle Nutzung."),
    ("cp1252", "Thanks for downloading! Don’t redistribute. “Enjoy”"),
    ("cp1256", "الترخيص: مجاني للاستخدام التجاري"),
    ("cp1252", "# Sizes in pixel\n\tTilesize:\t\t 16 × 16\n\tMargin:\t\t\t  1 ×  1\n"),  # like Kenney's Size.txt
    ("cp1252", "\t\t# Sizes in pixel\n\n\tNormal crosshair:\t\t 64 ×  64\n\tDouble crosshair:\t\t128 × 128\n"),
    ("cp1252", "Thank you for purchasing a license for this song! Café edition.\n"),
    ("utf-16-le", "Readme\nLicense: CC0 for everything in this pack."),  # no BOM
    ("utf-16-be", "Readme\nLicense: CC0 for everything in this pack."),
    ("utf-16", "ライセンス: CC0"),  # with a BOM
    ("utf-8-sig", "ライセンス: CC0"),
    ("utf-8", "Plain readme, nothing special."),
]


# readable in no likely code page (b"\x81\x8d..." alone isn't: that's Cyrillic in cp1251)
GARBLED = b"License: CC0 " + bytes(range(0x80, 0x100))


@pytest.mark.parametrize("enc, text", SAMPLES, ids=[f"{e}-{i}" for i, (e, _) in enumerate(SAMPLES)])
def test_decode_text(enc, text):
    out, _, guessed = decode_text(text.encode(enc))
    assert out == text and not guessed


def test_undecodable_text_is_flagged():
    out, enc, guessed = decode_text(GARBLED)
    assert guessed and enc == "cp1252" and out.startswith("License: CC0 ")


# --- the Japanese pack in the shared library ---------------------------------------------------

def test_japanese_pack_indexed(env, con, capsys):
    p = con.execute("SELECT * FROM packs WHERE id=?", (JP,)).fetchone()
    assert p and p["key"] == "ドット絵パック" and p["group_id"] == "packs/ドット工房" and p["asset_count"] == 2
    row = con.execute("SELECT text, info FROM files WHERE path=?", ("Packs/ドット工房/ドット絵パック/readme.txt",)).fetchone()
    assert row["text"] == make_library.SJIS_README.strip()
    assert json.loads(row["info"]) == {"encoding": "cp932"}
    hits = run_json(capsys, "search", "商用", "--packs")
    assert [h["id"] for h in hits] == [JP]
    shown = run_json(capsys, "show", "ドット絵パック")
    assert shown["id"] == JP and "クレジット表記不要" in json.dumps(shown, ensure_ascii=False)
    assert "text_encoding" not in [i["id"] for i in run_json(capsys, "doctor")["issues"]]


def test_japanese_pack_in_server(env):
    from fastapi.testclient import TestClient

    from assetcat.server import create_app

    with TestClient(create_app(env["cfg"])) as client:
        assert client.get("/api/pack", params={"id": JP}).json()["name"] == "ドット絵パック"
        tree = client.post("/api/layout", json={"group": "packs/ドット工房"}).json()
        assert [n["key"] for n in tree["tree"]] == ["ドット絵パック"]


def test_japanese_pack_edit_round_trip(env, con):
    cfg = env["cfg"]
    path = cfg.profile / "curation" / "packs" / "ドット工房.toml"
    original = path.read_bytes()
    E.edit_pack(cfg, con, JP, {"license": "cc0", "notes": "見本"})
    assert '[packs."ドット絵パック"]' in path.read_text(encoding="utf-8")
    assert con.execute("SELECT license FROM packs WHERE id=?", (JP,)).fetchone()[0] == "cc0"
    E.edit_pack(cfg, con, JP, {"license": None, "notes": None})
    assert path.read_bytes() == original


# --- a library of clashing names --------------------------------------------------------------

NAMES = ["ドット絵パック", "خطوط عربية", "Пиксели", "Ógma Fadas", "İstanbul Tiles", "Tiles!", "tiles", "★", "☆"]


@pytest.fixture(scope="module")
def clash(tmp_path_factory):
    base = tmp_path_factory.mktemp("clash")
    lib = base / "Lib"
    for i, n in enumerate(NAMES):
        (lib / "Store" / n).mkdir(parents=True)
        (lib / "Store" / n / "a.png").write_bytes(make_library.png(16, 16, i))
    # two bundles whose names give the same key
    for i, n in enumerate(["Café Bundle", "Café Bundle!"]):
        (lib / "Bundles" / n / "Pack").mkdir(parents=True)
        (lib / "Bundles" / n / "Pack" / "b.png").write_bytes(make_library.png(16, 16, 50 + i))
    (lib / "Store" / "Пиксели" / "readme.txt").write_bytes("Лицензия: CC0. Указание автора не требуется.".encode("cp1251"))
    (lib / "Store" / "Ógma Fadas" / "license.txt").write_bytes(GARBLED)
    before = snapshot(lib)
    mp = pytest.MonkeyPatch()
    mp.setenv("ASSETCAT_CONFIG", str(base / "config.toml"))
    mp.setenv("ASSETCAT_CACHE", str(base / "cache"))
    mp.delenv("ASSETCAT_LIBRARY", raising=False)
    mp.delenv("ASSETCAT_PROFILE", raising=False)
    from assetcat.cli import main
    from assetcat.config import load_config
    from assetcat.indexer import run_index

    main(["init", str(lib)])
    cfg = load_config()
    stats = run_index(cfg, workers=1)
    yield {"lib": lib, "cfg": cfg, "stats": stats, "before": before}
    mp.undo()


def test_every_folder_is_a_pack(clash):
    from assetcat import db as DB

    con = DB.connect(clash["cfg"].db_path)
    try:
        packs = {r["name"]: r for r in con.execute("SELECT * FROM packs WHERE section='store'")}
        assert set(packs) == set(NAMES) and len({p["id"] for p in packs.values()}) == len(NAMES)
        assert packs["خطوط عربية"]["key"] == "خطوط-عربية" and packs["Ógma Fadas"]["key"] == "ogma-fadas"
        assert packs["tiles"]["key"] == "tiles" and packs["Tiles!"]["key"].startswith("tiles-")
        files = {r[0] for r in con.execute("SELECT path FROM files WHERE ext='png'")}
        assert len(files) == len(NAMES) + 2
        assert con.execute("SELECT COUNT(*) FROM assets WHERE pack_id IS NULL").fetchone()[0] == 0
        groups = [r[0] for r in con.execute("SELECT id FROM groups WHERE section='bundles' ORDER BY id")]
        assert len(groups) == 2 and groups[0] == "bundles/cafe-bundle"
    finally:
        con.close()
    assert len(clash["stats"]["key_collisions"]) == 3  # Tiles!/tiles, ★/☆, the two bundles


def test_collisions_in_doctor(clash, capsys):
    issues = {i["id"]: i for i in run_json(capsys, "doctor")["issues"]}
    items = issues["key_collisions"]["items"]
    assert any("Tiles!" in x and "tiles" in x for x in items) and len(items) == 3
    assert issues["text_encoding"]["items"] == ["Store/Ógma Fadas/license.txt"]


def test_layout_editor_keys_match_index(clash):
    from assetcat import db as DB
    from assetcat.curation import load_curation
    from assetcat.scan import find_group, group_tree

    cfg = clash["cfg"]
    con = DB.connect(cfg.db_path)
    try:
        for gid, n in con.execute("SELECT group_id, COUNT(*) FROM packs GROUP BY group_id"):
            found = find_group(cfg, gid)
            assert found, gid
            sec, gdir = found
            t = group_tree(cfg, sec, gdir, gid, load_curation(cfg).group(gid).packs)
            keys = {r[0] for r in con.execute("SELECT key FROM packs WHERE group_id=?", (gid,))}
            assert {k["key"] for k in t["tree"]} == keys and len(t["packs"]) == n
    finally:
        con.close()


def test_cp1251_readme_searchable(clash, capsys):
    hits = run_json(capsys, "search", "Лицензия", "--packs")
    assert [h["name"] for h in hits] == ["Пиксели"]


def test_old_doc_text_is_reread(clash):
    from assetcat import db as DB
    from assetcat.indexer import run_index

    cfg, rel = clash["cfg"], "Store/Пиксели/readme.txt"
    con = DB.connect(cfg.db_path)
    try:
        good = con.execute("SELECT text FROM files WHERE path=?", (rel,)).fetchone()[0]
        con.execute("UPDATE files SET text='mojibake', info=NULL, meta_version=3 WHERE path=?", (rel,))
        con.commit()
        run_index(cfg, workers=1)
        assert con.execute("SELECT text, info FROM files WHERE path=?", (rel,)).fetchone()[:] == (good, '{"encoding": "cp1251"}')
    finally:
        con.close()


def test_clash_library_untouched(clash):
    assert snapshot(clash["lib"]) == clash["before"]


def test_cli_output_beyond_the_console_encoding(env):
    # Windows writes piped or redirected output as cp1252 unless told otherwise
    import os
    import subprocess
    import sys

    run = "import sys; from assetcat.cli import main; sys.exit(main())"
    environ = {**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"}
    for args in (["show", "ドット絵パック"], ["packs", "--json"]):
        r = subprocess.run([sys.executable, "-c", run, *args], capture_output=True, env=environ)
        assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
        assert "ドット絵パック" in r.stdout.decode("utf-8")
