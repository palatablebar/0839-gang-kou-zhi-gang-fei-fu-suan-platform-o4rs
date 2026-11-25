from app.domain.appointments import *  # noqa: F401,F403
from app.domain.appointments import (
    arrange_appointments,
    plan_appointments,
    schedule_appointments,
    schedule_pickups,
)

schedule = plan_appointments
arrange_pickup_appointments = arrange_appointments

__all__ = [
    "plan_appointments",
    "schedule_appointments",
    "schedule_pickups",
    "arrange_appointments",
    "schedule",
    "arrange_pickup_appointments",
]
