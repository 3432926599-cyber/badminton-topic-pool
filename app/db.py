"""SQLite 数据层：作品池、热点池、候选池、配置、权重变更记录。"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, Iterator

from .config import (
    CANDIDATES_PER_DAY,
    DB_PATH,
    DEFAULT_DEEPSEEK_BASE,
    DEFAULT_DEEPSEEK_MODEL,
    DEFAULT_DIRECTIONS,
    DEFAULT_PUSH_TIME,
    DEFAULT_TEMPERATURE,
    DEFAULT_WEIGHTS,
    SCRIPT_MAX_CHARS,
    TAGS_PER_CANDIDATE,
)

_lock = threading.RLock()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    with _lock:
        conn = sqlite3.connect(DB_PATH, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def init_db() -> None:
    with connect() as c:
        c.executescript(
            """
            PRAGMA journal_mode=WAL;

            CREATE TABLE IF NOT EXISTS works (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                publish_date TEXT NOT NULL,
                created_at TEXT NOT NULL,
                script TEXT NOT NULL,
                title TEXT DEFAULT '',
                direction TEXT DEFAULT '其他',
                tags TEXT DEFAULT '',
                views INTEGER NOT NULL,
                likes INTEGER DEFAULT 0,
                comments INTEGER NOT NULL,
                shares INTEGER NOT NULL,
                favs INTEGER NOT NULL,
                danmu INTEGER DEFAULT 0,
                finish REAL NOT NULL,       -- 完播率 0~1
                jump2 REAL NOT NULL,        -- 2s跳出率 0~1
                ret2s REAL NOT NULL,        -- 2s留存率 = 1-jump2
                note TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS hotspots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                crawl_date TEXT NOT NULL,
                keyword TEXT NOT NULL,
                heat REAL DEFAULT 0,
                source TEXT DEFAULT '',
                url TEXT DEFAULT '',
                UNIQUE(crawl_date, keyword)
            );

            CREATE TABLE IF NOT EXISTS candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gen_date TEXT NOT NULL,
                slot INTEGER NOT NULL,
                direction TEXT NOT NULL,
                title TEXT NOT NULL,
                script TEXT NOT NULL,
                tags TEXT NOT NULL,
                rationale TEXT DEFAULT '',
                score REAL DEFAULT 0,
                status TEXT DEFAULT 'draft',   -- draft / adopted / discarded
                adopted_at TEXT DEFAULT '',
                feedback_views INTEGER DEFAULT 0,
                feedback_finish REAL DEFAULT 0,
                feedback_ret2s REAL DEFAULT 0,
                feedback_shares INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS weight_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                changed_at TEXT NOT NULL,
                trigger TEXT NOT NULL,          -- auto / manual / init
                weights TEXT NOT NULL,          -- JSON
                sample_size INTEGER NOT NULL,
                note TEXT DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_works_date ON works(publish_date);
            CREATE INDEX IF NOT EXISTS idx_hotspots_date ON hotspots(crawl_date);
            CREATE INDEX IF NOT EXISTS idx_candidates_date ON candidates(gen_date, slot);
            """
        )
        defaults = {
            "push_time": DEFAULT_PUSH_TIME,
            "smtp_host": "", "smtp_port": "465", "smtp_user": "",
            "smtp_password": "", "mail_from": "", "mail_to": "",
            "smtp_use_ssl": "1",
            "deepseek_api_key": "",
            "deepseek_base": DEFAULT_DEEPSEEK_BASE,
            "deepseek_model": DEFAULT_DEEPSEEK_MODEL,
            "temperature": str(DEFAULT_TEMPERATURE),
            "directions": json.dumps(DEFAULT_DIRECTIONS, ensure_ascii=False),
            "weights": json.dumps(DEFAULT_WEIGHTS),
            "weights_locked": "0",
            "candidates_per_day": str(CANDIDATES_PER_DAY),
            "script_max_chars": str(SCRIPT_MAX_CHARS),
            "tags_per_candidate": str(TAGS_PER_CANDIDATE),
        }
        for k, v in defaults.items():
            c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))


# ---------- settings ----------

def get_setting(key: str, default: str = "") -> str:
    with connect() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO settings(key,value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def set_settings(mapping: dict[str, str]) -> None:
    with connect() as c:
        for k, v in mapping.items():
            c.execute(
                "INSERT INTO settings(key,value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (k, v),
            )


# ---------- works ----------

WORK_FIELDS = [
    "publish_date", "script", "title", "direction", "tags", "views",
    "likes", "comments", "shares", "favs", "danmu", "finish", "jump2", "ret2s", "note",
]


def add_work(data: dict[str, Any]) -> int:
    payload = {k: data.get(k) for k in WORK_FIELDS}
    payload["created_at"] = datetime.now().isoformat(timespec="seconds")
    cols = ["created_at"] + WORK_FIELDS
    with connect() as c:
        cur = c.execute(
            f"INSERT INTO works({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            [payload[k] for k in cols],
        )
        return int(cur.lastrowid)


def update_work(work_id: int, data: dict[str, Any]) -> bool:
    fields = [k for k in WORK_FIELDS if k in data]
    if not fields:
        return False
    with connect() as c:
        cur = c.execute(
            f"UPDATE works SET {','.join(k + '=?' for k in fields)} WHERE id=?",
            [data[k] for k in fields] + [work_id],
        )
        return cur.rowcount > 0


def delete_work(work_id: int) -> bool:
    with connect() as c:
        cur = c.execute("DELETE FROM works WHERE id=?", (work_id,))
        return cur.rowcount > 0


def list_works() -> list[dict[str, Any]]:
    with connect() as c:
        rows = c.execute("SELECT * FROM works ORDER BY publish_date DESC, id DESC").fetchall()
        return [dict(r) for r in rows]


def get_work(work_id: int) -> dict[str, Any] | None:
    with connect() as c:
        row = c.execute("SELECT * FROM works WHERE id=?", (work_id,)).fetchone()
        return dict(row) if row else None


# ---------- hotspots ----------

def upsert_hotspot(crawl_date: str, keyword: str, heat: float = 0,
                   source: str = "", url: str = "") -> bool:
    with connect() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO hotspots(crawl_date,keyword,heat,source,url) "
            "VALUES (?,?,?,?,?)",
            (crawl_date, keyword, heat, source, url),
        )
        return cur.rowcount > 0


def list_hotspots(limit: int = 200) -> list[dict[str, Any]]:
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM hotspots ORDER BY crawl_date DESC, heat DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def hotspots_for_date(d: str) -> list[dict[str, Any]]:
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM hotspots WHERE crawl_date=? ORDER BY heat DESC", (d,)
        ).fetchall()
        return [dict(r) for r in rows]


# ---------- candidates ----------

def add_candidate(data: dict[str, Any]) -> int:
    fields = [
        "gen_date", "slot", "direction", "title", "script", "tags",
        "rationale", "score", "status",
    ]
    with connect() as c:
        cur = c.execute(
            f"INSERT INTO candidates({','.join(fields)}) VALUES ({','.join('?' * len(fields))})",
            [data.get(k, "") for k in fields],
        )
        return int(cur.lastrowid)


def recent_candidates(days: int = 7) -> list[dict[str, Any]]:
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM candidates WHERE gen_date >= date('now', ?) "
            "ORDER BY gen_date DESC",
            (f"-{days} days",),
        ).fetchall()
        return [dict(r) for r in rows]


def candidates_for_date(d: str) -> list[dict[str, Any]]:
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM candidates WHERE gen_date=? ORDER BY slot", (d,)
        ).fetchall()
        return [dict(r) for r in rows]


def set_candidate_status(cid: int, status: str) -> bool:
    with connect() as c:
        cur = c.execute(
            "UPDATE candidates SET status=?, adopted_at=? WHERE id=?",
            (status, datetime.now().isoformat(timespec="seconds") if status == "adopted" else "", cid),
        )
        return cur.rowcount > 0


def clear_draft_candidates(d: str) -> int:
    """清空当天未采用的草稿候选，用于强制重新生成；已采用/弃用的保留。"""
    with connect() as c:
        cur = c.execute(
            "DELETE FROM candidates WHERE gen_date=? AND status='draft'", (d,)
        )
        return cur.rowcount


# ---------- weight history ----------

def add_weight_history(weights: dict[str, float], sample_size: int,
                       trigger: str, note: str = "") -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO weight_history(changed_at,trigger,weights,sample_size,note) "
            "VALUES (?,?,?,?,?)",
            (
                datetime.now().isoformat(timespec="seconds"),
                trigger,
                json.dumps(weights, ensure_ascii=False),
                sample_size,
                note,
            ),
        )


def list_weight_history(limit: int = 30) -> list[dict[str, Any]]:
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM weight_history ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def today_str() -> str:
    return date.today().isoformat()
