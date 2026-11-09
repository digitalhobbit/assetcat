"""Browser checks of the browse UI against a running `assetcat serve`, in headless Chrome.

    assetcat serve --port 8766 &
    uv run --with playwright --with pillow python scripts/ui_check.py http://127.0.0.1:8766/ [--shots DIR]

Uses the installed Google Chrome (`channel="chrome"`), so no Playwright browser download is
needed. Read-only: it opens the curation editor but never saves, and never stars anything.
Exits 1 if a check fails or the page logs a JavaScript error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright


# Not page errors: a missing file (404), and three.js reporting a model's missing texture.
EXPECTED_CONSOLE = re.compile(r"Failed to load resource|THREE\.\w+Loader: Couldn't load texture")


def colors_in(png: bytes) -> int:
    """Distinct colors in a screenshot: a rendered model has shading, an empty stage two or three."""
    import io

    from PIL import Image

    im = Image.open(io.BytesIO(png)).convert("RGB").resize((200, 150))
    return len(im.getcolors(1 << 16) or [])


def api(base: str, path: str, **params):
    q = ("?" + urllib.parse.urlencode(params)) if params else ""
    with urllib.request.urlopen(base.rstrip("/") + path + q) as r:
        return json.loads(r.read())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url", nargs="?", default="http://127.0.0.1:8765/")
    ap.add_argument("--shots", help="save screenshots here")
    a = ap.parse_args()
    base = a.url.rstrip("/") + "/"
    shots = Path(a.shots) if a.shots else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)

    meta = api(base, "/api/meta")
    graphics = api(base, "/api/packs", kind="graphics", sort="assets")["items"]
    # a big graphics pack whose first screen is mostly images the browser can show
    for c in graphics[:40]:
        first = api(base, "/api/assets", pack=c["id"], limit=24)["items"]
        if sum(x["ext"] in ("png", "gif", "jpg", "jpeg", "webp") for x in first) >= 12:
            graphics = [c]
            break
    audio = api(base, "/api/packs", kind="sfx")["items"] or api(base, "/api/packs", kind="music")["items"]
    fonts = api(base, "/api/packs", kind="fonts")["items"]
    models = api(base, "/api/packs", kind="3d", sort="assets")["items"]

    results: list[tuple[bool, str]] = []
    js_errors: list[str] = []
    failed_loads: list[str] = []

    def check(ok, what):
        results.append((bool(ok), what))
        print(("ok    " if ok else "FAIL  ") + what)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.on("pageerror", lambda e: js_errors.append(str(e)))
        page.on("console", lambda m: (js_errors if m.type == "error" and not EXPECTED_CONSOLE.search(m.text) else []).append(m.text))
        page.on("requestfailed", lambda r: failed_loads.append(r.url))
        page.on("response", lambda r: failed_loads.append(f"{r.status} {r.url}") if r.status >= 500 else None)

        def on_timeout(e):
            if shots:
                page.screenshot(path=str(shots / "failure.png"))
            check(False, f"timed out at {page.url}: {str(e).splitlines()[0]}")

        def shot(name):
            if shots:
                page.screenshot(path=str(shots / f"{name}.png"))

        def loaded_images(sel, n=8):
            page.wait_for_timeout(300)
            return page.eval_on_selector_all(sel, f"els => els.slice(0, {n}).filter(e => e.complete && e.naturalWidth > 0).length")

        # pack wall
        page.goto(base)
        page.wait_for_selector(".card", timeout=20000)
        check(page.locator(".card").count() > 0, f"pack wall shows cards ({page.locator('.card').count()} rendered)")
        page.wait_for_function("() => [...document.querySelectorAll('.card img')].slice(0, 6).some(i => i.complete && i.naturalWidth > 0)", timeout=20000)
        check(loaded_images(".card img") > 0, "pack covers load")
        chips = page.locator(".chips").first.locator(".chip").all_inner_texts()
        check([c.strip() for c in chips][: len(meta["sections"])] == meta["sections"], f"section chips come from the profile: {meta['sections']}")
        page.locator(".chips").first.locator(".chip", has_text=meta["sections"][0]).click()
        page.wait_for_function("s => location.hash.includes('section=' + s)", arg=meta["sections"][0])
        page.wait_for_function("() => /\\d+ packs?/.test(document.querySelector('.packs')?.innerText || '')", timeout=20000)
        page.wait_for_timeout(300)
        check(page.locator(".card").count() > 0, f"section filter '{meta['sections'][0]}' shows packs")
        shot("packs")

        # tag facets (once packs are tagged)
        if meta.get("tag_facets"):
            f = meta["tag_facets"][0]
            val = max(f["counts"], key=f["counts"].get)
            page.goto(base + "#/packs")
            page.wait_for_selector(".card")
            page.locator(".facets div", has=page.locator("label.h", has_text={"kind": "Content"}.get(f["ns"], f["ns"].title()))).locator(".chip", has_text=val).first.click()
            page.wait_for_function("() => location.hash.includes('tag=')")
            page.wait_for_function("() => /\\d+ packs?/.test(document.querySelector('.packs')?.innerText || '')", timeout=20000)
            page.wait_for_timeout(300)
            check(page.locator(".card").count() > 0, f"tag facet {f['ns']}:{val} shows packs")

        # header search
        word = graphics[0]["name"].split()[0] if graphics else meta["groups"][0]["name"].split()[0]
        page.goto(base + "#/packs")  # drop the section filter first
        page.wait_for_selector(".card")
        page.fill("form.search input[name=q]", word)
        page.press("form.search input[name=q]", "Enter")
        page.wait_for_function("() => location.hash.includes('q=')")
        page.wait_for_function("() => /\\d+ packs?/.test(document.querySelector('.packs')?.innerText || '')", timeout=20000)
        page.wait_for_timeout(300)
        check(page.locator(".card").count() > 0, f"pack search '{word}' finds packs")

        # a graphics pack: assets grid, viewer, editor
        if graphics:
            p = graphics[0]
            page.goto(base + "#/pack/" + p["id"])
            page.wait_for_selector(".tile img", state="attached", timeout=20000)
            check(page.locator("h1", has_text=p["name"]).count() == 1, f"pack page: {p['id']}")
            page.wait_for_function("() => [...document.querySelectorAll('.tile img')].some(i => i.complete && i.naturalWidth > 0)", timeout=20000)
            check(loaded_images(".tile img") > 0, "asset thumbnails load")
            check(page.locator(".tags .tag.add").count() == 1, f"tag row with '+ tag' ({page.locator('.tags .tag.ai').count()} AI tags)")
            shot("pack")
            page.locator(".tile", has=page.locator("img[src*='.png'], img[src*='.gif'], img[src*='.jpg']")).first.click()
            page.wait_for_selector(".viewer", timeout=10000)
            page.wait_for_function("() => [...document.querySelectorAll('.viewer img')].some(i => i.complete && i.naturalWidth > 0)", timeout=20000)
            check(True, "viewer opens and shows the image")
            page.keyboard.press("2")
            page.keyboard.press("ArrowRight")
            page.wait_for_timeout(300)
            shot("viewer")
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
            check(page.locator(".viewer").count() == 0, "Esc closes the viewer")
            page.get_by_role("button", name="Edit curation").click()
            page.wait_for_selector("text=Save pack", timeout=5000)
            check(page.locator("text=Save pack").count() > 0, "curation editor opens (not saved)")
            shot("editor")

            # layout page from the pack: preview a role change, then discard it (never saved)
            page.goto(base + "#/pack/" + p["id"])
            page.wait_for_selector("text=Change pack tree", timeout=20000)
            page.locator("text=Change pack tree").click()
            page.wait_for_selector(".lnode.focus", timeout=30000)
            check(page.locator(".lnode.focus").count() == 1, "layout page opens with the pack highlighted")
            shot("layout")
            sel = page.locator(".lnode.focus select")
            before = page.locator(".lnode").count()
            sel.select_option("ignore" if sel.input_value() != "ignore" else "pack")
            page.wait_for_selector("text=unsaved change", timeout=30000)
            check(page.locator(".lnode.focus.role-ignore, .lnode.focus.role-pack").count() == 1, "role change shows in the preview tree")
            page.get_by_role("button", name="Discard").click()
            page.wait_for_function("() => !document.body.innerText.includes('unsaved change')", timeout=30000)
            page.wait_for_timeout(300)
            check(page.locator(".lnode").count() == before, "Discard restores the saved tree")

        # an audio pack: list layout, player bar
        if audio:
            p = audio[0]
            page.goto(base + "#/pack/" + p["id"])
            page.wait_for_selector(f"h1:has-text({json.dumps(p['name'])})", timeout=20000)
            page.wait_for_selector(".tile svg.wave", timeout=20000)
            check(page.locator(".tile svg.wave").count() > 0, f"audio pack shows waveform tiles: {p['id']}")
            page.locator(".tile").first.click()
            page.wait_for_selector(".player", timeout=5000)
            check(page.locator(".player").count() == 1, "clicking a sound opens the player bar")
            shot("audio")

        if fonts:
            page.goto(base + "#/pack/" + fonts[0]["id"])
            page.wait_for_selector(f"h1:has-text({json.dumps(fonts[0]['name'])})", timeout=20000)
            page.wait_for_selector(".tile .cap", timeout=20000)
            check(page.locator(".tile").count() > 0, f"font pack renders: {fonts[0]['id']}")

        # a 3D pack: model thumbnails (bundled or rendered in the page), the 3D viewer
        if models:
            p = models[0]
            page.goto(base + "#/pack/" + p["id"] + "?media=model")
            page.wait_for_selector(f"h1:has-text({json.dumps(p['name'])})", timeout=20000)
            page.wait_for_function("() => [...document.querySelectorAll('.tile img')].filter(i => i.complete && i.naturalWidth > 0).length >= "
                                   "Math.min(3, document.querySelectorAll('.tile').length)", timeout=60000)
            check(True, f"3D pack shows model thumbnails: {p['id']}")
            shot("models")
            page.locator(".tile").first.click()
            page.wait_for_selector(".m3d-info", timeout=30000)
            tris = page.locator(".m3d-info").inner_text()
            check("triangles" in tris and not tris.startswith("0 "), f"3D viewer loads the model ({tris.split(' · ')[0]})")
            page.wait_for_timeout(500)
            n = colors_in(page.locator(".m3d canvas").screenshot())
            check(n > 20, f"3D viewer draws the model ({n} colors on the canvas)")
            shot("viewer3d")
            page.keyboard.press("ArrowRight")
            page.wait_for_timeout(300)
            page.wait_for_selector(".m3d-info, .m3d-msg.err", timeout=30000)
            check(page.locator(".m3d canvas").count() == 1, "next model reuses the 3D stage")
            page.keyboard.press("Escape")
            animated = api(base, "/api/assets", media="model", animated=1, limit=1)["items"]
            if animated:
                page.goto(base + "#/assets?media=model&animated=true")
                page.wait_for_selector(".tile", timeout=20000)
                page.locator(".tile").first.click()
                page.wait_for_selector(".m3d-info", timeout=30000)
                check(page.locator(".m3d .tools select[title='Animation clip']").count() == 1, "animated model offers its clips")
                page.keyboard.press("Escape")

        # assets search and shortlists
        page.goto(base + "#/assets?q=" + urllib.parse.quote(word))
        page.wait_for_selector(".tile", timeout=20000)
        check(page.locator(".tile").count() > 0, f"asset search '{word}' finds assets")
        shot("assets")
        if meta.get("embed", {}).get("ready"):
            page.goto(base + "#/assets?meaning=1&q=" + urllib.parse.quote("a treasure chest"))
            # a hash change keeps the old page's tiles for a moment: wait for the new result count
            page.wait_for_function("() => /1,000 assets/.test(document.body.innerText) && document.querySelector('.tile img')", timeout=60000)
            check(page.locator(".tile").count() > 0, "search by meaning finds images")
            page.locator(".tile").first.click()
            page.wait_for_selector(".viewer", timeout=10000)
            page.get_by_role("button", name="More like this").click()
            page.wait_for_selector("text=Looks like", timeout=30000)
            page.wait_for_timeout(1000)  # both result pages show "1,000 assets"
            page.wait_for_function("() => /1,000 assets/.test(document.body.innerText) && document.querySelector('.tile img')", timeout=30000)
            check(page.locator(".tile").count() > 0, "More like this lists similar images")
            shot("similar")
        page.goto(base + "#/shortlists/")
        page.wait_for_timeout(800)
        check(page.locator("text=shortlists/").count() > 0, f"shortlists page loads ({len(meta['shortlists'])} lists)")
        for sl in meta["shortlists"][:3]:
            page.goto(base + "#/shortlists/" + sl["name"])
            page.wait_for_selector("h1", timeout=10000)
            check(page.locator("h1", has_text=sl["name"]).count() == 1, f"shortlist '{sl['name']}' opens ({sl['count']} items)")
        browser.close()

    check(not js_errors, "no JavaScript errors" + (f": {js_errors[:3]}" if js_errors else ""))
    check(not failed_loads, "no failed requests or server errors" + (f": {failed_loads[:3]}" if failed_loads else ""))
    bad = [w for ok, w in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} checks passed")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
