"""Generate a small synthetic asset library for tests and demos.

    uv run python tests/make_library.py DIR

Layout (three sections, like a typical hoard):

    Kenney Game Assets All-in-1/   one group, packs at <category>/<pack>, plus an ignored Archive/
    Bundles/<bundle>/...           groups with grouping folders (Graphics/, Audio/), a duplicate pack
    Packs/<store>/<pack>           a store folder

Covers 3D models (GLB, OBJ + MTL, glTF + .bin, external textures, a missing one, an animation
clip, a bundled render), PNG sheets with a Tiled .tsx, XML and JSON texture atlases, a Kenney Tilesheet.txt, a size
in a file name, PNG/PSD and WAV/OGG variants, a font, readmes and licenses, preview images.
Everything is deterministic: the same files and bytes every run.
"""

from __future__ import annotations

import base64
import io
import json
import random
import struct
import sys
import wave
from pathlib import Path

KENNEY = "Kenney Game Assets All-in-1"


def png(w: int, h: int, seed: int, colors: int = 6) -> bytes:
    """A deterministic pixel-art-ish PNG: a few colors in tile-sized blocks, hard alpha."""
    from PIL import Image

    rnd = random.Random(seed)
    pal = [(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256), 255) for _ in range(colors)]
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    px = im.load()
    for y in range(h):
        for x in range(w):
            if (x // 4 + y // 4 + seed) % 5:
                px[x, y] = pal[(x // 4 * 7 + y // 4 * 3 + seed) % colors]
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def psd(w: int, h: int) -> bytes:
    """Just a PSD header (all the indexer reads) plus padding."""
    return b"8BPS" + struct.pack(">H6xHIIHH", 1, 4, h, w, 8, 3) + b"\0" * 64


def wav(seconds: float, seed: int) -> bytes:
    rnd = random.Random(seed)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"".join(struct.pack("<h", rnd.randint(-3000, 3000)) for _ in range(int(8000 * seconds))))
    return buf.getvalue()


def ttf(family: str) -> bytes:
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    def box(pen, x0, y0, x1, y1):
        pen.moveTo((x0, y0)); pen.lineTo((x0, y1)); pen.lineTo((x1, y1)); pen.lineTo((x1, y0)); pen.closePath()

    glyphs = {}
    for name in (".notdef", "space", "A"):
        pen = TTGlyphPen(None)
        if name != "space":
            box(pen, 50, 0, 450, 700)
        glyphs[name] = pen.glyph()
    fb = FontBuilder(1000, isTTF=True)
    fb.setupGlyphOrder(list(glyphs))
    fb.setupCharacterMap({32: "space", 65: "A"})
    fb.setupGlyf(glyphs)
    fb.setupHorizontalMetrics({n: (500, 50) for n in glyphs})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupNameTable({"familyName": family, "styleName": "Regular"})
    fb.setupOS2()
    fb.setupPost()
    buf = io.BytesIO()
    fb.save(buf)
    return buf.getvalue()


def _cube() -> tuple[bytes, list[dict], list[dict]]:
    """A textured unit cube: binary buffer, buffer views and accessors (positions, normals, UVs, indices)."""
    pos, nrm, uv, idx = [], [], [], []
    for axis in range(3):
        for sign in (1, -1):
            n = [0.0, 0.0, 0.0]
            n[axis] = float(sign)
            u, v = [(1, 2), (0, 2), (0, 1)][axis]
            base = len(pos)
            for a, b in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
                p = [0.0, 0.0, 0.0]
                p[axis], p[u], p[v] = 0.5 * sign, 0.5 * a * sign, 0.5 * b
                pos.append(p); nrm.append(n); uv.append([(a + 1) / 2, (b + 1) / 2])
            e1 = [pos[base + 1][i] - pos[base][i] for i in range(3)]
            e2 = [pos[base + 2][i] - pos[base][i] for i in range(3)]
            cross = [e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0]]
            if sum(c * x for c, x in zip(cross, n)) > 0:  # counter-clockwise seen from outside
                idx += [base, base + 1, base + 2, base, base + 2, base + 3]
            else:
                idx += [base, base + 2, base + 1, base, base + 3, base + 2]
    chunks = [b"".join(struct.pack("<3f", *x) for x in pos), b"".join(struct.pack("<3f", *x) for x in nrm),
              b"".join(struct.pack("<2f", *x) for x in uv), b"".join(struct.pack("<H", i) for i in idx)]
    views, accs, off = [], [], 0
    for c in chunks:
        views.append({"buffer": 0, "byteOffset": off, "byteLength": len(c)})
        off += len(c) + (-len(c) % 4)
    blob = b"".join(c + b"\0" * (-len(c) % 4) for c in chunks)
    accs = [{"bufferView": 0, "componentType": 5126, "count": 24, "type": "VEC3", "min": [-0.5] * 3, "max": [0.5] * 3},
            {"bufferView": 1, "componentType": 5126, "count": 24, "type": "VEC3"},
            {"bufferView": 2, "componentType": 5126, "count": 24, "type": "VEC2"},
            {"bufferView": 3, "componentType": 5123, "count": 36, "type": "SCALAR"}]
    return blob, views, accs


def gltf(texture: str | None = None, spin: bool = False, bin_uri: str | None = None) -> tuple[bytes, bytes | None]:
    """A cube model: a GLB (or, with bin_uri, .gltf JSON plus its .bin), optionally with an external
    texture and a spinning animation clip."""
    blob, views, accs = _cube()
    mat: dict = {"pbrMetallicRoughness": {"baseColorFactor": [0.8, 0.5, 0.2, 1], "metallicFactor": 0}}
    j: dict = {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0, "name": "cube"}],
               "meshes": [{"primitives": [{"attributes": {"POSITION": 0, "NORMAL": 1, "TEXCOORD_0": 2}, "indices": 3, "material": 0}]}],
               "materials": [mat], "bufferViews": views, "accessors": accs}
    if texture:
        mat["pbrMetallicRoughness"] = {"baseColorTexture": {"index": 0}, "metallicFactor": 0}
        j.update(textures=[{"source": 0}], images=[{"uri": texture}])
    if spin:
        t = struct.pack("<2f", 0, 2)
        q = struct.pack("<8f", 0, 0, 0, 1, 0, 1, 0, 0)
        views += [{"buffer": 0, "byteOffset": len(blob), "byteLength": 8}, {"buffer": 0, "byteOffset": len(blob) + 8, "byteLength": 32}]
        accs += [{"bufferView": 4, "componentType": 5126, "count": 2, "type": "SCALAR", "min": [0], "max": [2]},
                 {"bufferView": 5, "componentType": 5126, "count": 2, "type": "VEC4"}]
        blob += t + q
        j["animations"] = [{"name": "spin", "channels": [{"sampler": 0, "target": {"node": 0, "path": "rotation"}}],
                            "samplers": [{"input": 4, "output": 5}]}]
    j["buffers"] = [{"byteLength": len(blob), **({"uri": bin_uri} if bin_uri else {})}]
    js = json.dumps(j, separators=(",", ":")).encode()
    if bin_uri:
        return js, blob
    js += b" " * (-len(js) % 4)
    out = struct.pack("<4sII", b"glTF", 2, 12 + 8 + len(js) + 8 + len(blob))
    return out + struct.pack("<I4s", len(js), b"JSON") + js + struct.pack("<I4s", len(blob), b"BIN\0") + blob, None


OBJ_CUBE = "mtllib crate.mtl\no crate\n" + "".join(f"v {x} {y} {z}\n" for x in (-0.5, 0.5) for y in (-0.5, 0.5) for z in (-0.5, 0.5)) + \
    "vt 0 0\nvt 1 0\nvt 1 1\nvt 0 1\nusemtl colormap\n" + \
    "".join(f"f {a}/1 {b}/2 {c}/3 {d}/4\n" for a, b, c, d in ((1, 2, 4, 3), (5, 7, 8, 6), (1, 5, 6, 2), (3, 4, 8, 7), (1, 3, 7, 5), (2, 6, 8, 4)))


def ogg() -> bytes:
    return base64.b64decode(OGG_B64)


CC0 = "License (Creative Commons Zero, CC0)\nhttp://creativecommons.org/publicdomain/zero/1.0/\n\nYou may use these assets in personal and commercial projects.\n"


def build(root: Path) -> Path:
    root = Path(root)
    files: dict[str, bytes | str] = {}

    def add(rel: str, data: bytes | str):
        files[rel] = data

    # --- Kenney: <category>/<pack>
    k = KENNEY
    add(f"{k}/2D assets/Tiny Dungeon/License.txt", CC0)
    add(f"{k}/2D assets/Tiny Dungeon/Preview.png", png(160, 120, 1))
    add(f"{k}/2D assets/Tiny Dungeon/Tilesheet.txt",
        "Tilesheet information\n\nTile size • 16px × 16px\nSpace between tiles • 1px\nTotal tiles (horizontal) • 4 tiles\n")
    add(f"{k}/2D assets/Tiny Dungeon/Tilemap/tilemap.png", png(16 * 4 + 3, 16 * 3 + 2, 2))
    for i in range(4):
        add(f"{k}/2D assets/Tiny Dungeon/Tiles/tile_{i:04d}.png", png(16, 16, 10 + i))
    add(f"{k}/2D assets/Pixel Shmup/License.txt", CC0)
    add(f"{k}/2D assets/Pixel Shmup/Preview.png", png(160, 120, 3))
    for i in range(3):
        add(f"{k}/2D assets/Pixel Shmup/Ships/ship_{i:04d}.png", png(24, 24, 20 + i))
    add(f"{k}/Audio/Impact Sounds/License.txt", CC0)
    for i in range(3):
        add(f"{k}/Audio/Impact Sounds/Audio/impactWood_{i:03d}.ogg", ogg())
        add(f"{k}/Audio/Impact Sounds/Audio/impactWood_{i:03d}.wav", wav(0.2, 30 + i))
    add(f"{k}/Archive/Old Pack/old.png", png(16, 16, 99))  # ignored by the section config
    m = f"{k}/3D assets/Mini Crates"  # like Kenney's 3D kits: GLB/OBJ/glTF variants, external textures
    add(f"{m}/License.txt", CC0)
    add(f"{m}/Preview.png", png(160, 120, 4))
    add(f"{m}/Previews/crate.png", png(160, 160, 5))  # a render of crate.glb
    add(f"{m}/Models/GLB format/crate.glb", gltf("Textures/colormap.png")[0])
    add(f"{m}/Models/GLB format/barrel.glb", gltf("Textures/colormap.png", spin=True)[0])
    add(f"{m}/Models/GLB format/lamp.glb", gltf("Textures/lamp.png")[0])  # its texture isn't there
    add(f"{m}/Models/GLB format/Textures/colormap.png", png(32, 32, 6))
    add(f"{m}/Models/OBJ format/crate.obj", OBJ_CUBE)
    add(f"{m}/Models/OBJ format/crate.mtl", "newmtl colormap\nKd 1 1 1\nmap_Kd Textures/colormap.png\n")
    add(f"{m}/Models/OBJ format/Textures/colormap.png", png(32, 32, 6))
    rock, rock_bin = gltf(bin_uri="rock.bin")
    add(f"{m}/Models/glTF format/rock.gltf", rock)
    add(f"{m}/Models/glTF format/rock.bin", rock_bin)

    # --- Bundles: groups with grouping folders
    b = "Bundles/Mega Fantasy Bundle"
    add(f"{b}/License.txt", "Bundle license\nUse in commercial games. Do not resell the assets.\n")
    forest = {
        "Preview.png": png(128, 96, 40),
        "forest.png": png(64, 32, 41),
        "forest.tsx": '<?xml version="1.0" encoding="UTF-8"?>\n<tileset version="1.10" name="forest" tilewidth="16" tileheight="16" tilecount="8" columns="4">\n <image source="forest.png" width="64" height="32"/>\n</tileset>\n',
        "readme.txt": "Forest Tileset\n\nA 16x16 forest tileset with trees, grass and water.\n",
        "props/tree.png": png(32, 48, 42),
        "props/rock.png": png(16, 16, 43),
        # Kenney's layout: the sheet in one folder, its Tiled file in a sibling folder
        "Tilemap/forest_packed.png": png(64, 32, 44),
        "Tiled/forest_packed.tsx": '<?xml version="1.0" encoding="UTF-8"?>\n<tileset version="1.10" name="forest_packed" tilewidth="16" tileheight="16" tilecount="8" columns="4">\n <image source="../Tilemap/forest_packed.png" width="64" height="32"/>\n</tileset>\n',
    }
    for rel, data in forest.items():
        add(f"{b}/Graphics/Forest Tileset/{rel}", data)
    h = f"{b}/Graphics/Hero Sprites"
    add(f"{h}/Preview.png", png(128, 96, 50))
    add(f"{h}/hero_sheet.png", png(128, 32, 51))
    add(f"{h}/hero_sheet.xml",
        '<TextureAtlas imagePath="hero_sheet.png">\n'
        + "".join(f' <SubTexture name="hero_{i}.png" x="{32 * i}" y="0" width="32" height="32"/>\n' for i in range(4))
        + "</TextureAtlas>\n")
    add(f"{h}/monsters.png", png(96, 64, 52))
    add(f"{h}/monsters.json", json.dumps({
        "frames": {f"slime_{i}": {"frame": {"x": 32 * (i % 3), "y": 32 * (i // 3), "w": 32, "h": 32}} for i in range(6)},
        "meta": {"image": "monsters.png", "size": {"w": 96, "h": 64}},
    }, indent=1))
    add(f"{h}/PNG/hero_idle.png", png(32, 32, 53))
    add(f"{h}/PSD/hero_idle.psd", psd(32, 32))
    s = f"{b}/Audio/Battle Sounds"
    add(f"{s}/readme.txt", "Battle Sounds\nSword hits and a battle theme.\n")
    add(f"{s}/wav/sword_hit.wav", wav(0.3, 60))
    add(f"{s}/ogg/sword_hit.ogg", ogg())
    add(f"{s}/music/battle_theme.ogg", ogg())
    add(f"{b}/Fonts Pack/readme.txt", "Pixel font, free for commercial use.\n")
    add(f"{b}/Fonts Pack/Pixelade.ttf", ttf("Pixelade"))

    b2 = "Bundles/Space Bundle"
    add(f"{b2}/Space Ships/Preview.png", png(128, 96, 70))
    for i in range(3):
        add(f"{b2}/Space Ships/ships/ship_{i:02d}.png", png(32, 32, 71 + i))
    for rel, data in forest.items():  # the same pack again, in another bundle
        add(f"{b2}/Forest Tileset Copy/{rel}", data)

    # --- Packs: <store>/<pack>
    u = "Packs/Pixel Store/UI Kit"
    add(f"{u}/Preview.png", png(128, 96, 80))
    add(f"{u}/license.txt", "Licensed for use in games.\r\nAttribution appreciated.\r\n")  # CRLF, for --license-lf
    for i in range(3):
        add(f"{u}/buttons/button_{i}.png", png(48, 16, 81 + i))
    add(f"{u}/cursors_16x16.png", png(64, 16, 85))  # grid from the name

    # junk the scanner must skip
    add(f"{b}/.DS_Store", b"\0" * 32)
    add(f"{b}/__MACOSX/._x.png", b"\0" * 32)

    for rel, data in files.items():
        p = root.joinpath(*rel.split("/"))
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, str):
            p.write_text(data, encoding="utf-8", newline="\n")
        else:
            p.write_bytes(data)
    return root


# 0.3 s stereo Ogg Vorbis (a sine beep), made once with ffmpeg; nothing in the stdlib encodes Vorbis.
OGG_B64 = """
T2dnUwACAAAAAAAAAAAAAAAAAAAAAMHVvV8BHgF2b3JiaXMAAAAAAhErAAAAAAAAAAAAAAAAAAC7AU9nZ1MAAAAAAAAAAAAAAAAA
AAEAAAA/3JbKDi3///////////////+7A3ZvcmJpcwYAAABmZm1wZWcBAAAAEwAAAGVuY29kZXI9TGF2YyB2b3JiaXMBBXZvcmJp
cxxCQ1YCABAAAIR0mlmqASLMQIaB0JCVAAACAABghCIMMSA0ZCUAABAAACCGkoNoQmvON+c4aJaDplJsTgcnUm2e5KZibs4555xz
sjlnjHPOOacoZxaDZkJrzjknMWiWgmZCa84550lsHrSmSmvOOWecczoYZ4RxzjmnSWsepGZjbc45Z0FrmqPmUmzOOSdSbp7U5lJt
zjnnnHPOOeecc845p3pxOgfnhHPOOSdqb67lJnRxzjnnk3G6NyeEc84555xzzjnnnHPOOScIDVkJAAABABCEYWMYdwqC9DkaiFGE
mIZMetA9OkyCxiCnkHo0OhoppQ5CSWWclNIJQkNWAgCAAAAQQkghhRRSSCGFFFJIIYUYYoghhpxyyimooJJKKqooo8wyyyyzzDLL
LLMOO+usww5DDDHE0EorsdRUW4011pp7zrnmIK2V1lprrZRSSimllILQkJUAAAgAAIGQQQYZZBRSSCGFGGLKKaecggoqIDRkJQAA
BgDAIWeggQYaaKCBBhpooHHGGYgggggiqKSSTDoKKbXYaswx116DDjr3nnvvufgchFJKKaWUUkoppZRSSiklCA1ZCQCAAAAACCGE
EFJIIYUUUooxxhxzDjoJJQRCQ1YCAGAAAAwxxBhkkEFIIYUYYoopxxxzDDoIIZRSUmihhVxqiCWWVlqJpaWYaoux1lhz7THW3nvv
vffee++99957zoHQkJUAQAQAAIMMIogggowxBiEEhIasBABAAAAQYogxxiCEEFKIIaecgkwy6aSjkAKhISsBACcAAIQRRyRxBBJn
oIEIKqkgo8xCLLG11lprrbXWWmuttdZaa6211lprrbXWWmuttdZaay0QGrISAIgAAGCQQQYZRBBBBBlkgNCQlQAACAAAI4xABBml
FGOOOeYYdNBBJx2FFlogNGQlAOAEAEAgoYgyzDAEEVVUUUYVVRRSRymllFJKKaWUUkoppZRSSimllFJKKaWUUkoppVRKKaUEQkNW
AgBkAACQopRSKS1FgiKlGKQYS0YVc1BaiqhyDFLNqVLOIOYklogxhJSTVDLmFEIMQuocdUwpBi2VGELGGKTYckuhcw4IDVkhAIRm
ADgcB5AsC5AsCwAAAAAAAAAkTQM0zwMszQMAAAAAAAAASdMAy9MAzfMAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAJA0DdA8D9A8DwAAAAAAAAA0zwM8TwQ8UQQAAAAAAAAAy/MATfQATxQB
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAJA0DdA8D9A8
DwAAAAAAAAAszwM8UQQ0TwQAAAAAAAAAy/MATxQBT/QAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEAAAKOAAAClgIhYasCADiBAAckgRJgiRB8wCS
ZUHToGkwTYBkWdA0aBpMEwAAAAAAAAAAAABJ06Bp0DSIIkDSNGgaNA2iCAAAAAAAAAAAAICkadA0aBpEESBpGjQNmgZRBAAAAAAA
AAAAAMAzTYgiRBGmCfBME6IIUYRpAgAAAAAAAAAAAAAAAAAAAAAAAAAAAAACAAAJHAAABUwoA4WGrAgA4gQAHI5iWQAA4DiOZQEA
gOM4lgUAAJZliSIAAFiWJooAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAIAAAkcAAAFTCgDhYasBACiAAAcimJZwHEsCziOZQFJsiyAZQE0D6BpAFEEAAIAAA0cAAAFbNCUWByg0JCVAEAUAIBB
cSxL00SRJGma5okiSdI0zxNFmuZ5nmea8DzPM02IoiiaJkRRFE0TpmmaqgpMU1UFAAAaOAAACtigKbE4QKEhKwGAkAAAh6JYlqZ5
nueJommqJknSNM8TRVE0TdNUVZKkaZ4niqJomqapqixL0zxPFEXRNFVVVaFpnieKomiaqqq68DzPE0VRNE1VdV14nueJoiiapqq6
LkRRFE3TNFVTVV0XiKJpmqaqqqrrAtETRdNUVdd1XeB5omiaquqqrgtE0zRVVVVdV5YBpmmaquq6sgxQVVV1XdeVZYCqqqrruq4s
A1TVdV1XlmUZgOu6rizLsgAAQAQHAEABI+gko8oibDThwgNQaMiKACAKAAAwhinFlDKMSQgphIYxCSGFkElJqbSUKgiplFRKBSGV
kkrJKKWUWkoVhFRKKqWCkEpJpRQAAIvgAACLYCEUGrISAMgDACCMUYoxxpyTCCnFmHPOSYSUYsw556RSjDnnnHNSSsYcc845KaVz
zjnnnJSSOeecc05K6ZxzzjknpZTSOeeck1JKCaFz0EkppXTOOecEAIAaOAAACtgosjnBSFChISsBgFQAAIPjWJameZ4omqYlSZrm
eZ4niqapSZKmeZ7niaJq8jzPE0VRNE1V5XmeJ4qiaJqqynVF0TRNU1VVlyyLommapqq6LkzTNFXVdV0Xpmmaquq6rgvbVlVVdV1Z
hm2rqqq6riwD13VdWbZlIMuuK7u2LAAAXsEBANTAhtURTorGAgsNWQkAZAAAEMYgpBBCSBmEkEIIIaUUQgIAAAkcAAAFTCgDhYas
BABSAQAAY6y11lprrTXQWWuttdZaKyCz1lprrbXWWmuttdZaa6211FprrbXWWmuttdZaa6211lprrbXWWmuttdZaa6211lprrbXW
WmuttdZaa6211lprrbXWWksppZRSSimllFJKKaWUUkoppZRSAUC/Fg4A/xA2rI5wUjQWWGjISgAgHAAAMEYpxhyDUEopFUKMOScd
ldZirBBizDkJKbUWW/GccxBKSKW1GIvnnINQSkqx1VhUCqGUlFKLLdaiUuiopJRSazUWY0wqqbXWYquxGGNSCi211mKMxQhbU2ot
ttpqLMbYmkoLLcYYYzHCFxlbi6m2WoMxwsgWS0u11hqMMUb31mKpreZijA++thRLjDUXAODu4ACAqGDjDCtJZ4WjwYWGrAQAQgIA
CISUYowxxpxzzjmpFGOOOeecgxBCKJVijDHnnIMQQgglY4w55xyEEEIIoZSSMecchBBCCCGklDrnHIQQQgghhFJK55yDEEIIIYRQ
SukghBBCCCGEEkopKYUQQgghhBBCKimlEEIIoZQQSkglpRRCCCGEUEoJKaWUQgihlBBCKCGllFJKIYQQQimlpJRSSqmEUkIJoYRU
SkophRJCCKWUklJKKZVSQiihhFJKSSmllFIIIYRSSgEAgAgOAIACRtBJRpVF2GjChQcgAAAABAAgCJEZIlGwAAwOVABCwhQAUFhg
kAMADQ4PaRcX0GWAC7q460AIQQhCEIsDKCABByfc8MQbnnCDE3SKSg0IAAAAAAAZAHwAACQPQERENHMQERITFBUWFxgZGhscAACA
AAIAABAAAAAAAAgAAAAAEE9nZ1MABAANAAAAAAAAAAAAAAIAAACeDrjECP91X1j/U/9yntYKYEEyBTEbIQU6ZhmhTJTWCmBBMgUx
GyEFOmYZoUx0zj8AAADA2aPJslxVVQAAAIAUMiIzIrPSUANRLSMyBUgk0YQJEiRMmCBBwoQJEiRMmCBBwoQJEiRMmCCRhIkmSJAw
0QSJJEyYIJGEiSZIJGGiCRJJmGiCRBImmiCRhIkmSCRhogkSkzDxRCzMLcwsLc0sTc10RjCaCzNhsJDSxMTC0tTcYGJMGJMgNmFM
gtiEMQliE8YkiE0YExcbHxMXGx8TF42PxEXjI3HR+EhcNDYSE42NxER7dnq0PZsebc+mR9uz6dH2bHq03aZH2206bbfptN2m03ab
TtttOm236bTdptN2m05106luOqUf+qEf+qEf+qEf+qEf+qEf+qEf+qEf+qEf+qEf+qEf+qEf+vH48eNoNBqNRqPRaDQajUaj0Wg0
Go1Go9FoNBqNRqPRaDQajUaj0Wg0Go1Go9FoNBqNRqPRaDQajUaj0Wg0Go1Go9EoPsLZRHMTyvXfocYcvwCxRwj98e/HCZPTCGcT
zU0o13+HGnP8AsQeIfTHvx8nTE4A/AMAKAAAAAAAAAAAAAgAACAAAAAAAFDJSk0AABKBRCARqEJjKfVmOh0SAOAGwQFewnn4q9DO
/75UYo1fAIfoj38/ykSFiEo4D38V2vnfl0qs8QvgEP3x70eZqBARAD8AAHVEAAAAAAAAAAAgAAAAAAAAAACAImLVSIMREQDQG3Wm
UpEAAGQAnlbKwAK6QLZTKLoJmY8ywmmlDCygC2Q7haKbkPkoI/zkRD9b+tPanNh8WtHX2v7EZiv62foRWZaKolBWy6ipUxAA4Jwx
WJarigAAAAAAgMXqYNrFZjgYVtNmNyzqaHVUm2G3WK2Go2HDro6mxbDbTKs4iE1MHOwiVjsmFhwsqoqKWmOtxRgRqdeuXWamCEDR
KwahN9OZmmv05qbSkEiMEyYajSSSQLGJxpMgMTFhoomGMYkkID7RWCVIEIkmTOiYBDHEx0eDuLggGh9LJC7i2NgwiIlRNLar06Op
nt1qenR0e7bpdNJ2u5pOo9tt0+mk7baaTlPdbqXTaLutppPqttV0mu+8tbf6QnldL/r1pb3PW3ntF/M6tO+8tbd6y+t60We8Hn0b
/T32F1cbN3gy+Hr0bfRv7C+uNm7wZHA9+jb6N1aN1VqDJ4Pr0bfhX6wa8wHeZFGhA1kQq5bC6AgVhFBGqMmiQgeyIFYthdERKgih
jNCz0c6e9vamvTHZ+4ayyCzKiLJQFplFGVEWykpmUUb8AwAAALwV1UJZZBalKMPZY7AsV1UAAAAAAJuYpg2bmjhYRK0OGFixWwS7
I4bYMSKCiFpjFYwVURWLirXGWCRDRooUZUQZUUaUoQwZRBprrBFBECMY0ZhKE2mmF1ozNEi9iVZvYdRaWBgMSGFuqjUkmpjYaGIS
DWIST0DCxMSHiSQaiUk0geMTiXWChJEgYZxjE8QSkzASxMeF0bhYYuIjio0Lw5hYReIjxPZoq0fPND07uj3a6vRM07NTbY+uTrdJ
t1Ntp6vTs0m3U22nq+k2up2qTjdNt9F22up0k26j7bSabv7jpfoqX+6rS/+OR/2Vl/sqX37Hpf/K41f5so/Qv+O5nn61fC5TlYmn
A6fT19Ovls8yVRk4HTidvp6+l0mZqninA4fp6/leJmXK9k7rh2k=
"""

if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: make_library.py DIR")
    out = build(Path(sys.argv[1]))
    print(out)
