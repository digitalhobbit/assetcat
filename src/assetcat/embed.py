"""Image embeddings: search by description ("a slime monster") and "more like this".

- **Model:** a SigLIP 2 image-text model run in-process with PyTorch (the `ai` extra), weights
  downloaded from Hugging Face on first use. Default `google/siglip2-base-patch16-256`; bigger ones
  find somewhat more on hard queries at 3-4x the time (`embed_model` in the user config).
- **Keyed by content hash** (BLAKE2, as the indexer's duplicate hashes): duplicates across
  bundles are embedded once, renamed or moved files keep their vectors.
- **Stored in the profile**, append-only, per model and machine: `ai/<model>/embed-<machine>-NNNN.npy`
  (float16, normalized) with `.jsonl` alongside (one line per row: hash, path, size). Chunks are
  never rewritten; a run that stops writes what it has as a smaller chunk.
- **Sheets and strips** are embedded by one frame (`cell_for`), recorded as `frame` in the
  `.jsonl`; when a newer chunk has a hash again, its vector wins.
- **Matching** a vector to today's assets: by path and size first, then by the file's hash in the
  index (the indexer hashes every file whose size collides, which covers duplicates), so a second
  machine never has to hash the whole library.
- **Subjects** (what an image shows: potion, sword, slime) are scored zero-shot from the stored
  vectors against "a <subject>" for each value of the `subject` namespace in `tags.toml`. The
  subject text vectors are stored next to the image vectors (`subjects.npz`), so `index` scores
  them with NumPy alone, and a vocabulary change re-scores without touching the images.
- **Search** is brute-force cosine similarity in NumPy over all vectors (~350 MB for 225k images
  with the base model). Only typed descriptions need the model; "more like this" doesn't.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import numpy as np

from .ai import model_slug
from .config import Config

DEFAULT_MODEL = "google/siglip2-base-patch16-256"
CHUNK = 10_000
LOADABLE = {"png", "gif", "jpg", "jpeg", "webp", "bmp"}
INSTALL_HINT = ("needs the AI extra (PyTorch and transformers): "
                "uv tool install --force 'assetcat[ai] @ git+https://github.com/digitalhobbit/assetcat'")


class EmbedError(Exception):
    pass


def model_name(uc: dict, override: str | None = None) -> str:
    return override or uc.get("embed_model") or DEFAULT_MODEL


# --- the model ---------------------------------------------------------------------------------

def needs_download(name: str) -> bool:
    """Whether the model isn't in the local Hugging Face cache yet (a local folder never is)."""
    if Path(name).is_dir():
        return False
    try:
        from huggingface_hub import try_to_load_from_cache
    except ImportError:
        return False
    return not isinstance(try_to_load_from_cache(name, "config.json"), str)


class Encoder:
    """A SigLIP-style model: images and texts to normalized vectors in the same space."""

    def __init__(self, name: str):
        import logging

        logging.getLogger("torch.distributed.elastic.multiprocessing.redirects").setLevel(logging.ERROR)  # a macOS note
        try:
            import torch
            import transformers
            from transformers import AutoModel, AutoProcessor
        except ImportError as e:
            raise EmbedError(INSTALL_HINT) from e
        transformers.logging.set_verbosity_error()  # config notes
        if needs_download(name):  # first use: say so, and show the download's progress bars
            from .config import eprint

            eprint(f"downloading {name} (first use only; the default model is about 1.5 GB)...")
            transformers.logging.enable_progress_bar()
        else:
            transformers.logging.disable_progress_bar()  # the weight-loading bar on every run
        self.torch = torch
        self.device = "cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu"
        self.dtype = torch.float32 if self.device == "cpu" else torch.float16
        try:
            self.model = AutoModel.from_pretrained(name, torch_dtype=self.dtype).to(self.device).eval()
            self.proc = AutoProcessor.from_pretrained(name)
        except OSError as e:
            raise EmbedError(f"can't load model '{name}': {e}") from e
        size = self.proc.image_processor.size
        self.side = size.get("height") or size.get("shortest_edge") or 256

    def logits(self) -> tuple[float, float] | None:
        """SigLIP's learned scale and bias: sigmoid(scale * cos + bias) is a per-label probability."""
        m = self.model
        if not hasattr(m, "logit_bias"):
            return None
        return float(m.logit_scale.detach().exp()), float(m.logit_bias.detach())

    def _out(self, x):
        v = x if isinstance(x, self.torch.Tensor) else x.pooler_output
        return self.torch.nn.functional.normalize(v.float(), dim=-1).cpu().numpy().astype(np.float16)

    def images(self, ims: list) -> np.ndarray:
        px = self.proc(images=ims, return_tensors="pt", do_resize=False)["pixel_values"].to(self.device, self.dtype)
        with self.torch.no_grad():
            return self._out(self.model.get_image_features(pixel_values=px))

    def texts(self, texts: list[str]) -> np.ndarray:
        tx = self.proc(text=texts, padding="max_length", max_length=64, truncation=True, return_tensors="pt").to(self.device)
        with self.torch.no_grad():
            return self._out(self.model.get_text_features(**tx))


FRAMED = {"sprite", "character", "vfx", "sheet"}


def cell_for(r: dict) -> tuple[int, int, int, int] | None:
    """The frame size (w, h, spacing, margin) to embed instead of the whole image, for sprite
    sheets and animation strips: a whole 8-frame strip padded to a square shows the model eight
    specks. From the grid when one is known, else a strip of 3+ square frames."""
    if r.get("type") not in FRAMED:
        return None
    if r.get("grid_w") and (r.get("grid_cols") or 1) * (r.get("grid_rows") or 1) > 1:
        return r["grid_w"], r["grid_h"], r.get("grid_spacing") or 0, r.get("grid_margin") or 0
    w, h = r.get("width") or 0, r.get("height") or 0
    if w and h and h <= 256 and w % h == 0 and 3 <= w // h <= 64:
        return h, h, 0, 0
    if w and h and w <= 256 and h % w == 0 and 3 <= h // w <= 64:
        return w, w, 0, 0
    return None


def _best_cell(im, cell):
    """Of the first 16 cells, the one with the most visible pixels (an effect's first frame is
    often nearly empty)."""
    cw, ch, sp, mg = cell
    best, best_n = None, -1
    k = 0
    for y in range(mg, im.height - ch + 1, ch + sp):
        for x in range(mg, im.width - cw + 1, cw + sp):
            c = im.crop((x, y, x + cw, y + ch))
            n = sum(c.getchannel("A").point(lambda a: 255 if a > 16 else 0).histogram()[255:])
            if n > best_n:
                best, best_n = c, n
            k += 1
            if k >= 16:
                return best
    return best or im


def prepare(path: str, side: int, cell: tuple | None = None):
    """An image as the model should see it: first frame (or the best cell of a sheet), transparency
    on mid-gray, padded square, small pixel art scaled up by whole numbers (nearest neighbor) so
    its pixels stay sharp."""
    from PIL import Image

    try:
        with Image.open(path) as im:
            im.seek(0)
            im = im.convert("RGBA")
    except Exception:
        return None
    if cell:
        im = _best_cell(im, cell)
        # a frame leaves room for the animation: crop to what's visible (tested: whole images
        # don't gain from this, frames do)
        bb = im.getchannel("A").point(lambda a: 255 if a > 16 else 0).getbbox()
        if bb:
            im = im.crop(bb)
    w, h = im.size
    if not w or not h:
        return None
    s = max(w, h)
    sq = Image.new("RGBA", (s, s), (128, 128, 128, 255))
    sq.alpha_composite(im, ((s - w) // 2, (s - h) // 2))
    sq = sq.convert("RGB")
    if s < side:
        k = max(1, side // s)
        sq = sq.resize((s * k, s * k), Image.NEAREST)
    return sq.resize((side, side), Image.BICUBIC if sq.size[0] > side else Image.NEAREST)


# --- shards in the profile ---------------------------------------------------------------------

def store_dir(cfg: Config, model: str) -> Path:
    return cfg.profile / "ai" / model_slug(model)


def shards(cfg: Config, model: str) -> list[Path]:
    return sorted(store_dir(cfg, model).glob("embed-*.npy"))


def write_chunk(cfg: Config, model: str, machine: str, vecs: np.ndarray, meta: list[dict]) -> Path:
    d = store_dir(cfg, model)
    cfg.writable(d / "x")
    d.mkdir(parents=True, exist_ok=True)
    nums = [int(m.group(1)) for p in d.glob(f"embed-{machine}-*.npy") if (m := re.search(r"-(\d+)\.npy$", p.name))]
    stem = f"embed-{machine}-{max(nums, default=0) + 1:04d}"
    # the .jsonl first: a reader only takes a chunk whose .npy exists, and writes the .npy last
    with open(cfg.writable(d / f"{stem}.jsonl"), "w", encoding="utf-8", newline="\n") as f:
        f.writelines(json.dumps(m) + "\n" for m in meta)
    tmp = cfg.writable(d / f"{stem}.tmp.npy")
    np.save(tmp, vecs.astype(np.float16))
    tmp.replace(d / f"{stem}.npy")
    return d / f"{stem}.npy"


def _read_meta(p: Path) -> list[dict]:
    j = p.with_suffix(".jsonl")
    if not j.exists():
        return []
    with open(j, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


FRAME_PREP = 2  # bump when the way frames are cut changes; older frame vectors are redone


def done_hashes(cfg: Config, model: str) -> dict[str, bool]:
    """hash -> whether its newest vector is of a single frame, cut the current way."""
    out: dict[str, bool] = {}
    for p in shards(cfg, model):
        if not p.name.endswith(".tmp.npy"):
            for m in _read_meta(p):
                out[m["hash"]] = bool(m.get("frame")) and m.get("prep", 1) == FRAME_PREP
    return out


class Vectors:
    """Every stored vector of a model, matched to the index's asset ids."""

    def __init__(self, cfg: Config, con, model: str):
        self.model = model
        parts, meta = [], []
        for p in shards(cfg, model):
            if p.name.endswith(".tmp.npy"):
                continue
            m = _read_meta(p)
            v = np.load(p, mmap_mode="r")
            if len(m) != len(v):
                continue  # a chunk still syncing
            parts.append(np.asarray(v))
            meta += m
        self.vecs = np.concatenate(parts) if parts else np.zeros((0, 1), np.float16)
        by_path = {(m["path"], m["size"]): i for i, m in enumerate(meta)}
        by_hash = {m["hash"]: i for i, m in enumerate(meta)}
        ids, rows = [], []
        for aid, path, size, h in con.execute("SELECT a.id, a.path, f.size, f.hash FROM assets a JOIN files f ON f.path=a.path "
                                              "WHERE a.media='visual'"):
            i = by_path.get((path, size))
            if i is None and h:
                i = by_hash.get(h)
            if i is not None:
                ids.append(aid)
                rows.append(i)
        self.ids = np.array(ids, dtype=np.int64)
        self.rows = np.array(rows, dtype=np.int64)
        self.row_of = dict(zip(ids, rows))

    def __len__(self):
        return len(self.ids)

    def vector(self, asset_id: int) -> np.ndarray | None:
        i = self.row_of.get(asset_id)
        return None if i is None else self.vecs[i]

    def rank(self, q: np.ndarray, only: set[int] | None = None, limit: int = 200) -> list[tuple[int, float]]:
        """Asset ids by cosine similarity to q, best first (optionally only among `only`)."""
        if not len(self.ids):
            return []
        sel = np.array([k for k, a in enumerate(self.ids) if a in only], dtype=np.int64) if only is not None else None
        ids = self.ids if sel is None else self.ids[sel]
        rows = self.rows if sel is None else self.rows[sel]
        q = q.astype(np.float32)
        scores = np.empty(len(rows), np.float32)
        for s in range(0, len(rows), 50_000):  # float16 -> float32 a slice at a time
            scores[s:s + 50_000] = self.vecs[rows[s:s + 50_000]].astype(np.float32) @ q
        k = min(limit, len(scores))
        if k == 0:
            return []
        top = np.argpartition(-scores, k - 1)[:k]
        top = top[np.argsort(-scores[top])]
        return [(int(ids[t]), float(scores[t])) for t in top]


# --- subjects ----------------------------------------------------------------------------------

def subject_spec(vocab) -> dict | None:
    s = vocab.namespaces.get("subject", {})
    if not s.get("values"):
        return None
    return {"labels": list(s["values"]), "template": s.get("template", "a {}"),
            "threshold": float(s.get("threshold", 0.3)), "thresholds": dict(s.get("thresholds", {})), "max": int(s.get("max", 2))}


def subjects_path(cfg: Config, model: str) -> Path:
    return store_dir(cfg, model) / "subjects.npz"


def subject_texts_current(cfg: Config, model: str, vocab) -> bool:
    spec = subject_spec(vocab)
    cur = load_subject_texts(cfg, model) if spec else None
    return spec is None or (cur is not None and cur["labels"] == spec["labels"] and cur["template"] == spec["template"])


def update_subject_texts(cfg: Config, model: str, vocab, enc) -> bool:
    """Encode the subject texts if the stored ones don't match the vocabulary. True if written."""
    spec = subject_spec(vocab)
    if spec is None or subject_texts_current(cfg, model, vocab):
        return False
    lg = enc.logits()
    if lg is None:
        return False  # not a SigLIP-style model: no calibrated per-label scores
    texts = [spec["template"].format(v.replace("-", " ")) for v in spec["labels"]]
    vecs = np.concatenate([enc.texts(texts[i:i + 64]) for i in range(0, len(texts), 64)])
    p = cfg.writable(subjects_path(cfg, model))
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg.writable(p.with_name("subjects.tmp.npz"))
    np.savez(tmp, vecs=vecs, labels=np.array(spec["labels"]), template=np.array(spec["template"]),
             scale=np.float32(lg[0]), bias=np.float32(lg[1]))
    tmp.replace(p)
    return True


def load_subject_texts(cfg: Config, model: str) -> dict | None:
    p = subjects_path(cfg, model)
    if not p.exists():
        return None
    with np.load(p) as z:
        return {"vecs": z["vecs"], "labels": [str(x) for x in z["labels"]], "template": str(z["template"]),
                "scale": float(z["scale"]), "bias": float(z["bias"])}


def assign_subjects(vecs: Vectors, texts: dict, spec: dict) -> dict[int, list[tuple[str, float]]]:
    """asset id -> [(subject, probability)], best first, for subjects at or above the threshold.
    Only labels still in the vocabulary count (a removed subject disappears before re-encoding)."""
    keep = [i for i, lab in enumerate(texts["labels"]) if lab in set(spec["labels"])]
    if not keep or not len(vecs):
        return {}
    T = texts["vecs"][keep].astype(np.float32)
    labels = [texts["labels"][i] for i in keep]
    th = np.array([float(spec["thresholds"].get(lab, spec["threshold"])) for lab in labels], np.float32)
    uniq, inv = np.unique(vecs.rows, return_inverse=True)  # duplicates share a vector: score it once
    found: dict[int, list] = {}
    for s in range(0, len(uniq), 20_000):
        L = vecs.vecs[uniq[s:s + 20_000]].astype(np.float32) @ T.T * texts["scale"] + texts["bias"]
        P = 1 / (1 + np.exp(-L))
        P = np.where(P >= th, P, 0)  # below its threshold a subject doesn't count
        for k in np.nonzero(P.max(axis=1) > 0)[0]:
            top = np.argsort(-P[k])[: spec["max"]]
            hits = [(labels[j], round(float(P[k, j]), 3)) for j in top if P[k, j] > 0]
            if hits:
                found[s + k] = hits
    return {int(vecs.ids[i]): found[u] for i, u in enumerate(inv) if u in found}


# --- the classify run --------------------------------------------------------------------------

def run(cfg: Config, con, model: str, machine: str, asset_rows: list[dict], *, encoder=None, batch: int = 64,
        progress=print) -> dict:
    """Embed the given assets (dicts with path, size, file_id, hash) that this model hasn't seen.
    Hashes missing files first. Stops cleanly on Ctrl-C, keeping what's done."""
    from concurrent.futures import ThreadPoolExecutor

    from .indexer import _hash_file

    need = [r for r in asset_rows if not r["hash"]]
    if need:
        progress(f"hashing {len(need):,} files...")
        with ThreadPoolExecutor(max_workers=8) as ex:
            res = list(ex.map(_hash_file, [(r["file_id"], cfg.fs(r["path"])) for r in need]))
        con.executemany("UPDATE files SET hash=? WHERE id=?", [(h, fid) for fid, h in res if h])
        con.commit()
        got = dict(res)
        for r in need:
            r["hash"] = got.get(r["file_id"])
    done = done_hashes(cfg, model)
    todo, seen = [], set()
    for r in asset_rows:
        r["cell"] = cell_for(r)
        h = r["hash"]
        # redo a whole-image vector when the image is now known to be a sheet or strip
        if h and h not in seen and (h not in done or (r["cell"] and not done[h])):
            seen.add(h)
            todo.append(r)
    stats = {"model": model, "assets": len(asset_rows), "embedded": 0, "unreadable": 0, "already": len(asset_rows) - len(todo),
             "chunks": []}
    if not todo:
        return stats
    enc = encoder or Encoder(model)
    progress(f"{len(todo):,} images to embed with {model} on {getattr(enc, 'device', '?')}")
    buf_v, buf_m = [], []

    def flush():
        if buf_v:
            stats["chunks"].append(write_chunk(cfg, model, machine, np.concatenate(buf_v), buf_m[:]).name)
            buf_v.clear()
            buf_m.clear()

    t0 = time.time()
    pool = ThreadPoolExecutor(max_workers=8)
    load = lambda rows: list(pool.map(lambda r: prepare(cfg.fs(r["path"]), enc.side, r["cell"]), rows))  # noqa: E731
    batches = [todo[i:i + batch] for i in range(0, len(todo), batch)]
    try:
        nxt = pool.submit(load, batches[0])
        for b, rows in enumerate(batches):
            ims = nxt.result()
            if b + 1 < len(batches):
                nxt = pool.submit(load, batches[b + 1])  # read the next batch while this one runs
            ok = [(r, im) for r, im in zip(rows, ims) if im is not None]
            stats["unreadable"] += len(rows) - len(ok)
            if ok:
                buf_v.append(enc.images([im for _, im in ok]))
                buf_m.extend({"hash": r["hash"], "path": r["path"], "size": r["size"], **({"frame": list(r["cell"]), "prep": FRAME_PREP} if r["cell"] else {})}
                             for r, _ in ok)
                stats["embedded"] += len(ok)
            if sum(len(v) for v in buf_v) >= CHUNK:
                flush()
            if (b + 1) % 50 == 0 or b + 1 == len(batches):
                n, el = (b + 1) * batch, time.time() - t0
                rate = min(n, len(todo)) / el
                left = (len(todo) - min(n, len(todo))) / rate if rate else 0
                progress(f"[{min(n, len(todo)):,}/{len(todo):,}] {rate:.0f} images/s" + (f", ~{left / 60:.0f} min left" if left > 60 else ""))
    except KeyboardInterrupt:
        progress("stopped; what's done is saved, and the next run continues from there")
    finally:
        flush()
        pool.shutdown(wait=False, cancel_futures=True)
    stats["seconds"] = round(time.time() - t0, 1)
    return stats
