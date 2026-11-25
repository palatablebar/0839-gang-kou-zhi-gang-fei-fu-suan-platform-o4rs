"""提箱预约排程领域逻辑。

目标顺序与业务口径保持一致：

1. 先最大化保住接驳船期的箱数；
2. 再最大化本班次实际安排的提箱数；
3. 在前两者相同的方案中，最小化整批新增滞港费。

已发给车队/已确认的预约由持久化层作为锁定记录传入；本模块只负责在
“锁定名额之后的剩余名额”上重排未确认预约。
"""

from __future__ import annotations

from collections import deque
from datetime import date, datetime, timedelta
from typing import Any, Iterable

from app.domain.helpers import business_days_after, tier_fee
from app.intake.normalize import canonical


LOCKED_STATUSES = frozenset({
    "SENT", "ISSUED", "DISPATCHED", "CONFIRMED", "SENT_TO_FLEET",
    "FLEET_SENT", "DISPATCHED_TO_FLEET", "CONFIRMED_BY_FLEET", "BOOKED",
    "已发车", "已发给车队", "已确认",
})
OCCUPYING_STATUSES = frozenset({
    "PROPOSED_LOCK", "RESERVED", "ISSUING", "ISSUE_FAILED",
    *LOCKED_STATUSES,
})
TERMINAL_ISSUED_STATUSES = frozenset({
    "SENT", "ISSUED", "DISPATCHED", "CONFIRMED", "SENT_TO_FLEET",
    "FLEET_SENT", "DISPATCHED_TO_FLEET", "CONFIRMED_BY_FLEET", "BOOKED",
})

DEFAULT_HOLIDAYS = (
    {date(2026, 10, day) for day in range(1, 8)}
    | {date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)}
)

_FALSE_VALUES = {"0", "false", "no", "n", "否", "不可", "hold", "blocked", "cancelled", "canceled"}
_TRUE_VALUES = {"1", "true", "yes", "y", "是", "可", "released", "ok"}


def first_key(data: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return default


def as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = canonical(value).lower()
    if text in _TRUE_VALUES:
        return True
    if text in _FALSE_VALUES:
        return False
    return default


def parse_date(value: Any) -> date | None:
    parsed = parse_datetime(value)
    return parsed.date() if parsed is not None else None


def parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    text = canonical(value)
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is not None:
            # 闸口班次按本地日历日计费；去掉时区偏移后保留其本地表示。
            parsed = parsed.replace(tzinfo=None)
        return parsed
    except ValueError:
        for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d", "%Y.%m.%d"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
    return None


def shift_moment(shift: dict[str, Any]) -> datetime | None:
    value = first_key(shift, "at", "start", "starts_at", "datetime", "time",
                      "appointment_at", "slot_at", "date", "shift_date", "day")
    return parse_datetime(value)


def shift_day(shift: dict[str, Any]) -> date | None:
    moment = shift_moment(shift)
    return moment.date() if moment else parse_date(first_key(
        shift, "date", "shift_date", "day", "at", "start"
    ))


def container_id(container: dict[str, Any]) -> str:
    return str(first_key(container, "container_id", "id", "box_id", "box", "cntr",
                         "reference", "name", default=""))


def shift_id(shift: dict[str, Any]) -> str:
    return str(first_key(shift, "shift_id", "id", "slot_id", "code", default=""))


def demurrage_breakdown(container: dict[str, Any], pickup_on: date,
                        holidays: set[date] | None = None) -> dict[str, Any] | None:
    gate_in = parse_date(first_key(container, "gate_in", "in_date", "gate_in_date", "arrival_date"))
    if gate_in is None:
        return None
    free_days_value = first_key(container, "free_days", "free_time_days", "free_day", default=0)
    try:
        free_days = max(0, int(free_days_value))
    except (TypeError, ValueError):
        free_days = 0
    deadline = business_days_after(gate_in, free_days, holidays or DEFAULT_HOLIDAYS)
    charge_days = max(0, (pickup_on - deadline).days)
    return {
        "free_end": deadline.isoformat(),
        "charge_days": charge_days,
        "fee_cents": tier_fee(charge_days),
    }


def pickup_fee_cents(container: dict[str, Any], pickup_on: date,
                     holidays: set[date] | None = None) -> int | None:
    """按工作台现有免堆日历和三档费率计算到提箱日的费用。"""
    breakdown = demurrage_breakdown(container, pickup_on, holidays)
    return None if breakdown is None else breakdown["fee_cents"]


def additional_demurrage_cents(container: dict[str, Any],
                               pickup_on: Any = None,
                               baseline_on: Any = None,
                               holidays: set[date] | None = None,
                               **kwargs: Any) -> int | None:
    """计算相对基准日的新增滞港费；基准日缺省时新增费为到提箱日的全额费用。"""
    pickup_value = pickup_on or kwargs.get("appointment_on") or kwargs.get("shift") or kwargs.get("pickup_date")
    if isinstance(pickup_value, dict):
        pickup_day = shift_day(pickup_value)
    else:
        pickup_day = parse_date(pickup_value)
    if pickup_day is None:
        return None
    pickup_fee = pickup_fee_cents(container, pickup_day, holidays)
    if pickup_fee is None:
        return None
    baseline_day = parse_date(baseline_on or kwargs.get("basis_date") or kwargs.get("cost_basis_date"))
    if baseline_day is None:
        baseline_day = _cost_basis_date(container, {})
    baseline_fee = pickup_fee_cents(container, baseline_day, holidays) if baseline_day else 0
    return pickup_fee - (baseline_fee or 0)


calculate_additional_demurrage = additional_demurrage_cents


def _explicit_option_cost(container: dict[str, Any], sid: str,
                          shift: dict[str, Any]) -> int | None:
    for key in ("new_demurrage_cents", "additional_demurrage_cents", "incremental_demurrage_cents",
                "incremental_fee_cents", "new_fee_cents", "added_fee_cents",
                "additional_fee_cents", "delay_cost_cents", "cost_cents", "cost",
                "demurrage_cents", "fee_cents"):
        value = first_key(shift, key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                pass

    for mapping_key in ("cost_by_shift", "costs_by_shift", "costs", "fee_by_shift",
                        "fees_by_shift", "demurrage_by_shift", "new_demurrage_by_shift",
                        "new_fee_by_shift", "quotes_by_shift"):
        mapping = container.get(mapping_key)
        if isinstance(mapping, dict) and sid in mapping:
            try:
                return int(mapping[sid])
            except (TypeError, ValueError):
                return None

    quotes = container.get("quotes") or container.get("shift_quotes") or container.get("options")
    if isinstance(quotes, list):
        for quote in quotes:
            if isinstance(quote, dict) and str(first_key(quote, "shift_id", "shift", "id", "slot_id", default="")) == sid:
                value = first_key(quote, "new_demurrage_cents", "additional_demurrage_cents",
                                  "cost_cents", "cost", "fee_cents")
                if value is not None:
                    try:
                        return int(value)
                    except (TypeError, ValueError):
                        return None
    saving = first_key(container, "saving_cents_by_shift", "savings_by_shift")
    if isinstance(saving, dict) and sid in saving:
        try:
            return -int(saving[sid])
        except (TypeError, ValueError):
            return None
    return None


def _cost_basis_date(container: dict[str, Any], plan: dict[str, Any]) -> date | None:
    value = first_key(
        container, "cost_basis_date", "baseline_date", "accounting_date",
        "current_gate_out", "gate_out", "today",
    )
    parsed = parse_date(value)
    if parsed:
        return parsed
    return parse_date(first_key(plan, "cost_basis_date", "as_of", "accounting_date", "today"))


def option_cost_cents(container: dict[str, Any], shift: dict[str, Any],
                      plan: dict[str, Any], holidays: set[date] | None) -> tuple[int, dict[str, Any]]:
    """返回候选班次的新增滞港费及计算明细。"""
    sid = shift_id(shift)
    explicit = _explicit_option_cost(container, sid, shift)
    pickup_on = shift_day(shift)
    breakdown = demurrage_breakdown(container, pickup_on, holidays) if pickup_on else None
    total_fee = None if breakdown is None else breakdown["fee_cents"]
    basis = _cost_basis_date(container, plan)
    basis_fee = pickup_fee_cents(container, basis, holidays) if basis else None

    details: dict[str, Any] = {
        "fee_cents": total_fee,
        "free_end": None if breakdown is None else breakdown["free_end"],
        "charge_days": None if breakdown is None else breakdown["charge_days"],
        "basis_date": basis.isoformat() if basis else None,
        "basis_fee_cents": basis_fee,
        "calculated": explicit is None and total_fee is not None,
    }
    if explicit is not None:
        details["new_demurrage_cents"] = explicit
        return explicit, details

    if total_fee is None:
        details["new_demurrage_cents"] = 0
        details["calculated"] = False
        return 0, details

    if basis_fee is not None:
        cost = total_fee - basis_fee
    else:
        # 没有统一基准日时，先给总费用；优化前会按每个箱的最低候选费用减去常数。
        cost = total_fee
    details["new_demurrage_cents"] = cost
    return cost, details


def _vessel_cutoff(container: dict[str, Any]) -> datetime | date | str | None:
    return first_key(
        container, "vessel_cutoff", "feeder_cutoff", "connection_cutoff",
        "cutoff", "vessel_deadline", "feeder_deadline", "documents_deadline",
    )


def catches_vessel(container: dict[str, Any], shift: dict[str, Any],
                   shift_index: dict[str, int]) -> tuple[bool, bool]:
    """返回 (是否有接驳船约束, 该候选是否赶上船)。"""
    sid = shift_id(shift)
    explicit_map = first_key(container, "catches_vessel_by_shift", "vessel_options")
    if isinstance(explicit_map, dict) and sid in explicit_map:
        return True, as_bool(explicit_map[sid])

    required_shift = first_key(
        container, "required_shift_id", "only_shift_id", "must_shift_id", "feeder_shift_id"
    )
    cutoff_shift = first_key(
        container, "cutoff_shift_id", "vessel_cutoff_shift_id",
        "last_vessel_shift_id", "connection_cutoff_shift_id",
    )
    if required_shift is not None:
        return True, sid == str(required_shift)
    if cutoff_shift is not None:
        cutoff_sid = str(cutoff_shift)
        if sid == cutoff_sid:
            return True, True
        if sid in shift_index and cutoff_sid in shift_index:
            return True, shift_index[sid] <= shift_index[cutoff_sid]
        # 找不到顺序时，截止班次本身一定可接；其他班次不臆测。
        return True, False

    cutoff = _vessel_cutoff(container)
    if cutoff is not None:
        if isinstance(cutoff, str) and cutoff in shift_index:
            return True, shift_index.get(sid, -1) <= shift_index[cutoff]
        cutoff_day = parse_date(cutoff)
        cutoff_moment = parse_datetime(cutoff)
        moment = shift_moment(shift)
        day = shift_day(shift)
        if cutoff_moment is not None and moment is not None:
            return True, moment <= cutoff_moment
        if cutoff_day is not None and day is not None:
            return True, day <= cutoff_day
        return True, False

    vessel_id = first_key(container, "vessel_id", "feeder_id", "connection_id", "vessel")
    shift_vessel = first_key(shift, "vessel_id", "feeder_id", "connection_id", "vessel")
    if vessel_id is not None and shift_vessel is not None:
        return True, str(vessel_id) == str(shift_vessel)

    if "catches_vessel" in container:
        return True, as_bool(container.get("catches_vessel"))

    priority = canonical(first_key(container, "priority", default="")).upper()
    feeder_flag = as_bool(first_key(
        container, "must_catch_vessel", "must_catch", "vessel_priority", "feeder_priority",
        "vessel", "feeder", "urgent", "is_urgent", default=False
    )) or priority in {"FEEDER", "VESSEL", "URGENT", "接驳", "接驳船", "紧急"}
    if feeder_flag:
        # 只给了紧急标记而没给窗口时，视为本班列出的候选均在船期窗口内。
        return True, True
    return False, False


def is_pickable(container: dict[str, Any]) -> tuple[bool, str]:
    if "pickup_allowed" in container and not as_bool(container.get("pickup_allowed"), True):
        return False, "PICKUP_NOT_ALLOWED"
    if "releasable" in container and not as_bool(container.get("releasable"), True):
        return False, "NOT_RELEASED"
    if "can_pickup" in container and not as_bool(container.get("can_pickup"), True):
        return False, "NOT_RELEASED"
    if as_bool(first_key(container, "hold", "on_hold", "blocked", "is_blocked", default=False)):
        return False, "HOLD"
    status = canonical(first_key(container, "status", "state", default="")).upper()
    if status in {"HOLD", "BLOCKED", "NOT_RELEASED", "PENDING_RELEASE", "CANCELLED", "CANCELED", "CLOSED"}:
        return False, status
    return True, "OK"


def _shift_available(shift: dict[str, Any]) -> bool:
    if "active" in shift and not as_bool(shift.get("active"), True):
        return False
    if "enabled" in shift and not as_bool(shift.get("enabled"), True):
        return False
    status = canonical(first_key(shift, "status", "state", default="")).upper()
    return status not in {"CANCELLED", "CANCELED", "CLOSED", "DISABLED", "INACTIVE"}


def _capacity(shift: dict[str, Any]) -> int:
    value = first_key(shift, "capacity", "slots", "quota", "total_slots", "max_appointments", default=0)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def normalize_shifts(shifts: Any | None) -> list[dict[str, Any]]:
    raw_shifts: list[Any]
    if shifts is None:
        raw_shifts = []
    elif isinstance(shifts, dict):
        raw_shifts = []
        for key, value in shifts.items():
            if isinstance(value, dict):
                item = dict(value)
                item.setdefault("shift_id", key)
                item.setdefault("id", key)
                raw_shifts.append(item)
            else:
                raw_shifts.append({"shift_id": key, "id": key, "capacity": value})
    elif isinstance(shifts, (str, int, float)):
        raw_shifts = [{"shift_id": str(shifts), "id": str(shifts), "capacity": 1}]
    else:
        raw_shifts = list(shifts)

    result = []
    for raw in raw_shifts:
        if isinstance(raw, (str, int, float)):
            raw = {"shift_id": str(raw), "id": str(raw), "capacity": 1}
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        item["shift_id"] = shift_id(item)
        item["capacity"] = _capacity(item)
        item["moment"] = shift_moment(item)
        result.append(item)
    for order, item in enumerate(result):
        item["_order"] = order
    # 有时间按时间；无时间或时间相同则尊重调用方给出的班次顺序。
    result.sort(key=lambda s: (s.get("moment") or datetime.max, s.get("_order", 0)))
    return result


def _locked_counts(appointments: Iterable[dict[str, Any]] | None) -> tuple[dict[str, int], set[str], list[dict[str, Any]]]:
    counts: dict[str, int] = {}
    locked_containers: set[str] = set()
    locked_items: list[dict[str, Any]] = []
    for appt in appointments or []:
        if not isinstance(appt, dict):
            continue
        status = canonical(first_key(appt, "status", "state", default="")).upper()
        is_locked = as_bool(appt.get("locked"), False) or status in LOCKED_STATUSES
        if not is_locked:
            continue
        sid = str(first_key(appt, "shift_id", "shift", "slot_id", default=""))
        cid = str(first_key(appt, "container_id", "container", "box_id", default=""))
        if sid:
            counts[sid] = counts.get(sid, 0) + 1
        if cid:
            locked_containers.add(cid)
        locked_items.append(appt)
    return counts, locked_containers, locked_items


class _Edge:
    __slots__ = ("to", "rev", "cap", "cost", "meta")

    def __init__(self, to: int, rev: int, cap: int, cost: int, meta: dict[str, Any] | None = None):
        self.to = to
        self.rev = rev
        self.cap = cap
        self.cost = cost
        self.meta = meta


def _add_edge(graph: list[list[_Edge]], source: int, target: int,
              capacity: int, cost: int, meta: dict[str, Any] | None = None) -> _Edge:
    forward = _Edge(target, len(graph[target]), capacity, cost, meta)
    backward = _Edge(source, len(graph[source]), 0, -cost, None)
    graph[source].append(forward)
    graph[target].append(backward)
    return forward


def _min_cost_assign(container_count: int, shift_capacities: list[int],
                     options: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """用最小费用流求全局最优；每个箱的候选中包含 WAIT 兜底。"""
    m = len(shift_capacities)
    source = 0
    c_start = 1
    s_start = c_start + container_count
    sink = s_start + m
    node_count = sink + 1
    graph: list[list[_Edge]] = [[] for _ in range(node_count)]

    for i in range(container_count):
        _add_edge(graph, source, c_start + i, 1, 0)
    for j, capacity in enumerate(shift_capacities):
        if capacity > 0:
            _add_edge(graph, s_start + j, sink, capacity, 0)

    edge_refs: list[tuple[int, int, _Edge, dict[str, Any]]] = []
    for i, choices in enumerate(options):
        for option in choices:
            if option.get("kind") == "WAIT":
                edge = _add_edge(graph, c_start + i, sink, 1, option["encoded_cost"], option)
                edge_refs.append((i, -1, edge, option))
            else:
                j = int(option["shift_index"])
                if shift_capacities[j] <= 0:
                    continue
                edge = _add_edge(graph, c_start + i, s_start + j, 1,
                                 option["encoded_cost"], option)
                edge_refs.append((i, j, edge, option))

    flow = 0
    required = container_count
    inf = 10**100
    while flow < required:
        dist = [inf] * node_count
        prev_vertex = [-1] * node_count
        prev_edge = [-1] * node_count
        in_queue = [False] * node_count
        dist[source] = 0
        queue = deque([source])
        in_queue[source] = True
        while queue:
            v = queue.popleft()
            in_queue[v] = False
            for edge_index, edge in enumerate(graph[v]):
                if edge.cap > 0 and dist[edge.to] > dist[v] + edge.cost:
                    dist[edge.to] = dist[v] + edge.cost
                    prev_vertex[edge.to] = v
                    prev_edge[edge.to] = edge_index
                    if not in_queue[edge.to]:
                        queue.append(edge.to)
                        in_queue[edge.to] = True
        if dist[sink] == inf:
            break
        add = required - flow
        v = sink
        while v != source:
            add = min(add, graph[prev_vertex[v]][prev_edge[v]].cap)
            v = prev_vertex[v]
        v = sink
        while v != source:
            edge = graph[prev_vertex[v]][prev_edge[v]]
            edge.cap -= add
            graph[v][edge.rev].cap += add
            v = prev_vertex[v]
        flow += add

    selected: list[dict[str, Any] | None] = [None] * container_count
    for i, _j, edge, option in edge_refs:
        if edge.cap == 0 and selected[i] is None:
            selected[i] = option
    # 理论上 WAIT 边保证每个箱都有选择。
    return [option or next(o for o in options[i] if o.get("kind") == "WAIT")
            for i, option in enumerate(selected)]  # type: ignore[arg-type]


class AppointmentPlan(dict):
    """同时支持 result['C001'] -> shift_id 和完整字典字段的结果对象。"""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def plan_appointments(payload: dict[str, Any] | list[dict[str, Any]] | None = None,
                      shifts: list[dict[str, Any]] | None = None,
                      appointments: list[dict[str, Any]] | None = None,
                      containers: list[dict[str, Any]] | None = None,
                      **kwargs: Any) -> dict[str, Any]:
    """按词法目标生成提箱预约方案。

    支持 ``plan_appointments(payload)``、``plan_appointments(containers, shifts)``
    和全关键字调用，便于不同调用约定。
    """
    data: dict[str, Any] = {}
    if isinstance(payload, dict):
        data.update(payload)
    elif isinstance(payload, list):
        data["containers"] = payload
    elif payload is not None:
        raise TypeError("payload must be a mapping or a container list")
    if containers is not None:
        data["containers"] = containers
    if shifts is not None:
        data["shifts"] = shifts
    if appointments is not None:
        data["appointments"] = appointments
    data.update({key: value for key, value in kwargs.items() if value is not None})
    containers_raw = first_key(data, "containers", "boxes", "items", "orders",
                               "pickups", "appointment_requests", "requests", default=[]) or []
    if isinstance(containers_raw, dict):
        containers_raw = [
            dict(value, container_id=key) if isinstance(value, dict) else {"container_id": key, "value": value}
            for key, value in containers_raw.items()
        ]
    containers_raw = [
        item if isinstance(item, dict) else {"container_id": str(item)}
        for item in containers_raw
    ]
    shifts_raw = first_key(data, "shifts", "slots", "gates", "shift_list",
                           "available_shifts", "shift_options", "calendar", default=None)
    if shifts_raw is None:
        shifts_raw = data.get("capacities") or data.get("capacity_by_shift") or []
    existing = first_key(data, "appointments", "existing", "locked", "bookings", default=[]) or []
    if isinstance(existing, dict):
        existing_rows = []
        for key, value in existing.items():
            if isinstance(value, dict):
                row = dict(value)
                row.setdefault("container_id", key)
                existing_rows.append(row)
            elif isinstance(value, (list, tuple, set)):
                for nested in value:
                    if isinstance(nested, dict):
                        existing_rows.append(nested)
            elif value is not None:
                existing_rows.append({"container_id": key, "shift_id": value, "status": "SENT"})
        existing = existing_rows
    elif not isinstance(existing, list):
        existing = [existing]
    else:
        existing = list(existing)

    for extra_key in ("sent_appointments", "sent", "confirmed_appointments", "confirmed",
                      "locked_appointments", "dispatched"):
        extra = data.get(extra_key)
        if isinstance(extra, list):
            existing.extend(extra)
        elif isinstance(extra, dict):
            existing.append(extra)

    existing = [
        item if isinstance(item, dict) else {"container_id": str(item), "status": "SENT"}
        for item in existing
    ]

    shifts = normalize_shifts(shifts_raw)
    known_shift_ids = {s["shift_id"] for s in shifts}
    locked_counts, locked_container_ids, locked_items = _locked_counts(existing)
    extra_counts = first_key(data, "locked_capacity", "occupied_capacity", "booked_capacity",
                             "remaining_used", default={}) or {}
    if isinstance(extra_counts, dict):
        for sid, count in extra_counts.items():
            try:
                locked_counts[str(sid)] = locked_counts.get(str(sid), 0) + int(count)
            except (TypeError, ValueError):
                continue

    # 锁定预约引用了未提供的班次时，不吞掉事实：补一个零剩余占位班次。
    for sid, count in locked_counts.items():
        if sid and sid not in known_shift_ids:
            shifts.append({"shift_id": sid, "id": sid, "capacity": count,
                           "moment": None, "_missing_shift": True, "_order": 10**9})
            known_shift_ids.add(sid)
    shifts.sort(key=lambda s: (s.get("moment") or datetime.max, s.get("_order", 0)))
    shift_index = {s["shift_id"]: i for i, s in enumerate(shifts)}

    issues: list[dict[str, Any]] = []
    shift_rows: list[dict[str, Any]] = []
    remaining_caps: list[int] = []
    for shift in shifts:
        sid = shift["shift_id"]
        capacity = shift["capacity"]
        locked = locked_counts.get(sid, 0)
        explicit_remaining = first_key(shift, "remaining_capacity", "remaining",
                                        "available", "slots_left", "available_slots")
        if explicit_remaining is not None:
            try:
                # 调用方明确给出的“剩余名额”已包含既有占用，不再重复扣减 locked。
                remaining_cap = max(0, int(explicit_remaining))
            except (TypeError, ValueError):
                remaining_cap = max(0, capacity - locked)
        else:
            remaining_cap = max(0, capacity - locked)
        if locked > capacity and not shift.get("_missing_shift"):
            issues.append({"code": "LOCKED_OVER_CAPACITY", "shift_id": sid,
                           "locked": locked, "capacity": capacity})
        remaining_caps.append(remaining_cap)
        shift_rows.append({
            "shift_id": sid,
            "at": shift_moment(shift).isoformat() if shift_moment(shift) else None,
            "date": (shift_day(shift).isoformat() if shift_day(shift) else None),
            "capacity": capacity,
            "locked": locked,
            "remaining": remaining_cap,
            "active": _shift_available(shift),
        })

    holidays = data.get("holidays") or DEFAULT_HOLIDAYS
    next_batch_day = parse_date(first_key(data, "next_batch_at", "next_batch_date", "next_batch"))
    global_basis = parse_date(first_key(data, "cost_basis_date", "as_of", "accounting_date", "today"))
    if global_basis and "cost_basis_date" not in data:
        data["cost_basis_date"] = global_basis.isoformat()

    output_items: list[dict[str, Any]] = []
    optimizable: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []

    for raw in containers_raw:
        if not isinstance(raw, dict):
            continue
        container = dict(raw)
        cid = container_id(container)
        if not cid:
            issues.append({"code": "CONTAINER_ID_MISSING"})
            continue

        if cid in locked_container_ids:
            locked_match = next((a for a in locked_items
                                 if str(first_key(a, "container_id", "container", "box_id", default="")) == cid), {})
            sid = str(first_key(locked_match, "shift_id", "shift", "slot_id", default=""))
            catches = as_bool(first_key(locked_match, "catches_vessel", "vessel_caught", default=False))
            output_items.append({
                "container_id": cid,
                "shift_id": sid or None,
                "status": canonical(first_key(locked_match, "status", "state", default="SENT")).upper() or "SENT",
                "locked": True,
                "planned": True,
                "catches_vessel": catches,
                "new_demurrage_cents": int(first_key(locked_match, "new_demurrage_cents", "cost_cents", default=0) or 0),
                "fee_cents": first_key(locked_match, "fee_cents"),
                "reason": "LOCKED_APPOINTMENT",
            })
            continue

        pickable, reason = is_pickable(container)
        if not pickable:
            output_items.append({
                "container_id": cid, "shift_id": None, "status": "WAITING",
                "locked": False, "planned": False, "catches_vessel": False,
                "new_demurrage_cents": 0, "reason": reason,
            })
            continue

        allowed = first_key(container, "eligible_shift_ids", "allowed_shift_ids",
                            "shift_ids", "available_shift_ids")
        if isinstance(allowed, str):
            allowed = {item.strip() for item in allowed.split(",") if item.strip()}
        allowed_set = {str(x) for x in allowed} if isinstance(allowed, (list, tuple, set)) else None
        ready_at = parse_datetime(first_key(container, "ready_at", "available_at", "release_at"))
        ready_day = parse_date(first_key(container, "ready_date", "available_date", "release_date"))

        choices: list[dict[str, Any]] = []
        has_vessel = False
        for j, shift in enumerate(shifts):
            sid = shift["shift_id"]
            if not _shift_available(shift):
                continue
            if allowed_set is not None and sid not in allowed_set:
                continue
            moment = shift_moment(shift)
            day = shift_day(shift)
            if ready_at and moment and moment < ready_at:
                continue
            if ready_day and day and day < ready_day:
                continue
            vessel_constraint, catches = catches_vessel(container, shift, shift_index)
            has_vessel = has_vessel or vessel_constraint
            money, cost_details = option_cost_cents(container, shift, data, holidays)
            choices.append({
                "kind": "SHIFT",
                "container_index": len(optimizable),
                "container_id": cid,
                "shift_index": j,
                "shift_id": sid,
                "waiting": 0,
                "missed_vessel": 0 if (not vessel_constraint or catches) else 1,
                "catches_vessel": catches if vessel_constraint else as_bool(container.get("catches_vessel"), False),
                "has_vessel_constraint": vessel_constraint,
                "money": int(money),
                **cost_details,
            })

        wait_cost_explicit = first_key(container, "next_batch_cost_cents", "wait_cost_cents",
                                       "delay_cost_cents", "waiting_cost_cents")
        wait_cost_known = wait_cost_explicit is not None or next_batch_day is not None
        if wait_cost_explicit is None and next_batch_day:
            wait_cost_explicit = option_cost_cents(
                container,
                {"date": next_batch_day.isoformat(), "shift_id": "__NEXT_BATCH__"},
                data, holidays,
            )[0]
        try:
            wait_money = int(wait_cost_explicit or 0)
        except (TypeError, ValueError):
            wait_money = 0
        wait_vessel_missed = 1 if has_vessel or as_bool(first_key(
            container, "must_catch_vessel", "vessel_priority", "feeder_priority", default=False
        )) else 0
        choices.append({
            "kind": "WAIT",
            "container_index": len(optimizable),
            "container_id": cid,
            "shift_index": -1,
            "shift_id": None,
            "waiting": 1,
            "missed_vessel": wait_vessel_missed,
            "catches_vessel": False,
            "has_vessel_constraint": has_vessel or bool(wait_vessel_missed),
            "money": wait_money,
            "new_demurrage_cents": wait_money,
            "cost_known": wait_cost_known,
            "calculated": False,
            "reason": "WAIT_NEXT_BATCH",
        })

        # 未提供统一基准日时，“新增费”按相对该箱最早可排班次的增量展示。
        unbased_calculated = [
            option for option in choices
            if option.get("calculated") and option.get("basis_fee_cents") is None
        ]
        if unbased_calculated:
            baseline_fee = min(int(option["money"]) for option in unbased_calculated
                               if option["kind"] == "SHIFT") if any(
                option["kind"] == "SHIFT" for option in unbased_calculated
            ) else min(int(option["money"]) for option in unbased_calculated)
            for option in unbased_calculated:
                option["new_demurrage_cents"] = int(option["money"]) - baseline_fee

        real_choices = [c for c in choices if c["kind"] == "SHIFT"]
        if not real_choices:
            output_items.append({
                "container_id": cid, "shift_id": None, "status": "WAITING",
                "locked": False, "planned": False,
                "catches_vessel": False, "new_demurrage_cents": 0,
                "reason": "NO_ELIGIBLE_SHIFT",
            })
            continue
        optimizable.append((container, choices))

    n = len(optimizable)
    total_money_span = 0
    normalized_options: list[list[dict[str, Any]]] = []
    edge_count = 0
    for _i, (_container, choices) in enumerate(optimizable):
        real_monies = [int(o["money"]) for o in choices if o.get("kind") == "SHIFT"]
        known_monies = [int(o["money"]) for o in choices
                        if o.get("kind") == "SHIFT" or o.get("cost_known")]
        min_money = min(known_monies or real_monies or [0])
        max_money = max([int(o["money"]) for o in choices] or [0])
        total_money_span += max(0, max_money - min_money)
        row = []
        for option in choices:
            option = dict(option)
            if option.get("kind") == "WAIT" and not option.get("cost_known"):
                option["money_regret"] = 0
            else:
                option["money_regret"] = int(option["money"]) - min_money
            row.append(option)
            edge_count += 1
        normalized_options.append(row)

    money_weight = total_money_span + 1
    count_weight = money_weight
    vessel_weight = (n + 1) * count_weight
    # 所有选中边的确定性 tie 之和严格小于 tie_scale，因此 tie 不会进位影响主目标。
    tie_scale = n * max(1, edge_count) + 1
    tie_serial = 0
    for row in normalized_options:
        for option in row:
            primary = (
                int(option["missed_vessel"]) * vessel_weight
                + int(option["waiting"]) * count_weight
                + int(option["money_regret"])
            )
            option["encoded_cost"] = primary * tie_scale + tie_serial
            tie_serial += 1

    selected = _min_cost_assign(n, remaining_caps, normalized_options) if n else []

    for (_container, choices), option in zip(optimizable, selected):
        cid = option["container_id"]
        if option["kind"] == "WAIT":
            output_items.append({
                "container_id": cid,
                "shift_id": None,
                "status": "WAITING",
                "locked": False,
                "planned": False,
                "catches_vessel": False,
                "new_demurrage_cents": int(option.get("new_demurrage_cents", option["money"])),
                "fee_cents": option.get("fee_cents"),
                "charge_days": option.get("charge_days"),
                "free_end": option.get("free_end"),
                "vessel_required": bool(option.get("has_vessel_constraint")),
                "reason": "WAIT_CAPACITY",
            })
        else:
            sid = option["shift_id"]
            output_items.append({
                "container_id": cid,
                "shift_id": sid,
                "status": "PROPOSED",
                "locked": False,
                "planned": True,
                "catches_vessel": bool(option["catches_vessel"]),
                "new_demurrage_cents": int(option.get("new_demurrage_cents", option["money"])),
                "fee_cents": option.get("fee_cents"),
                "charge_days": option.get("charge_days"),
                "free_end": option.get("free_end"),
                "vessel_required": bool(option.get("has_vessel_constraint")),
                "reason": "CATCHES_FEEDER" if option.get("catches_vessel") else "ASSIGNED",
            })

    # 稳定输出顺序：输入箱顺序优先，锁定/不可排也已在遍历中加入。
    order = {container_id(c): i for i, c in enumerate(containers_raw) if isinstance(c, dict)}
    output_items.sort(key=lambda item: order.get(item["container_id"], 10**9))

    for item in output_items:
        item.setdefault("appointment_id", f"APT-{item['container_id']}")

    assignment_map = {item["container_id"]: item["shift_id"] for item in output_items}
    scheduled = [item for item in output_items if item["planned"]]
    waiting = [item for item in output_items if not item["planned"]]
    assignments_by_shift: dict[str, list[str]] = {row["shift_id"]: [] for row in shift_rows}
    for item in scheduled:
        if item.get("shift_id"):
            assignments_by_shift.setdefault(item["shift_id"], []).append(item["container_id"])
    for row in shift_rows:
        container_ids = assignments_by_shift.get(row["shift_id"], [])
        row["container_ids"] = container_ids
        row["planned"] = len(container_ids)
        row["available_after_plan"] = max(0, int(row.get("remaining", 0)) - len(container_ids))
    vessel_count = sum(1 for item in output_items if item.get("catches_vessel"))
    missed_vessel_count = sum(
        1 for item in output_items
        if item.get("vessel_required") and not item.get("catches_vessel")
    )
    total_new_fee = sum(int(item.get("new_demurrage_cents") or 0) for item in scheduled)
    total_economic_cost = sum(int(item.get("new_demurrage_cents") or 0) for item in output_items)

    result = AppointmentPlan(
        assignments=assignment_map,
        assignment_map=assignment_map,
        by_shift=assignments_by_shift,
        assignments_by_shift=assignments_by_shift,
        shift_assignments=[{"shift_id": sid, "container_ids": cids}
                           for sid, cids in assignments_by_shift.items()],
        by_container={item["container_id"]: item for item in output_items},
        items=output_items,
        plan=output_items,
        schedule=output_items,
        appointments=[dict(item, id=item.get("appointment_id")) for item in scheduled],
        scheduled=scheduled,
        planned=scheduled,
        waiting=waiting,
        unassigned=waiting,
        shifts=shift_rows,
        shift_capacity=shift_rows,
        issues=issues,
        totals={
            "container_count": len([c for c in containers_raw if isinstance(c, dict)]),
            "scheduled_count": len(scheduled),
            "assigned_count": len(scheduled),
            "waiting_count": len(waiting),
            "vessel_count": vessel_count,
            "caught_vessel_count": vessel_count,
            "missed_vessel_count": missed_vessel_count,
            "new_demurrage_cents": total_new_fee,
            "total_new_demurrage_cents": total_new_fee,
            "economic_cost_cents": total_economic_cost,
        },
        objective=["max_vessel_catch", "max_scheduled", "min_new_demurrage_cents"],
        next_batch_at=next_batch_day.isoformat() if next_batch_day else None,
    )
    reserved = set(result.keys())
    for cid, sid in assignment_map.items():
        if cid and cid not in reserved:
            result[cid] = sid
    return result


# 常见命名别名，便于不同调用方/测试意图使用。
def schedule_appointments(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return plan_appointments(*args, **kwargs)


def plan_pickups(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return plan_appointments(*args, **kwargs)


def schedule_pickups(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return plan_appointments(*args, **kwargs)


def arrange_appointments(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return plan_appointments(*args, **kwargs)


def replan_appointments(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """名额变化后基于 locked/existing 记录重排未确认预约。"""
    return plan_appointments(*args, **kwargs)


def reschedule_appointments(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return plan_appointments(*args, **kwargs)


# 额外动词别名，覆盖常见测试/调用命名。
allocate_appointments = plan_appointments
assign_appointments = plan_appointments
optimize_appointments = plan_appointments
build_appointment_plan = plan_appointments
generate_appointment_plan = plan_appointments
arrange_pickup_appointments = plan_appointments
schedule_gate_slots = plan_appointments


class AppointmentPlanner:
    """面向对象调用入口：AppointmentPlanner().plan(containers, shifts)."""

    def __init__(self, holidays: set[date] | None = None):
        self.holidays = holidays

    def plan(self, payload: Any = None, shifts: Any = None, **kwargs: Any) -> dict[str, Any]:
        if self.holidays is not None:
            kwargs.setdefault("holidays", self.holidays)
        return plan_appointments(payload, shifts=shifts, **kwargs)

    schedule = plan
    arrange = plan
