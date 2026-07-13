"""Motore di generazione automatica dei turni.

La logica è volutamente indipendente dal database (lavora solo su strutture
dati semplici) in modo da poter essere testata facilmente e da poter essere
riutilizzata anche fuori dal contesto Flask.
"""

from calendar import monthrange
from dataclasses import dataclass, field

WEEKDAY_NAMES = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]


@dataclass
class EmployeeInput:
    id: int
    name: str
    max_shifts_per_week: int | None = None
    qualified_shift_type_ids: set = field(default_factory=set)


@dataclass
class ShiftTypeInput:
    id: int
    name: str
    requirements_by_weekday: dict  # {0..6: required_staff}
    sort_order: int = 0


@dataclass
class ScheduleResult:
    assignments: list  # list of dicts {employee_id, shift_type_id, day}
    warnings: list  # list of str


def generate_schedule(
    year,
    month,
    employees,
    shift_types,
    availability,  # dict (employee_id, day) -> status string ("AVAILABLE"/"UNAVAILABLE"/"PREFERRED_OFF")
    forbidden_sequences,  # set of (prev_shift_type_id, next_shift_type_id)
    max_consecutive_work_days=6,
    require_weekly_day_off=True,
):
    first_weekday, num_days = monthrange(year, month)  # first_weekday: 0=Lunedì

    ordered_shift_types = sorted(shift_types, key=lambda s: s.sort_order)

    state = {
        emp.id: {
            "consecutive_days": 0,
            "total_shifts": 0,
            "week_shifts": {},
            "week_work_days": {},
            "shift_by_day": {},
        }
        for emp in employees
    }

    def week_index(day):
        return (day - 1 + first_weekday) // 7

    assignments = []
    warnings = []

    for day in range(1, num_days + 1):
        weekday = (day - 1 + first_weekday) % 7
        wk = week_index(day)
        assigned_today = set()

        for shift_type in ordered_shift_types:
            required = shift_type.requirements_by_weekday.get(weekday, 0)
            if required <= 0:
                continue

            candidates = []
            for emp in employees:
                emp_state = state[emp.id]

                if emp.id in assigned_today:
                    continue
                if shift_type.id not in emp.qualified_shift_type_ids:
                    continue

                status = availability.get((emp.id, day), "AVAILABLE")
                if status == "UNAVAILABLE":
                    continue

                if emp_state["consecutive_days"] + 1 > max_consecutive_work_days:
                    continue

                prev_shift = emp_state["shift_by_day"].get(day - 1)
                if prev_shift is not None and (prev_shift, shift_type.id) in forbidden_sequences:
                    continue

                if emp.max_shifts_per_week is not None:
                    if emp_state["week_shifts"].get(wk, 0) >= emp.max_shifts_per_week:
                        continue

                if require_weekly_day_off and weekday == 6:
                    week_start_day = max(day - weekday, 1)
                    prior_days_in_week = day - week_start_day
                    if prior_days_in_week > 0 and emp_state["week_work_days"].get(wk, 0) >= prior_days_in_week:
                        continue

                candidates.append(
                    (
                        status == "PREFERRED_OFF",
                        emp_state["total_shifts"],
                        emp_state["week_shifts"].get(wk, 0),
                        emp.id,
                        emp,
                    )
                )

            candidates.sort(key=lambda c: c[:4])
            chosen = [c[4] for c in candidates[:required]]

            for emp in chosen:
                assignments.append({"employee_id": emp.id, "shift_type_id": shift_type.id, "day": day})
                assigned_today.add(emp.id)
                emp_state = state[emp.id]
                emp_state["total_shifts"] += 1
                emp_state["week_shifts"][wk] = emp_state["week_shifts"].get(wk, 0) + 1
                emp_state["week_work_days"][wk] = emp_state["week_work_days"].get(wk, 0) + 1
                emp_state["shift_by_day"][day] = shift_type.id

            if len(chosen) < required:
                warnings.append(
                    f"Giorno {day} ({WEEKDAY_NAMES[weekday]}): turno '{shift_type.name}' coperto da "
                    f"{len(chosen)}/{required} persone per mancanza di disponibilità."
                )

        for emp in employees:
            emp_state = state[emp.id]
            if emp.id in assigned_today:
                emp_state["consecutive_days"] += 1
            else:
                emp_state["consecutive_days"] = 0

    return ScheduleResult(assignments=assignments, warnings=warnings)
