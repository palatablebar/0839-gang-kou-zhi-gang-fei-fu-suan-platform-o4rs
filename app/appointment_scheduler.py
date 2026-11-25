from app.appointments.service import AppointmentService, confirm, create_plan, issue, replan
from app.domain.appointments import (
    allocate_appointments,
    arrange_appointments,
    plan_appointments,
    schedule_appointments,
)

AppointmentScheduler = AppointmentService

__all__ = [
    "AppointmentScheduler",
    "AppointmentService",
    "allocate_appointments",
    "arrange_appointments",
    "confirm",
    "create_plan",
    "issue",
    "plan_appointments",
    "replan",
    "schedule_appointments",
]
