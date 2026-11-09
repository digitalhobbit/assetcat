"""AI pack tags: a vision-language model behind an OpenAI-compatible chat API (LM Studio, Ollama,
llama.cpp's server, vLLM or a cloud API) looks at each pack and answers with tags from the
profile's `tags.toml` vocabulary.

- **Input per pack:** its cover and contact sheet, plus text the index already knows: name,
  bundle, asset types, file formats, common image sizes, folders and a readme excerpt.
- **Results** go in the profile, one JSON Lines shard per model and machine, append-only:
  `ai/<model>/pack-tags-<machine>.jsonl`. Readers merge every shard and take the newest result
  per pack, so a second machine never recomputes and a newer model's results win.
- **Fresh** means the result was made with the current prompt (a hash of it, so editing the
  vocabulary re-tags) and the pack still has the same number of files.
- **Human overrides** live in curation, not here: `tags` adds, `tags_remove` removes, at group or
  pack level (see `apply_layers`). Re-running a model never undoes them.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import io
import json
import os
import re
import socket
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .config import STARTER_DIR, Config

DEFAULT_URL = "http://127.0.0.1:1234/v1"  # LM Studio's local server
RECOMMENDED_MODEL = "google/gemma-4-26b-a4b-qat"
KEY_ENV = "ASSETCAT_AI_KEY"  # sent as a bearer token when set (cloud APIs)
IMAGE_SIDE = 1024


class AIError(Exception):
    pass


# --- vocabulary --------------------------------------------------------------------------------

@dataclass
class Vocab:
    namespaces: dict[str, dict]
    aliases: dict[str, str] = field(default_factory=dict)
    by_value: dict[str, list[str]] = field(default_factory=dict)  # value -> namespaces that have it

    def norm(self, tag: str) -> str:
        """Canonical form of a tag: lowercase, aliases applied, and a bare value that belongs to
        exactly one namespace gets it ("pixel-art" -> "style:pixel-art"). Free-form tags stay."""
        t = str(tag).strip().lower()
        t = self.aliases.get(t, t)
        if ":" not in t and len(self.by_value.get(t, [])) == 1:
            t = f"{self.by_value[t][0]}:{t}"
        return t

    def pack_namespaces(self) -> list[tuple[str, dict]]:
        """Namespaces the pack classification answers: level "pack" with a list of values."""
        return [(ns, s) for ns, s in self.namespaces.items() if s.get("level") == "pack" and s.get("values")]

    def facets(self) -> list[str]:
        return [ns for ns, s in self.namespaces.items() if s.get("facet")]


def load_vocab(cfg: Config) -> Vocab:
    p = cfg.profile / "tags.toml"
    if not p.exists():
        p = STARTER_DIR / "tags.toml"
    data = tomllib.loads(p.read_text(encoding="utf-8"))
    nss = data.get("namespaces", {})
    by_value: dict[str, list[str]] = {}
    for ns, s in nss.items():
        for v in s.get("values", []):
            by_value.setdefault(v, []).append(ns)
    return Vocab(nss, {k.lower(): v for k, v in data.get("aliases", {}).items()}, by_value)


def apply_layers(ai_tags: list[str], layers: list[dict], vocab: Vocab) -> list[str]:
    """Effective tags: the AI's, then each curation layer in turn (group, parent packs, the pack)
    removes its `tags_remove` and adds its `tags`. A later layer can re-add what an earlier one
    removed."""
    out = list(dict.fromkeys(vocab.norm(t) for t in ai_tags))
    for layer in layers:
        removed = {vocab.norm(t) for t in layer.get("tags_remove", [])}
        out = [t for t in out if t not in removed]
        for t in layer.get("tags", []):
            n = vocab.norm(t)
            if n and n not in out:
                out.append(n)
    return out


# --- prompt ------------------------------------------------------------------------------------

INTRO = """You tag game asset packs for a searchable catalog. The images show one pack: its cover (if it has one) and a contact sheet of its files. The text below them is what the file index knows about the pack.
Tag what the pack IS, not every object that appears in it: a UI pack with a potion icon is kind "ui", not "props".
Only use a value if it clearly applies. Leave a list empty when unsure or when the namespace doesn't apply."""


def build_prompt(vocab: Vocab) -> tuple[str, dict, str]:
    """(instructions, JSON schema, prompt version). The version is a hash of both, so a change
    to the vocabulary or the wording makes earlier results stale."""
    lines = [INTRO, ""]
    for ns, s in vocab.pack_namespaces():
        lines.append(f"{ns} (at most {s.get('max', 2)}): {s.get('about', '')}".rstrip())
        for v, h in s.get("hints", {}).items():
            lines.append(f'  "{v}" = {h}')
    lines.append('description: one sentence, under 25 words, saying what the pack contains.')
    lines.append("")
    lines.append("Answer with only a JSON object: {" + ", ".join(f'"{ns}": [...]' for ns, _ in vocab.pack_namespaces())
                 + ', "description": "..."}, using only these values: '
                 + "; ".join(f"{ns}: {', '.join(s['values'])}" for ns, s in vocab.pack_namespaces()))
    text = "\n".join(lines)
    props = {ns: {"type": "array", "maxItems": int(s.get("max", 2)), "items": {"type": "string", "enum": list(s["values"])}}
             for ns, s in vocab.pack_namespaces()}
    props["description"] = {"type": "string", "maxLength": 200}
    schema = {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}
    version = hashlib.blake2b((text + json.dumps(schema, sort_keys=True)).encode(), digest_size=4).hexdigest()
    return text, schema, version


def parse_answer(out: dict, vocab: Vocab) -> dict:
    """Tags from the model's JSON, checked against the vocabulary (a server without schema
    support may answer anything), deduplicated and capped."""
    tags = []
    for ns, s in vocab.pack_namespaces():
        vals = out.get(ns) or []
        if isinstance(vals, str):
            vals = [vals]
        ok = [v for v in dict.fromkeys(str(v).strip().lower() for v in vals) if v in s["values"]]
        tags += [f"{ns}:{v}" for v in ok[: int(s.get("max", 2))]]
    desc = out.get("description")
    return {"tags": tags, "description": desc.strip()[:300] if isinstance(desc, str) and desc.strip() else None}


# --- input -------------------------------------------------------------------------------------

def pack_input(con, cfg: Config, p: dict) -> tuple[str, list[str]]:
    """The text and images (base64 PNG) the model gets for a pack."""
    from .indexer import descendants

    ids = descendants(con, p["id"])
    q = ",".join("?" * len(ids))
    group = con.execute("SELECT name FROM groups WHERE id=?", (p["group_id"],)).fetchone()
    parent = con.execute("SELECT name FROM packs WHERE id=?", (p["parent_id"],)).fetchone() if p["parent_id"] else None
    exts = con.execute(f"SELECT ext, COUNT(*) FROM files WHERE pack_id IN ({q}) AND category IN ('image','vector','source','model','audio','font') "
                       "GROUP BY ext ORDER BY 2 DESC LIMIT 8", ids).fetchall()
    sizes = con.execute(f"SELECT width || 'x' || height, COUNT(*) FROM assets WHERE pack_id IN ({q}) AND width > 0 AND type != 'preview' "
                        "GROUP BY 1 ORDER BY 2 DESC LIMIT 3", ids).fetchall()
    folders: dict[str, int] = {}
    for (path,) in con.execute(f"SELECT path FROM assets WHERE pack_id IN ({q})", ids):
        parts = path[len(p["path"]) + 1:].split("/")[:-1][:2]
        if parts:
            k = "/".join(parts)
            folders[k] = folders.get(k, 0) + 1
    readme = con.execute("SELECT text FROM docs WHERE pack_id=? AND role='readme' ORDER BY length(path) LIMIT 1", (p["id"],)).fetchone()
    shown = sorted(folders.items())[:30]
    lines = [f"Pack name: {p['name']}",
             f"In: {group[0] if group else p['group_id']}" + (f" / {parent[0]}" if parent else ""),
             f"Asset types (from file analysis): {p['type_counts']}"]
    if exts:
        lines.append("Files by format: " + ", ".join(f"{e} {n}" for e, n in exts))
    if sizes:
        lines.append("Most common image sizes in pixels: " + ", ".join(f"{s} ({n})" for s, n in sizes))
    if shown:
        lines.append("Folders (assets in each): " + ", ".join(f"{k} ({n})" for k, n in shown)
                     + (f", and {len(folders) - len(shown)} more" if len(folders) > len(shown) else ""))
    if readme and readme[0].strip():
        lines.append("Readme excerpt: " + " ".join(readme[0].split())[:600])

    images = []
    if p["cover_kind"] == "bundled" and p["cover"]:
        images.append(cfg.fs(p["cover"]))
    if p["sheet"] and Path(p["sheet"]).exists():
        images.append(p["sheet"])
    return "\n".join(lines), [b for b in (_image_b64(f) for f in images) if b]


def _image_b64(path: str) -> str | None:
    from PIL import Image

    try:
        with Image.open(path) as im:
            im.load()
            im = im.convert("RGBA")
    except Exception:
        return None  # a placeholder that isn't synced yet, or not an image
    bg = Image.new("RGBA", im.size, (128, 128, 128, 255))
    im = Image.alpha_composite(bg, im).convert("RGB")
    im.thumbnail((IMAGE_SIDE, IMAGE_SIDE))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def taggable(p: dict) -> bool:
    return (p["asset_count"] or 0) > 0 and p["kind"] not in ("engine", "archived", "other")


# --- the model server --------------------------------------------------------------------------

class Client:
    """An OpenAI-compatible chat completions endpoint."""

    def __init__(self, url: str, model: str):
        self.url = url.rstrip("/")
        self.model = model
        self.key = os.environ.get(KEY_ENV)
        self.schema_ok = True  # cleared when the server rejects JSON schema output
        self.effort_ok = True  # cleared when the server rejects reasoning_effort

    def _req(self, path: str, body: dict | None = None, timeout: float = 600):
        headers = {"content-type": "application/json"}
        if self.key:
            headers["authorization"] = f"Bearer {self.key}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data, headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise AIError(f"{e.code} from {self.url}{path}: {e.read().decode(errors='replace')[:400]}") from e
        except (urllib.error.URLError, ConnectionError, socket.timeout) as e:
            raise AIError(f"can't reach a model server at {self.url} ({getattr(e, 'reason', e)}). "
                          "Start LM Studio's local server (Developer tab, or `lms server start`), "
                          "or point `ai_url` at yours: `assetcat config ai_url <url>`") from e

    def models(self) -> list[str]:
        return [m["id"] for m in self._req("/models", timeout=15).get("data", [])]

    def check(self):
        ids = self.models()
        if ids and self.model not in ids:
            raise AIError(f"model '{self.model}' isn't on {self.url}. Available: {', '.join(ids)}")

    def tag(self, prompt: str, schema: dict, text: str, images: list[str]) -> tuple[dict, dict]:
        """(parsed JSON answer, usage)."""
        content = [{"type": "text", "text": prompt + "\n\n" + text}]
        content += [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + b}} for b in images]
        body = {"model": self.model, "temperature": 0, "messages": [{"role": "user", "content": content}]}
        if self.schema_ok:
            try:
                r = self._chat({**body, "max_tokens": 500, "response_format": {
                    "type": "json_schema", "json_schema": {"name": "pack_tags", "strict": True, "schema": schema}}})
                return _json_from(r), r.get("usage", {})
            except AIError as e:
                if not str(e).startswith("4"):
                    raise
                self.schema_ok = False  # no (or partial) JSON schema support: ask in the prompt only
        r = self._chat({**body, "max_tokens": 3000})
        return _json_from(r), r.get("usage", {})

    def _chat(self, body: dict) -> dict:
        # Models that think before answering (Gemma 4, Qwen 3, ...) spend max_tokens on reasoning
        # and can run out before the answer; tagging doesn't need it, so ask for none.
        if self.effort_ok:
            try:
                return self._req("/chat/completions", {**body, "reasoning_effort": "none"})
            except AIError as e:
                if not (str(e).startswith("4") and "reasoning" in str(e).lower()):
                    raise
                self.effort_ok = False
        return self._req("/chat/completions", body)


def _json_from(r: dict) -> dict:
    choice = (r.get("choices") or [{}])[0]
    msg = choice.get("message", {}).get("content") or ""
    msg = re.sub(r"<think>.*?</think>", "", msg, flags=re.S)
    if not msg.strip() and choice.get("finish_reason") == "length":
        raise AIError("the model ran out of tokens before answering, probably while reasoning. "
                      "Turn its reasoning (thinking) off in the model server's settings")
    try:
        return json.loads(msg[msg.index("{"): msg.rindex("}") + 1])
    except ValueError as e:
        raise AIError(f"the model didn't answer with JSON: {msg[:200]!r}") from e


# --- results in the profile --------------------------------------------------------------------

def model_slug(model: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", model).strip("_") or "model"


def machine_name(uc: dict) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", str(uc.get("machine") or socket.gethostname()).lower().split(".")[0]).strip("-") or "machine"


def shard_path(cfg: Config, model: str, machine: str) -> Path:
    return cfg.profile / "ai" / model_slug(model) / f"pack-tags-{machine}.jsonl"


def append_result(cfg: Config, model: str, machine: str, rec: dict):
    p = cfg.writable(shard_path(cfg, model, machine))
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load_results(cfg: Config, model: str | None = None) -> dict[str, dict]:
    """Newest pack-tag result per pack id over every shard (of one model, if given)."""
    root = cfg.profile / "ai"
    out: dict[str, dict] = {}
    if not root.is_dir():
        return out
    for p in sorted(root.glob("*/pack-tags-*.jsonl")):
        if model and p.parent.name != model_slug(model):
            continue
        with open(p, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue  # a line cut short by a sync conflict or a crash
                pid = rec.get("pack")
                if pid and (pid not in out or rec.get("at", "") >= out[pid].get("at", "")):
                    out[pid] = rec
    return out


def is_fresh(rec: dict | None, p: dict, version: str) -> bool:
    return bool(rec) and rec.get("prompt") == version and rec.get("files") == p["file_count"]


def now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")
