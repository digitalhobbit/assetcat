"""AI pack tags: vocabulary, override layers, and `classify` against a fake OpenAI-compatible server."""

from __future__ import annotations

import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from assetcat import ai as A
from assetcat import edit as E
from assetcat.cli import main
from assetcat.curation import group_file
from assetcat.indexer import run_index
from conftest import snapshot

FOREST = "bundles/mega-fantasy-bundle/graphics/forest-tileset"


def test_vocab_norm_and_layers(env):
    v = A.load_vocab(env["cfg"])
    assert v.norm("Pixel-Art") == "style:pixel-art" and v.norm("8bit") == "style:pixel-art"
    assert v.norm("forest") == "forest" and v.norm("genre:fantasy") == "genre:fantasy"
    ai = ["kind:tileset", "style:pixel-art", "genre:fantasy"]
    group = {"tags_remove": ["genre:fantasy"]}
    pack = {"tags": ["forest", "fantasy"], "tags_remove": ["pixel-art"]}
    assert A.apply_layers(ai, [group], v) == ["kind:tileset", "style:pixel-art"]
    # the pack re-adds what the group removed, and removes an AI tag
    assert A.apply_layers(ai, [group, pack], v) == ["kind:tileset", "forest", "genre:fantasy"]


def test_prompt_and_answer_parsing(env):
    v = A.load_vocab(env["cfg"])
    text, schema, version = A.build_prompt(v)
    assert '"pixel-art" = visible square pixels' in text and "perspective (at most 1)" in text
    assert schema["properties"]["perspective"]["maxItems"] == 1 and "subject" not in schema["properties"]
    assert len(version) == 8 and A.build_prompt(v)[2] == version
    got = A.parse_answer({"kind": ["tileset", "tileset", "nonsense"], "style": "pixel-art",
                          "perspective": ["top-down", "side"], "description": " Trees. "}, v)
    assert got == {"tags": ["kind:tileset", "style:pixel-art", "perspective:top-down"], "description": "Trees."}


class FakeVLM(BaseHTTPRequestHandler):
    schema_ok = True
    effort_ok = True
    calls: list[dict] = []

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send(200, {"data": [{"id": "fake/vlm"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        if "response_format" in body and not FakeVLM.schema_ok:
            return self._send(400, {"error": "Unimplemented keys: maxItems"})
        if "reasoning_effort" in body and not FakeVLM.effort_ok:
            return self._send(400, {"error": "Unrecognized request argument supplied: reasoning_effort"})
        FakeVLM.calls.append(body)
        text = body["messages"][0]["content"][0]["text"]
        if "Pack name: Forest Tileset" in text:
            ans = {"kind": ["tileset"], "style": ["pixel-art"], "perspective": ["top-down"], "genre": ["fantasy", "bogus"],
                   "description": "Forest tiles."}
        else:
            ans = {"kind": ["props"], "style": ["vector"], "perspective": [], "genre": [], "description": "Things."}
        content = json.dumps(ans) if "response_format" in body else "Sure! " + json.dumps(ans)
        self._send(200, {"choices": [{"message": {"content": content}}], "usage": {}})


@pytest.fixture
def vlm():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeVLM)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    FakeVLM.schema_ok, FakeVLM.effort_ok, FakeVLM.calls = True, True, []
    yield f"http://127.0.0.1:{srv.server_port}/v1"
    srv.shutdown()


def test_classify_overrides_and_reindex(env, con, vlm, capsys, monkeypatch):
    cfg = env["cfg"]
    ai_dir = cfg.profile / "ai"
    gfile = group_file(cfg, "bundles/mega-fantasy-bundle")
    before_curation = gfile.read_bytes()
    test_config = env["base"] / "config" / "ai-test.toml"  # a copy, so the AI settings stay in this test
    shutil.copy(env["base"] / "config" / "config.toml", test_config)
    monkeypatch.setenv("ASSETCAT_CONFIG", str(test_config))
    try:
        # no model configured: a clear message, nothing written
        with pytest.raises(SystemExit) as e:
            main(["classify", "--url", vlm])
        assert "no model set" in str(e.value) and "fake/vlm" in str(e.value)

        # a sample saves nothing
        main(["classify", "--url", vlm, "--model", "fake/vlm", "--sample", "2"])
        assert "2/2" in capsys.readouterr().out and not ai_dir.exists()

        main(["config", "ai_url", vlm])
        main(["config", "ai_model", "fake/vlm"])
        main(["classify", "--no-index"])
        out = capsys.readouterr().out
        shards = list(ai_dir.glob("fake_vlm/pack-tags-*.jsonl"))
        assert len(shards) == 1 and "Forest Tileset: kind:tileset style:pixel-art perspective:top-down genre:fantasy" in out
        first = next(c["messages"][0]["content"] for c in FakeVLM.calls
                     if "Pack name: Forest Tileset" in c["messages"][0]["content"][0]["text"])
        assert "Files by format:" in first[0]["text"] and any(c["type"] == "image_url" for c in first)
        assert all(c.get("reasoning_effort") == "none" for c in FakeVLM.calls)
        p = dict(con.execute("SELECT * FROM packs WHERE id=?", (FOREST,)).fetchone())
        assert json.loads(p["tags"])[:4] == ["kind:tileset", "style:pixel-art", "perspective:top-down", "genre:fantasy"]
        assert p["description"] == "Forest tiles." and p["description_source"] == "ai" and p["ai_stale"] == 0

        # everything is up to date: a second run does nothing
        n = len(FakeVLM.calls)
        main(["classify", "--no-index"])
        assert len(FakeVLM.calls) == n and "Nothing to tag" in capsys.readouterr().out

        # one-click edits: remove an AI tag, add one of yours, then undo both: byte-identical file
        E.edit_tags(cfg, con, FOREST, [], ["genre:fantasy"])
        E.edit_tags(cfg, con, FOREST, ["Perspective:Side", "swamp"], [])
        t = json.loads(con.execute("SELECT tags FROM packs WHERE id=?", (FOREST,)).fetchone()[0])
        assert "genre:fantasy" not in t and {"perspective:side", "swamp", "kind:tileset"} <= set(t)
        assert 'tags_remove = ["genre:fantasy"]' in gfile.read_text()
        E.edit_tags(cfg, con, FOREST, ["genre:fantasy"], ["perspective:side", "swamp"])
        assert gfile.read_bytes() == before_curation

        # a server without JSON schema or reasoning_effort support: the prompt alone, JSON found in the reply
        FakeVLM.schema_ok = FakeVLM.effort_ok = False
        n = len(FakeVLM.calls)
        main(["classify", "--pack", FOREST, "--redo", "--no-index"])
        assert "genre:fantasy" in capsys.readouterr().out
        assert "reasoning_effort" not in FakeVLM.calls[n]

        # the index rebuild loads the results from the profile; search finds AI tags
        run_index(cfg, workers=1)
        main(["search", "--packs", "pixel", "--json"])
        assert FOREST in [x["id"] for x in json.loads(capsys.readouterr().out)]
        main(["packs", "--tag", "pixel-art", "--json"])
        assert FOREST in [x["id"] for x in json.loads(capsys.readouterr().out)]
        main(["packs", "--tag", "pixel-art", "--tag", "kind:props", "--json"])
        assert json.loads(capsys.readouterr().out) == []
        main(["show", FOREST])
        assert "AI tags by fake/vlm" in capsys.readouterr().out
        main(["doctor", "--json"])
        assert "ai_tags" not in [i["id"] for i in json.loads(capsys.readouterr().out)["issues"]]
        assert snapshot(env["library"]) == env["before"]
    finally:
        if ai_dir.exists():
            shutil.rmtree(ai_dir)
        gfile.write_bytes(before_curation)
        run_index(cfg, workers=1)
