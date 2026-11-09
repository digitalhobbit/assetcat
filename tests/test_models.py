"""3D models: metadata from glTF/OBJ, bundled renders, search filters, thumbnails and pull."""

from __future__ import annotations

import io
import json
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from assetcat.pull import run_pull
from assetcat.server import create_app
from conftest import snapshot

M = "Kenney Game Assets All-in-1/3D assets/Mini Crates"
GLB = f"{M}/Models/GLB format"


@pytest.fixture(scope="module")
def client(env):
    with TestClient(create_app(env["cfg"])) as c:
        yield c


def info(con, rel):
    return json.loads(con.execute("SELECT info FROM files WHERE path=?", (rel,)).fetchone()[0])


def test_model_metadata(con):
    crate = info(con, f"{GLB}/crate.glb")
    assert crate == {"kind": "model", "tris": 12, "meshes": 1, "refs": [f"{GLB}/Textures/colormap.png"]}
    assert info(con, f"{GLB}/barrel.glb")["clips"] == ["spin"]
    obj = info(con, f"{M}/Models/OBJ format/crate.obj")
    assert obj["tris"] == 12  # six quads
    assert obj["refs"] == [f"{M}/Models/OBJ format/crate.mtl", f"{M}/Models/OBJ format/Textures/colormap.png"]
    assert info(con, f"{M}/Models/glTF format/rock.gltf")["refs"] == [f"{M}/Models/glTF format/rock.bin"]


def test_model_assets_and_renders(con):
    rows = {r["name"]: dict(r) for r in con.execute(
        "SELECT * FROM assets WHERE pack_id='kenney/3d-assets/mini-crates' AND media='model'")}
    assert rows["crate"]["media"] == "model" and rows["crate"]["formats"] == "glb,obj"
    assert rows["crate"]["preview"] == f"{M}/Previews/crate.png"
    assert rows["barrel"]["clips"] == 1 and rows["barrel"]["preview"] is None
    assert rows["lamp"]["missing"] == 1 and rows["crate"]["missing"] is None
    render = con.execute("SELECT preview_of FROM assets WHERE path=?", (f"{M}/Previews/crate.png",)).fetchone()[0]
    assert render == rows["crate"]["id"]
    kind = con.execute("SELECT kind FROM packs WHERE id='kenney/3d-assets/mini-crates'").fetchone()[0]
    assert kind == "3d"


def test_asset_api(client):
    r = client.get("/api/assets", params={"pack": "kenney/3d-assets/mini-crates"}).json()
    names = [i["name"] for i in r["items"]]
    assert "crate" in names and names.count("crate") == 1  # its render isn't listed separately
    r = client.get("/api/assets", params={"media": "model", "animated": True}).json()
    assert [i["name"] for i in r["items"]] == ["barrel"]
    lamp = next(i for i in client.get("/api/assets", params={"media": "model"}).json()["items"] if i["name"] == "lamp")
    d = client.get("/api/asset", params={"id": lamp["id"]}).json()
    assert d["model"][f"{GLB}/lamp.glb"]["missing"] == [f"{GLB}/Textures/lamp.png"]


def test_files_the_viewer_loads(client):
    # three.js resolves textures and buffers relative to the model's URL
    assert client.get("/lib/" + quote(f"{GLB}/Textures/colormap.png")).status_code == 200
    assert client.get("/lib/" + quote(f"{M}/Models/glTF format/rock.bin")).status_code == 200
    assert client.get("/web/vendor/three/three.module.js").status_code == 200
    assert client.get("/web/vendor/three/addons/loaders/GLTFLoader.js").status_code == 200
    assert client.get("/web/model3d.js").status_code == 200


def test_thumbnails(client, env):
    t = client.get("/thumb", params={"path": f"{GLB}/crate.glb", "s": 128})  # the bundled render
    assert t.status_code == 200 and t.headers["content-type"].startswith("image/")
    assert client.get("/thumb", params={"path": f"{GLB}/barrel.glb", "s": 128}).status_code == 404  # not rendered yet

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGBA", (256, 256), (200, 100, 50, 255)).save(buf, "PNG")
    before = snapshot(env["library"])
    r = client.post("/api/render", params={"path": f"{GLB}/barrel.glb"}, content=buf.getvalue())
    assert r.status_code == 200
    t = client.get("/thumb", params={"path": f"{GLB}/barrel.glb", "s": 128})
    assert t.status_code == 200 and t.headers["content-type"] == "image/webp"
    assert snapshot(env["library"]) == before  # renders live in the cache

    assert client.post("/api/render", params={"path": f"{GLB}/barrel.glb"}, content=b"nope").status_code == 400
    assert client.post("/api/render", params={"path": f"{M}/Preview.png"}, content=buf.getvalue()).status_code == 400
    assert client.post("/api/render", params={"path": "../x.glb"}, content=buf.getvalue()).status_code == 404


def files_in(d: Path) -> list[str]:
    return sorted(p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file())


def test_pull_brings_textures(env, con, tmp_path):
    r = run_pull(env["cfg"], con, [f"{GLB}/crate.glb", f"{M}/Models/OBJ format/crate.obj"], str(tmp_path))
    # one Textures/colormap.png: the GLB's and the OBJ's copies are the same bytes
    assert files_in(tmp_path) == ["License-Mini-Crates.txt", "Textures/colormap.png", "crate.glb", "crate.mtl", "crate.obj"]
    assert r["warnings"] == []
    r = run_pull(env["cfg"], con, [f"{GLB}/lamp.glb"], str(tmp_path / "lamp"))
    assert len(r["warnings"]) == 1 and "Textures/lamp.png" in r["warnings"][0]


def test_pull_flatten_keeps_model_files_next_to_it(env, con, tmp_path):
    run_pull(env["cfg"], con, [f"{M}/Models/glTF format", GLB], str(tmp_path), flatten=True)
    got = files_in(tmp_path)
    assert {"rock.gltf", "rock.bin", "crate.glb", "Textures/colormap.png"} <= set(got)
    assert "colormap.png" not in got


def test_new_model_metadata_rereads_only_models(env, con):
    """Indexes from before 3D support get model metadata without re-reading every other file."""
    from assetcat.indexer import run_index

    png = f"{M}/Preview.png"
    con.execute("UPDATE files SET meta_version=2, info=NULL WHERE path=?", (f"{GLB}/crate.glb",))
    con.execute("UPDATE files SET meta_version=2 WHERE path=?", (png,))
    con.commit()
    run_index(env["cfg"], workers=1)
    assert info(con, f"{GLB}/crate.glb")["tris"] == 12
    assert con.execute("SELECT meta_version FROM files WHERE path=?", (png,)).fetchone()[0] == 2
