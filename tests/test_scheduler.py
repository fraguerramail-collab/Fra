from calendar import monthrange

from app.scheduler import (
    EmployeeInput,
    PreferenceInput,
    ShiftTypeInput,
    SkillRuleInput,
    WeekendRoleInput,
    generate_schedule,
)


def emp(id_, name, category="", skills=None):
    return EmployeeInput(id=id_, name=name, category=category, skills=skills or set())


def shift(id_, name, **kwargs):
    kwargs.setdefault("requirements_by_weekday", {wd: 1 for wd in range(7)})
    return ShiftTypeInput(id=id_, name=name, **kwargs)


def run(year=2026, month=1, **kwargs):
    kwargs.setdefault("weekend_roles", [])
    kwargs.setdefault("preferences", [])
    kwargs.setdefault("availability", {})
    kwargs.setdefault("suppressions", set())
    kwargs.setdefault("extra_activations", {})
    return generate_schedule(year=year, month=month, **kwargs)


def test_skill_matching_excludes_unqualified():
    employees = [emp(1, "A", skills={"PR"}), emp(2, "B", skills={"SR"})]
    shift_types = [shift(1, "Notte", skill_required="SR")]

    result = run(employees=employees, shift_types=shift_types)

    used = {a["employee_id"] for a in result.assignments}
    assert used == {2}


def test_exclusive_day_blocks_second_shift_same_day():
    employees = [emp(1, "A", skills={"X"})]
    exclusive = shift(1, "Notte", skill_required="X", exclusive_day=True, priority=1)
    other = shift(2, "Giorno", skill_required="X", priority=2)

    result = run(employees=employees, shift_types=[exclusive, other])

    by_day = {}
    for a in result.assignments:
        by_day.setdefault(a["day"], set()).add(a["shift_type_id"])
    for day, ids in by_day.items():
        assert not (1 in ids and 2 in ids)


def test_min_gap_days_spacing():
    employees = [emp(1, "A", skills={"X"}), emp(2, "B", skills={"X"})]
    shift_types = [shift(1, "Notte", skill_required="X", min_gap_days=2)]

    result = run(employees=employees, shift_types=shift_types)

    days_by_employee = {}
    for a in result.assignments:
        days_by_employee.setdefault(a["employee_id"], []).append(a["day"])

    for days in days_by_employee.values():
        days.sort()
        for a, b in zip(days, days[1:]):
            assert b - a > 2


def test_requires_rest_next_day_blocked_without_exception():
    employees = [emp(1, "A", skills={"X"})]
    night = shift(1, "Notte", skill_required="X", requires_rest_next_day=True, exclusive_day=True, priority=1)
    day_shift = shift(2, "Giorno", skill_required="X", priority=2)

    result = run(employees=employees, shift_types=[night, day_shift])

    night_days = {a["day"] for a in result.assignments if a["shift_type_id"] == 1}
    day_days = {a["day"] for a in result.assignments if a["shift_type_id"] == 2}
    for d in night_days:
        assert d + 1 not in day_days


def test_requires_rest_next_day_exception_saturday_sunday():
    # 2026-01-01 e' giovedi': il primo sabato e' il 3, domenica il 4.
    employees = [emp(1, "A", skills={"X"})]
    night = shift(
        1, "Notte", skill_required="X", requires_rest_next_day=True, exclusive_day=True,
        rest_exception_shift_type_id=2, priority=1,
    )
    sunday_shift = shift(2, "Reperibilita", skill_required="X", priority=2)

    weekend_roles = [
        WeekendRoleInput(role_code="A", day="SAB", shift_type_id=1, skill_required="X"),
        WeekendRoleInput(role_code="A", day="DOM", shift_type_id=2, skill_required="X"),
    ]

    result = run(employees=employees, shift_types=[night, sunday_shift], weekend_roles=weekend_roles)

    assignments_by_shift_day = {(a["shift_type_id"], a["day"]) for a in result.assignments}
    assert (1, 3) in assignments_by_shift_day
    assert (2, 4) in assignments_by_shift_day


def test_weekend_role_same_day_shifts_both_assigned_when_not_exclusive():
    # Un ruolo puo' legittimamente assegnare due turni diversi lo stesso
    # giorno alla stessa persona (es. Guardia Notte + Reperibilita Urgenza
    # la domenica), purche' nessuno dei due sia "esclusivo".
    employees = [emp(1, "A", skills={"X"})]
    night = shift(1, "Notte", skill_required="X", priority=1, requirements_by_weekday={})
    urgenza = shift(2, "Urgenza", skill_required="X", priority=2, requirements_by_weekday={})

    weekend_roles = [
        WeekendRoleInput(role_code="B", day="DOM", shift_type_id=1, skill_required="X"),
        WeekendRoleInput(role_code="B", day="DOM", shift_type_id=2, skill_required="X"),
    ]

    result = run(employees=employees, shift_types=[night, urgenza], weekend_roles=weekend_roles)

    assignments_by_shift_day = {(a["shift_type_id"], a["day"]) for a in result.assignments}
    assert (1, 4) in assignments_by_shift_day
    assert (2, 4) in assignments_by_shift_day
    assert not result.warnings


def test_weekend_role_internal_conflict_warns_instead_of_silently_dropping():
    # Se un ruolo unisce due turni incompatibili lo stesso giorno (uno
    # "esclusivo"), il secondo non puo' essere assegnato: deve comparire un
    # avviso esplicito invece di sparire senza spiegazione.
    employees = [emp(1, "A", skills={"X"})]
    night = shift(1, "Notte", skill_required="X", exclusive_day=True, priority=1, requirements_by_weekday={})
    urgenza = shift(2, "Urgenza", skill_required="X", priority=2, requirements_by_weekday={})

    weekend_roles = [
        WeekendRoleInput(role_code="B", day="DOM", shift_type_id=1, skill_required="X"),
        WeekendRoleInput(role_code="B", day="DOM", shift_type_id=2, skill_required="X"),
    ]

    result = run(employees=employees, shift_types=[night, urgenza], weekend_roles=weekend_roles)

    assignments_by_shift_day = {(a["shift_type_id"], a["day"]) for a in result.assignments}
    assert (1, 4) in assignments_by_shift_day
    assert (2, 4) not in assignments_by_shift_day
    assert any("conflitto" in w for w in result.warnings)


def test_weekend_role_only_blocks_its_own_day_not_the_whole_weekend():
    # GN e' nel ruolo weekend solo di sabato, "Reperibilita" solo di domenica:
    # GN deve restare assegnabile ordinariamente la domenica, e "Reperibilita"
    # deve restare assegnabile ordinariamente il sabato (2026-01-03 = sabato).
    employees = [emp(1, "A", skills={"X"}), emp(2, "B", skills={"X"})]
    night = shift(1, "Notte", skill_required="X", priority=1)
    reperibilita = shift(2, "Reperibilita", skill_required="X", priority=2)

    weekend_roles = [
        WeekendRoleInput(role_code="A", day="SAB", shift_type_id=1, skill_required="X"),
        WeekendRoleInput(role_code="A", day="DOM", shift_type_id=2, skill_required="X"),
    ]

    result = run(employees=employees, shift_types=[night, reperibilita], weekend_roles=weekend_roles)

    assignments_by_shift_day = {(a["shift_type_id"], a["day"]) for a in result.assignments}
    assert (1, 4) in assignments_by_shift_day  # Notte anche la domenica (giorno 4)
    assert (2, 3) in assignments_by_shift_day  # Reperibilita anche il sabato (giorno 3)


def test_weekly_block_same_employee_all_week():
    employees = [emp(1, "A", skills=set()), emp(2, "B", skills=set())]
    corsia = ShiftTypeInput(
        id=1, name="Corsia", weekly_block=True, block_group="CORSIA",
        days_set={0, 1, 2, 3, 4}, requirements_by_weekday={},
    )

    result = run(employees=employees, shift_types=[corsia])

    first_weekday, _ = monthrange(2026, 1)
    by_week_employee = {}
    for a in result.assignments:
        week = (a["day"] - 1 + first_weekday) // 7
        by_week_employee.setdefault(week, set()).add(a["employee_id"])

    for employees_in_week in by_week_employee.values():
        assert len(employees_in_week) == 1


def test_extra_shift_only_assigned_when_activated():
    employees = [emp(1, "A", skills={"X"})]
    extra = shift(1, "Extra", skill_required="X", is_extra=True, requirements_by_weekday={})

    result = run(employees=employees, shift_types=[extra], extra_activations={5: [1]})

    days = {a["day"] for a in result.assignments}
    assert days == {5}


def test_suppression_prevents_assignment():
    employees = [emp(1, "A", skills={"X"})]
    shift_types = [shift(1, "Notte", skill_required="X")]

    result = run(employees=employees, shift_types=shift_types, suppressions={(1, 10)})

    days = {a["day"] for a in result.assignments}
    assert 10 not in days


def test_excluded_category_cannot_be_assigned():
    employees = [emp(1, "SP", category="SP", skills={"X"}), emp(2, "Reg", category="STD", skills={"X"})]
    shift_types = [shift(1, "Weekend", skill_required="X", excluded_categories={"SP"})]

    result = run(employees=employees, shift_types=shift_types)

    used = {a["employee_id"] for a in result.assignments}
    assert 1 not in used


def test_prev_month_last_shift_triggers_rest_on_day_one():
    employees = [emp(1, "A", skills={"X"})]
    night = shift(1, "Notte", skill_required="X", requires_rest_next_day=True, exclusive_day=True, priority=1)
    day_shift = shift(2, "Giorno", skill_required="X", priority=2)

    result = run(
        employees=employees, shift_types=[night, day_shift],
        prev_month_last_shifts={1: [1]},
    )

    day_one_shifts = {a["shift_type_id"] for a in result.assignments if a["day"] == 1}
    assert 2 not in day_one_shifts


def test_max_consecutive_days_generates_warning():
    employees = [emp(1, "A", skills={"X"})]
    shift_types = [shift(1, "Giorno", skill_required="X")]

    result = run(employees=employees, shift_types=shift_types, max_consecutive_work_days=3)

    assert any("giorni lavorativi consecutivi" in w for w in result.warnings)


def test_preference_max_mese_is_soft_not_hard():
    employees = [emp(1, "Solo", skills={"X"})]
    shift_types = [shift(1, "Turno", skill_required="X")]
    prefs = [PreferenceInput(employee_id=1, shift_type_id=1, days_set=set(), pref_type="MAX_MESE", weight=2)]

    result = run(employees=employees, shift_types=shift_types, preferences=prefs)

    # unico candidato disponibile: la regola MAX_MESE penalizza ma non blocca
    assert len(result.assignments) == 31


def test_skill_rule_block_excludes_from_every_shift_that_weekday():
    # 2026-01-05 e' lunedi'. Chi ha skill BREAST non deve comparire in
    # NESSUN turno di lunedi', anche se qualificato e disponibile.
    breast_holder = emp(1, "Breast1", skills={"BREAST", "X"})
    other = emp(2, "Altro", skills={"X"})
    shift_types = [shift(1, "Turno", skill_required="X")]
    skill_rules = [SkillRuleInput(skill="BREAST", weekday=0, mode="BLOCK")]

    result = run(employees=[breast_holder, other], shift_types=shift_types, skill_rules=skill_rules)

    monday_assignees = {a["employee_id"] for a in result.assignments if a["day"] == 5}
    assert 1 not in monday_assignees
    assert 2 in monday_assignees


def test_skill_rule_reserve_keeps_at_least_one_free():
    # 3 persone con skill BREAST; almeno 1 deve restare libera ogni martedi'.
    employees = [emp(i, f"B{i}", skills={"BREAST"}) for i in (1, 2, 3)]
    shift_types = [shift(1, "Turno", skill_required="BREAST", requirements_by_weekday={wd: 3 for wd in range(7)})]
    skill_rules = [SkillRuleInput(skill="BREAST", weekday=1, mode="RESERVE", min_free=1)]

    result = run(employees=employees, shift_types=shift_types, skill_rules=skill_rules)

    # 2026-01-06 e' martedi'
    tuesday_assignees = {a["employee_id"] for a in result.assignments if a["day"] == 6}
    assert len(tuesday_assignees) <= 2
    assert any(
        "candidati" in w or "coperto" in w for w in result.warnings
    )  # il fabbisogno di 3 non e' piu' copribile con la riserva attiva
