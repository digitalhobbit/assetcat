"""SQLite schema. `files` persists between runs (incremental metadata); everything else is
derived and rebuilt by each `assetcat index`."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1
DERIVED_VERSION = 4  # bump when the derived tables change; `index` rebuilds them

FILES = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,      -- relative to the library root, '/' separated
    ext TEXT NOT NULL,
    category TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    meta_version INTEGER NOT NULL DEFAULT 0,   -- 0 = not extracted yet
    width INTEGER, height INTEGER, frames INTEGER, palette INTEGER, alpha INTEGER,
    duration REAL, channels INTEGER, sample_rate INTEGER,
    font_family TEXT,
    text TEXT,                      -- docs: extracted text
    info TEXT,                      -- data files: parsed JSON (atlases, Tiled tilesets)
    hash TEXT,                      -- content hash, only for files whose size collides
    error TEXT,
    group_id TEXT,
    pack_id TEXT
);
CREATE INDEX IF NOT EXISTS files_pack ON files(pack_id);
CREATE INDEX IF NOT EXISTS files_size ON files(size);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

DERIVED = """
DROP TABLE IF EXISTS groups;
DROP TABLE IF EXISTS packs;
DROP TABLE IF EXISTS assets;
DROP TABLE IF EXISTS asset_files;
DROP TABLE IF EXISTS asset_subjects;
DROP TABLE IF EXISTS docs;
DROP TABLE IF EXISTS pack_dupes;
DROP TABLE IF EXISTS assets_fts;
DROP TABLE IF EXISTS packs_fts;

CREATE TABLE groups (
    id TEXT PRIMARY KEY, section TEXT, path TEXT, name TEXT,
    store TEXT, purchased INTEGER, license TEXT, ai_generated TEXT, ai_reason TEXT, notes TEXT,
    curation_file TEXT
);
CREATE TABLE packs (
    rid INTEGER PRIMARY KEY,
    id TEXT NOT NULL UNIQUE, key TEXT, group_id TEXT, section TEXT, path TEXT, name TEXT,
    role TEXT, parent_id TEXT, depth INTEGER,
    kind TEXT, media TEXT, engines TEXT,
    file_count INTEGER, total_size INTEGER, pending INTEGER, asset_count INTEGER,
    type_counts TEXT,               -- JSON {type: n}
    license TEXT, license_from TEXT, license_file TEXT,
    ai_generated TEXT, ai_reason TEXT,
    cover TEXT, cover_kind TEXT,    -- library path of a bundled preview, or 'sheet'
    sheet TEXT,                     -- cache path of the generated contact sheet
    tags TEXT,                      -- JSON list: effective tags (AI, then curation tags / tags_remove)
    notes TEXT, description TEXT, description_source TEXT,   -- source: curation | ai
    ai_tags TEXT, ai_model TEXT, ai_at TEXT, ai_stale INTEGER, ai_description TEXT,  -- newest AI result
    contains TEXT                   -- JSON {subject: images}, from asset subjects, sub-packs included
);
CREATE INDEX packs_group ON packs(group_id);
CREATE TABLE assets (
    id INTEGER PRIMARY KEY,
    pack_id TEXT, name TEXT, folder TEXT, path TEXT, media TEXT, type TEXT,
    formats TEXT, variants INTEGER, available INTEGER, displayable INTEGER,
    width INTEGER, height INTEGER, frames INTEGER, palette INTEGER, duration REAL,
    grid_w INTEGER, grid_h INTEGER, grid_spacing INTEGER, grid_margin INTEGER,
    grid_cols INTEGER, grid_rows INTEGER, grid_source TEXT, grid_confidence REAL,
    atlas TEXT,                     -- library path of an atlas/Tiled file describing it
    preview TEXT,                   -- models: library path of a bundled render of this model
    preview_of INTEGER,             -- that render's own asset row points back at the model
    tris INTEGER, clips INTEGER, rigged INTEGER,  -- models: triangles, animation clips, skinned
    missing INTEGER                 -- models: referenced files (textures, buffers) not in the library
);
CREATE INDEX assets_pack ON assets(pack_id);
CREATE TABLE asset_files (asset_id INTEGER, file_id INTEGER, PRIMARY KEY (asset_id, file_id));
CREATE TABLE asset_subjects (asset_id INTEGER, subject TEXT, score REAL, PRIMARY KEY (asset_id, subject));
CREATE INDEX asset_subjects_subject ON asset_subjects(subject);
CREATE TABLE docs (
    file_id INTEGER PRIMARY KEY, path TEXT, role TEXT, pack_id TEXT, group_id TEXT, text TEXT
);
CREATE TABLE pack_dupes (
    pack_a TEXT, pack_b TEXT, shared INTEGER, frac_a REAL, frac_b REAL,
    PRIMARY KEY (pack_a, pack_b)
);
CREATE VIRTUAL TABLE assets_fts USING fts5(name, folder, pack, grp, type, tags, content='');
CREATE VIRTUAL TABLE packs_fts USING fts5(name, grp, words, docs, description, tags, content='');
"""


def connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(FILES)
    v = con.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
    if v is None or int(v[0]) != SCHEMA_VERSION:
        con.executescript("DROP TABLE IF EXISTS files;" + FILES)
        con.execute("INSERT OR REPLACE INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
        con.commit()
    return con


def has_derived(con: sqlite3.Connection) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE name='packs'").fetchone() is not None


def derived_current(con: sqlite3.Connection) -> bool:
    """Were the derived tables built by this version's schema?"""
    v = con.execute("SELECT value FROM meta WHERE key='derived'").fetchone()
    return v is not None and int(v[0]) == DERIVED_VERSION
