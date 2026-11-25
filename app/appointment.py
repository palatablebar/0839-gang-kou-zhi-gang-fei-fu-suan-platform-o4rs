from app.domain.appointments import *  # noqa: F401,F403
from app.domain.appointments import plan_appointments

plan_appointment = plan_appointments
arrange_pickups = plan_appointments
schedule_gate_slots = plan_appointments

__all__ = ["plan_appointments", "plan_appointment", "arrange_pickups", "schedule_gate_slots"]
