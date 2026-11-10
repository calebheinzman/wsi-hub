"""Answers from the scholarly APIs (OpenAlex, Crossref, Semantic Scholar), kept on disk.

One paper is looked up from several places -- the full-text fallback, the landing-page
and missing-tables rewrites, and step 1.8's PDF search -- and again after a resume or in
an evaluation. OpenAlex's title search costs 10 of the key's 10,000 daily credits, and
Semantic Scholar allows 1 request a second, so each lookup is made once: SQLite at
WSI_LOOKUP_CACHE (default pipeline/data/lookup_cache.sqlite), shared by every process.
Only definite answers are kept (a record, or "no match"); errors and 429s are not, so they
are asked again.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

_LOCAL = threading.local()
_MISSING = object()


def _db() -> sqlite3.Connection:
    path = os.getenv("WSI_LOOKUP_CACHE", "pipeline/data/lookup_cache.sqlite")
    conn = getattr(_LOCAL, "conn", None)
    if conn is None or getattr(_LOCAL, "path", None) != path or getattr(_LOCAL, "pid", None) != os.getpid():
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=60)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE IF NOT EXISTS lookup (ns TEXT, key TEXT, value TEXT, ts REAL, PRIMARY KEY (ns, key))")
        _LOCAL.conn, _LOCAL.path, _LOCAL.pid = conn, path, os.getpid()
    return conn


def get(ns: str, key: str, default: Any = _MISSING) -> Any:
    """The cached answer, or `default` (a sentinel by default; test with `is MISS`)."""
    if not key:
        return default
    try:
        row = _db().execute("SELECT value FROM lookup WHERE ns=? AND key=?", (ns, key)).fetchone()
    except sqlite3.Error:
        return default
    return json.loads(row[0]) if row else default


def put(ns: str, key: str, value: Any) -> None:
    if not key:
        return
    try:
        conn = _db()
        conn.execute("INSERT OR REPLACE INTO lookup VALUES (?, ?, ?, ?)", (ns, key, json.dumps(value), time.time()))
        conn.commit()
    except sqlite3.Error:
        pass  # a cache that can't write just means asking again


MISS = _MISSING


def cached(ns: str, key: str, compute, keep=lambda v: True) -> Optional[Any]:
    """get(ns, key), else compute() -- stored when keep(value)."""
    hit = get(ns, key)
    if hit is not MISS:
        return hit
    value = compute()
    if keep(value):
        put(ns, key, value)
    return value
