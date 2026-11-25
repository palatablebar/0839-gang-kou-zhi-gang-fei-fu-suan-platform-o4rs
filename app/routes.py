from flask import Blueprint, abort, current_app, jsonify, render_template, request, send_file

from app.appointments.service import SAMPLE_APPOINTMENT_PAYLOAD, change_capacity, confirm as confirm_appointment, create_plan, issue as issue_appointment, replan as replan_appointments
from app.catalog import SPEC
from app.config import SAMPLES
from app.errors import DomainIssue
from app.evidence.render import render
from app.review.service import approve, create_run, serialize
from app.service import pipeline


api = Blueprint("api", __name__)
STAGE_INDEX = {"intake": 1, "prepare": 2, "calculate": 3, "decision": 4, "reconcile": 5}


def store():
    from flask import current_app
    return current_app.extensions["runs"]


def appointment_store():
    return current_app.extensions["appointments"]


@api.get("/api/catalog")
def catalog():
    return jsonify(code=SPEC["code"], name=SPEC["name"], actor=SPEC["actor"], rule=SPEC["rule"],
                   rule_short=SPEC["rule_short"],
                   stages=[{"id": item, "name": name} for item, name in zip(
                       ["intake", "prepare", "calculate", "decision", "reconcile", "review", "evidence"],
                       SPEC["stages"])],
                   chart=SPEC["chart"], result_columns=SPEC["result_columns"],
                   columns=SPEC["columns"], samples=["dirty", "clean"])


@api.post("/api/stages/<stage_id>")
def stage(stage_id):
    if stage_id not in STAGE_INDEX:
        raise DomainIssue("STAGE_UNKNOWN", 404)
    data = request.get_json(silent=True) or {}
    profile = data.get("profile", "dirty")
    upto = SPEC["stages"][STAGE_INDEX[stage_id] - 1]
    return jsonify(pipeline(profile, upto=upto))


@api.post("/api/runs")
def create():
    data = request.get_json(silent=True) or {}
    profile = data.get("profile", "dirty")
    payload = pipeline(profile)
    return jsonify(create_run(store(), payload))


@api.get("/api/runs/<run_id>")
def get_run(run_id):
    run = serialize(store().get(run_id))
    if not run:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    return jsonify(run)


@api.post("/api/runs/<run_id>/review")
def review(run_id):
    data = request.get_json(silent=True) or {}
    if "decision" not in data or "expected_revision" not in data:
        raise DomainIssue("BAD_REQUEST")
    return jsonify(approve(store(), run_id, data["decision"], data.get("comment", ""), int(data["expected_revision"])))


@api.get("/api/runs/<run_id>/evidence")
def evidence(run_id):
    run = serialize(store().get(run_id))
    return jsonify(render(run))


# ---------------- 提箱预约工作台 ----------------

@api.get("/appointments")
def appointments_page():
    return render_template("appointments.html", p=SPEC)


@api.get("/api/appointments")
def appointment_state():
    return jsonify(appointment_store().state())


@api.get("/api/appointments/sample")
def appointment_sample():
    return jsonify(SAMPLE_APPOINTMENT_PAYLOAD)


@api.route("/api/appointments/plan", methods=["POST"])
@api.route("/api/appointments/schedule", methods=["POST"])
@api.route("/api/appointments/arrange", methods=["POST"])
@api.route("/api/appointment/plan", methods=["POST"])
@api.route("/api/appointment/schedule", methods=["POST"])
@api.route("/api/pickups/plan", methods=["POST"])
@api.route("/api/pickups/schedule", methods=["POST"])
@api.route("/api/bookings/plan", methods=["POST"])
@api.route("/api/schedule", methods=["POST"])
def appointment_plan():
    data = request.get_json(silent=True) or {}
    try:
        result = create_plan(data, appointment_store(), persist=bool(data.get("persist", True)))
    except (TypeError, ValueError) as exc:
        raise DomainIssue("BAD_REQUEST", 400, str(exc))
    return jsonify(result)


@api.post("/api/appointments/replan")
@api.post("/api/appointment/replan")
def appointment_replan():
    data = request.get_json(silent=True) or {}
    try:
        return jsonify(replan_appointments(appointment_store(), data))
    except KeyError as exc:
        raise DomainIssue(str(exc).strip("'"), 404)


@api.post("/api/appointments/capacity")
@api.post("/api/shifts/<shift_id>/capacity")
@api.patch("/api/shifts/<shift_id>")
@api.put("/api/shifts/<shift_id>/capacity")
def appointment_capacity(shift_id=None):
    data = request.get_json(silent=True) or {}
    sid = shift_id or data.get("shift_id") or data.get("id")
    store = appointment_store()
    raw_capacity = data.get("capacity")
    if raw_capacity is None:
        raw_capacity = data.get("total_capacity", data.get("slots", data.get("quota")))
    if raw_capacity is None:
        remaining_value = data.get("remaining_capacity", data.get("remaining", data.get("available")))
        if remaining_value is not None and sid:
            try:
                desired_remaining = max(0, int(remaining_value))
            except (TypeError, ValueError):
                raise DomainIssue("CAPACITY_INVALID", 400)
            raw_capacity = store.occupying_count(str(sid)) + desired_remaining
    if not sid or raw_capacity is None:
        raise DomainIssue("BAD_REQUEST")
    try:
        capacity = int(raw_capacity)
        if capacity < 0:
            raise ValueError
    except (TypeError, ValueError):
        raise DomainIssue("CAPACITY_INVALID", 400)
    try:
        return jsonify(change_capacity(store, str(sid), capacity, data))
    except KeyError:
        raise DomainIssue("SHIFT_NOT_FOUND", 404)


@api.post("/api/appointments/confirm")
def appointment_confirm_by_body():
    data = request.get_json(silent=True) or {}
    appointment_id = data.get("appointment_id") or data.get("id")
    if not appointment_id:
        raise DomainIssue("BAD_REQUEST")
    return _confirm_one(str(appointment_id), data)


@api.post("/api/appointments/<appointment_id>/confirm")
def appointment_confirm(appointment_id):
    return _confirm_one(appointment_id, request.get_json(silent=True) or {})


def _idempotency_key(data):
    return (
        request.headers.get("Idempotency-Key")
        or request.headers.get("X-Idempotency-Key")
        or data.get("idempotency_key")
        or data.get("key")
    )


def _confirm_one(appointment_id, data):
    expected_revision = data.get("expected_revision")
    try:
        expected_revision = None if expected_revision is None else int(expected_revision)
        result = confirm_appointment(
            appointment_store(), appointment_id, _idempotency_key(data), expected_revision
        )
    except (TypeError, ValueError):
        raise DomainIssue("BAD_REQUEST", 400, "expected_revision must be an integer")
    except PermissionError:
        raise DomainIssue("STALE_REVIEW", 409)
    except KeyError:
        raise DomainIssue("APPOINTMENT_NOT_FOUND", 404)
    except OverflowError:
        raise DomainIssue("SHIFT_FULL", 409)
    except RuntimeError as exc:
        code = str(exc)
        if code == "IDEMPOTENCY_CONFLICT":
            raise DomainIssue("IDEMPOTENCY_CONFLICT", 409)
        if code == "IN_PROGRESS":
            raise DomainIssue("ISSUE_IN_PROGRESS", 409)
        raise DomainIssue("BAD_REQUEST", 409, code)
    return jsonify(result)


@api.post("/api/appointments/issue")
@api.post("/api/appointments/retry")
def appointment_issue_by_body():
    data = request.get_json(silent=True) or {}
    appointment_id = data.get("appointment_id") or data.get("id")
    if not appointment_id:
        raise DomainIssue("BAD_REQUEST")
    return appointment_issue(str(appointment_id))


@api.post("/api/appointments/<appointment_id>/issue")
@api.post("/api/appointments/<appointment_id>/send")
@api.post("/api/appointments/<appointment_id>/retry")
def appointment_issue(appointment_id):
    data = request.get_json(silent=True) or {}
    key = _idempotency_key(data)

    def maybe_fail(row):
        # 仅用于本地/接口演练：第一次签发失败，原 ID/幂等键重试成功。
        if request.headers.get("X-Simulate-Issue-Failure") == "1" or data.get("simulate_failure"):
            if not appointment_issue.__dict__.get(f"failed_{appointment_id}", False):
                appointment_issue.__dict__[f"failed_{appointment_id}"] = True
                raise RuntimeError("GATEWAY_TIMEOUT")

    try:
        result = issue_appointment(appointment_store(), appointment_id, key, maybe_fail)
    except KeyError:
        raise DomainIssue("APPOINTMENT_NOT_FOUND", 404)
    except OverflowError:
        raise DomainIssue("SHIFT_FULL", 409)
    except RuntimeError as exc:
        code = str(exc)
        if code == "IDEMPOTENCY_CONFLICT":
            raise DomainIssue("IDEMPOTENCY_CONFLICT", 409)
        if code == "IN_PROGRESS":
            raise DomainIssue("ISSUE_IN_PROGRESS", 409)
        # issue_with_retry 已把单据置为 ISSUE_FAILED 并保留占位。
        row = appointment_store().get_appointment(appointment_id)
        response = jsonify(row or {"code": "ISSUANCE_FAILED"})
        response.status_code = 502
        return response
    except Exception:
        row = appointment_store().get_appointment(appointment_id)
        response = jsonify(row or {"code": "ISSUANCE_FAILED"})
        response.status_code = 502
        return response
    return jsonify(result)


@api.get("/api/appointments/<appointment_id>")
def appointment_detail(appointment_id):
    row = appointment_store().get_appointment(appointment_id)
    if row is None:
        raise DomainIssue("APPOINTMENT_NOT_FOUND", 404)
    return jsonify(row)


@api.get("/api/examples/<profile>")
def example(profile):
    if profile not in {"clean", "dirty"}:
        abort(404)
    return send_file(SAMPLES / f"{profile}.csv", as_attachment=True)
