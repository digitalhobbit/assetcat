"""File categories, asset types, variant keys and other naming rules. Pure functions."""

from __future__ import annotations

import re

_CATS = {
    "image": "png gif jpg jpeg bmp tga webp",
    "vector": "svg",
    "source": "psd ai eps cdr aseprite ase pyxel xcf kra pdn sai clip ora afdesign afphoto sketch fla swf spine",
    "audio": "wav ogg mp3 flac aif aiff m4a opus wma",
    "model": "fbx obj glb gltf dae stl blend 3ds ply vox max ma mb",
    "font": "ttf otf woff woff2",
    "doc": "txt rtf pdf html htm md url webloc docx doc",
    "data": "json xml tsx tmx csv plist atlas scml scon mtl bin fnt yaml yml",
    "engine": (
        "unitypackage prefab meta anim controller overridecontroller unity mat asset physicmaterial "
        "uasset uproject umap gd tscn tres godot import yy yyp capx c3p"
    ),
    "archive": "zip rar 7z gz tar",
    "video": "mp4 mov webm avi mkv",
}
EXT_CATEGORY = {ext: cat for cat, exts in _CATS.items() for ext in exts.split()}

# Asset media classes: files of these categories become assets.
MEDIA = {"image": "visual", "vector": "visual", "source": "visual", "audio": "audio", "model": "model", "font": "font"}

# Primary-variant preference (lower = better).
_PREF = {
    "visual": "png gif webp jpg jpeg bmp tga svg psd aseprite ase ai eps pyxel kra xcf cdr",
    "audio": "ogg wav flac mp3 aif aiff m4a opus wma",
    "model": "glb gltf fbx obj dae blend 3ds stl ply vox",
    "font": "ttf otf woff2 woff",
}
PREF_RANK = {m: {e: i for i, e in enumerate(s.split())} for m, s in _PREF.items()}
BROWSER_IMAGE = {"png", "gif", "jpg", "jpeg", "webp", "bmp", "svg"}

ENGINE_EXT = {
    "unity": "unitypackage prefab meta anim controller overridecontroller unity mat asset physicmaterial",
    "unreal": "uasset uproject umap",
    "godot": "gd tscn tres godot import",
    "gamemaker": "yy yyp",
    "construct": "capx c3p",
}
EXT_ENGINE = {e: eng for eng, s in ENGINE_EXT.items() for e in s.split()}


def ext_of(name: str) -> str:
    i = name.rfind(".")
    return name[i + 1 :].lower() if i > 0 else ""


def stem_of(name: str) -> str:
    i = name.rfind(".")
    return name[:i] if i > 0 else name


def category(ext: str) -> str:
    return EXT_CATEGORY.get(ext, "other")


def slugify(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s or "x"


_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|(?<=[A-Za-z])(?=[0-9])|(?<=[0-9])(?=[A-Za-z])")


def words(s: str) -> list[str]:
    """Split names into lowercase words: 'BatMonster_01-idle' -> bat monster 01 idle."""
    s = _CAMEL.sub(" ", s)
    return [w for w in re.split(r"[^A-Za-z0-9]+", s.lower()) if w]


# Folder names that only say which format is inside; dropped when grouping variants.
_FORMAT_FOLDER = re.compile(
    r"^(png|pngs|wav|wavs|mp3|mp3s|ogg|oggs|flac|aiff?|psd|psds|ai|eps|svg|svgs|vector|vectors|jpe?g|gif|gifs|"
    r"fbx|obj|glb|gltf|dae|stl|blend|ttf|otf|aseprite|pyxel|source|sources|source ?files|raw|"
    r"(png|wav|mp3|ogg|fbx|obj|glb|gltf|dae|stl|svg|psd|vector) ?(format|files|version|\d+ ?k(hz)?|\d+ ?bit.*)|"
    r"default ?size|retina|hd|sd|[1-4]x|x[1-4]|double|mono|stereo|\d+ ?bit|\d+ ?k(hz)?|\d+ ?bit[ _-]\d+ ?k(hz)?|"
    r".+[_\- ](png|psd|ai|eps|svg|jpg|vector|wav|mp3|ogg)s?)$",
    re.I,
)
FORMAT_FOLDER = _FORMAT_FOLDER


def variant_folder(folder: str) -> str:
    """Folder path with format-only components removed (for grouping variants)."""
    parts = [p for p in folder.split("/") if p and not _FORMAT_FOLDER.match(p.strip())]
    return "/".join(p.lower() for p in parts)


_PREVIEW = re.compile(
    r"^(preview|mockup|mock-up|sample|cover|screenshot|screen ?shot|promo|showcase|thumbnail|thumb|banner|"
    r"overview|example|demo|presentation|poster|display)s?([ _\-\d].*)?$",
    re.I,
)
PREVIEW_FOLDER = re.compile(r"^(previews?|screenshots?|mockups?|promo|showcase|examples?|samples?)$", re.I)


_PREVIEW_WORDS = {"preview", "previews", "mockup", "screenshot", "screenshots", "promo", "showcase", "example", "thumbnail", "banner"}


def is_preview_name(stem: str) -> bool:
    return bool(_PREVIEW.match(stem)) or bool(set(words(stem)) & _PREVIEW_WORDS)


_TYPE_RULES: list[tuple[str, set[str]]] = [
    ("ui", {"ui", "gui", "hud", "button", "buttons", "interface", "panel", "panels", "menu", "menus", "cursor", "cursors", "window", "windows", "dialog", "healthbar", "bars", "prompts", "prompt"}),
    ("icon", {"icon", "icons", "badge", "badges"}),
    ("tileset", {"tile", "tiles", "tileset", "tilesets", "tilesheet", "tilemap", "tilemaps", "terrain", "autotile"}),
    ("background", {"background", "backgrounds", "bg", "bgs", "backdrop", "parallax", "sky", "skies", "wallpaper", "wallpapers"}),
    ("portrait", {"portrait", "portraits", "avatar", "avatars", "face", "faces", "faceset", "bust"}),
    ("vfx", {"fx", "vfx", "effect", "effects", "particle", "particles", "explosion", "explosions", "spell", "spells", "smoke", "fire", "impact"}),
    ("character", {"character", "characters", "hero", "heroes", "player", "enemy", "enemies", "monster", "monsters", "npc", "npcs", "creature", "creatures", "mob", "mobs", "boss", "bosses"}),
]


def visual_type(stem: str, folder: str, pack_name: str = "") -> str:
    if is_preview_name(stem) or any(PREVIEW_FOLDER.match(p) for p in folder.split("/") if p):
        return "preview"
    ws = set(words(stem)) | set(words(folder))
    for t, keys in _TYPE_RULES:
        if ws & keys:
            return t
    pw = set(words(pack_name))
    for t, keys in _TYPE_RULES:
        if pw & keys:
            return t
    return "sprite"


_MUSIC = {"music", "bgm", "song", "songs", "soundtrack", "ost", "tune", "tunes"}
_MUSIC_WEAK = {"theme", "themes", "track", "tracks", "loop", "loops", "jingle", "jingles", "ambient", "ambience", "stinger", "stingers"}
_SFX_PACK = ("sfx", "soundfx", "sound fx", "soundeffect", "sound effect", "sound-effect", "sounds", "foley", "fx pack")
_MUSIC_PACK = ("music", "tune", "song", "soundtrack", "jingle")
_VOICE = {"voice", "voices", "vocal", "vocals", "vocalization", "vocalizations", "vo", "speech"}


def audio_type(stem: str, folder: str, pack_name: str, duration: float | None) -> str:
    ws = set(words(stem)) | set(words(folder))
    pn = pack_name.lower()
    if ws & _VOICE:
        return "voice"
    if ws & _MUSIC:
        return "music"
    sfx_pack = any(k in pn for k in _SFX_PACK) and not any(k in pn for k in _MUSIC_PACK)
    if duration is not None and duration >= 45:
        return "music"
    if sfx_pack:
        return "sfx"
    if ws & _MUSIC_WEAK and (duration is None or duration >= 8):
        return "music"
    if set(words(pack_name)) & _VOICE or "vocal" in pn:
        return "voice"
    if any(k in pn for k in _MUSIC_PACK) and (duration is None or duration >= 8):
        return "music"
    return "sfx"


_DOC_LICENSE = re.compile(r"(licen[cs]e|copyright|terms|eula|\btos\b|royalty)", re.I)
_DOC_README = re.compile(r"(read ?me|instructions|credits?|attribution|info|about|notes?|description)", re.I)


def doc_role(name: str) -> str | None:
    if _DOC_LICENSE.search(name):
        return "license"
    if _DOC_README.search(name):
        return "readme"
    return None
