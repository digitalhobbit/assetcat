"""Contact sheets: a labeled grid of images for a pack or folder. Pixel art is scaled up by whole
numbers with nearest-neighbor; larger images are scaled down."""

from __future__ import annotations

import hashlib
import warnings
from pathlib import Path

CELL = 128
CAPTION = 14
PAD = 6
HEADER = 28
BG = (32, 33, 38)
CELL_BG = (48, 50, 57)
TEXT = (220, 222, 228)
DIM = (150, 153, 162)


def signature(items: list[tuple]) -> str:
    h = hashlib.blake2b(digest_size=6)
    for it in items:
        h.update(repr(it).encode())
    return h.hexdigest()


def pick(assets: list[dict], limit: int) -> list[dict]:
    """Round-robin across top-level folders so one big folder doesn't fill the sheet."""
    by_folder: dict[str, list[dict]] = {}
    for a in assets:
        top = a["folder"].split("/")[0] if a["folder"] else ""
        by_folder.setdefault(top, []).append(a)
    queues = [sorted(v, key=lambda a: (a["folder"], a["name"].lower())) for _, v in sorted(by_folder.items())]
    out: list[dict] = []
    i = 0
    while len(out) < limit and any(queues):
        q = queues[i % len(queues)]
        if q:
            out.append(q.pop(0))
        i += 1
    return out


def render(job: tuple[str, str, list[tuple[str, str]], int, int]) -> tuple[str, str | None]:
    """job = (out_path, title, [(fs_path, caption)], total, cols) -> (out_path, error)."""
    out_path, title, items, total, cols = job
    try:
        from PIL import Image, ImageDraw, ImageFont

        Image.MAX_IMAGE_PIXELS = None
        cols = max(1, min(cols, len(items)))
        rows = (len(items) + cols - 1) // cols
        cw, ch = CELL + PAD, CELL + CAPTION + PAD
        W = max(cols * cw + PAD, 420)
        H = HEADER + rows * ch + PAD
        sheet = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(sheet)
        font = ImageFont.load_default(size=11)
        tfont = ImageFont.load_default(size=14)
        shown = f"{len(items)} of {total} shown" if total > len(items) else f"{total} shown"
        d.text((PAD, 7), title, fill=TEXT, font=tfont)
        tw = d.textlength(shown, font=font)
        d.text((W - PAD - tw, 9), shown, fill=DIM, font=font)
        for n, (path, caption) in enumerate(items):
            x = PAD + (n % cols) * cw
            y = HEADER + (n // cols) * ch
            d.rectangle([x, y, x + CELL - 1, y + CELL - 1], fill=CELL_BG)
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    with Image.open(path) as im:
                        im.seek(0)
                        im = im.convert("RGBA")
                        im = _fit(im)
                        sheet.paste(im, (x + (CELL - im.width) // 2, y + (CELL - im.height) // 2), im)
            except Exception:  # noqa: BLE001
                d.text((x + 4, y + CELL // 2 - 6), "unreadable", fill=DIM, font=font)
            cap = caption
            while d.textlength(cap, font=font) > CELL and len(cap) > 4:
                cap = "…" + cap[2:]
            d.text((x, y + CELL + 1), cap, fill=DIM, font=font)
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        sheet.save(out_path, optimize=True)
        return out_path, None
    except Exception as e:  # noqa: BLE001
        return out_path, f"{type(e).__name__}: {e}"


def _fit(im):
    from PIL import Image

    w, h = im.size
    m = max(w, h)
    if m <= CELL:
        k = max(1, CELL // m)
        return im.resize((w * k, h * k), Image.NEAREST) if k > 1 else im
    s = CELL / m
    return im.resize((max(1, round(w * s)), max(1, round(h * s))), Image.BOX)
