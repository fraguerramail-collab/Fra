"""Motore di generazione automatica dei turni.

Indipendente dal database: lavora su dataclass semplici cosi' e' facile
da testare. Riproduce in forma generalizzata (configurabile per qualsiasi
codice turno, non hardcoded) le regole tipiche di una pianificazione
ospedaliera complessa:

- fasce orarie e compatibilita' tra turni nello stesso giorno
- turno "esclusivo" (nessun altro turno lo stesso giorno, es. notte)
- riposo obbligatorio il giorno dopo un turno (smonto), con eccezione
  configurabile per il cambio sabato/domenica
- distanza minima in giorni tra due occorrenze dello stesso turno
- turni "a blocco settimanale" (assegnati per l'intera settimana, es. corsia)
- turni weekend a ruoli (stesso medico copre piu' turni nel weekend)
- skill richieste (con OR tramite 'A|B') e categorie escluse
- preferenze pesate (EVITA / PREFERISCI / RISERVA / MAX_MESE)
- turni extra attivabili su date specifiche con pool di equita' separato
- continuita' con il mese precedente (smonto, weekend) tramite storico assegnazioni
"""

from calendar import monthrange
from dataclasses import dataclass, field

WEEKDAY_NAMES = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]
SATURDAY, SUNDAY = 5, 6


@dataclass
class EmployeeInput:
    id: int
    name: str
    category: str = ""
    skills: set = field(default_factory=set)
    max_shifts_per_week: int | None = None


@dataclass
class ShiftTypeInput:
    id: int
    name: str
    priority: int = 100
    time_bands: set = field(default_factory=set)
    skill_required: str = ""
    days_set: set = field(default_factory=set)
    excluded_categories: set = field(default_factory=set)
    exclusive_day: bool = False
    requires_rest_next_day: bool = False
    rest_exception_shift_type_id: int | None = None
    min_gap_days: int = 0
    weekly_block: bool = False
    block_group: str | None = None
    is_extra: bool = False
    balance_pool: str | None = None
    requirements_by_weekday: dict = field(default_factory=dict)


@dataclass
class WeekendRoleInput:
    role_code: str
    day: str  # 'SAB' o 'DOM'
    shift_type_id: int
    skill_required: str = ""


@dataclass
class PreferenceInput:
    employee_id: int | None
    shift_type_id: int | None
    days_set: set
    pref_type: str  # EVITA / PREFERISCI / RISERVA / MAX_MESE
    weight: int = 1


@dataclass
class ScheduleResult:
    assignments: list  # dict {employee_id, shift_type_id, day}
    warnings: list


def _has_skill(employee, skill_formula):
    required = [s.strip() for s in (skill_formula or "").split("|") if s.strip()]
    if not required:
        return True
    return bool(employee.skills & set(required))


def _bands_compatible(full_block, blocked_bands, shift_bands):
    if full_block:
        return False
    if not blocked_bands:
        return True
    if not shift_bands:
        return True
    return not (blocked_bands & shift_bands)


class _MonthState:
    """Stato mutabile durante la generazione di un mese."""

    def __init__(self, employees):
        self.total = {e.id: 0 for e in employees}
        self.per_shift_type = {e.id: {} for e in employees}
        self.per_pool = {e.id: {} for e in employees}
        self.per_block_group_weeks = {e.id: set() for e in employees}
        self.assigned_day = {e.id: {} for e in employees}  # day -> shift_type_id list
        self.weekend_keys = {e.id: set() for e in employees}

    def record(self, employee_id, shift_type, day):
        self.total[employee_id] += 1
        self.per_shift_type[employee_id][shift_type.id] = (
            self.per_shift_type[employee_id].get(shift_type.id, 0) + 1
        )
        if shift_type.balance_pool:
            self.per_pool[employee_id][shift_type.balance_pool] = (
                self.per_pool[employee_id].get(shift_type.balance_pool, 0) + 1
            )
        self.assigned_day[employee_id].setdefault(day, []).append(shift_type.id)


def _preference_score(employee, shift_type, day, weekday, preferences, state):
    score = 0
    for p in preferences:
        if p.employee_id is not None and p.employee_id != employee.id:
            continue
        if p.shift_type_id is not None and p.shift_type_id != shift_type.id:
            continue
        if p.days_set and weekday not in p.days_set:
            continue
        if p.pref_type == "EVITA":
            score += p.weight
        elif p.pref_type == "PREFERISCI":
            score -= p.weight
        elif p.pref_type == "RISERVA":
            score += p.weight * 500
        elif p.pref_type == "MAX_MESE":
            already = state.per_shift_type[employee.id].get(shift_type.id, 0)
            if p.weight > 0 and already >= p.weight:
                score += 50000 + (already - p.weight + 1) ** 2 * 20000
    return score


def _fairness_score(employee, shift_type, state):
    total = state.total[employee.id]
    same = state.per_shift_type[employee.id].get(shift_type.id, 0)
    score = total * 90 + same * 250
    if total > 10:
        score += (total - 10) ** 2 * 350
    if shift_type.balance_pool:
        pool_count = state.per_pool[employee.id].get(shift_type.balance_pool, 0)
        score += pool_count * 3000
    return score


def _day_has_exclusive_conflict(state, employee_id, day, shift_types_by_id, new_shift):
    existing_ids = state.assigned_day[employee_id].get(day, [])
    if not existing_ids:
        return False
    if new_shift.exclusive_day:
        return True
    for sid in existing_ids:
        st = shift_types_by_id[sid]
        if st.exclusive_day:
            return True
        if st.time_bands & new_shift.time_bands:
            return True
    return False


def _min_gap_violated(state, employee_id, shift_type, day):
    if shift_type.min_gap_days <= 0:
        return False
    for other_day, shift_ids in state.assigned_day[employee_id].items():
        if shift_type.id in shift_ids and abs(other_day - day) <= shift_type.min_gap_days:
            return True
    return False


def _rest_violation(state, employee_id, shift_type, day, shift_types_by_id, weekday, prev_month_last_shifts):
    """Controlla se ieri il dipendente ha fatto un turno con requires_rest_next_day."""
    if day == 1:
        yesterday_shifts = prev_month_last_shifts.get(employee_id, [])
        yesterday_weekday = (weekday - 1) % 7
    else:
        yesterday_shifts = state.assigned_day[employee_id].get(day - 1, [])
        yesterday_weekday = (weekday - 1) % 7

    for sid in yesterday_shifts:
        prev_shift = shift_types_by_id.get(sid)
        if prev_shift is None or not prev_shift.requires_rest_next_day:
            continue
        exception_ok = (
            prev_shift.rest_exception_shift_type_id == shift_type.id
            and yesterday_weekday == SATURDAY
            and weekday == SUNDAY
        )
        if not exception_ok:
            return True
    return False


def generate_schedule(
    year,
    month,
    employees,
    shift_types,
    weekend_roles,
    preferences,
    availability,  # dict (employee_id, day) -> (full_block: bool, blocked_bands: set)
    suppressions,  # set of (shift_type_id, day)
    extra_activations,  # dict day -> list of shift_type_id attivi quel giorno
    prev_month_last_shifts=None,  # dict employee_id -> [shift_type_id,...] assegnati l'ultimo giorno del mese precedente
    prev_month_weekend_count=None,  # dict employee_id -> weekend lavorati nell'ultimo weekend del mese precedente (bool-like int)
    max_consecutive_work_days=6,
):
    prev_month_last_shifts = prev_month_last_shifts or {}
    prev_month_weekend_count = prev_month_weekend_count or {}

    first_weekday, num_days = monthrange(year, month)
    shift_types_by_id = {st.id: st for st in shift_types}
    warnings = []
    assignments = []
    state = _MonthState(employees)

    def weekday_of(day):
        return (day - 1 + first_weekday) % 7

    def week_index(day):
        return (day - 1 + first_weekday) // 7

    def is_available(employee_id, day, shift):
        full_block, blocked_bands = availability.get((employee_id, day), (False, set()))
        return _bands_compatible(full_block, blocked_bands, shift.time_bands)

    def is_suppressed(shift_type_id, day):
        return (shift_type_id, day) in suppressions

    def try_assign(employee, shift_type, day, weekday):
        if _day_has_exclusive_conflict(state, employee.id, day, shift_types_by_id, shift_type):
            return False
        if _min_gap_violated(state, employee.id, shift_type, day):
            return False
        if _rest_violation(state, employee.id, shift_type, day, shift_types_by_id, weekday, prev_month_last_shifts):
            return False
        state.record(employee.id, shift_type, day)
        assignments.append({"employee_id": employee.id, "shift_type_id": shift_type.id, "day": day})
        return True

    def candidates_for(shift_type, day, weekday, skill_formula=None):
        result = []
        for emp in employees:
            if not _has_skill(emp, skill_formula if skill_formula is not None else shift_type.skill_required):
                continue
            if emp.category in shift_type.excluded_categories:
                continue
            if not is_available(emp.id, day, shift_type):
                continue
            if _day_has_exclusive_conflict(state, emp.id, day, shift_types_by_id, shift_type):
                continue
            if _min_gap_violated(state, emp.id, shift_type, day):
                continue
            if _rest_violation(state, emp.id, shift_type, day, shift_types_by_id, weekday, prev_month_last_shifts):
                continue
            result.append(emp)
        return result

    # --- Pass 1: weekend a ruoli (stesso medico copre tutti i turni del proprio ruolo nel weekend) ---
    # Un turno e' "coperto dal weekend" solo nel giorno specifico (SAB o DOM) in cui
    # compare in un ruolo: es. GN il sabato e R2N la domenica sono due turni diversi,
    # e ciascuno resta soggetto al fabbisogno ordinario nell'altro giorno del weekend.
    weekend_controlled_day = {(r.shift_type_id, r.day) for r in weekend_roles}
    weekends = []
    d = 1
    while d <= num_days:
        if weekday_of(d) == SATURDAY:
            sun = d + 1 if d + 1 <= num_days and weekday_of(d + 1) == SUNDAY else None
            weekends.append((d, sun))
        d += 1

    roles_by_code = {}
    for r in weekend_roles:
        roles_by_code.setdefault(r.role_code, []).append(r)

    for sab_day, dom_day in weekends:
        role_days = [(sab_day, "SAB")]
        if dom_day:
            role_days.append((dom_day, "DOM"))

        used_this_weekend = set()
        for role_code, role_entries in sorted(roles_by_code.items()):
            applicable = [r for r in role_entries if (r.day == "SAB") or (r.day == "DOM" and dom_day)]
            if not applicable:
                continue

            candidates = None
            for r in applicable:
                day = sab_day if r.day == "SAB" else dom_day
                weekday = weekday_of(day)
                shift_type = shift_types_by_id[r.shift_type_id]
                skill_formula = r.skill_required or shift_type.skill_required
                day_candidates = set(c.id for c in candidates_for(shift_type, day, weekday, skill_formula))
                candidates = day_candidates if candidates is None else (candidates & day_candidates)

            if not candidates:
                warnings.append(
                    f"Weekend {sab_day}-{dom_day or sab_day}: nessun candidato disponibile per il ruolo {role_code}."
                )
                continue

            candidates -= used_this_weekend
            if not candidates:
                warnings.append(
                    f"Weekend {sab_day}-{dom_day or sab_day}: ruolo {role_code} senza candidati residui (gia' usati altri ruoli)."
                )
                continue

            def weekend_score(emp_id):
                already = len(state.weekend_keys[emp_id]) + prev_month_weekend_count.get(emp_id, 0)
                emp = next(e for e in employees if e.id == emp_id)
                return (already, state.total[emp_id], emp_id)

            chosen_id = min(candidates, key=weekend_score)
            chosen = next(e for e in employees if e.id == chosen_id)
            used_this_weekend.add(chosen_id)
            state.weekend_keys[chosen_id].add(sab_day)

            for r in applicable:
                day = sab_day if r.day == "SAB" else dom_day
                weekday = weekday_of(day)
                shift_type = shift_types_by_id[r.shift_type_id]
                try_assign(chosen, shift_type, day, weekday)

    # --- Pass 2: turni a blocco settimanale (es. corsia) ---
    weekly_types = [st for st in shift_types if st.weekly_block]
    weeks = {}
    for day in range(1, num_days + 1):
        wd = weekday_of(day)
        if wd >= 5:  # solo giorni feriali per i blocchi settimanali
            continue
        weeks.setdefault(week_index(day), []).append(day)

    for wk, days_in_week in sorted(weeks.items()):
        for shift_type in weekly_types:
            if shift_type.days_set:
                valid_days = [d for d in days_in_week if weekday_of(d) in shift_type.days_set]
            else:
                valid_days = list(days_in_week)
            if not valid_days:
                continue

            candidate_ids = None
            for d in valid_days:
                weekday = weekday_of(d)
                if is_suppressed(shift_type.id, d):
                    candidate_ids = set()
                    break
                day_candidates = set(c.id for c in candidates_for(shift_type, d, weekday))
                candidate_ids = day_candidates if candidate_ids is None else (candidate_ids & day_candidates)

            if not candidate_ids:
                warnings.append(
                    f"Settimana giorno {valid_days[0]}: nessun candidato per l'intera settimana del turno '{shift_type.name}'."
                )
                continue

            def block_score(emp_id):
                group = shift_type.block_group or shift_type.id
                weeks_done = len({w for w in state.per_block_group_weeks[emp_id] if w[0] == group})
                emp = next(e for e in employees if e.id == emp_id)
                return (weeks_done, state.total[emp_id], emp_id)

            chosen_id = min(candidate_ids, key=block_score)
            chosen = next(e for e in employees if e.id == chosen_id)
            group = shift_type.block_group or shift_type.id
            state.per_block_group_weeks[chosen_id].add((group, wk))

            for d in valid_days:
                weekday = weekday_of(d)
                try_assign(chosen, shift_type, d, weekday)

    # --- Pass 3: turni giornalieri ordinari (per priorita' crescente) ---
    # I turni che fanno parte di un ruolo weekend sono gestiti dal Pass 1 solo
    # per sabato/domenica: nei giorni feriali restano soggetti al fabbisogno normale.
    ordinary_types = sorted(
        [st for st in shift_types if not st.weekly_block and not st.is_extra],
        key=lambda s: s.priority,
    )

    for day in range(1, num_days + 1):
        weekday = weekday_of(day)
        day_code = "SAB" if weekday == SATURDAY else ("DOM" if weekday == SUNDAY else None)
        for shift_type in ordinary_types:
            if day_code and (shift_type.id, day_code) in weekend_controlled_day:
                continue
            if shift_type.days_set and weekday not in shift_type.days_set:
                continue
            required = shift_type.requirements_by_weekday.get(weekday, 0)
            if required <= 0:
                continue
            if is_suppressed(shift_type.id, day):
                continue

            for _slot in range(required):
                candidates = candidates_for(shift_type, day, weekday)
                if not candidates:
                    warnings.append(
                        f"Giorno {day} ({WEEKDAY_NAMES[weekday]}): turno '{shift_type.name}' senza candidati disponibili."
                    )
                    continue

                candidates.sort(
                    key=lambda e: (
                        _fairness_score(e, shift_type, state)
                        + _preference_score(e, shift_type, day, weekday, preferences, state),
                        e.id,
                    )
                )
                try_assign(candidates[0], shift_type, day, weekday)

    # --- Pass 4: turni extra (attivati su date specifiche, pool di equita' separato) ---
    for day, active_ids in sorted(extra_activations.items()):
        for shift_type_id in active_ids:
            shift_type = shift_types_by_id.get(shift_type_id)
            if shift_type is None:
                continue
            weekday = weekday_of(day)
            candidates = candidates_for(shift_type, day, weekday)
            if not candidates:
                warnings.append(
                    f"Giorno {day} ({WEEKDAY_NAMES[weekday]}): turno extra '{shift_type.name}' senza candidati disponibili."
                )
                continue
            candidates.sort(
                key=lambda e: (
                    _fairness_score(e, shift_type, state)
                    + _preference_score(e, shift_type, day, weekday, preferences, state),
                    e.id,
                )
            )
            try_assign(candidates[0], shift_type, day, weekday)

    # --- Controllo finale: giorni lavorativi consecutivi ---
    for emp in employees:
        run = 1 if prev_month_last_shifts.get(emp.id) else 0
        for day in range(1, num_days + 1):
            worked = bool(state.assigned_day[emp.id].get(day))
            if worked:
                run += 1
                if run == max_consecutive_work_days + 1:
                    warnings.append(
                        f"{emp.name}: supera {max_consecutive_work_days} giorni lavorativi consecutivi (dal giorno {day - max_consecutive_work_days})."
                    )
            else:
                run = 0

    return ScheduleResult(assignments=assignments, warnings=warnings)
