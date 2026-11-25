from app.domain.appointments import *  # noqa: F401,F403
from app.domain.appointments import plan_appointments as plan_bookings

plan_booking = plan_bookings
schedule_bookings = plan_bookings

__all__ = ["plan_bookings", "plan_booking", "schedule_bookings"]
