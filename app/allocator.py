from app.domain.appointments import allocate_appointments as allocate

class Allocator:
    allocate = staticmethod(allocate)
    plan = staticmethod(allocate)
