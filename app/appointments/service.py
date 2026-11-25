"""预约排程应用服务：连接纯领域排程与 SQLite 状态。"""

from __future__ import annotations

from typing import Any

from app.appointments.store import AppointmentStore, issue_with_retry
from app.domain.appointments import (
    OCCUPYING_STATUSES,
    plan_appointments,
)


SAMPLE_APPOINTMENT_PAYLOAD: dict[str, Any] = {
    "next_batch_at": "2026-11-30",
    "cost_basis_date": "2026-11-25",
    "shifts": [
        {"shift_id": "S1", "at": "2026-11-26T09:00:00", "capacity": 1, "name": "周四早班"},
        {"shift_id": "S2", "at": "2026-11-26T14:00:00", "capacity": 1, "name": "周四午班"},
        {"shift_id": "S3", "at": "2026-11-27T09:00:00", "capacity": 2, "name": "周五早班"},
    ],
    "containers": [
        {
            "container_id": "C1", "gate_in": "2026-11-02", "free_days": 3,
            "vessel_cutoff_shift_id": "S1", "feeder": True,
        },
        {
            "container_id": "C2", "gate_in": "2026-11-06", "free_days": 1,
        },
        {
            "container_id": "C3", "gate_in": "2026-09-29", "free_days": 3,
            "vessel_cutoff_shift_id": "S2",
        },
        {
            "container_id": "C4", "gate_in": "2026-11-02", "free_days": 30,
        },
        {
            "container_id": "C5", "gate_in": "2026-11-20", "free_days": 5,
            "pickup_allowed": False, "reason": "CUSTOMS_HOLD",
        },
    ],
}


def _attach_snapshots(plan: dict[str, Any], containers: list[dict[str, Any]]) -> dict[str, Any]:
    by_id = {str(c.get("container_id") or c.get("id")): c for c in containers if isinstance(c, dict)}
    for item in plan.get("items", []):
        cid = item["container_id"]
        item.setdefault("appointment_id", f"APT-{cid}")
        snapshot = by_id.get(cid)
        if snapshot is not None:
            item["container"] = snapshot
    for item in plan.get("appointments", []):
        if isinstance(item, dict):
            item.setdefault("appointment_id", f"APT-{item.get('container_id')}")
            item["id"] = item.get("appointment_id")
    return plan


def create_plan(payload: dict[str, Any] | None = None, store: AppointmentStore | None = None,
                *, persist: bool = True) -> dict[str, Any]:
    data = dict(payload or {})
    if not data.get("containers") and not data.get("boxes"):
        data = SAMPLE_APPOINTMENT_PAYLOAD
    plan = plan_appointments(data)
    containers = data.get("containers") or data.get("boxes") or []
    _attach_snapshots(plan, containers)
    if store is not None and persist and data.get("persist", True):
        store.save_plan(plan, batch_key=data.get("batch_key"))
        plan["persisted"] = True
    return plan


# 语义别名。
plan_pickups = create_plan
schedule = create_plan


def replan(store: AppointmentStore, changes: dict[str, Any] | None = None) -> dict[str, Any]:
    """名额变化后：硬占位照旧，软/未确认记录按剩余名额重排。"""
    changes = dict(changes or {})
    state = store.state()

    raw_shifts = changes.get("shifts") or state["shifts"]
    # 数据库 payload 中可能保留上一次计算出的 remaining；重排只信任总名额和硬状态。
    shifts = []
    for raw_shift in raw_shifts:
        if not isinstance(raw_shift, dict):
            continue
        shift = dict(raw_shift)
        shift.setdefault("shift_id", shift.get("id"))
        shift.setdefault("id", shift.get("shift_id"))
        for stale_key in ("remaining", "remaining_capacity", "available", "slots_left"):
            shift.pop(stale_key, None)
        shifts.append(shift)
    hard = [a for a in state["appointments"] if a["status"] in OCCUPYING_STATUSES]

    containers = changes.get("containers") or changes.get("boxes")
    if not containers:
        containers = []
        seen: set[str] = set()
        for appt in state["appointments"]:
            snapshot = (appt.get("payload") or {}).get("container")
            cid = appt["container_id"]
            if isinstance(snapshot, dict) and cid not in seen:
                containers.append(snapshot)
                seen.add(cid)

    payload = {
        "containers": containers,
        "shifts": shifts,
        "appointments": changes.get("appointments") or hard,
        "next_batch_at": changes.get("next_batch_at"),
        "cost_basis_date": changes.get("cost_basis_date"),
    }
    plan = plan_appointments(payload)
    _attach_snapshots(plan, containers)
    store.save_plan(plan)
    plan["persisted"] = True
    return plan


def change_capacity(store: AppointmentStore, shift_id: str, capacity: int,
                    replan_body: dict[str, Any] | None = None) -> dict[str, Any]:
    shift = store.set_capacity(shift_id, capacity)
    if shift is None:
        # 允许在调整名额时顺带登记一个新班次。
        body = replan_body or {}
        shift_data = next((s for s in body.get("shifts", [])
                           if str(s.get("shift_id") or s.get("id")) == shift_id), None)
        if shift_data is None:
            raise KeyError("SHIFT_NOT_FOUND")
        shift_data = dict(shift_data)
        shift_data["shift_id"] = shift_id
        shift_data["capacity"] = capacity
        store.upsert_shift(shift_data)
    result = replan(store, replan_body)
    result["capacity_changed"] = {"shift_id": shift_id, "capacity": max(0, int(capacity))}
    return result


def confirm(store: AppointmentStore, appointment_id: str,
            idempotency_key: str | None = None,
            expected_revision: int | None = None) -> dict[str, Any]:
    row, problem, replayed = store.confirm_atomically(
        appointment_id, idempotency_key, expected_revision=expected_revision
    )
    if problem == "missing":
        raise KeyError("APPOINTMENT_NOT_FOUND")
    if problem == "full":
        raise OverflowError("SHIFT_FULL")
    if problem == "stale":
        raise PermissionError("STALE_REVIEW")
    if problem:
        raise RuntimeError(problem.upper())
    assert row is not None
    row["replayed"] = bool(replayed)
    return row


def issue(store: AppointmentStore, appointment_id: str,
          idempotency_key: str | None = None, issuer=None) -> dict[str, Any]:
    return issue_with_retry(store, appointment_id, issuer, idempotency_key=idempotency_key)


class AppointmentService:
    """便于面向对象调用的薄封装。"""

    def __init__(self, store: AppointmentStore | None = None, *, db_path: str | None = None):
        self.store = store or AppointmentStore(db_path or ":memory:")

    def plan(self, payload: dict[str, Any] | None = None, *, persist: bool = True) -> dict[str, Any]:
        return create_plan(payload, self.store, persist=persist)

    def replan(self, changes: dict[str, Any] | None = None) -> dict[str, Any]:
        return replan(self.store, changes)

    def change_capacity(self, shift_id: str, capacity: int, body: dict[str, Any] | None = None):
        return change_capacity(self.store, shift_id, capacity, body)

    def confirm(self, appointment_id: str, idempotency_key: str | None = None,
                expected_revision: int | None = None):
        return confirm(self.store, appointment_id, idempotency_key, expected_revision)

    def issue(self, appointment_id: str, idempotency_key: str | None = None, issuer=None):
        return issue(self.store, appointment_id, idempotency_key, issuer)


BookingService = AppointmentService
GateAppointmentService = AppointmentService

# 函数命名别名。
confirm_appointment = confirm
issue_appointment = issue
plan_appointment_batch = create_plan
