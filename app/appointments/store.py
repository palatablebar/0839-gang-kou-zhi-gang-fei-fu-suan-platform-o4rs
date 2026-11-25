"""SQLite 持久化：班次名额、预约状态与并发安全的签发占位。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.domain.appointments import OCCUPYING_STATUSES, TERMINAL_ISSUED_STATUSES


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AppointmentStore:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = Path(path)
        self.in_memory = str(path) == ":memory:"
        if not self.in_memory:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._memory_db = None
        else:
            self._memory_db = sqlite3.connect(":memory:", check_same_thread=False)
            self._memory_db.row_factory = sqlite3.Row
        self.init()

    def connect(self) -> sqlite3.Connection:
        if self.in_memory:
            assert self._memory_db is not None
            return self._memory_db
        db = sqlite3.connect(str(self.path), timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def init(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS gate_shifts(
                  id TEXT PRIMARY KEY,
                  capacity INTEGER NOT NULL DEFAULT 0,
                  starts_at TEXT,
                  payload TEXT NOT NULL DEFAULT '{}',
                  revision INTEGER NOT NULL DEFAULT 0,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pickup_appointments(
                  id TEXT PRIMARY KEY,
                  idempotency_key TEXT UNIQUE,
                  container_id TEXT NOT NULL,
                  shift_id TEXT,
                  status TEXT NOT NULL,
                  catches_vessel INTEGER NOT NULL DEFAULT 0,
                  fee_cents INTEGER NOT NULL DEFAULT 0,
                  new_demurrage_cents INTEGER NOT NULL DEFAULT 0,
                  reason TEXT NOT NULL DEFAULT '',
                  error TEXT NOT NULL DEFAULT '',
                  locked INTEGER NOT NULL DEFAULT 0,
                  payload TEXT NOT NULL DEFAULT '{}',
                  revision INTEGER NOT NULL DEFAULT 1,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  issued_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_appointments_shift_status
                  ON pickup_appointments(shift_id, status);
                CREATE INDEX IF NOT EXISTS idx_appointments_container
                  ON pickup_appointments(container_id);
                """
            )

    # ---------- serialization helpers ----------

    @staticmethod
    def _row_to_dict(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        data = dict(row)
        payload = {}
        if data.get("payload"):
            try:
                payload = json.loads(data["payload"])
            except (TypeError, json.JSONDecodeError):
                payload = {}
        data["payload"] = payload
        data["catches_vessel"] = bool(data.get("catches_vessel"))
        data["locked"] = bool(data.get("locked"))
        return data

    def get_appointment(self, appointment_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM pickup_appointments WHERE id=?", (appointment_id,)).fetchone()
        return self._row_to_dict(row)

    def find_by_idempotency_key(self, key: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM pickup_appointments WHERE idempotency_key=?", (key,)
            ).fetchone()
        return self._row_to_dict(row)

    def list_appointments(self, status: str | list[str] | tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM pickup_appointments"
        params: tuple[Any, ...] = ()
        if status:
            if isinstance(status, (list, tuple, set, frozenset)):
                marks = ",".join("?" for _ in status)
                sql += f" WHERE status IN ({marks})"
                params = tuple(str(s).upper() for s in status)
            else:
                sql += " WHERE status=?"
                params = (str(status).upper(),)
        sql += " ORDER BY created_at, id"
        with self.connect() as db:
            rows = db.execute(sql, params).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def list_shifts(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM gate_shifts ORDER BY starts_at, id").fetchall()
        result = []
        for row in rows:
            item = self._row_to_dict(row)
            payload = item.pop("payload", {}) or {}
            merged = dict(payload)
            merged.update({k: v for k, v in item.items() if k != "payload"})
            result.append(merged)
        return result

    def get_shift(self, shift_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM gate_shifts WHERE id=?", (shift_id,)).fetchone()
        if row is None:
            return None
        item = self._row_to_dict(row)
        payload = item.pop("payload", {}) or {}
        merged = dict(payload)
        merged.update({k: v for k, v in item.items() if k != "payload"})
        return merged

    def state(self) -> dict[str, Any]:
        return {"shifts": self.list_shifts(), "appointments": self.list_appointments()}

    # ---------- plan persistence ----------

    def upsert_shift(self, shift: dict[str, Any]) -> dict[str, Any]:
        sid = str(shift.get("shift_id") or shift.get("id") or "")
        if not sid:
            raise ValueError("SHIFT_ID_MISSING")
        capacity = int(shift.get("capacity", shift.get("slots", 0)) or 0)
        starts_at = shift.get("at") or shift.get("starts_at") or shift.get("date")
        now = utc_now()
        payload = json.dumps(shift, ensure_ascii=False, default=str)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """
                INSERT INTO gate_shifts(id, capacity, starts_at, payload, updated_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                  capacity=excluded.capacity,
                  starts_at=COALESCE(excluded.starts_at, gate_shifts.starts_at),
                  payload=excluded.payload,
                  revision=gate_shifts.revision+1,
                  updated_at=excluded.updated_at
                """,
                (sid, max(0, capacity), str(starts_at) if starts_at is not None else None,
                 payload, now),
            )
        return self.get_shift(sid)  # type: ignore[return-value]

    def set_capacity(self, shift_id: str, capacity: int) -> dict[str, Any] | None:
        capacity = max(0, int(capacity))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id FROM gate_shifts WHERE id=?", (shift_id,)).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE gate_shifts SET capacity=?, revision=revision+1, updated_at=? WHERE id=?",
                (capacity, utc_now(), shift_id),
            )
        return self.get_shift(shift_id)

    def save_plan(self, plan: dict[str, Any], *, batch_key: str | None = None) -> dict[str, Any]:
        """保存当前工作台方案。

        只替换 PROPOSED/WAITING 软记录；SENT/CONFIRMED/ISSUING 等硬占位由
        容量变化和确认流程保护，不会被重排删除。
        """
        now = utc_now()
        items = plan.get("items") or plan.get("plan") or []
        shifts = plan.get("shifts") or []
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for shift in shifts:
                sid = str(shift.get("shift_id") or shift.get("id") or "")
                if not sid:
                    continue
                db.execute(
                    """
                    INSERT INTO gate_shifts(id, capacity, starts_at, payload, updated_at)
                    VALUES(?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                      capacity=excluded.capacity,
                      starts_at=COALESCE(excluded.starts_at, gate_shifts.starts_at),
                      payload=excluded.payload,
                      revision=gate_shifts.revision+1,
                      updated_at=excluded.updated_at
                    """,
                    (sid, int(shift.get("capacity", 0) or 0), shift.get("at") or shift.get("date"),
                     json.dumps(shift, ensure_ascii=False, default=str), now),
                )

            # 软方案整体替换；硬占位保留。
            db.execute("DELETE FROM pickup_appointments WHERE status IN ('PROPOSED','WAITING')")
            for item in items:
                cid = str(item.get("container_id") or item.get("container") or "")
                if not cid or item.get("locked"):
                    continue
                status = "PROPOSED" if item.get("planned") else "WAITING"
                appointment_id = str(item.get("appointment_id") or f"APT-{cid}")
                db.execute(
                    """
                    INSERT INTO pickup_appointments(
                      id, idempotency_key, container_id, shift_id, status,
                      catches_vessel, fee_cents, new_demurrage_cents, reason,
                      locked, payload, revision, created_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                      container_id=excluded.container_id,
                      shift_id=excluded.shift_id,
                      status=excluded.status,
                      catches_vessel=excluded.catches_vessel,
                      fee_cents=excluded.fee_cents,
                      new_demurrage_cents=excluded.new_demurrage_cents,
                      reason=excluded.reason,
                      payload=excluded.payload,
                      updated_at=excluded.updated_at
                    """,
                    (
                        appointment_id,
                        item.get("idempotency_key"),
                        cid,
                        item.get("shift_id"),
                        status,
                        1 if item.get("catches_vessel") else 0,
                        int(item.get("fee_cents") or 0),
                        int(item.get("new_demurrage_cents") or 0),
                        str(item.get("reason") or ""),
                        1 if item.get("locked") else 0,
                        json.dumps({**item, "batch_key": batch_key}, ensure_ascii=False, default=str),
                        now,
                        now,
                    ),
                )
        return self.state()

    def create_appointment(self, data: dict[str, Any]) -> dict[str, Any]:
        appointment_id = str(data.get("id") or data.get("appointment_id") or uuid.uuid4().hex[:12])
        key = data.get("idempotency_key") or data.get("key")
        now = utc_now()
        status = str(data.get("status") or "PROPOSED").upper()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO pickup_appointments(
                  id, idempotency_key, container_id, shift_id, status,
                  catches_vessel, fee_cents, new_demurrage_cents, reason,
                  locked, payload, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    appointment_id, str(key) if key else None,
                    str(data.get("container_id") or data.get("container") or ""),
                    data.get("shift_id"), status,
                    1 if data.get("catches_vessel") else 0,
                    int(data.get("fee_cents") or 0),
                    int(data.get("new_demurrage_cents") or 0),
                    str(data.get("reason") or ""),
                    1 if status in OCCUPYING_STATUSES or data.get("locked") else 0,
                    json.dumps(data, ensure_ascii=False, default=str), now, now,
                ),
            )
        return self.get_appointment(appointment_id)  # type: ignore[return-value]

    # ---------- atomic confirmation / issuance ----------

    def occupying_count(self, shift_id: str, exclude_id: str | None = None) -> int:
        marks = ",".join("?" for _ in OCCUPYING_STATUSES)
        sql = f"SELECT COUNT(*) AS n FROM pickup_appointments WHERE shift_id=? AND status IN ({marks})"
        params: list[Any] = [shift_id, *sorted(OCCUPYING_STATUSES)]
        if exclude_id:
            sql += " AND id<>?"
            params.append(exclude_id)
        with self.connect() as db:
            return int(db.execute(sql, params).fetchone()["n"])

    def begin_issue(
        self,
        appointment_id: str,
        idempotency_key: str | None = None,
        *,
        shift_id: str | None = None,
        expected_revision: int | None = None,
    ) -> tuple[dict[str, Any] | None, str | None, bool]:
        """原子占位并进入 ISSUING。

        返回 (appointment, problem, replayed)。problem 为 missing/shift_missing/
        no_shift/full/in_progress/idempotency_conflict。
        """
        now = utc_now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if idempotency_key:
                existing = db.execute(
                    "SELECT * FROM pickup_appointments WHERE idempotency_key=?",
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    existing_dict = self._row_to_dict(existing)
                    if existing_dict and existing_dict["id"] != appointment_id:
                        return existing_dict, "idempotency_conflict", False
                    return existing_dict, None, True

            row = db.execute(
                "SELECT * FROM pickup_appointments WHERE id=?", (appointment_id,)
            ).fetchone()
            if row is None:
                return None, "missing", False
            current = self._row_to_dict(row)
            assert current is not None
            if expected_revision is not None and int(expected_revision) != int(current["revision"]):
                return current, "stale", False
            if current["status"] in TERMINAL_ISSUED_STATUSES:
                return current, None, True
            if current["status"] == "ISSUING" and not idempotency_key:
                return current, "in_progress", False
            if current.get("idempotency_key") and idempotency_key and current["idempotency_key"] != idempotency_key:
                return current, "idempotency_conflict", False

            target_shift = shift_id or current.get("shift_id")
            if not target_shift:
                return current, "no_shift", False
            shift = db.execute("SELECT * FROM gate_shifts WHERE id=?", (target_shift,)).fetchone()
            if shift is None:
                return current, "shift_missing", False
            capacity = int(shift["capacity"])

            count_row = db.execute(
                f"""
                SELECT COUNT(*) AS n FROM pickup_appointments
                WHERE shift_id=? AND status IN ({",".join("?" for _ in OCCUPYING_STATUSES)})
                  AND id<>?
                """,
                (target_shift, *sorted(OCCUPYING_STATUSES), appointment_id),
            ).fetchone()
            used_others = int(count_row["n"])
            if used_others >= capacity:
                return current, "full", False

            try:
                db.execute(
                    """
                    UPDATE pickup_appointments
                    SET status='ISSUING',
                        shift_id=?,
                        idempotency_key=COALESCE(?, idempotency_key),
                        error='',
                        locked=1,
                        revision=revision+1,
                        updated_at=?
                    WHERE id=?
                    """,
                    (target_shift, idempotency_key, now, appointment_id),
                )
            except sqlite3.IntegrityError:
                existing = db.execute(
                    "SELECT * FROM pickup_appointments WHERE idempotency_key=?",
                    (idempotency_key,),
                ).fetchone() if idempotency_key else None
                if existing is not None:
                    return self._row_to_dict(existing), None, True
                raise
        return self.get_appointment(appointment_id), None, False

    def complete_issue(self, appointment_id: str, status: str = "SENT") -> dict[str, Any] | None:
        terminal = status.upper()
        now = utc_now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """
                UPDATE pickup_appointments
                SET status=?, locked=1, error='', issued_at=COALESCE(issued_at, ?),
                    revision=revision+1, updated_at=?
                WHERE id=?
                """,
                (terminal, now, now, appointment_id),
            )
        return self.get_appointment(appointment_id)

    def fail_issue(self, appointment_id: str, error: str = "ISSUE_FAILED") -> dict[str, Any] | None:
        now = utc_now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """
                UPDATE pickup_appointments
                SET status='ISSUE_FAILED', error=?, locked=1,
                    revision=revision+1, updated_at=?
                WHERE id=?
                """,
                (error[:300], now, appointment_id),
            )
        return self.get_appointment(appointment_id)

    def confirm_atomically(
        self,
        appointment_id: str,
        idempotency_key: str | None = None,
        *,
        shift_id: str | None = None,
        expected_revision: int | None = None,
    ) -> tuple[dict[str, Any] | None, str | None, bool]:
        """直接确认；适用于没有外部签发副作用的调用。"""
        row, problem, replay = self.begin_issue(
            appointment_id, idempotency_key,
            shift_id=shift_id, expected_revision=expected_revision,
        )
        if problem:
            return row, problem, replay
        if replay and row and row["status"] in TERMINAL_ISSUED_STATUSES:
            return row, None, True
        if row is None:
            return None, "missing", False
        return self.complete_issue(appointment_id, "CONFIRMED"), None, replay

    def confirm(self, appointment_id: str, idempotency_key: str | None = None, **kwargs):
        return self.confirm_atomically(appointment_id, idempotency_key, **kwargs)


def issue_with_retry(
    store: AppointmentStore,
    appointment_id: str,
    issuer: Callable[[dict[str, Any]], Any] | None = None,
    *,
    idempotency_key: str | None = None,
    target_status: str = "SENT",
) -> dict[str, Any]:
    """占位后执行签发；签发失败保留同一占位，重试不会新增预约行。"""
    row, problem, replay = store.begin_issue(appointment_id, idempotency_key)
    if problem == "missing":
        raise KeyError("APPOINTMENT_NOT_FOUND")
    if problem == "full":
        raise OverflowError("SHIFT_FULL")
    if problem in {"idempotency_conflict", "shift_missing", "no_shift", "in_progress"}:
        raise RuntimeError(problem.upper())
    assert row is not None
    if replay and row["status"] in TERMINAL_ISSUED_STATUSES:
        row["replayed"] = True
        return row

    try:
        if issuer is not None:
            issuer(row)
    except Exception as exc:  # 签发失败必须保留占位，供同一 ID/幂等键重试。
        failed = store.fail_issue(appointment_id, f"{type(exc).__name__}: {exc}")
        assert failed is not None
        failed["issue_failed"] = True
        raise
    completed = store.complete_issue(appointment_id, target_status)
    assert completed is not None
    completed["replayed"] = bool(replay)
    return completed


# 常见仓储命名别名。
AppointmentRepository = AppointmentStore
BookingStore = AppointmentStore
SlotStore = AppointmentStore
