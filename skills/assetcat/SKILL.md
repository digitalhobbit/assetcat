---
name: assetcat
description: Find, check and pull game assets (sprites, tilesets, sounds, music, fonts, 3D models) from the user's indexed asset library with the `assetcat` CLI. Use when the user asks for assets for a game or prototype, wants to know what they own ("do I have a pixel-art dungeon tileset?"), asks about an asset's license or whether it's AI-generated, or wants assets copied into a project with credits.
---

# Asset library (assetcat)

The user's asset library (bundles, store packs, Kenney's all-in-1...) is indexed by `assetcat`.
Search it, look at what you find, check license and AI status, then pull the chosen files into
the project. Never copy files out of the library by hand: `pull` also copies the license and
records where every file came from.

Run it as `assetcat ...` if it's installed. Otherwise use `uvx assetcat ...`, or whatever command the
project's own docs give (a project may wrap it). If it says there's no library yet, the user
has to run `assetcat init <library folder>` and `assetcat index` once on this machine; don't run
`init` for them.

## Find

Add `--json` to every command whose output you parse. Paths in JSON are absolute.

```sh
assetcat search --packs dungeon tileset          # packs: names, contents, readmes, tags
assetcat search slime --type character,sheet     # assets inside packs
assetcat search --type tileset --grid --max-size 16x16
assetcat search door --pack <pack>               # within one pack (id, key or name)
assetcat packs --kind sfx                        # list packs; --tree shows the structure
assetcat packs --tag style:pixel-art --tag perspective:top-down   # packs by tag
assetcat show <pack>                             # license, AI status, folders, grids, readme, duplicates
assetcat show <library path of a file>           # one asset: size, grid, variants, license
assetcat search --meaning a slime monster --type character,sheet   # images by what they show
assetcat search --similar <library path of an image>              # images that look like it
assetcat search barrel --media model [--animated] [--rigged]      # 3D models (glTF clips, skeletons)
```

**By meaning.** If the library has image vectors (`assetcat classify --kind embed` was run),
`--meaning` finds images by what they show, whatever their names, and `--similar` finds images
that look like one you found. Results come ranked by `score`; they always return something, so
look at the scores (a big drop means the rest are weak) and at the images. Combine with filters
(`--type`, `--pack`, `--tag`). If it says there are no vectors or the AI extra is missing, fall
back to word search; don't install anything or run `classify` yourself.

**Subjects.** With vectors, images may also carry AI subjects (what they show: potion, sword,
slime). Word search matches them, `show <file> --json` lists them as `subjects` with a score,
and `show <pack> --json` has `contains` (subject -> number of images). They're cautious, so a
missing subject means little; a present one is usually right.

Asset types: sprite, character, sheet, tileset, ui, icon, background, portrait, vfx, preview,
sfx, music, voice, model, font. Pack kinds: graphics, 3d, music, sfx, audio, mixed, fonts,
engine, archived, other. Names are matched by word prefix, so try synonyms and broader words
when a search comes back empty, and search packs before assets.

**Tags.** Packs may carry tags as `namespace:value`: `kind:` (ui, tileset, characters, icons,
vfx, backgrounds, props, music, sfx, fonts, 3d), `style:` (pixel-art, vector, hand-drawn,
painted, low-poly, realistic), `perspective:` (top-down, side, isometric, first-person),
`genre:` and `mood:`, plus the user's own words. Most come from a vision model (`ai_tags` in
`show --json`, with `ai_model`) and some from the user. AI tags are good hints, not facts: check
the contact sheet before relying on one. Not every library has them; `assetcat classify` makes
them, and that's the user's call to run (it needs a local model server and takes a while).

**Look before you choose.** `assetcat sheet <pack> [folder]` renders a contact sheet and prints
its PNG path; open that image to see what a pack or folder actually looks like. Single image
files can be viewed directly by their absolute path. Describe what you saw when you propose
assets; don't pick by file name alone.

**Grids.** For sheets and tilesets, `show` and `search` report the tile size when it's known
(from Tiled files, atlases, Kenney's Tilesheet.txt, the file name, or the user's curation).
Use that size when setting up a TileSet or sprite frames; pulls copy sheets whole, not sliced.

**3D models.** An asset groups a model's formats (`formats: glb,fbx,obj`); prefer glTF/GLB for
Godot and the web. `show` lists triangles, animation clips (glTF) and the files a model loads
(textures, `.bin` buffers, `.mtl`), marking missing ones. Many models come with a rendered
preview image (`preview` in `show --json`): open it to see the model.

**Shortlists.** The user can star packs, folders and assets into named shortlists in the browse
UI (`assetcat serve`). They're TOML files in the profile's `shortlists/` folder (the profile
path is `profile` in `assetcat stats --json`), with `kind` and `ref` per item (`ref` is a pack id
or a library path). If the user says "use my X shortlist",
read that file and start from its items.

## Check before pulling

From `show <pack> --json`:

- `license` and `license_info` (name, summary, `attribution_required`, url). `unknown` means
  nobody has recorded it: tell the user and don't ship the asset until they have.
- `ai_generated`: `no` (human-made, with `ai_reason`), `yes`, or `unknown`. If the project cares
  about AI provenance (its docs say so, or the user asked), only use packs marked `no`, and say so
  when you skip others. Never change the recorded status yourself.
- `duplicates`: the same files may exist in another pack under a different license; prefer the
  one with the clearer license.
- `pending` > 0: some files are still cloud placeholders and can't be pulled yet.

## Pull

```sh
assetcat pull <pack | folder | file>... --to <project assets folder> \
    --manifest <project>/assets/sources.toml [--require-ai no] [--flatten] [--dry-run] [--json]
```

- Targets: a pack (id, key or name), or a library path of a folder or file inside a pack. A pack
  lands in `<to>/<pack folder>/`, a folder in `<to>/<folder>/`, a file in `<to>/<file>` (with its
  atlas or Tiled file if it has one; a 3D model with the textures, buffers and `.mtl` files it
  loads, at the same relative path, e.g. `Textures/colormap.png`). `--flatten` puts everything
  directly in `<to>`, except those model files, which stay where the model looks for them.
  Warnings in the output name files a model needs that aren't in the library.
- Each pack's license file is copied as `License-<Pack>.<ext>` next to the pulled files; a
  bundle-wide license is named after the bundle (`--license-name` changes the pattern,
  `--license-lf` normalizes line endings). If a pack has none, `pull` says so; then the license
  recorded in curation is all there is.
- `--manifest` records every file's source, pack and content hash, plus each pack's license,
  AI status and a ready-made credit line. Keep one manifest per project and always pass it.
- `--require-ai no` refuses packs not marked human-made. Only add `--force` if the user
  explicitly accepts that pack.
- It never overwrites a different existing file unless you pass `--overwrite`; ask first.
- Use `--dry-run` to show the user what a large pull would copy.

Pull only what the project uses: a folder or a few files, not a 2,000-file pack for one sprite.

## After pulling

- Add the credit lines `pull` printed (also in the manifest under `[packs."..."].credit`) to the
  project's credits, README or about screen. Attribution is mandatory where
  `attribution_required = true`.
- Keep the pulled license files with the assets.
- If the project discloses AI use (for example a Steam store page), the manifest's `ai_generated`
  and `ai_reason` per pack are the record to base it on.

## Don't

- Don't write anything inside the asset library. Curation (licenses, AI status, notes) is edited
  in the browse UI or the profile's TOML files by the user, not by you, unless they ask.
- Don't assume a license from the store or bundle name; use what `show` reports.
