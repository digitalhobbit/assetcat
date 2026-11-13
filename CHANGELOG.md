# Changelog

What changed in each release of `assetcat`, newest first. Versions are on
[PyPI](https://pypi.org/project/assetcat/); pin one with `uvx assetcat@X.Y.Z`.

## 1.0.2 (2026-11-12)

### Fixed

- Pack and bundle folders named in non-Latin scripts (Japanese, Chinese, Arabic, Cyrillic, ...)
  get keys of their own (`ドット絵パック`, `خطوط-عربية`). Before, they all came out as `x`, so
  all but one of them went missing from the index without a warning. Folders whose names differ
  only in punctuation or case (`Tiles!` and `tiles`) no longer collide either: when sibling
  folders give the same key, all but the first get a short suffix, `index` warns and `doctor`
  lists them.
- Readmes and licenses that aren't UTF-8 (Shift-JIS, GBK, Big5, EUC-KR, Windows code pages,
  UTF-16 without a byte-order mark) are detected and read correctly, so they show and search
  properly. Files whose encoding had to be guessed are listed in `doctor`. The next `index`
  re-reads doc files once.
- Pack pages in the browse UI open for packs whose ids aren't ASCII.
- Windows: the CLI no longer crashes printing names beyond the console's code page (such as
  `ドット工房`) when its output is piped or redirected, as Claude's skill does. Output is UTF-8.

### Check after upgrading

- Pack names with accented Latin letters get new keys: accents are now dropped instead of the
  letter (`Ógma Fadas` was `gma-fadas`, now `ogma-fadas`). Plain ASCII names keep their keys.
  If you set curation, AI tags or shortlist entries on such a pack, `assetcat doctor` lists the
  old keys under "curation keys matching no folder"; rename them to the new key that
  `assetcat packs --tree` shows.

## 1.0.1 (2026-11-09)

- The first search by meaning (or `classify --kind embed`) says it's downloading the SigLIP 2
  model (about 1.5 GB) and shows progress, instead of looking like a hang.
- Windows: thumbnails no longer fail now and then with a server error when many load at once.
- README: Windows GPU setup is one uv command (`--torch-backend auto` picks the CUDA build).

## 1.0.0 (2026-11-09)

First public release: an indexer, CLI and local browse UI for big game asset libraries, with
licenses, AI provenance, notes, tags and shortlists kept in plain TOML files next to the
library. Full-text search, optional search by meaning and AI tags from local models, `pull` into
a project with licenses and a credits manifest, and a Claude Code skill.
