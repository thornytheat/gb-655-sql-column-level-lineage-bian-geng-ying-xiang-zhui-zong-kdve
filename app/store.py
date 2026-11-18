"""SQLite 读写：批次快照、运行清单、隔离行、键值状态。

只放数据访问，不放判定；调用方拿到的是普通字典。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path

from app import config

_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    schema_id      TEXT PRIMARY KEY,
    version_label  TEXT NOT NULL,
    source_path    TEXT NOT NULL,
    source_sha256  TEXT NOT NULL,
    encoding       TEXT NOT NULL,
    bom            INTEGER NOT NULL,
    field_count    INTEGER NOT NULL,
    blocked_count  INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    payload_json   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id         TEXT PRIMARY KEY,
    batch_key      TEXT NOT NULL UNIQUE,
    old_schema_id  TEXT NOT NULL,
    new_schema_id  TEXT NOT NULL,
    version_seq    INTEGER NOT NULL,
    input_sha256   TEXT NOT NULL,
    ruleset        TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to   TEXT,
    summary_json   TEXT NOT NULL,
    payload_json   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quarantine (
    schema_id     TEXT NOT NULL,
    line          INTEGER,
    table_name    TEXT,
    column_name   TEXT,
    reason        TEXT NOT NULL,
    blocking      INTEGER NOT NULL,
    detail        TEXT,
    raw           TEXT
);
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS publish_keys (
    publish_key     TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    request_sha256  TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quarantine_schema ON quarantine(schema_id);
"""


def db_path() -> Path:
    raw = os.environ.get("LINEAGEGUARD_DB", "").strip()
    path = Path(raw) if raw else config.DEFAULT_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with _LOCK, _connect() as conn:
        conn.executescript(SCHEMA)


def reset() -> None:
    """测试与重建用：清空全部业务表。"""
    with _LOCK, _connect() as conn:
        conn.executescript(SCHEMA)
        for table in ("snapshots", "runs", "quarantine", "kv", "publish_keys"):
            conn.execute(f"DELETE FROM {table}")


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(value):
    if not value:
        return None
    return json.loads(value)


# ---------- 快照 ----------

def save_snapshot(snapshot: dict) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT INTO snapshots (schema_id, version_label, source_path, source_sha256,
                   encoding, bom, field_count, blocked_count, created_at, payload_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(schema_id) DO UPDATE SET
                   version_label=excluded.version_label,
                   source_path=excluded.source_path,
                   source_sha256=excluded.source_sha256,
                   encoding=excluded.encoding,
                   bom=excluded.bom,
                   field_count=excluded.field_count,
                   blocked_count=excluded.blocked_count,
                   created_at=excluded.created_at,
                   payload_json=excluded.payload_json""",
            (
                snapshot["schema_id"], snapshot["version_label"], snapshot["source_path"],
                snapshot["source_sha256"], snapshot["encoding"], int(bool(snapshot["bom"])),
                int(snapshot["field_count"]), int(snapshot["blocked_count"]),
                snapshot["created_at"], _dumps(snapshot),
            ),
        )


def load_snapshot(schema_id: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM snapshots WHERE schema_id=?", (schema_id,)
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def list_snapshots(limit: int = 20) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT schema_id, version_label, source_path, source_sha256, encoding, bom,
                      field_count, blocked_count, created_at
               FROM snapshots ORDER BY created_at DESC, schema_id LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def save_quarantine(schema_id: str, rows: list[dict]) -> None:
    with _LOCK, _connect() as conn:
        conn.execute("DELETE FROM quarantine WHERE schema_id=?", (schema_id,))
        conn.executemany(
            """INSERT INTO quarantine (schema_id, line, table_name, column_name, reason,
                   blocking, detail, raw) VALUES (?,?,?,?,?,?,?,?)""",
            [
                (
                    schema_id, int(row.get("line") or 0), row.get("table") or "",
                    row.get("column") or "", row["reason"], int(bool(row.get("blocking"))),
                    row.get("detail") or "", row.get("raw") or "",
                )
                for row in rows
            ],
        )


def list_quarantine(schema_id: str) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT line, table_name, column_name, reason, blocking, detail, raw
               FROM quarantine WHERE schema_id=? ORDER BY line, reason""",
            (schema_id,),
        ).fetchall()
    return [
        {
            "line": row["line"],
            "table": row["table_name"],
            "column": row["column_name"],
            "reason": row["reason"],
            "blocking": bool(row["blocking"]),
            "detail": row["detail"],
            "raw": row["raw"],
        }
        for row in rows
    ]


# ---------- 运行清单 ----------

def _insert_run(conn: sqlite3.Connection, run: dict) -> None:
    conn.execute(
        """INSERT INTO runs (run_id, batch_key, old_schema_id, new_schema_id, version_seq,
               input_sha256, ruleset, created_at, effective_from, effective_to,
               summary_json, payload_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            run["run_id"], run["batch_key"], run["old_schema_id"], run["new_schema_id"],
            int(run["version_seq"]), run["input_sha256"], run["ruleset"],
            run["created_at"], run["effective_from"], run["effective_to"],
            _dumps(run["summary"]), _dumps(run),
        ),
    )


def _allocate_seq(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(version_seq), 0) AS n FROM runs").fetchone()
    return int(row["n"]) + 1


def save_run(run: dict, summarize=None) -> None:
    """写批次；序号与摘要都在锁内落定，避免摘要里记下空序号。

    写锁用 BEGIN IMMEDIATE：先拿写锁再读 max，跨进程也不会撞号。
    """
    with _LOCK, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if run.get("version_seq") in (None, ""):
            run["version_seq"] = _allocate_seq(conn)
        if summarize is not None:
            run["summary"] = summarize(run)
        _insert_run(conn, run)


def list_run_payloads() -> list[dict]:
    """全部批次的完整结果，按版本序号升序；取证按生效区间挑选时用。"""
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT payload_json FROM runs ORDER BY version_seq, run_id"
        ).fetchall()
    return [_loads(row["payload_json"]) for row in rows]


def find_publish_key(publish_key: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            """SELECT publish_key, run_id, request_sha256, created_at
               FROM publish_keys WHERE publish_key=?""",
            (publish_key,),
        ).fetchone()
    return dict(row) if row else None


def register_publish_key(publish_key: str, run_id: str, request_sha256: str,
                         created_at: str) -> None:
    """把一个发布键钉到已有批次上；重复登记保持首次落库的那一条。"""
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO publish_keys
               (publish_key, run_id, request_sha256, created_at)
               VALUES (?,?,?,?)""",
            (publish_key, run_id, request_sha256, created_at),
        )


def publish_run(run: dict, publish_key: str, request_sha256: str,
                summarize=None) -> dict:
    """原子发布：登记幂等键、分配序号、写批次，同一事务内完成。

    三种结局：
    - 发布键已登记且输入摘要相同：原样返回上次发布的批次（重试不重复发布）；
    - 发布键已登记但输入摘要不同：返回冲突，调用方报 409，不允许顶掉已发证据；
    - 新键：分配序号并入库，失败整体回滚。
    同一批次键已存在（同一输入用另一个键发布）时只补登记键，不再新增批次。
    """
    with _LOCK, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        known = conn.execute(
            "SELECT run_id, request_sha256 FROM publish_keys WHERE publish_key=?",
            (publish_key,),
        ).fetchone()
        if known:
            if known["request_sha256"] != request_sha256:
                return {"status": "conflict", "run_id": known["run_id"],
                        "published_sha256": known["request_sha256"]}
            return {"status": "replayed", "run_id": known["run_id"]}

        same_batch = conn.execute(
            "SELECT run_id FROM runs WHERE batch_key=?", (run["batch_key"],)
        ).fetchone()
        if same_batch is not None:
            conn.execute(
                """INSERT INTO publish_keys
                   (publish_key, run_id, request_sha256, created_at)
                   VALUES (?,?,?,?)""",
                (publish_key, same_batch["run_id"], request_sha256, run["created_at"]),
            )
            return {"status": "replayed", "run_id": same_batch["run_id"]}

        run["version_seq"] = _allocate_seq(conn)
        if summarize is not None:
            run["summary"] = summarize(run)
        _insert_run(conn, run)
        conn.execute(
            """INSERT INTO publish_keys (publish_key, run_id, request_sha256, created_at)
               VALUES (?,?,?,?)""",
            (publish_key, run["run_id"], request_sha256, run["created_at"]),
        )
        return {"status": "published", "run_id": run["run_id"],
                "version_seq": run["version_seq"]}


def find_run_by_key(batch_key: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM runs WHERE batch_key=?", (batch_key,)
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def run_by_id(run_id: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def latest_run() -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM runs ORDER BY version_seq DESC LIMIT 1"
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def list_runs(limit: int = 30) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT summary_json FROM runs ORDER BY version_seq DESC, run_id LIMIT ?""",
            (limit,),
        ).fetchall()
    return [_loads(row["summary_json"]) for row in rows]


def count_runs() -> int:
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()
    return int(row["n"])


def current_version_seq() -> int:
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT COALESCE(MAX(version_seq), 0) AS n FROM runs").fetchone()
    return int(row["n"])


def list_windows() -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT run_id, effective_from, effective_to FROM runs ORDER BY version_seq"
        ).fetchall()
    return [dict(row) for row in rows]


# ---------- 键值 ----------

def set_kv(key: str, value) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO kv (k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, _dumps(value)),
        )


def get_kv(key: str, default=None):
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
    return _loads(row["v"]) if row else default
