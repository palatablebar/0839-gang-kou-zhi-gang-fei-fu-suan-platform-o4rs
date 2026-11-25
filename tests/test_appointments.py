import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.appointments.service import change_capacity, create_plan, issue, replan
from app.appointments.store import AppointmentStore
from app.domain.appointments import plan_appointments


def test_last_slot_prefers_feeder_over_cost_saving():
    result = plan_appointments(
        {
            "shifts": [{"shift_id": "ONLY", "capacity": 1}],
            "next_batch_cost_cents": 100,
            "containers": [
                {"container_id": "FEEDER", "cutoff_shift_id": "ONLY",
                 "next_batch_cost_cents": 9_000_000},
                {"container_id": "COST_SAVER", "next_batch_cost_cents": 1},
            ],
        }
    )

    assert result.assignments["FEEDER"] == "ONLY"
    assert result.assignments["COST_SAVER"] is None
    assert result.totals["vessel_count"] == 1


def test_same_vessel_and_count_uses_lowest_total_new_fee():
    result = plan_appointments(
        {
            "shifts": [
                {"shift_id": "EARLY", "capacity": 1},
                {"shift_id": "LATE", "capacity": 1},
            ],
            "containers": [
                {"container_id": "HIGH_DELAY",
                 "costs_by_shift": {"EARLY": 0, "LATE": 1000}},
                {"container_id": "LOW_DELAY",
                 "costs_by_shift": {"EARLY": 0, "LATE": 10}},
            ],
        }
    )

    assert result.assignments["HIGH_DELAY"] == "EARLY"
    assert result.assignments["LOW_DELAY"] == "LATE"
    assert result.totals["scheduled_count"] == 2
    assert result.totals["new_demurrage_cents"] == 10


def test_tier_rates_drive_lowest_added_demurrage_choice():
    result = plan_appointments({
        "shifts": [
            {"shift_id": "EARLY", "date": "2026-11-26", "capacity": 1},
            {"shift_id": "LATE", "date": "2026-11-27", "capacity": 1},
        ],
        "containers": [
            # 已在第三档，延后一天增加 22000 分。
            {"container_id": "THIRD_TIER", "gate_in": "2026-10-01", "free_days": 0},
            # 仍在第一档边界，延后一天增加 8000 分。
            {"container_id": "FIRST_TIER", "gate_in": "2026-11-24", "free_days": 0},
        ],
    })

    assert result.assignments["THIRD_TIER"] == "EARLY"
    assert result.assignments["FIRST_TIER"] == "LATE"
    assert result.totals["new_demurrage_cents"] == 8000


def test_pickable_containers_are_scheduled_up_to_capacity():
    result = plan_appointments(
        {
            "shifts": [{"shift_id": "S1", "capacity": 2}, {"shift_id": "S2", "capacity": 1}],
            "containers": [{"container_id": f"C{i}"} for i in range(5)],
        }
    )

    assert result.totals["scheduled_count"] == 3
    assert result.totals["waiting_count"] == 2
    assert all(item["status"] in {"PROPOSED", "WAITING"} for item in result["items"])


def test_unpickable_container_is_not_forced_into_slot():
    result = plan_appointments(
        {
            "shifts": [{"shift_id": "S1", "capacity": 5}],
            "containers": [
                {"container_id": "READY"},
                {"container_id": "HELD", "pickup_allowed": False},
            ],
        }
    )

    assert result.assignments["READY"] == "S1"
    assert result.assignments["HELD"] is None
    assert result.by_container["HELD"]["reason"] == "PICKUP_NOT_ALLOWED"


def test_sent_appointments_are_locked_when_capacity_changes(tmp_path):
    store = AppointmentStore(tmp_path / "appt.sqlite3")
    payload = {
        "shifts": [
            {"shift_id": "S1", "capacity": 1},
            {"shift_id": "S2", "capacity": 1},
        ],
        "containers": [{"container_id": "A"}, {"container_id": "B"}],
    }
    plan = create_plan(payload, store)
    assert plan["assignments"]["A"] == "S1"
    assert plan["assignments"]["B"] == "S2"

    store.begin_issue("APT-A", "issue-a")
    store.complete_issue("APT-A", "SENT")

    changed = change_capacity(store, "S2", 0, {})
    by_id = changed["by_container"]
    assert by_id["A"]["shift_id"] == "S1"
    assert by_id["A"]["status"] == "SENT"
    assert by_id["B"]["shift_id"] is None

    restored = change_capacity(store, "S2", 1, {})
    assert restored["assignments"]["B"] == "S2"
    assert restored["by_container"]["A"]["status"] == "SENT"


def test_concurrent_confirmation_never_exceeds_capacity(tmp_path):
    store = AppointmentStore(tmp_path / "race.sqlite3")
    store.upsert_shift({"shift_id": "S1", "capacity": 1})
    store.create_appointment({"id": "A", "container_id": "A", "shift_id": "S1", "status": "PROPOSED"})
    store.create_appointment({"id": "B", "container_id": "B", "shift_id": "S1", "status": "PROPOSED"})
    barrier = threading.Barrier(2)

    def confirm(appointment_id):
        barrier.wait()
        return store.confirm_atomically(appointment_id, f"key-{appointment_id}")

    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(confirm, ["A", "B"]))

    problems = [problem for _row, problem, _replay in rows if problem]
    assert problems == ["full"] or problems == [None]
    assert store.occupying_count("S1") == 1
    statuses = {cid: store.get_appointment(cid)["status"] for cid in ("A", "B")}
    assert sorted(statuses.values()) == ["CONFIRMED", "PROPOSED"]


def test_failed_issue_retry_uses_same_occupancy(tmp_path):
    store = AppointmentStore(tmp_path / "retry.sqlite3")
    store.upsert_shift({"shift_id": "S1", "capacity": 1})
    store.create_appointment({"id": "A", "container_id": "A", "shift_id": "S1", "status": "PROPOSED"})
    calls = 0

    def flaky(_row):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("issuer down")

    with pytest.raises(RuntimeError):
        issue(store, "A", "retry-key", flaky)

    failed = store.get_appointment("A")
    assert failed["status"] == "ISSUE_FAILED"
    assert store.occupying_count("S1") == 1

    sent = issue(store, "A", "retry-key", flaky)
    assert sent["status"] == "SENT"
    assert calls == 2
    assert store.occupying_count("S1") == 1
    assert len([a for a in store.list_appointments() if a["container_id"] == "A"]) == 1


def test_appointment_plan_and_confirm_http(client):
    response = client.post("/api/appointments/plan", json={
        "persist": True,
        "shifts": [{"shift_id": "S1", "capacity": 1}],
        "containers": [{"container_id": "C1"}, {"container_id": "C2"}],
    })
    assert response.status_code == 200
    plan = response.get_json()
    chosen = next(item for item in plan["items"] if item["shift_id"] == "S1")

    confirmed = client.post(
        f"/api/appointments/{chosen['appointment_id']}/confirm",
        json={"idempotency_key": "http-key"},
    )
    assert confirmed.status_code == 200
    assert confirmed.get_json()["status"] == "CONFIRMED"

    replay = client.post(
        f"/api/appointments/{chosen['appointment_id']}/confirm",
        json={"idempotency_key": "http-key"},
    )
    assert replay.status_code == 200
    assert replay.get_json()["status"] == "CONFIRMED"


def test_stale_appointment_revision_is_rejected(client):
    plan = client.post("/api/appointments/plan", json={
        "shifts": [{"shift_id": "S1", "capacity": 1}],
        "containers": [{"container_id": "C1"}],
    }).get_json()
    appointment_id = plan["items"][0]["appointment_id"]
    current = client.get(f"/api/appointments/{appointment_id}").get_json()

    ok = client.post(f"/api/appointments/{appointment_id}/confirm",
                     json={"expected_revision": current["revision"]})
    assert ok.status_code == 200
    stale = client.post(f"/api/appointments/{appointment_id}/confirm",
                        json={"expected_revision": current["revision"]})
    assert stale.status_code == 409
    assert stale.get_json()["code"] == "STALE_REVIEW"


def test_appointments_page_is_linked_from_workbench(client):
    response = client.get("/appointments")
    assert response.status_code == 200
    assert "提箱预约排程".encode() in response.data
