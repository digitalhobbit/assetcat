# AssetCat (`assetcat`)

An indexer, CLI and local browse UI for big game asset libraries, with licenses and AI provenance
kept in plain files. MIT open source, built with Claude Code. README.md covers usage; pull
requests aren't accepted for now (feature requests come in as issues).

## Layout

- `src/assetcat/`: the package. `config` (user config, library/profile/cache lookup, the write
  guard), `init` (layout detection, starter profile), `scan` (walk, groups, pack tree),
  `classify` (file categories, asset types, variant keys; pure functions), `extract` (per-file
  metadata, runs in worker processes), `grids`, `sheets` (contact sheets), `curation` (reads the
  profile), `edit` (curation writes with tomlkit, shortlists), `indexer` (the pipeline), `db`
  (schema), `cli`, `server` (FastAPI: JSON API, file and thumbnail serving), `pull` (copy
  assets into a project with licenses and a manifest), `ai` (pack tags from a vision model over an
  OpenAI-compatible API, result shards in the profile's `ai/`, tag vocabulary and override layers).
  `embed` (image vectors from a SigLIP 2 model in-process, the optional `ai` extra; shards in
  `ai/<model>/`, search by meaning and "more like this" in NumPy).
- `src/assetcat/web/`: the UI, a no-build Preact + htm page; `vendor/` is checked in.
  `model3d.js` (three.js viewer and thumbnail renders) is imported only when a 3D model shows.
- `src/assetcat/starter/`: files `assetcat init` copies into a new profile.
- `skills/assetcat/SKILL.md`: the generic Claude skill (search, check, pull, credits). Keep it in
  step with the CLI: when a command or flag changes, update the skill too.
- `tests/`: pytest. `make_library.py` generates the synthetic library every test runs on.
- `scripts/ui_check.py`: Playwright checks against a running `assetcat serve` (Chrome, headless).

## Rules

- **The library is read-only.** The only place the tool writes inside the library is the
  profile folder; every such write goes through `Config.writable()` (or `ensure_inside` in init).
  `pull` writes only to its destination and manifest, and refuses both inside the library. Tests
  check the library is byte-for-byte unchanged after init, index, edits and pulls.
- **The database is disposable.** It's a cache built from the library plus the profile, never
  the only home of anything. No migrations: derived tables are rebuilt on every `index`; only
  `files` persists, and changing it means bumping `db.SCHEMA_VERSION` (drop and rebuild).
  `extract.META_VERSION` with `META_MIN` (every file) or `META_MIN_BY_CATEGORY` (one category)
  forces re-extraction; `db.DERIVED_VERSION` (bump it when the derived tables change) makes
  commands ask for an `index` instead of failing on an old one. Anything a human decided or that
  is expensive to compute goes in the profile.
- **Profile files** carry `format = N` (missing means 1). Read older formats; refuse newer ones.
  Edits must keep the user's comments and layout: an edit followed by its reverse leaves the file
  byte-identical (tested).
- **No personal data in the repo:** no absolute paths of a real machine, no real purchase
  details, no real curation content. Examples and tests use the synthetic library.
- **Cross-platform:** macOS, Windows and Linux (CI runs all three). Library paths in the
  database are `/`-separated and relative; use `cfg.abs()` / `cfg.fs()` for file system access
  (`fs()` is long-path safe on Windows). Write text files with `newline="\n"`.
- The server binds to 127.0.0.1 by default because it can edit curation files, and only serves
  library files that are in the index.

## Commands

```sh
uv sync                                     # dev environment (--extra ai adds PyTorch for embeddings)
uv run pytest                               # all tests (a few seconds)
uv run assetcat ...                         # the CLI from this checkout
uv run --with playwright python scripts/ui_check.py http://127.0.0.1:8765/ --shots /tmp/shots
```

Use uv for everything Python. To try things on a library without touching your own config, set
`ASSETCAT_CONFIG` and `ASSETCAT_CACHE` to scratch paths and run `assetcat init` on a library made
with `tests/make_library.py`.

## Releases

Bump `version` in `pyproject.toml` and `src/assetcat/__init__.py`, turn `## Unreleased` in
`CHANGELOG.md` into `## X.Y.Z (date)`, commit, then tag `vX.Y.Z` and push the tag. CI tests it
on all three systems and publishes it to PyPI (trusted publishing, the `publish` job in
`ci.yml`; it refuses a tag that doesn't match the version). A published version can't be
replaced, only yanked: fix forward with the next patch version. Other repos pin a version with
`uvx assetcat@X.Y.Z`.

## Workflow

- Commit only when asked.
- Changes users would notice go in `CHANGELOG.md` under `## Unreleased` (Fixed / Added / Changed,
  plus "Check after upgrading" when they may need to act), in the same commit as the change.
- After changing the UI or server, run the UI check against a real `assetcat serve`, not only
  the tests.
