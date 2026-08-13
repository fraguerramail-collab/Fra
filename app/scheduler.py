"""Motore di generazione automatica dei turni.

Indipendente dal database: lavora su dataclass semplici cosi' e' facile
da testare. Usa un risolutore a vincoli (Google OR-Tools CP-SAT) che
valuta l'intero mese in un colpo solo, invece di riempire i turni uno
alla volta in ordine di priorita': evita l'effetto "imbuto" per cui le
prime assegnazioni restringono progressivamente le possibilita' di
quelle successive, specialmente nei mesi con molte assenze.

Regole modellate come vincoli:

- fasce orarie e compatibilita' tra turni nello stesso giorno
- turno "esclusivo" (nessun altro turno lo stesso giorno, es. notte)
- riposo obbligatorio il giorno dopo un turno (smonto), con eccezione
  configurabile per il cambio sabato/domenica
- distanza minima in giorni tra due occorrenze dello stesso turno
- turni "a blocco settimanale" (assegnati per l'intera settimana, es. corsia)
- turni weekend a ruoli (stesso medico copre piu' turni nel weekend)
- skill richieste (con OR tramite 'A|B') e categorie escluse
- regole legate a una skill (blocco/riserva), non a un turno specifico
- turni extra attivabili su date specifiche con pool di equita' separato
- continuita' con il mese precedente (smonto, weekend) tramite storico assegnazioni

L'obiettivo del risolutore, in ordine di importanza: (1) minimizzare le
scoperture (coprire quanto piu' possibile), (2) equita' del carico totale
tra i dipendenti, (3) equita' dei weekend lavorati, (4) preferenze pesate.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass, field

from ortools.sat.python import cp_model

WEEKDAY_NAMES = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]
SATURDAY, SUNDAY = 5, 6

# Peso della penalita' per ogni "unita'" di scopertura (turno non coperto,
# ruolo weekend o blocco settimanale non assegnato): domina su tutto il
# resto, il risolutore sacrifica equita'/preferenze pur di coprire di piu'.
SHORTFALL_PENALTY = 1_000_000
WEEKEND_FAIRNESS_WEIGHT = 200
FAIRNESS_WEIGHT = 400
MAX_MESE_PENALTY = 5_000
BLOCK_DEVIATION_UNIT_WEIGHT = 300
SOLVER_TIME_LIMIT_SECONDS = 90


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
    time_bands: set = field(default_factory=set)
    skill_required: str = ""
    skill_by_weekday: dict = field(default_factory=dict)  # weekday -> skill_required specifica (sovrascrive quella generale)
    days_set: set = field(default_factory=set)
    excluded_categories: set = field(default_factory=set)
    exclusive_day: bool = False
    requires_rest_next_day: bool = False
    rest_exception_shift_type_id: int | None = None
    min_gap_days: int = 0
    min_gap_excludes_weekend: bool = False  # non applicare la distanza minima tra sab+dom dello stesso weekend
    weekly_block: bool = False
    weekly_block_strictness: int = 10
    block_group: str | None = None
    block_cooldown_weeks: int = 0  # min. settimane prima di poter ripossedere una settimana nello stesso block_group
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
    time_bands: set = field(default_factory=set)  # vuoto = tutte le fasce


@dataclass
class SkillRuleInput:
    """Regola trasversale legata a una skill (o a una categoria/fascia), non
    a un turno specifico.

    BLOCK: chi ha questa skill (o questa categoria, es. 'specializzando')
    non puo' essere assegnato a NESSUN turno in questo giorno della
    settimana (es. dedicato ad attivita' fuori app, oppure escluso dai
    weekend). RESERVE: tra chi ha questa skill, almeno 'min_free' devono
    restare liberi (non assegnati a nulla) in questo giorno della
    settimana (solo per skill, non per categoria).
    """

    skill: str
    weekday: int  # 0=Lunedì .. 6=Domenica
    mode: str  # "BLOCK" o "RESERVE"
    min_free: int = 1
    category: str | None = None  # se impostata, la regola BLOCK vale per categoria invece che per skill


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


def _shift_type_conflicts(shift_types):
    """Coppie di turni incompatibili lo stesso giorno per la stessa persona:
    uno dei due e' 'esclusivo', oppure le fasce orarie si sovrappongono."""
    conflicts = set()
    sts = list(shift_types)
    for i in range(len(sts)):
        for j in range(i + 1, len(sts)):
            a, b = sts[i], sts[j]
            if a.exclusive_day or b.exclusive_day or (a.time_bands & b.time_bands):
                conflicts.add((a.id, b.id))
    return conflicts


def _prev_month_last_weekday(year, month):
    py, pm = (year, month - 1) if month > 1 else (year - 1, 12)
    first_weekday, num_days = monthrange(py, pm)
    return (first_weekday + num_days - 1) % 7


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
    skill_rules=None,  # lista di SkillRuleInput
    pinned_assignments=None,  # set di (employee_id, shift_type_id, day) gia' inseriti a mano: il
    # risolutore li tratta come gia' occupati (contano sul fabbisogno, non si sommano un'altra
    # persona sopra) invece di ignorarli e assegnarne una in piu' per lo stesso posto.
    prev_block_group_last_day=None,  # dict block_group -> {employee_id: ultimo giorno lavorato su quel
    # gruppo prima di questo mese, numerato come i giorni del mese corrente ma <= 0 (es. 0 = giorno prima
    # dell'1, -3 = quattro giorni prima dell'1): serve al raffreddamento tra settimane per farlo valere
    # anche a cavallo di mese, senza dover ricalcolare da zero ogni volta.
):
    prev_month_last_shifts = prev_month_last_shifts or {}
    prev_month_weekend_count = prev_month_weekend_count or {}
    prev_block_group_last_day = prev_block_group_last_day or {}
    skill_rules = skill_rules or []
    skill_block_set = {(r.skill, r.weekday) for r in skill_rules if r.mode == "BLOCK"}
    category_block_set = {(r.category, r.weekday) for r in skill_rules if r.mode == "BLOCK" and r.category}
    skill_reserve_rules = [r for r in skill_rules if r.mode == "RESERVE"]
    pinned_assignments = pinned_assignments or set()
    employees_by_id = {e.id: e for e in employees}
    pinned_by_shift_day = {}
    for emp_id, st_id, day in pinned_assignments:
        # ignora un'assegnazione manuale che punta a un dipendente non piu'
        # attivo (o comunque non nella lista corrente): non c'e' nessuna
        # variabile del risolutore per lui a cui agganciare il 'pin'.
        if emp_id not in employees_by_id:
            continue
        pinned_by_shift_day.setdefault((st_id, day), set()).add(emp_id)

    first_weekday, num_days = monthrange(year, month)
    shift_types_by_id = {st.id: st for st in shift_types}
    warnings = []

    def weekday_of(day):
        return (day - 1 + first_weekday) % 7

    def week_index(day):
        return (day - 1 + first_weekday) // 7

    def is_available(employee_id, day, shift):
        full_block, blocked_bands = availability.get((employee_id, day), (False, set()))
        return _bands_compatible(full_block, blocked_bands, shift.time_bands)

    def is_suppressed(shift_type_id, day):
        return (shift_type_id, day) in suppressions

    def eligible(emp, shift_type, day, weekday, skill_formula=None):
        if not _has_skill(emp, skill_formula if skill_formula is not None else shift_type.skill_required):
            return False
        if emp.category in shift_type.excluded_categories:
            return False
        if not is_available(emp.id, day, shift_type):
            return False
        if any((skill, weekday) in skill_block_set for skill in emp.skills):
            return False
        if (emp.category, weekday) in category_block_set:
            return False
        return True

    model = cp_model.CpModel()
    x = {}  # (employee_id, shift_type_id, day) -> BoolVar
    objective_terms = []
    weekend_role_vars_by_employee = {e.id: [] for e in employees}

    def get_x(emp_id, st_id, day):
        return x.get((emp_id, st_id, day))

    def new_x(emp, st_id, day):
        var = model.NewBoolVar(f"x_e{emp.id}_s{st_id}_d{day}")
        x[(emp.id, st_id, day)] = var
        return var

    # ---------------------------------------------------------- ruoli weekend
    # Un turno e' "coperto dal weekend" solo nel giorno specifico (SAB o DOM)
    # in cui compare in un ruolo: es. GN il sabato e R2N la domenica sono due
    # turni diversi, e ciascuno resta soggetto al fabbisogno ordinario
    # nell'altro giorno del weekend.
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

    role_slots = []  # (role_code, sab_day, dom_day, role_vars)
    for sab_day, dom_day in weekends:
        for role_code, role_entries in sorted(roles_by_code.items()):
            applicable = [r for r in role_entries if (r.day == "SAB") or (r.day == "DOM" and dom_day)]
            if not applicable:
                continue

            entries = []  # (shift_type, day, weekday, skill_formula)
            for r in applicable:
                day_ = sab_day if r.day == "SAB" else dom_day
                weekday_ = weekday_of(day_)
                shift_type = shift_types_by_id[r.shift_type_id]
                skill_formula = r.skill_required or shift_type.skill_required
                entries.append((shift_type, day_, weekday_, skill_formula))

            eligible_ids = None
            for shift_type, day_, weekday_, skill_formula in entries:
                day_ids = {e.id for e in employees if eligible(e, shift_type, day_, weekday_, skill_formula)}
                eligible_ids = day_ids if eligible_ids is None else (eligible_ids & day_ids)
            eligible_ids = set(eligible_ids or set())
            pinned_ids = set()
            for shift_type, day_, _weekday_, _ in entries:
                pinned_ids |= pinned_by_shift_day.get((shift_type.id, day_), set())
            eligible_ids |= pinned_ids

            if not eligible_ids:
                warnings.append(
                    f"Weekend {sab_day}-{dom_day or sab_day}: nessun candidato disponibile per il ruolo {role_code}."
                )
                continue

            first_st, first_day, first_weekday_, _ = entries[0]
            role_vars = []
            for emp_id in eligible_ids:
                emp = employees_by_id[emp_id]
                y = new_x(emp, first_st.id, first_day)
                role_vars.append(y)
                weekend_role_vars_by_employee[emp_id].append(y)
                for shift_type, day_, weekday_, _ in entries[1:]:
                    x_other = new_x(emp, shift_type.id, day_)
                    model.Add(x_other == y)
                if emp_id in pinned_ids:
                    model.Add(y == 1)

            model.Add(sum(role_vars) <= max(1, len(pinned_ids)))
            objective_terms.append(SHORTFALL_PENALTY * (1 - sum(role_vars)))
            role_slots.append((role_code, sab_day, dom_day, role_vars))

    # ---------------------------------------------------- turni a blocco settimanale
    # I giorni della settimana coperti dipendono dal 'days_set' del singolo turno
    # (filtrato piu' sotto): un turno senza days_set copre tutti i 7 giorni, uno con
    # days_set sui soli feriali (es. corsia) resta automaticamente lun-ven.
    weekly_types = [st for st in shift_types if st.weekly_block]
    weeks = {}
    for day in range(1, num_days + 1):
        weeks.setdefault(week_index(day), []).append(day)

    block_slots = []  # (shift_type, valid_days, required, eligible_ids)
    block_group_owner_vars = {}  # block_group -> [(first_day, employee_id, y_var), ...]
    for wk, days_in_week in sorted(weeks.items()):
        for shift_type in weekly_types:
            if shift_type.days_set:
                valid_days = [dd for dd in days_in_week if weekday_of(dd) in shift_type.days_set]
            else:
                valid_days = list(days_in_week)
            if not valid_days:
                continue
            if any(is_suppressed(shift_type.id, dd) for dd in valid_days):
                warnings.append(
                    f"Settimana giorno {valid_days[0]}: nessun candidato per l'intera settimana del turno "
                    f"'{shift_type.name}' (soppresso in almeno un giorno della settimana)."
                )
                continue

            eligible_ids = None
            for dd in valid_days:
                weekday_ = weekday_of(dd)
                skill_override = shift_type.skill_by_weekday.get(weekday_)
                day_ids = {e.id for e in employees if eligible(e, shift_type, dd, weekday_, skill_override)}
                eligible_ids = day_ids if eligible_ids is None else (eligible_ids & day_ids)
            eligible_ids = set(eligible_ids or set())
            pinned_ids_this_week = set()
            for dd in valid_days:
                pinned_ids_this_week |= pinned_by_shift_day.get((shift_type.id, dd), set())
            eligible_ids |= pinned_ids_this_week

            if not eligible_ids:
                warnings.append(
                    f"Settimana giorno {valid_days[0]}: nessun candidato per l'intera settimana del turno '{shift_type.name}'."
                )
                continue

            first_day = valid_days[0]
            required = max([1] + [shift_type.requirements_by_weekday.get(weekday_of(dd), 0) for dd in valid_days])
            capacity = max(required, len(pinned_ids_this_week))
            day_vars_by_day = {dd: [] for dd in valid_days}
            for emp_id in eligible_ids:
                emp = employees_by_id[emp_id]
                pinned_days_here = {dd for dd in valid_days if emp_id in pinned_by_shift_day.get((shift_type.id, dd), set())}
                y = new_x(emp, shift_type.id, first_day)
                day_vars_by_day[first_day].append(y)
                if shift_type.block_group:
                    block_group_owner_vars.setdefault(shift_type.block_group, []).append((first_day, emp_id, y))
                if first_day in pinned_days_here:
                    model.Add(y == 1)
                for dd in valid_days[1:]:
                    x_other = new_x(emp, shift_type.id, dd)
                    day_vars_by_day[dd].append(x_other)
                    if dd in pinned_days_here:
                        model.Add(x_other == 1)
                    if shift_type.weekly_block_strictness >= 10:
                        model.Add(x_other == y)
                    else:
                        mismatch = model.NewBoolVar(f"blockdev_e{emp.id}_s{shift_type.id}_d{dd}")
                        model.Add(mismatch >= y - x_other)
                        model.Add(mismatch >= x_other - y)
                        weight = BLOCK_DEVIATION_UNIT_WEIGHT * shift_type.weekly_block_strictness
                        if weight > 0:
                            objective_terms.append(weight * mismatch)

            # fino a 'required' titolari distinti possono "possedere" la settimana in
            # parallelo (es. 2 posizioni di corsia); a rigore massimo (10) restano
            # identici tutta la settimana (x_other == y sopra), quindi il vincolo di
            # copertura sotto e' automaticamente lo stesso per ogni giorno. A rigore
            # parziale, invece, un titolare puo' deviare (con penalita') senza che
            # nessuno lo sostituisca: senza un vincolo/penalita' *per ogni giorno*
            # la scopertura dei giorni diversi dal primo passerebbe inosservata anche
            # se qualcun altro del pool idoneo potrebbe coprirla. Percio' il vincolo
            # 'sum <= required' e la penalita' di scopertura si applicano ad ogni
            # giorno della settimana, non solo al primo.
            for dd in valid_days:
                day_vars = day_vars_by_day[dd]
                model.Add(sum(day_vars) <= capacity)
                objective_terms.append(SHORTFALL_PENALTY * (required - sum(day_vars)))
            block_slots.append((shift_type, valid_days, required, eligible_ids))

    # raffreddamento tra settimane per 'block_group': chi possiede una settimana
    # su un turno del gruppo (es. Corsia A) non puo' possederne un'altra (nello
    # stesso turno o in un altro dello stesso gruppo, es. Corsia B) prima che
    # passino le settimane configurate. Se il rigore del blocco e' < 10, i
    # singoli giorni restano comunque copribili da qualcun altro del pool
    # idoneo (vedi vincolo di copertura sopra), quindi il raffreddamento non
    # lascia scoperture: sposta solo *chi* puo' possedere l'intera settimana.
    block_group_cooldown_weeks = {}
    for st in weekly_types:
        if st.block_group and st.block_cooldown_weeks > 0:
            block_group_cooldown_weeks[st.block_group] = max(
                block_group_cooldown_weeks.get(st.block_group, 0), st.block_cooldown_weeks
            )

    for group, cooldown_weeks in block_group_cooldown_weeks.items():
        min_gap_days_between_weeks = cooldown_weeks * 7
        owner_vars_by_employee = {}
        for first_day, emp_id, y in block_group_owner_vars.get(group, []):
            owner_vars_by_employee.setdefault(emp_id, []).append((first_day, y))

        for emp_id, week_owner_vars in owner_vars_by_employee.items():
            week_owner_vars.sort(key=lambda item: item[0])
            for i in range(len(week_owner_vars)):
                d1, y1 = week_owner_vars[i]
                for j in range(i + 1, len(week_owner_vars)):
                    d2, y2 = week_owner_vars[j]
                    if d2 - d1 >= min_gap_days_between_weeks:
                        break
                    model.Add(y1 + y2 <= 1)

            prev_last_day = prev_block_group_last_day.get(group, {}).get(emp_id)
            if prev_last_day is not None:
                for first_day, y in week_owner_vars:
                    if first_day - prev_last_day < min_gap_days_between_weeks:
                        model.Add(y == 0)

    # ------------------------------------------------------- turni giornalieri ordinari
    ordinary_types = [st for st in shift_types if not st.weekly_block and not st.is_extra]
    ordinary_slots = []  # (shift_type, day, weekday, required)

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

            skill_override = shift_type.skill_by_weekday.get(weekday)
            pinned_ids = pinned_by_shift_day.get((shift_type.id, day), set())
            candidates = [
                e for e in employees
                if eligible(e, shift_type, day, weekday, skill_override) or e.id in pinned_ids
            ]
            capacity = max(required, len(pinned_ids))
            slot_vars = []
            for e in candidates:
                v = new_x(e, shift_type.id, day)
                slot_vars.append(v)
                if e.id in pinned_ids:
                    model.Add(v == 1)
            if slot_vars:
                model.Add(sum(slot_vars) <= capacity)
            ordinary_slots.append((shift_type, day, weekday, required, slot_vars))
            objective_terms.append(SHORTFALL_PENALTY * (required - sum(slot_vars)))

    # ------------------------------------------------------------------- turni extra
    extra_slots = []  # (shift_type, day, weekday, slot_vars)
    for day, active_ids in sorted(extra_activations.items()):
        weekday = weekday_of(day)
        for shift_type_id in active_ids:
            shift_type = shift_types_by_id.get(shift_type_id)
            if shift_type is None:
                continue
            skill_override = shift_type.skill_by_weekday.get(weekday)
            pinned_ids = pinned_by_shift_day.get((shift_type.id, day), set())
            candidates = [
                e for e in employees
                if eligible(e, shift_type, day, weekday, skill_override) or e.id in pinned_ids
            ]
            capacity = max(1, len(pinned_ids))
            slot_vars = []
            for e in candidates:
                v = new_x(e, shift_type.id, day)
                slot_vars.append(v)
                if e.id in pinned_ids:
                    model.Add(v == 1)
            if slot_vars:
                model.Add(sum(slot_vars) <= capacity)
            extra_slots.append((shift_type, day, weekday, slot_vars))
            objective_terms.append(SHORTFALL_PENALTY * (1 - sum(slot_vars)))

    # ------------------------------------------------- vincoli trasversali per persona/giorno
    by_day = {}
    for (emp_id, st_id, day), var in x.items():
        by_day.setdefault((emp_id, day), []).append((st_id, var))

    conflicts = _shift_type_conflicts(shift_types)
    for (emp_id, day), items in by_day.items():
        for i in range(len(items)):
            st1_id, v1 = items[i]
            for j in range(i + 1, len(items)):
                st2_id, v2 = items[j]
                pair = (st1_id, st2_id) if st1_id < st2_id else (st2_id, st1_id)
                if pair in conflicts:
                    model.Add(v1 + v2 <= 1)

    # smonto: turno che richiede riposo il giorno dopo, con eccezione sab->dom
    for (emp_id, st1_id, day), v1 in list(x.items()):
        st1 = shift_types_by_id[st1_id]
        if not st1.requires_rest_next_day or day >= num_days:
            continue
        weekday1 = weekday_of(day)
        weekday2 = weekday_of(day + 1)
        for st2_id in shift_types_by_id:
            if weekday1 == SATURDAY and weekday2 == SUNDAY and st1.rest_exception_shift_type_id == st2_id:
                continue
            v2 = get_x(emp_id, st2_id, day + 1)
            if v2 is not None:
                model.Add(v1 + v2 <= 1)

    # smonto in eredita' dal mese precedente (giorno 1)
    prev_last_weekday = _prev_month_last_weekday(year, month)
    for emp in employees:
        for sid in prev_month_last_shifts.get(emp.id, []):
            prev_shift = shift_types_by_id.get(sid)
            if prev_shift is None or not prev_shift.requires_rest_next_day:
                continue
            for st2_id in shift_types_by_id:
                if (
                    prev_last_weekday == SATURDAY
                    and weekday_of(1) == SUNDAY
                    and prev_shift.rest_exception_shift_type_id == st2_id
                ):
                    continue
                v2 = get_x(emp.id, st2_id, 1)
                if v2 is not None:
                    model.Add(v2 == 0)

    # distanza minima in giorni tra due occorrenze dello stesso turno
    for shift_type in shift_types:
        if shift_type.min_gap_days <= 0:
            continue
        days_with_var = sorted({day for (eid, sid, day) in x if sid == shift_type.id})
        for emp in employees:
            emp_days = [dd for dd in days_with_var if get_x(emp.id, shift_type.id, dd) is not None]
            for i in range(len(emp_days)):
                for j in range(i + 1, len(emp_days)):
                    d1, d2 = emp_days[i], emp_days[j]
                    if d2 - d1 > shift_type.min_gap_days:
                        break
                    if (
                        shift_type.min_gap_excludes_weekend
                        and weekday_of(d1) in (SATURDAY, SUNDAY)
                        and weekday_of(d2) in (SATURDAY, SUNDAY)
                    ):
                        continue
                    v1 = get_x(emp.id, shift_type.id, d1)
                    v2 = get_x(emp.id, shift_type.id, d2)
                    model.Add(v1 + v2 <= 1)

    # "lavora quel giorno?" per ogni dipendente/giorno: serve sia per le
    # regole di riserva legate a una skill, sia per il limite di giorni
    # lavorativi consecutivi (vedi sotto).
    worked = {}
    for emp in employees:
        for day in range(1, num_days + 1):
            day_vars = [v for (eid, sid, dd), v in x.items() if eid == emp.id and dd == day]
            if not day_vars:
                continue
            w = model.NewBoolVar(f"worked_e{emp.id}_d{day}")
            model.AddMaxEquality(w, day_vars)
            worked[(emp.id, day)] = w

    # giorni lavorativi consecutivi massimi: vincolo rigido su ogni finestra
    # scorrevole di (max+1) giorni, compreso il giorno prima dell'inizio del
    # mese (eredita' dal mese precedente).
    if max_consecutive_work_days and max_consecutive_work_days > 0:
        window = max_consecutive_work_days + 1
        for emp in employees:
            day0 = 1 if prev_month_last_shifts.get(emp.id) else 0
            for start in range(1 - (window - 1), num_days - window + 2):
                days_in_window = range(start, start + window)
                terms = []
                for dd in days_in_window:
                    if dd < 1:
                        terms.append(day0)
                    elif (emp.id, dd) in worked:
                        terms.append(worked[(emp.id, dd)])
                if terms:
                    model.Add(sum(terms) <= max_consecutive_work_days)

    if skill_reserve_rules:
        for rule in skill_reserve_rules:
            pool = [e for e in employees if rule.skill in e.skills]
            if len(pool) <= rule.min_free:
                continue
            for day in range(1, num_days + 1):
                if weekday_of(day) != rule.weekday:
                    continue
                pool_vars = [worked[(e.id, day)] for e in pool if (e.id, day) in worked]
                if pool_vars:
                    model.Add(sum(pool_vars) <= len(pool) - rule.min_free)

    # ------------------------------------------------------------------- obiettivo
    total_per_employee = {}
    for emp in employees:
        emp_vars = [v for (eid, sid, dd), v in x.items() if eid == emp.id]
        total = sum(emp_vars) if emp_vars else 0
        total_per_employee[emp.id] = total

    if employees:
        max_total = model.NewIntVar(0, num_days * max(len(shift_types), 1), "max_total")
        min_total = model.NewIntVar(0, num_days * max(len(shift_types), 1), "min_total")
        model.AddMaxEquality(max_total, list(total_per_employee.values()))
        model.AddMinEquality(min_total, list(total_per_employee.values()))
        objective_terms.append(FAIRNESS_WEIGHT * (max_total - min_total))

        weekend_total_vars = []
        for emp in employees:
            base = prev_month_weekend_count.get(emp.id, 0)
            role_vars = weekend_role_vars_by_employee[emp.id]
            weekend_total_vars.append(base + (sum(role_vars) if role_vars else 0))
        max_weekend = model.NewIntVar(0, len(weekends) + 10, "max_weekend")
        min_weekend = model.NewIntVar(0, len(weekends) + 10, "min_weekend")
        model.AddMaxEquality(max_weekend, weekend_total_vars)
        model.AddMinEquality(min_weekend, weekend_total_vars)
        objective_terms.append(WEEKEND_FAIRNESS_WEIGHT * (max_weekend - min_weekend))

    # preferenze pesate: EVITA/PREFERISCI/RISERVA agiscono come costo diretto
    # sull'assegnazione; MAX_MESE penalizza il superamento di una soglia
    # mensile, senza mai vietarlo del tutto (e' un limite "morbido").
    maxmese_overflow_cache = {}
    for p in preferences:
        target_employees = [e for e in employees if p.employee_id is None or p.employee_id == e.id]
        target_shift_types = [
            st for st in shift_types
            if (p.shift_type_id is None or p.shift_type_id == st.id)
            and (not p.time_bands or (st.time_bands & p.time_bands))
        ]

        for emp in target_employees:
            for st in target_shift_types:
                relevant_days = [
                    dd for (eid, sid, dd) in x if eid == emp.id and sid == st.id
                    and (not p.days_set or weekday_of(dd) in p.days_set)
                ]
                if not relevant_days:
                    continue
                day_vars = [x[(emp.id, st.id, dd)] for dd in relevant_days]

                if p.pref_type == "EVITA":
                    for v in day_vars:
                        objective_terms.append(p.weight * v)
                elif p.pref_type == "PREFERISCI":
                    for v in day_vars:
                        objective_terms.append(-p.weight * v)
                elif p.pref_type == "RISERVA":
                    for v in day_vars:
                        objective_terms.append(p.weight * 500 * v)
                elif p.pref_type == "MAX_MESE" and p.weight > 0:
                    key = (emp.id, st.id)
                    if key not in maxmese_overflow_cache:
                        count_expr = sum(day_vars)
                        overflow = model.NewIntVar(0, num_days, f"overflow_e{emp.id}_s{st.id}")
                        model.Add(overflow >= count_expr - p.weight)
                        maxmese_overflow_cache[key] = overflow
                        objective_terms.append(MAX_MESE_PENALTY * overflow)

    model.Minimize(sum(objective_terms) if objective_terms else 0)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = SOLVER_TIME_LIMIT_SECONDS
    solver.parameters.num_search_workers = 8
    status = solver.Solve(model)

    assignments = []
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        for (emp_id, st_id, day), var in x.items():
            if solver.Value(var):
                assignments.append({"employee_id": emp_id, "shift_type_id": st_id, "day": day})
    else:
        warnings.append(
            "Il risolutore non ha trovato nessuna soluzione (anche parziale): controlla la configurazione."
        )

    # ------------------------------------------------------------ avvisi di copertura
    solved_ok = status in (cp_model.OPTIMAL, cp_model.FEASIBLE)

    for role_code, sab_day, dom_day, role_vars in role_slots:
        actual = sum(solver.Value(v) for v in role_vars) if solved_ok else 0
        if actual < 1:
            warnings.append(
                f"Weekend {sab_day}-{dom_day or sab_day}: ruolo {role_code} non assegnato a nessuno "
                f"(verifica se i turni del ruolo sono incompatibili tra loro, es. uno 'esclusivo', oppure se "
                f"manca personale disponibile per l'intero weekend)."
            )

    for shift_type, valid_days, required, eligible_ids in block_slots:
        for dd in valid_days:
            actual = (
                sum(solver.Value(x[(emp_id, shift_type.id, dd)]) for emp_id in eligible_ids)
                if solved_ok else 0
            )
            if actual < required:
                warnings.append(
                    f"Giorno {dd} ({WEEKDAY_NAMES[weekday_of(dd)]}): turno '{shift_type.name}' "
                    f"(blocco settimanale) coperto {actual}/{required} persone."
                )

    for shift_type, day, weekday, required, slot_vars in ordinary_slots:
        actual = sum(solver.Value(v) for v in slot_vars) if solved_ok else 0
        if actual < required:
            warnings.append(
                f"Giorno {day} ({WEEKDAY_NAMES[weekday]}): turno '{shift_type.name}' coperto {actual}/{required} "
                f"persone per mancanza di personale disponibile/qualificato."
            )

    for shift_type, day, weekday, slot_vars in extra_slots:
        actual = sum(solver.Value(v) for v in slot_vars) if solved_ok else 0
        if actual < 1 and slot_vars:
            warnings.append(
                f"Giorno {day} ({WEEKDAY_NAMES[weekday]}): turno extra '{shift_type.name}' senza candidati disponibili."
            )
        elif not slot_vars:
            warnings.append(
                f"Giorno {day} ({WEEKDAY_NAMES[weekday]}): turno extra '{shift_type.name}' senza candidati disponibili."
            )

    # ----------------------------------------------------- giorni consecutivi (avviso)
    assigned_day_of = {}
    for a in assignments:
        assigned_day_of.setdefault(a["employee_id"], {}).setdefault(a["day"], []).append(a["shift_type_id"])

    for emp in employees:
        run = 1 if prev_month_last_shifts.get(emp.id) else 0
        for day in range(1, num_days + 1):
            worked = bool(assigned_day_of.get(emp.id, {}).get(day))
            if worked:
                run += 1
                if run == max_consecutive_work_days + 1:
                    warnings.append(
                        f"{emp.name}: supera {max_consecutive_work_days} giorni lavorativi consecutivi "
                        f"(dal giorno {day - max_consecutive_work_days})."
                    )
            else:
                run = 0

    return ScheduleResult(assignments=assignments, warnings=warnings)
