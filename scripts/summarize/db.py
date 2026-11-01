#!/usr/bin/env python3
"""两个库：papers.db（证据层）与 structure.db（结构层 + 规范层 + 过程对象）。

证据层只增不改，带出处；结构层由归纳派生，所有变更走 proposals/changelog，
因此任何一次结构变化都能回滚。
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

EVIDENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS papers(
  paper_id TEXT PRIMARY KEY,
  doi TEXT, title TEXT, year INT, venue TEXT, authors TEXT, abstract TEXT,
  source TEXT, zotero_key TEXT, pdf_path TEXT, fulltext_path TEXT,
  fulltext_chars INT DEFAULT 0, meta_level TEXT DEFAULT 'abstract',
  added_at TEXT
);
CREATE TABLE IF NOT EXISTS contributions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  paper_id TEXT NOT NULL, target_text TEXT NOT NULL, technique TEXT,
  method TEXT, claim TEXT, addr_technique TEXT, addr_problem TEXT,
  evidence TEXT, level TEXT DEFAULT 'fulltext', source TEXT DEFAULT 'agent',
  axis_id TEXT, assign_conf REAL, assign_method TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS practices(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  paper_id TEXT NOT NULL, axis_id TEXT,
  baselines TEXT, benchmarks TEXT, params TEXT, metrics TEXT,
  evidence TEXT, level TEXT DEFAULT 'fulltext', created_at TEXT
);
CREATE TABLE IF NOT EXISTS critiques(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  paper_id TEXT, target_technique TEXT, target_text TEXT,
  problem TEXT, kind TEXT DEFAULT 'critique', evidence TEXT,
  source TEXT DEFAULT 'agent', created_at TEXT
);
CREATE TABLE IF NOT EXISTS ingest_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, paper_id TEXT, action TEXT,
  detail TEXT, at TEXT
);
CREATE INDEX IF NOT EXISTS idx_contrib_paper ON contributions(paper_id);
CREATE INDEX IF NOT EXISTS idx_contrib_axis ON contributions(axis_id);
CREATE INDEX IF NOT EXISTS idx_practice_paper ON practices(paper_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_papers_doi ON papers(doi) WHERE doi IS NOT NULL AND doi != '';
"""

STRUCTURE_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS axes(
  axis_id TEXT PRIMARY KEY, name TEXT, definition TEXT,
  status TEXT DEFAULT 'candidate',
  parent_id TEXT, lineage TEXT, pinned INT DEFAULT 0,
  evidence_count INT DEFAULT 0, group_count INT DEFAULT 0,
  tension TEXT, centroid TEXT, first_seen TEXT, last_change TEXT
);
CREATE TABLE IF NOT EXISTS axis_members(
  axis_id TEXT, paper_id TEXT, target_key TEXT, weight REAL,
  PRIMARY KEY(axis_id, paper_id, target_key)
);
CREATE TABLE IF NOT EXISTS edges(
  edge_id INTEGER PRIMARY KEY AUTOINCREMENT,
  src TEXT, dst TEXT, rel TEXT DEFAULT 'resolve', problem TEXT,
  evidence TEXT, axis_id TEXT, status TEXT DEFAULT 'candidate', created_at TEXT
);
CREATE TABLE IF NOT EXISTS combos(
  paper_id TEXT, axis_id TEXT, created_at TEXT,
  PRIMARY KEY(paper_id, axis_id)
);
CREATE TABLE IF NOT EXISTS couplings(
  a TEXT, b TEXT, ratio REAL, papers TEXT, updated_at TEXT,
  PRIMARY KEY(a, b)
);
CREATE TABLE IF NOT EXISTS clauses(
  clause_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,            -- baseline | benchmark | param | redline | writing
  axis_id TEXT, text TEXT NOT NULL,
  status TEXT DEFAULT 'candidate',   -- candidate | active | retired
  normative INT DEFAULT 0,
  support REAL, counter TEXT, evidence TEXT,
  created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS proposals(
  proposal_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,            -- new_axis | redefine_axis | split_axis | merge_axis
                                 -- | add_edge | add_clause | revise_clause | retire_clause
                                 -- | promote_axis | demote_axis | new_paradigm | new_tech | dispute
  payload TEXT NOT NULL, evidence TEXT, impact TEXT,
  risk TEXT DEFAULT 'agent',     -- silent | agent | human
  status TEXT DEFAULT 'draft',   -- draft | auto_committed | agent_approved | escalated
                                 -- | human_approved | rejected | reverted | needs_source
  agent_checks TEXT, reason TEXT, created_at TEXT, decided_at TEXT, decided_by TEXT
);
CREATE TABLE IF NOT EXISTS holes(
  hole_id TEXT PRIMARY KEY, kind TEXT NOT NULL, axis_id TEXT, desc TEXT,
  action TEXT, close_condition TEXT,
  status TEXT DEFAULT 'open',    -- open | running | closed | exhausted | escalated
  attempts INT DEFAULT 0, queries INT DEFAULT 0, note TEXT,
  created_at TEXT, closed_at TEXT
);
CREATE TABLE IF NOT EXISTS reflections(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  unit TEXT NOT NULL, candidates INT DEFAULT 0, zero_reason TEXT,
  payload TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS unadopted(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  unit TEXT, payload TEXT, failed_gate TEXT, reason TEXT, at TEXT
);
CREATE TABLE IF NOT EXISTS review_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  proposal_id TEXT, checks TEXT, verdict TEXT, reason TEXT, at TEXT
);
CREATE TABLE IF NOT EXISTS changelog(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  op TEXT, target TEXT, before TEXT, after TEXT,
  proposal_id TEXT, at TEXT
);
CREATE TABLE IF NOT EXISTS page_hashes(
  path TEXT PRIMARY KEY, hash TEXT, rendered_at TEXT, edited INT DEFAULT 0
);
CREATE TABLE IF NOT EXISTS claims(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  statement TEXT NOT NULL,
  status TEXT DEFAULT 'consensus',   -- consensus | contested | refuted | user_note
  support TEXT, counter TEXT, axis_id TEXT, created_at TEXT
);
"""


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class Store:
    """一个领域工作区的两个库。"""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.db_dir = self.root / "db"
        self.vault = self.root / "vault"
        self.papers_path = self.db_dir / "papers.db"
        self.structure_path = self.db_dir / "structure.db"
        self.artifacts = self.root / "研究档案"

    # -- init -------------------------------------------------------------
    def init(self) -> None:
        for d in (self.db_dir, self.vault, self.artifacts):
            d.mkdir(parents=True, exist_ok=True)
        self.papers().close()
        self.structure().close()

    # -- connections ------------------------------------------------------
    def papers(self) -> sqlite3.Connection:
        self.papers_path.parent.mkdir(parents=True, exist_ok=True)
        conn = _connect(self.papers_path)
        if conn.execute("PRAGMA user_version").fetchone()[0] < 1:
            conn.executescript(EVIDENCE_SCHEMA)
            conn.execute("PRAGMA user_version=1")
            conn.commit()
        return conn

    def structure(self) -> sqlite3.Connection:
        self.structure_path.parent.mkdir(parents=True, exist_ok=True)
        conn = _connect(self.structure_path)
        if conn.execute("PRAGMA user_version").fetchone()[0] < 1:
            conn.executescript(STRUCTURE_SCHEMA)
            conn.execute("PRAGMA user_version=1")
            conn.commit()
        return conn

    # -- helpers ----------------------------------------------------------
    def meta(self, key: str, default: str = "") -> str:
        with self.structure() as s:
            row = s.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self.structure() as s:
            s.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def log_change(self, op: str, target: str, before="", after="", proposal_id="") -> None:
        with self.structure() as s:
            s.execute(
                "INSERT INTO changelog(op,target,before,after,proposal_id,at) "
                "VALUES(?,?,?,?,?,?)",
                (op, target, json.dumps(before, ensure_ascii=False) if not isinstance(before, str) else before,
                 json.dumps(after, ensure_ascii=False) if not isinstance(after, str) else after,
                 proposal_id, now()),
            )

    def snapshot(self, note: str = "") -> str:
        """把两个库完整复制一份，返回快照目录。"""
        import shutil

        stamp = time.strftime("%Y%m%d-%H%M%S")
        dst = self.root / "db" / "snapshots" / stamp
        dst.mkdir(parents=True, exist_ok=True)
        for src in (self.papers_path, self.structure_path):
            if src.exists():
                shutil.copy2(src, dst / src.name)
        (dst / "note.txt").write_text(note or "", encoding="utf-8")
        return str(dst)


# ------------------------------------------------------------------ utils

def rows(conn: sqlite3.Connection, sql: str, args=()) -> list[sqlite3.Row]:
    return list(conn.execute(sql, args))


def one(conn: sqlite3.Connection, sql: str, args=()):
    r = conn.execute(sql, args).fetchone()
    return r


def js(v) -> list:
    if not v:
        return []
    if isinstance(v, list):
        return v
    try:
        out = json.loads(v)
        return out if isinstance(out, list) else [out]
    except Exception:
        return [x.strip() for x in str(v).split(",") if x.strip()]
