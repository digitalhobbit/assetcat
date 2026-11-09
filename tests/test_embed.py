"""Image vectors: `classify --kind embed`, search by meaning and "more like this", with a fake model
(the real one needs PyTorch, which CI doesn't install)."""

from __future__ import annotations

import json
import shutil

import numpy as np
import pytest
from fastapi.testclient import TestClient

from assetcat import embed as M
from assetcat.cli import main
from assetcat.server import create_app
from conftest import snapshot


class FakeEncoder:
    """Vectors from an image's mean color; texts name a color."""

    side = 32
    device = "fake"
    COLORS = {"red": (1, 0, 0), "green": (0, 1, 0), "blue": (0, 0, 1)}

    def __init__(self, name):
        self.name = name

    @staticmethod
    def _norm(v):
        v = np.asarray(v, np.float32) + 1e-6
        return (v / np.linalg.norm(v, axis=-1, keepdims=True)).astype(np.float16)

    def images(self, ims):
        return self._norm([np.asarray(im, np.float32).reshape(-1, 3).mean(0) for im in ims])

    def texts(self, texts):
        # "a knight" looks like the (gray-ish) noise images; other subjects like pure blue
        return self._norm([self.COLORS.get(t.split()[-1], (1, 1, 1) if t == "a knight" else (0, 0, 1)) for t in texts])

    def logits(self):
        return 20.0, -18.0


@pytest.fixture
def fake_model(env, monkeypatch):
    monkeypatch.setattr(M, "Encoder", FakeEncoder)
    yield
    ai = env["cfg"].profile / "ai"
    if ai.exists():
        shutil.rmtree(ai)


def test_embed_search_and_similar(env, con, fake_model, capsys):
    cfg = env["cfg"]
    main(["classify", "--kind", "embed", "--limit", "5"])  # a partial run, as if stopped...
    first = M.done_hashes(cfg, M.DEFAULT_MODEL)
    assert len(first) == 5
    main(["classify", "--kind", "embed"])  # ...and the rest
    out = capsys.readouterr().out
    assert "already had vectors" in out
    shards = M.shards(cfg, M.DEFAULT_MODEL)
    assert len(shards) == 2 and all(p.with_suffix(".jsonl").exists() for p in shards)
    v = M.Vectors(cfg, con, M.DEFAULT_MODEL)
    n = len(v)
    assert n > 10
    main(["classify", "--kind", "embed", "--json"])  # nothing left
    assert json.loads(capsys.readouterr().out)["embedded"] == 0

    # subjects: scored from the stored vectors by the index (the classify runs above re-indexed)
    assert M.load_subject_texts(cfg, M.DEFAULT_MODEL)["labels"][0] == "knight"
    subs = con.execute("SELECT subject, COUNT(*) FROM asset_subjects GROUP BY 1").fetchall()
    assert [tuple(x) for x in subs][0][0] == "knight" and len(subs) == 1
    contains = [json.loads(c) for (c,) in con.execute("SELECT contains FROM packs WHERE contains IS NOT NULL")]
    assert contains and all(set(c) == {"knight"} for c in contains)
    main(["search", "knight", "--json", "--limit", "100"])  # words find what images show
    hits = json.loads(capsys.readouterr().out)
    assert len(hits) >= subs[0][1] * 0.9 or len(hits) == 100

    # more like this: an image is most similar to itself (or an exact duplicate)
    path = con.execute("SELECT a.path FROM assets a JOIN files f ON f.path=a.path WHERE a.media='visual' AND f.ext='png' "
                       "ORDER BY a.path LIMIT 1").fetchone()[0]
    main(["search", "--similar", path, "--json", "--limit", "3"])
    res = json.loads(capsys.readouterr().out)
    assert res[0]["score"] > 0.99 and [r["score"] for r in res] == sorted((r["score"] for r in res), reverse=True)
    # by meaning, with filters: only sheets/tilesets
    main(["search", "--meaning", "something", "red", "--type", "tileset", "--json"])
    res = json.loads(capsys.readouterr().out)
    assert res and {r["type"] for r in res} == {"tileset"}
    reds = [np.asarray(r["score"]) for r in res]
    assert reds == sorted(reds, reverse=True)

    # the browse UI
    with TestClient(create_app(cfg)) as client:
        assert client.get("/api/meta").json()["embed"] == {"model": M.DEFAULT_MODEL, "ready": True}
        aid = con.execute("SELECT id FROM assets WHERE path=?", (path,)).fetchone()[0]
        r = client.get("/api/assets", params={"similar": aid, "limit": 5}).json()
        assert r["items"][0]["score"] > 0.99 and r["total"] == n
        r = client.get("/api/assets", params={"q": "blue", "meaning": True, "media": "visual", "limit": 5}).json()
        assert len(r["items"]) == 5 and r["items"][0]["score"] >= r["items"][-1]["score"]
        r = client.get("/api/assets", params={"q": "blue"}).json()  # without meaning: words, as before
        assert all("score" not in x for x in r["items"])
    assert snapshot(env["library"]) == env["before"]


def test_no_vectors_yet(env, capsys):
    with pytest.raises(SystemExit) as e:
        main(["search", "--meaning", "slime"])
    assert "classify --kind embed" in str(e.value)


def test_frames_of_sheets_and_strips(tmp_path):
    from PIL import Image

    assert M.cell_for({"type": "sprite", "width": 224, "height": 32}) == (32, 32, 0, 0)  # a 7-frame strip
    assert M.cell_for({"type": "sprite", "width": 64, "height": 32}) is None  # a wide sprite, not a strip
    assert M.cell_for({"type": "ui", "width": 300, "height": 50}) is None  # UI stays whole
    assert M.cell_for({"type": "sheet", "width": 64, "height": 64, "grid_w": 16, "grid_h": 16, "grid_cols": 4, "grid_rows": 4,
                       "grid_spacing": 0, "grid_margin": 0}) == (16, 16, 0, 0)
    # frame 0 is empty, frame 1 has a 4x2 red blob at its bottom: that frame, cropped to the blob
    im = Image.new("RGBA", (96, 32), (0, 0, 0, 0))
    im.paste((255, 0, 0, 255), (32 + 10, 28, 32 + 14, 30))
    p = tmp_path / "strip.png"
    im.save(p)
    out = M.prepare(str(p), 64, (32, 32, 0, 0))
    assert out.size == (64, 64)
    assert out.getpixel((32, 32))[0] > 200  # the blob fills the middle, not a speck at the bottom


def test_needs_download(tmp_path, monkeypatch):
    """The first use of a model announces its download; a cached model or a local folder doesn't."""
    import sys
    import types

    cache = {"google/cached-model": "/hf/cache/config.json"}
    hub = types.ModuleType("huggingface_hub")
    hub.try_to_load_from_cache = lambda repo, name: cache.get(repo)
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    assert M.needs_download("google/new-model")
    assert not M.needs_download("google/cached-model")
    assert not M.needs_download(str(tmp_path))  # a model in a local folder
