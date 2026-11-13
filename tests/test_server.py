"""The browse UI's HTTP endpoints, through FastAPI's test client."""

from __future__ import annotations

from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from assetcat.server import create_app
from conftest import snapshot

FOREST = "bundles/mega-fantasy-bundle/graphics/forest-tileset"


@pytest.fixture(scope="module")
def client(env):
    with TestClient(create_app(env["cfg"])) as c:
        yield c


def test_frontend(client):
    r = client.get("/")
    assert r.status_code == 200 and "/web/app.js" in r.text
    r = client.get("/web/app.js")
    assert r.status_code == 200 and "assetcat serve" in r.text
    assert client.get("/web/vendor/preact-htm.js").status_code == 200
    assert client.get("/web/logo.svg").headers["content-type"].startswith("image/svg")


def test_meta(client):
    m = client.get("/api/meta").json()
    assert m["sections"] == ["bundles", "kenney", "packs"]
    assert m["stats"]["packs"] == 12 and m["stats"]["assets"] == 48 and m["stats"]["files"] == 75
    assert "cc0" in m["licenses"] and isinstance(m["shortlists"], list)


def test_packs_list_and_filters(client):
    r = client.get("/api/packs").json()
    assert r["total"] == 12
    r = client.get("/api/packs", params={"q": "forest"}).json()
    assert {i["id"] for i in r["items"]} == {FOREST, "bundles/space-bundle/forest-tileset-copy"}
    r = client.get("/api/packs", params={"copies": "false"}).json()
    assert r["hidden_copies"] == 1
    r = client.get("/api/packs", params={"section": "kenney", "sort": "assets"}).json()
    assert [i["id"] for i in r["items"]][:2] == ["kenney/3d-assets/mini-crates", "kenney/2d-assets/tiny-dungeon"]
    r = client.get("/api/packs", params={"kind": "sfx"}).json()
    assert [i["id"] for i in r["items"]] == ["kenney/audio/impact-sounds"]


def test_pack_detail(client):
    p = client.get("/api/pack", params={"id": FOREST}).json()
    assert p["name"] == "Forest Tileset" and p["cover_url"] and p["sheet_url"]
    assert p["also_in"] == ["bundles/space-bundle/forest-tileset-copy"]
    assert p["curation"]["file"] == "curation/bundles/mega-fantasy-bundle.toml"
    assert any(d["role"] == "readme" for d in p["docs"])
    assert client.get(p["cover_url"]).headers["content-type"] == "image/webp"
    assert client.get(p["sheet_url"]).headers["content-type"] == "image/png"
    assert client.get("/api/pack", params={"id": "nope"}).status_code == 404
    f = client.get("/api/folders", params={"pack": FOREST}).json()
    assert {"folder": "props", "count": 2} in f


def test_assets_and_files(client):
    r = client.get("/api/assets", params={"pack": FOREST}).json()
    assert r["total"] == 5
    r = client.get("/api/assets", params={"q": "sword"}).json()
    assert [i["name"] for i in r["items"]] == ["sword_hit"]
    r = client.get("/api/assets", params={"grid": "true", "media": "visual"}).json()
    assert r["total"] == 8
    a = client.get("/api/asset", params={"id": r["items"][0]["id"]}).json()
    assert a["files"] and a["license_info"]["key"] == "unknown"
    lib = client.get("/lib/" + quote(a["path"]))
    assert lib.status_code == 200 and lib.content[:4] == b"\x89PNG"
    # small images redirect to the original; big ones get a webp thumbnail
    t = client.get("/thumb", params={"path": a["path"], "s": 512}, follow_redirects=False)
    assert t.status_code in (200, 307)
    big = "Bundles/Mega Fantasy Bundle/Graphics/Forest Tileset/Preview.png"
    t = client.get("/thumb", params={"path": big, "s": 96})
    assert t.status_code == 200 and t.headers["content-type"] == "image/webp"


def test_library_files_only_from_the_index(client):
    assert client.get("/lib/assetcat/catalog.toml").status_code == 404
    assert client.get("/lib/" + quote("../outside.txt")).status_code == 404
    assert client.get("/sheets/" + quote("../catalog.sqlite")).status_code == 404


def test_sheet_endpoint(client):
    r = client.post("/api/sheet", json={"pack": "kenney/2d-assets/tiny-dungeon", "folder": "Tiles"}).json()
    assert r["images"] == 4 and client.get(r["url"]).status_code == 200


def test_edits_through_the_api(env, client):
    path = env["cfg"].curation_dir / "packs" / "pixel-store.toml"
    original = path.read_bytes()
    pid = "packs/pixel-store/ui-kit"
    p = client.post("/api/edit/pack", json={"id": pid, "fields": {"license": "cc0", "tags": ["ui"]}}).json()
    assert p["license"] == "cc0" and p["tags"] == ["kind:ui"] and p["curation_values"]["license"] == "cc0"
    p = client.post("/api/edit/pack", json={"id": pid, "fields": {"license": None, "tags": None}}).json()
    assert p["license"] == "unknown"
    assert path.read_bytes() == original
    r = client.post("/api/edit/pack", json={"id": pid, "fields": {"license": "bogus"}})
    assert r.status_code == 400
    g = client.post("/api/edit/group", json={"id": "packs/pixel-store", "fields": {"store": "Pixel Store"}}).json()
    assert g["store"] == "Pixel Store"
    client.post("/api/edit/group", json={"id": "packs/pixel-store", "fields": {"store": ""}})
    assert b"store =" not in path.read_bytes()  # an empty value removes the key
    path.write_bytes(original)
    from assetcat import db as DB
    from assetcat.edit import refresh

    con = DB.connect(env["cfg"].db_path)
    refresh(env["cfg"], con)
    con.close()


def test_shortlists_through_the_api(client):
    client.post("/api/shortlist", json={"name": "api-test"})
    client.post("/api/shortlist/add", json={"name": "api-test", "kind": "pack", "ref": FOREST})
    client.post("/api/shortlist/add", json={"name": "api-test", "kind": "folder",
                                            "ref": "Bundles/Mega Fantasy Bundle/Graphics/Forest Tileset/props"})
    s = client.get("/api/shortlist", params={"name": "api-test"}).json()
    assert s["items"][0]["pack"]["id"] == FOREST
    assert s["items"][1]["pack"]["folder"] == "props"
    assert any(x["name"] == "api-test" for x in client.get("/api/shortlists").json())
    assert client.post("/api/shortlist/add", json={"name": "Bad Name", "kind": "pack", "ref": FOREST}).status_code == 400


def test_concurrent_requests_for_one_new_thumbnail(env, client):
    """Every request succeeds and no temp files are left (Windows refuses two writers on one file)."""
    from concurrent.futures import ThreadPoolExecutor

    path = "Kenney Game Assets All-in-1/2D assets/Pixel Shmup/Preview.png"
    with ThreadPoolExecutor(8) as ex:
        rs = list(ex.map(lambda _: client.get("/thumb", params={"path": path, "s": 128}), range(16)))
    assert [r.status_code for r in rs] == [200] * 16
    assert all(r.content[:4] == b"RIFF" for r in rs)
    thumbs = env["cfg"].cache / "thumbs"
    assert not list(thumbs.rglob("*.tmp"))



def test_thumbnail_served_even_if_cache_write_fails(env, client, monkeypatch, capsys):
    """A cache file Windows keeps locked is not a broken image: the bytes are served anyway."""
    import os

    def locked(*a, **k):
        raise PermissionError("locked by another process")

    monkeypatch.setattr(os, "replace", locked)
    path = "Bundles/Space Bundle/Space Ships/Preview.png"
    r = client.get("/thumb", params={"path": path, "s": 96})
    assert r.status_code == 200 and r.content[:4] == b"RIFF"
    assert "not cached (served anyway)" in capsys.readouterr().err
    assert not list((env["cfg"].cache / "thumbs").rglob("*.tmp"))


def test_cached_thumbnail_read_while_being_replaced(env, client, monkeypatch):
    """Windows refuses to open a file for a moment while another request renames a fresh copy
    over it: the cached thumbnail is read again, not served as an error."""
    from pathlib import Path

    path = "Bundles/Space Bundle/Space Ships/Preview.png"
    first = client.get("/thumb", params={"path": path, "s": 64})  # now it's cached
    assert first.status_code == 200
    real, calls = Path.read_bytes, []

    def busy_once(self):
        if self.suffix == ".webp" and not calls:
            calls.append(self)
            raise PermissionError(13, "Permission denied", str(self))
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", busy_once)
    r = client.get("/thumb", params={"path": path, "s": 64})
    assert calls and r.status_code == 200 and r.content == first.content


def _nodes(tree):
    for n in tree:
        yield n
        yield from _nodes(n["kids"])


def test_layout_roles_preview_save_apply(client, env):
    g, key = "bundles/mega-fantasy-bundle", "graphics/hero-sprites"
    path = env["cfg"].curation_dir / "bundles" / "mega-fantasy-bundle.toml"
    original = path.read_bytes()
    v = client.post("/api/layout", json={"group": g}).json()
    by = {n["key"]: n for n in _nodes(v["tree"])}
    assert by["graphics"]["role"] == "grouping" and by[key]["role"] == "pack" and by[key]["pack"] == f"{g}/{key}"
    assert by[f"{key}/png"]["role"] == "content" and not v["stale"]
    assert v["summary"] == v["indexed"] == {"packs": 4, "collections": 0}

    # preview: nothing is written
    v = client.post("/api/layout", json={"group": g, "roles": {key: "collection"}}).json()
    by = {n["key"]: n for n in _nodes(v["tree"])}
    assert (by[key]["role"], by[key]["auto"], by[key]["set"]) == ("collection", "pack", "collection")
    assert by[f"{key}/png"]["role"] == "pack" and by[f"{key}/png"]["ctx"] == "collection"
    assert v["summary"] == {"packs": 6, "collections": 1} and path.read_bytes() == original

    # a sub-folder only takes a role once its parent is a collection
    assert client.post("/api/edit/roles", json={"group": g, "roles": {f"{key}/png": "ignore"}}).status_code == 400
    assert client.post("/api/edit/roles", json={"group": g, "roles": {key: "bogus"}}).status_code == 400
    v = client.post("/api/edit/roles", json={"group": g, "roles": {key: "collection", f"{key}/png": "ignore"}}).json()
    assert v["stale"] and v["summary"] == {"packs": 5, "collections": 1} and v["indexed"]["packs"] == 4
    assert f'[packs."{key}"]\nrole = "collection"' in path.read_text(encoding="utf-8")

    try:
        r = client.post("/api/reindex").json()
        assert r["packs"] == 13
        v = client.post("/api/layout", json={"group": g}).json()
        assert not v["stale"] and v["indexed"] == {"packs": 5, "collections": 1}
        assert client.get("/api/pack", params={"id": f"{g}/{key}/psd"}).json()["parent_id"] == f"{g}/{key}"
    finally:
        client.post("/api/edit/roles", json={"group": g, "roles": {key: None, f"{key}/png": None}})
        assert path.read_bytes() == original
        client.post("/api/reindex")
    assert client.get("/api/meta").json()["stats"]["packs"] == 12
    assert snapshot(env["library"]) == env["before"]


def test_layout_unknown_group(client):
    assert client.post("/api/layout", json={"group": "bundles/nope"}).status_code == 404


def test_tag_edits_and_facets(client, env):
    from assetcat.curation import group_file

    gfile = group_file(env["cfg"], "bundles/mega-fantasy-bundle")
    before = gfile.read_bytes()
    m = client.get("/api/meta").json()
    assert "style:pixel-art" in m["tag_values"] and isinstance(m["tag_facets"], list)
    p = client.post("/api/edit/tags", json={"id": FOREST, "add": ["pixel-art", "swamp"]}).json()
    assert p["tags"] == ["style:pixel-art", "swamp"] and p["ai_tags"] == []
    m = client.get("/api/meta").json()
    assert {"ns": "style", "counts": {"pixel-art": 1}} in m["tag_facets"]
    r = client.get("/api/packs", params={"tag": "style:pixel-art,swamp"}).json()
    assert [c["id"] for c in r["items"]] == [FOREST] and r["items"][0]["tags"] == ["style:pixel-art", "swamp"]
    assert client.post("/api/edit/tags", json={"id": FOREST, "add": ["style:Bad Tag!"]}).status_code == 400
    client.post("/api/edit/tags", json={"id": FOREST, "remove": ["style:pixel-art", "swamp"]})
    assert gfile.read_bytes() == before


def test_waveform_and_shortlist_marks(client, env):
    r = client.get("/api/assets", params={"media": "audio", "limit": 50}).json()
    wav = next(x for x in r["items"] if x["ext"] in ("wav", "ogg"))
    peaks = client.get("/wave", params={"path": wav["path"], "n": 32}).json()
    assert 0 < len(peaks) <= 32 and max(peaks) == 1.0 and min(peaks) >= 0
    assert client.get("/wave", params={"path": wav["path"], "n": 32}).json() == peaks  # from the cache
    assert client.get("/wave", params={"path": "nope.wav"}).status_code == 404

    sl = env["cfg"].shortlist_dir
    before = {p.name for p in sl.glob("*.toml")} if sl.exists() else set()
    for name in ("ideas-a", "ideas-b"):
        client.post("/api/shortlist/add", json={"name": name, "kind": "pack", "ref": FOREST})
    marks = client.get("/api/shortlists/marks").json()
    assert {"ideas-a", "ideas-b"} <= set(marks[f"pack:{FOREST}"])
    for name in ("ideas-a", "ideas-b"):
        (sl / f"{name}.toml").unlink()
    assert {p.name for p in sl.glob("*.toml")} == before
