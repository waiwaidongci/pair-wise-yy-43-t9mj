from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import CLOSE_REQUIRED_KINDS, ID_PREFIX, STATES, STALE_CLOSE_KINDS


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
                    item_version INTEGER NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS close_conclusions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    version INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'valid'
                        CHECK(status IN ('valid','invalid')),
                    decided_by TEXT NOT NULL,
                    decided_at TEXT NOT NULL,
                    invalidated_at TEXT,
                    UNIQUE(item_id, version)
                );
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
            self._migrate()

    def _migrate(self) -> None:
        cols = [row[1] for row in self.conn.execute("PRAGMA table_info(records)").fetchall()]
        if "item_version" not in cols:
            self.conn.execute("ALTER TABLE records ADD COLUMN item_version INTEGER NOT NULL DEFAULT 0")

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
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            item = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if item is None:
                raise NotFoundError("项目不存在")
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                raise ConflictError("版本冲突，请刷新后重试")
            if target == "closed":
                new_version = int(expected_version) + 1
                self.conn.execute(
                    """INSERT INTO close_conclusions(item_id, version, status, decided_by, decided_at)
                       VALUES(?,?, 'valid', ?, ?)""",
                    (item_id, new_version, actor, now),
                )
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str,
                   expected_version: Optional[int] = None,
                   invalidate_close: bool = False) -> tuple:
        now = utc_now()
        with self._lock, self.conn:
            item = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if item is None:
                raise NotFoundError("项目不存在")
            if external_ref:
                existing = self.conn.execute(
                    "SELECT * FROM records WHERE item_id=? AND external_ref=?",
                    (item_id, external_ref),
                ).fetchone()
                if existing is not None:
                    return dict(existing), False
            if expected_version is not None and int(item["version"]) != int(expected_version):
                raise ConflictError("版本冲突，请刷新后重试")
            new_version = int(item["version"]) + 1
            cur = self.conn.execute(
                """INSERT INTO records(item_id, kind, detail, status, external_ref,
                   item_version, created_by, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (item_id, kind, detail, status, external_ref, new_version, actor, now),
            )
            record_id = int(cur.lastrowid)
            if invalidate_close:
                self.conn.execute(
                    "UPDATE items SET status='assessing', version=?, updated_at=? WHERE id=?",
                    (new_version, now, item_id),
                )
                self.conn.execute(
                    """UPDATE close_conclusions SET status='invalid', invalidated_at=?
                       WHERE item_id=? AND status='valid'""",
                    (now, item_id),
                )
            else:
                self.conn.execute(
                    "UPDATE items SET version=?, updated_at=? WHERE id=?",
                    (new_version, now, item_id),
                )
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            return dict(row), invalidate_close

    def add_records_batch(self, item_id: int, expected_version: int,
                          records: List[Dict[str, Any]], actor: str) -> tuple:
        now = utc_now()
        with self._lock, self.conn:
            item = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if item is None:
                raise NotFoundError("项目不存在")
            if int(item["version"]) != int(expected_version):
                raise ConflictError("版本冲突，批次未写入，请刷新后重试")
            current = int(item["version"])
            was_closed = item["status"] == "closed"
            results: List[Dict[str, Any]] = []
            invalidated = False
            for rec in records:
                if rec.get("external_ref"):
                    existing = self.conn.execute(
                        "SELECT * FROM records WHERE item_id=? AND external_ref=?",
                        (item_id, rec["external_ref"]),
                    ).fetchone()
                    if existing is not None:
                        results.append(dict(existing))
                        continue
                current += 1
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       item_version, created_by, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                    (item_id, rec["kind"], rec["detail"], rec["status"],
                     rec.get("external_ref"), current, actor, now),
                )
                row = self.conn.execute("SELECT * FROM records WHERE id=?", (int(cur.lastrowid),)).fetchone()
                results.append(dict(row))
                if was_closed and rec["kind"] in STALE_CLOSE_KINDS:
                    invalidated = True
            if invalidated:
                self.conn.execute(
                    "UPDATE items SET status='assessing', version=?, updated_at=? WHERE id=?",
                    (current, now, item_id),
                )
                self.conn.execute(
                    """UPDATE close_conclusions SET status='invalid', invalidated_at=?
                       WHERE item_id=? AND status='valid'""",
                    (now, item_id),
                )
            else:
                self.conn.execute(
                    "UPDATE items SET version=?, updated_at=? WHERE id=?",
                    (current, now, item_id),
                )
            return results, invalidated

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

    def missing_required_kinds(self, item_id: int) -> List[str]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT kind FROM records WHERE item_id=? AND status='closed'
                   GROUP BY kind""",
                (item_id,),
            ).fetchall()
        present = {row["kind"] for row in rows}
        return [kind for kind in CLOSE_REQUIRED_KINDS if kind not in present]

    def close_suspended(self, item_id: int) -> bool:
        with self._lock:
            row = self.conn.execute(
                """SELECT * FROM close_conclusions WHERE item_id=?
                   ORDER BY id DESC LIMIT 1""",
                (item_id,),
            ).fetchone()
        if row is None or row["status"] == "valid":
            return False
        with self._lock:
            rows = self.conn.execute(
                """SELECT kind FROM records WHERE item_id=? AND status='closed'
                   AND item_version > ? AND kind IN ('recovery','shoreline_monitoring')
                   GROUP BY kind""",
                (item_id, int(row["version"])),
            ).fetchall()
        present = {r["kind"] for r in rows}
        return not all(kind in present for kind in CLOSE_REQUIRED_KINDS)

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
