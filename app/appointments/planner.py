from app.domain.appointments import *  # noqa: F401,F403
from app.domain.appointments import (
    AppointmentPlan,
    AppointmentPlanner,
    arrange_appointments,
    plan_appointments,
    plan_pickups,
    schedule_appointments,
    schedule_pickups,
)

__all__ = [
    "AppointmentPlan",
    "AppointmentPlanner",
    "plan_appointments",
    "schedule_appointments",
    "plan_pickups",
    "schedule_pickups",
    "arrange_appointments",
]

Scheduler = AppointmentPlanner
AppointmentScheduler = AppointmentPlanner
PickupScheduler = AppointmentPlanner
GateScheduler = AppointmentPlanner
