from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError, ValidationError
from .rules import ID_PREFIX, STATES, is_reopen_trigger, reopen_reason


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()
        self._migrate_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    item_version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS closures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    item_version INTEGER NOT NULL,
                    conclusion TEXT NOT NULL,
                    valid INTEGER NOT NULL DEFAULT 1,
                    invalidated_reason TEXT,
                    invalidated_record_id INTEGER REFERENCES records(id),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    invalidated_at TEXT
                );
                CREATE INDEX IF NOT EXISTS ix_closures_item ON closures(item_id, id);
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
            """)

    def _migrate_schema(self) -> None:
        """旧库升级：items状态约束补充review，records补充item_version版本链列。"""
        schema_row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='items'"
        ).fetchone()
        need_review = schema_row is not None and "'review'" not in (schema_row[0] or "")
        record_cols = [r[1] for r in self.conn.execute("PRAGMA table_info(records)")]
        need_version_col = bool(record_cols) and "item_version" not in record_cols
        if not need_review and not need_version_col:
            self.conn.execute("PRAGMA user_version=1")
            return
        # PRAGMA foreign_keys 不能在事务内切换
        self.conn.execute("PRAGMA foreign_keys=OFF")
        try:
            if need_version_col:
                self.conn.execute(
                    "ALTER TABLE records ADD COLUMN item_version INTEGER NOT NULL DEFAULT 1"
                )
            if need_review:
                statuses = ",".join("'" + s + "'" for s in STATES)
                self.conn.executescript(f"""
                    CREATE TABLE items_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        title TEXT NOT NULL,
                        description TEXT NOT NULL,
                        severity TEXT NOT NULL,
                        quantity REAL NOT NULL DEFAULT 0,
                        threshold REAL NOT NULL DEFAULT 1,
                        status TEXT NOT NULL CHECK(status IN ({statuses})),
                        version INTEGER NOT NULL DEFAULT 1,
                        external_ref TEXT,
                        created_by TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    INSERT INTO items_new SELECT id,title,description,severity,quantity,
                        threshold,status,version,external_ref,created_by,created_at,updated_at
                        FROM items;
                    DROP TABLE items;
                    ALTER TABLE items_new RENAME TO items;
                    CREATE UNIQUE INDEX ux_items_external_ref
                        ON items(external_ref) WHERE external_ref IS NOT NULL;
                """)
            self.conn.execute("PRAGMA user_version=1")
        finally:
            self.conn.execute("PRAGMA foreign_keys=ON")

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str, conclusion: str
                        ) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM items WHERE id=?", (item_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("项目不存在")
            if row["version"] != expected_version:
                raise ConflictError("版本冲突，请刷新后重试")
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                raise ConflictError("版本冲突，请刷新后重试")
            closure_row = None
            if target == "closed":
                self.conn.execute(
                    "UPDATE closures SET valid=0 WHERE item_id=? AND valid=1",
                    (item_id,),
                )
                cur = self.conn.execute(
                    """INSERT INTO closures(item_id, item_version, conclusion, valid,
                       created_by, created_at) VALUES(?,?,?,1,?,?)""",
                    (item_id, expected_version + 1, conclusion, actor, now),
                )
                closure_id = int(cur.lastrowid)
                closure_row = self.conn.execute(
                    "SELECT * FROM closures WHERE id=?", (closure_id,)
                ).fetchone()
        return self.get_item(item_id), (dict(closure_row) if closure_row else None)

    def submit_records(self, item_id: int,
                       entries: List[Tuple[str, str, str, str]],
                       expected_version: Optional[int], actor: str
                       ) -> Dict[str, Any]:
        """单事务提交一个批次。

        entries 为 (kind, detail, status, field_ref)，已在服务层完成校验。
        现场单号已存在的条目按同号重传处理，沿用首次结果；存在新条目时校验
        expected_version 并把事件版本推进一档。任一步失败整批回滚。
        返回提交后的记录（按入参顺序）、是否新写入、版本及关闭失效信息。
        """
        now = utc_now()
        with self._lock, self.conn:
            item = self.conn.execute(
                "SELECT * FROM items WHERE id=?", (item_id,)
            ).fetchone()
            if item is None:
                raise NotFoundError("项目不存在")
            current_version = item["version"]

            replayed: List[bool] = []
            fresh: List[Tuple[str, str, str, str]] = []
            for kind, detail, status, field_ref in entries:
                exists = self.conn.execute(
                    "SELECT 1 FROM records WHERE item_id=? AND external_ref=?",
                    (item_id, field_ref),
                ).fetchone()
                if exists is not None:
                    replayed.append(True)
                else:
                    replayed.append(False)
                    fresh.append((kind, detail, status, field_ref))

            reopened = False
            reopen_record_id: Optional[int] = None
            reopen_kind: Optional[str] = None
            invalidated_closure_id: Optional[int] = None
            reopen_reason_text: Optional[str] = None

            if fresh:
                if expected_version is None:
                    raise ValidationError("expected_version必填")
                if expected_version != current_version:
                    raise ConflictError(
                        f"版本冲突，当前版本为{current_version}，请刷新后重试"
                    )
                next_version = current_version + 1
                inserted: List[Tuple[int, str, str, str, str]] = []
                for kind, detail, status, field_ref in fresh:
                    cur = self.conn.execute(
                        """INSERT INTO records(item_id, kind, detail, status, external_ref,
                           item_version, created_by, created_at)
                           VALUES(?,?,?,?,?,?,?,?)""",
                        (item_id, kind, detail, status, field_ref,
                         next_version, actor, now),
                    )
                    inserted.append((int(cur.lastrowid), kind, detail, status, field_ref))

                if item["status"] == "closed":
                    for record_id, kind, _detail, _status, _ref in inserted:
                        if is_reopen_trigger(kind):
                            reopen_record_id = record_id
                            reopen_kind = kind
                            reopen_reason_text = reopen_reason(kind)
                            break
                if reopen_record_id is not None:
                    cur = self.conn.execute(
                        """UPDATE closures SET valid=0, invalidated_reason=?,
                           invalidated_record_id=?, invalidated_at=?
                           WHERE item_id=? AND valid=1""",
                        (reopen_reason_text, reopen_record_id, now, item_id),
                    )
                    invalidated_closure_row = self.conn.execute(
                        """SELECT id FROM closures WHERE item_id=? AND valid=0
                           AND invalidated_record_id=?""",
                        (item_id, reopen_record_id),
                    ).fetchone()
                    if invalidated_closure_row is not None:
                        invalidated_closure_id = int(invalidated_closure_row["id"])
                    self.conn.execute(
                        """UPDATE items SET status='review', version=?, updated_at=?
                           WHERE id=?""",
                        (next_version, now, item_id),
                    )
                    reopened = True
                else:
                    self.conn.execute(
                        "UPDATE items SET version=?, updated_at=? WHERE id=?",
                        (next_version, now, item_id),
                    )
                new_version = next_version
            else:
                # 整批都是同号重传：沿用首次结果，不推进版本
                new_version = current_version

            records: List[Dict[str, Any]] = []
            for (_kind, _detail, _status, field_ref), is_replay in zip(entries, replayed):
                row = self.conn.execute(
                    "SELECT * FROM records WHERE item_id=? AND external_ref=?",
                    (item_id, field_ref),
                ).fetchone()
                records.append(dict(row))

        return {
            "records": records,
            "replayed": replayed,
            "version": new_version,
            "reopened": reopened,
            "reopen_record_id": reopen_record_id,
            "reopen_kind": reopen_kind,
            "reopen_reason": reopen_reason_text,
            "invalidated_closure_id": invalidated_closure_id,
        }

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def latest_closure(self, item_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM closures WHERE item_id=? ORDER BY id DESC LIMIT 1",
                (item_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def has_reverification_after(self, item_id: int, record_id: Optional[int]) -> bool:
        """关闭结论失效后，是否已提交编号更晚的重新核验记录。"""
        with self._lock:
            row = self.conn.execute(
                """SELECT 1 FROM records WHERE item_id=? AND kind='reverification'
                   AND id > COALESCE(?, 0) LIMIT 1""",
                (item_id, record_id),
            ).fetchone()
        return row is not None

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()
