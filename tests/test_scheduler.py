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
    exclusive = shift(1, "Notte", skill_required="X", exclusive_day=True)
    other = shift(2, "Giorno", skill_required="X")

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


def test_min_gap_days_respects_previous_month_history():
    # Se e' stato fatto lo stesso turno l'ultimo giorno del mese precedente
    # (giorno "0"), la distanza minima deve valere anche a inizio mese
    # nuovo, non "dimenticarsi" tutto al cambio di mese.
    solo = emp(1, "Solo", skills={"X"})
    turno = shift(1, "Notte", skill_required="X", min_gap_days=3)

    result = run(
        employees=[solo], shift_types=[turno],
        prev_shift_last_day={1: {1: 0}},
    )

    early_days = [a["day"] for a in result.assignments if a["day"] <= 3]
    assert not early_days, f"non doveva essere assegnato nei primi 3 giorni: {early_days}"


def test_min_gap_excludes_weekend_allows_saturday_sunday_together():
    # La reperibilita' del weekend DEVE restare sulla stessa persona sabato+
    # domenica (un giorno di distanza), ma la distanza minima di 2 giorni
    # servirebbe solo a evitare ripetizioni ravvicinate nei giorni feriali:
    # con l'esclusione weekend attiva, il ruolo weekend resta coperto senza
    # conflitto invece di andare in scopertura.
    employees = [emp(1, "A", skills={"X"})]
    reperibilita = shift(
        1, "Reperibilita", skill_required="X", min_gap_days=2, min_gap_excludes_weekend=True,
        requirements_by_weekday={},
    )
    weekend_roles = [
        WeekendRoleInput(role_code="A", day="SAB", shift_type_id=1, skill_required="X"),
        WeekendRoleInput(role_code="A", day="DOM", shift_type_id=1, skill_required="X"),
    ]

    result = run(employees=employees, shift_types=[reperibilita], weekend_roles=weekend_roles)

    assignments_by_shift_day = {(a["shift_type_id"], a["day"]) for a in result.assignments}
    assert (1, 3) in assignments_by_shift_day  # sabato 2026-01-03
    assert (1, 4) in assignments_by_shift_day  # domenica 2026-01-04
    assert not result.warnings


def test_requires_rest_next_day_blocked_without_exception():
    employees = [emp(1, "A", skills={"X"})]
    night = shift(1, "Notte", skill_required="X", requires_rest_next_day=True, exclusive_day=True)
    day_shift = shift(2, "Giorno", skill_required="X")

    result = run(employees=employees, shift_types=[night, day_shift])

    night_days = {a["day"] for a in result.assignments if a["shift_type_id"] == 1}
    day_days = {a["day"] for a in result.assignments if a["shift_type_id"] == 2}
    for d in night_days:
        assert d + 1 not in day_days


def test_requires_rest_next_day_exception_saturday_sunday():
    # 2026-01-01 e' giovedi': il primo sabato e' il 3, domenica il 4. L'unica
    # domanda del mese e' il ruolo weekend stesso (nessun fabbisogno
    # ordinario altrove), cosi' l'unica soluzione a copertura piena e'
    # proprio quella che sfrutta l'eccezione smonto sab->dom.
    employees = [emp(1, "A", skills={"X"})]
    night = shift(
        1, "Notte", skill_required="X", requires_rest_next_day=True, exclusive_day=True,
        rest_exception_shift_type_id=2, requirements_by_weekday={},
    )
    sunday_shift = shift(2, "Reperibilita", skill_required="X", requirements_by_weekday={})

    weekend_roles = [
        WeekendRoleInput(role_code="A", day="SAB", shift_type_id=1, skill_required="X"),
        WeekendRoleInput(role_code="A", day="DOM", shift_type_id=2, skill_required="X"),
    ]

    result = run(employees=employees, shift_types=[night, sunday_shift], weekend_roles=weekend_roles)

    assignments_by_shift_day = {(a["shift_type_id"], a["day"]) for a in result.assignments}
    assert (1, 3) in assignments_by_shift_day
    assert (2, 4) in assignments_by_shift_day
    assert not result.warnings


def test_weekend_role_same_day_shifts_both_assigned_when_not_exclusive():
    # Un ruolo puo' legittimamente assegnare due turni diversi lo stesso
    # giorno alla stessa persona (es. Guardia Notte + Reperibilita Urgenza
    # la domenica), purche' nessuno dei due sia "esclusivo".
    employees = [emp(1, "A", skills={"X"})]
    night = shift(1, "Notte", skill_required="X", requirements_by_weekday={})
    urgenza = shift(2, "Urgenza", skill_required="X", requirements_by_weekday={})

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
    # "esclusivo"), non esiste nessuna persona che possa coprire l'intero
    # ruolo: il motore non assegna ne' l'uno ne' l'altro (un ruolo e' o
    # tutto coperto dalla stessa persona, o niente) e lo segnala con un
    # avviso esplicito invece di sparire senza spiegazione.
    employees = [emp(1, "A", skills={"X"})]
    night = shift(1, "Notte", skill_required="X", exclusive_day=True, requirements_by_weekday={})
    urgenza = shift(2, "Urgenza", skill_required="X", requirements_by_weekday={})

    weekend_roles = [
        WeekendRoleInput(role_code="B", day="DOM", shift_type_id=1, skill_required="X"),
        WeekendRoleInput(role_code="B", day="DOM", shift_type_id=2, skill_required="X"),
    ]

    result = run(employees=employees, shift_types=[night, urgenza], weekend_roles=weekend_roles)

    assignments_by_shift_day = {(a["shift_type_id"], a["day"]) for a in result.assignments}
    assert (1, 4) not in assignments_by_shift_day
    assert (2, 4) not in assignments_by_shift_day
    assert any("non assegnato" in w for w in result.warnings)


def test_weekend_role_only_blocks_its_own_day_not_the_whole_weekend():
    # GN e' nel ruolo weekend solo di sabato, "Reperibilita" solo di domenica:
    # GN deve restare assegnabile ordinariamente la domenica, e "Reperibilita"
    # deve restare assegnabile ordinariamente il sabato (2026-01-03 = sabato).
    employees = [emp(1, "A", skills={"X"}), emp(2, "B", skills={"X"})]
    night = shift(1, "Notte", skill_required="X")
    reperibilita = shift(2, "Reperibilita", skill_required="X")

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


def test_weekly_block_without_days_set_covers_the_whole_week_including_weekend():
    employees = [emp(1, "A", skills=set()), emp(2, "B", skills=set())]
    ria_fisso = ShiftTypeInput(
        id=1, name="Rianimazione (fisso)", weekly_block=True, requirements_by_weekday={},
    )  # nessun days_set -> tutti e 7 i giorni

    result = run(employees=employees, shift_types=[ria_fisso], max_consecutive_work_days=7)

    first_weekday, num_days = monthrange(2026, 1)
    assigned_days = {a["day"] for a in result.assignments}
    assert assigned_days == set(range(1, num_days + 1))  # coperto anche sab/dom, nessun buco

    by_week_employee = {}
    for a in result.assignments:
        week = (a["day"] - 1 + first_weekday) // 7
        by_week_employee.setdefault(week, set()).add(a["employee_id"])
    for employees_in_week in by_week_employee.values():
        assert len(employees_in_week) == 1  # stessa persona tutta la settimana, weekend compreso


def test_weekly_block_supports_more_than_one_concurrent_person():
    employees = [
        emp(1, "A", skills={"RIA"}), emp(2, "B", skills={"RIA"}),
        emp(3, "C", skills={"RIA"}), emp(4, "D", skills={"RIA"}),
    ]
    ria = ShiftTypeInput(
        id=1, name="Rianimazione 2 Mattina", skill_required="RIA", weekly_block=True,
        days_set={0, 1, 2, 3, 4}, requirements_by_weekday={d: 2 for d in range(5)},
    )

    result = run(employees=employees, shift_types=[ria])

    first_weekday, _ = monthrange(2026, 1)
    by_week_employees = {}
    by_day_count = {}
    for a in result.assignments:
        week = (a["day"] - 1 + first_weekday) // 7
        by_week_employees.setdefault(week, set()).add(a["employee_id"])
        by_day_count[a["day"]] = by_day_count.get(a["day"], 0) + 1

    # ogni settimana coperta da 2 titolari distinti, ed entrambi presenti ogni giorno feriale
    for employees_in_week in by_week_employees.values():
        assert len(employees_in_week) == 2
    for day, count in by_day_count.items():
        assert count == 2
    assert not result.warnings


def test_weekly_block_strictness_10_is_rigid_even_at_the_cost_of_a_shortfall():
    employees = [
        emp(1, "A", skills={"CORSIA", "URGENTE"}),
        emp(2, "B", skills=set()),
    ]
    corsia = shift(
        1, "Corsia", skill_required="CORSIA", weekly_block=True, weekly_block_strictness=10,
        exclusive_day=True, days_set={0, 1, 2, 3, 4}, requirements_by_weekday={},
    )
    urgente = shift(2, "Urgente", skill_required="URGENTE", requirements_by_weekday={})

    # Solo A ha le skill per entrambe: con il blocco rigido non puo' fare la
    # corsia (tutta la settimana) e anche Urgente lo stesso giorno, quindi
    # qualcosa resta scoperto, indipendentemente da cosa il risolutore scelga
    # di sacrificare.
    result = run(employees=employees, shift_types=[corsia, urgente], extra_activations={7: [2]})

    assert result.warnings


def test_weekly_block_strictness_0_frees_up_the_owner_for_another_shift():
    employees = [
        emp(1, "A", skills={"CORSIA", "URGENTE"}),
        emp(2, "B", skills=set()),
    ]
    corsia = shift(
        1, "Corsia", skill_required="CORSIA", weekly_block=True, weekly_block_strictness=0,
        exclusive_day=True, days_set={0, 1, 2, 3, 4}, requirements_by_weekday={},
    )
    urgente = shift(2, "Urgente", skill_required="URGENTE", requirements_by_weekday={})

    result = run(employees=employees, shift_types=[corsia, urgente], extra_activations={7: [2]})

    # a rigore 0 il costo di deviare e' zero: il risolutore copre sempre Urgente
    # (evita il forte SHORTFALL_PENALTY), senza pero' alcun obbligo di tenere A
    # sulla corsia negli altri giorni della settimana (nessuna preferenza in tal senso);
    # i giorni di corsia lasciati scoperti da questa liberta' devono comparire come avviso
    # (prima passavano inosservati: e' proprio il bug segnalato dall'utente).
    assignments_by_shift = {(a["shift_type_id"], a["day"]): a["employee_id"] for a in result.assignments}
    assert assignments_by_shift.get((2, 7)) == 1  # A copre Urgente il 7 gennaio (mercoledi')
    assert any("Corsia" in w and "blocco settimanale" in w for w in result.warnings)


def test_block_cooldown_prevents_owning_two_group_weeks_too_close():
    # MODA e MODB condividono lo stesso 'block_group': chi possiede una
    # settimana su uno dei due non puo' possederne un'altra (nemmeno
    # sull'altro turno del gruppo) prima di 3 settimane (21 giorni).
    employees = [emp(1, "A", skills=set()), emp(2, "B", skills=set())]
    moda = ShiftTypeInput(
        id=1, name="Corsia A", weekly_block=True, weekly_block_strictness=10,
        block_group="CORSIA", block_cooldown_weeks=3,
        days_set={0, 1, 2, 3, 4}, requirements_by_weekday={},
    )
    modb = ShiftTypeInput(
        id=2, name="Corsia B", weekly_block=True, weekly_block_strictness=10,
        block_group="CORSIA", block_cooldown_weeks=3,
        days_set={0, 1, 2, 3, 4}, requirements_by_weekday={},
    )

    result = run(employees=employees, shift_types=[moda, modb], year=2026, month=3)

    first_weekday, _ = monthrange(2026, 3)

    def week_index(day):
        return (day - 1 + first_weekday) // 7

    owned_weeks_by_employee = {}
    for a in result.assignments:
        wk = week_index(a["day"])
        owned_weeks_by_employee.setdefault(a["employee_id"], set()).add(wk)

    for emp_id, weeks in owned_weeks_by_employee.items():
        weeks_sorted = sorted(weeks)
        for w1, w2 in zip(weeks_sorted, weeks_sorted[1:]):
            assert (w2 - w1) * 7 >= 21, f"dipendente {emp_id}: settimane {w1} e {w2} troppo vicine"

    # sanity check: qualcosa e' stato comunque coperto (non e' tutto vuoto)
    assert result.assignments


def test_block_cooldown_respects_previous_month_history():
    # L'unico dipendente ha lavorato su CORSIA fino al giorno prima dell'1
    # del mese precedente (giorno "0"): con 3 settimane (21 giorni) di
    # raffreddamento non puo' possedere nessuna settimana che inizi prima
    # del giorno 21.
    solo = emp(1, "Solo", skills=set())
    moda = ShiftTypeInput(
        id=1, name="Corsia A", weekly_block=True, weekly_block_strictness=10,
        block_group="CORSIA", block_cooldown_weeks=3,
        days_set={0, 1, 2, 3, 4}, requirements_by_weekday={},
    )

    result = run(
        employees=[solo], shift_types=[moda],
        prev_block_group_last_day={"CORSIA": {1: 0}},
    )

    assert all(a["day"] >= 21 for a in result.assignments)


def test_skill_by_weekday_overrides_general_skill_on_that_day_only():
    employees = [
        emp(1, "A", skills={"SALA2"}),
        emp(2, "B", skills={"ORTO"}),
        emp(3, "C", skills={"VASC"}),
    ]
    sala2 = shift(
        1, "Sala 2", skill_required="SALA2", skill_by_weekday={0: "ORTO", 4: "VASC"},
        days_set={0, 1, 2, 3, 4}, requirements_by_weekday={d: 1 for d in range(5)},
    )

    result = run(employees=employees, shift_types=[sala2])
    assignments_by_day = {a["day"]: a["employee_id"] for a in result.assignments}

    # gennaio 2026: 5=lunedi', 6=martedi', 9=venerdi'
    assert assignments_by_day.get(5) == 2  # lunedi': serve ORTO -> solo B
    assert assignments_by_day.get(6) == 1  # martedi': skill normale SALA2 -> solo A
    assert assignments_by_day.get(9) == 3  # venerdi': serve VASC -> solo C
    assert not result.warnings


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
    night = shift(1, "Notte", skill_required="X", requires_rest_next_day=True, exclusive_day=True)
    day_shift = shift(2, "Giorno", skill_required="X")

    result = run(
        employees=employees, shift_types=[night, day_shift],
        prev_month_last_shifts={1: [1]},
    )

    day_one_shifts = {a["shift_type_id"] for a in result.assignments if a["day"] == 1}
    assert 2 not in day_one_shifts


def test_max_consecutive_days_is_a_hard_limit():
    # Unico candidato per un turno richiesto tutti i giorni: il limite di
    # giorni consecutivi e' un vincolo rigido, quindi il motore lo rispetta
    # sempre (si ferma dopo 3 giorni di fila e lascia scoperti gli altri,
    # invece di sforare e limitarsi a un avviso).
    employees = [emp(1, "A", skills={"X"})]
    shift_types = [shift(1, "Giorno", skill_required="X")]

    result = run(employees=employees, shift_types=shift_types, max_consecutive_work_days=3)

    assigned_days = sorted(a["day"] for a in result.assignments)
    run_length = 0
    for i, d in enumerate(assigned_days):
        run_length = run_length + 1 if i > 0 and assigned_days[i - 1] == d - 1 else 1
        assert run_length <= 3
    assert not any("supera" in w and "consecutivi" in w for w in result.warnings)
    assert any("coperto" in w for w in result.warnings)


def test_preference_max_mese_is_soft_not_hard():
    employees = [emp(1, "Solo", skills={"X"})]
    shift_types = [shift(1, "Turno", skill_required="X")]
    prefs = [PreferenceInput(employee_id=1, shift_type_id=1, days_set=set(), pref_type="MAX_MESE", weight=2)]

    # max_consecutive_work_days alto per isolare il comportamento di MAX_MESE
    # dal limite (ora rigido) dei giorni lavorativi consecutivi.
    result = run(employees=employees, shift_types=shift_types, preferences=prefs, max_consecutive_work_days=31)

    # unico candidato disponibile: la regola MAX_MESE penalizza ma non blocca
    assert len(result.assignments) == 31


def test_preference_time_band_applies_to_every_shift_in_that_band_not_just_one():
    # niente shift_type_id: la preferenza deve valere per QUALSIASI turno
    # nella fascia oraria scelta, non serve piu' crearne uno per ciascuno.
    employees = [emp(1, "A", skills={"X"}), emp(2, "B", skills={"X"})]
    morning = shift(1, "Turno mattina", skill_required="X", time_bands={"MATTINA"})
    afternoon = shift(2, "Turno pomeriggio", skill_required="X", time_bands={"POMERIGGIO"})
    prefs = [
        PreferenceInput(
            employee_id=1, shift_type_id=None, days_set={0}, time_bands={"POMERIGGIO"},
            pref_type="EVITA", weight=20,
        )
    ]

    result = run(employees=employees, shift_types=[morning, afternoon], preferences=prefs)

    first_weekday = monthrange(2026, 1)[0]
    mondays = [d for d in range(1, 32) if (d - 1 + first_weekday) % 7 == 0]
    afternoon_on_monday = {
        a["employee_id"] for a in result.assignments if a["shift_type_id"] == 2 and a["day"] in mondays
    }
    assert 1 not in afternoon_on_monday
    morning_on_monday = {
        a["employee_id"] for a in result.assignments if a["shift_type_id"] == 1 and a["day"] in mondays
    }
    assert 1 in morning_on_monday  # nessuna penalita' sul turno del mattino


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


def test_category_rule_blocks_weekend_for_specializzandi():
    # Uno specializzando non deve mai comparire sabato o domenica, un
    # dipendente normale invece si', per non lasciare tutto scoperto.
    specializzando = emp(1, "Spec", category="SPEC", skills={"X"})
    strutturato = emp(2, "Strutt", category="STRUTT", skills={"X"})
    shift_types = [shift(1, "Turno", skill_required="X")]
    skill_rules = [
        SkillRuleInput(skill="", category="SPEC", weekday=5, mode="BLOCK"),
        SkillRuleInput(skill="", category="SPEC", weekday=6, mode="BLOCK"),
    ]

    result = run(employees=[specializzando, strutturato], shift_types=shift_types, skill_rules=skill_rules)

    # 2026-01-03 e' sabato, 2026-01-04 e' domenica
    weekend_assignees = {a["employee_id"] for a in result.assignments if a["day"] in (3, 4)}
    assert 1 not in weekend_assignees
    assert 2 in weekend_assignees
    # nei giorni feriali lo specializzando resta assegnabile normalmente (in
    # almeno un giorno feriale: con 2 persone equivalenti la fairness puo'
    # scegliere l'una o l'altra nel singolo giorno, ma non e' mai escluso).
    weekday_assignees = {a["employee_id"] for a in result.assignments if a["day"] not in (3, 4, 10, 11, 17, 18, 24, 25, 31)}
    assert 1 in weekday_assignees


def test_pinned_manual_assignment_is_not_duplicated_on_weekly_block():
    # Riproduce il bug segnalato: MODA e' assegnato a mano (BER) per la
    # settimana. Rigenerando, il risolutore non deve aggiungere una seconda
    # persona sullo stesso posto (il fabbisogno e' 1 al giorno).
    ber = emp(1, "BER", skills=set())
    altro = emp(2, "TRI", skills=set())
    moda = ShiftTypeInput(
        id=1, name="Corsia A", weekly_block=True, weekly_block_strictness=10,
        days_set={0, 1, 2, 3, 4}, requirements_by_weekday={},
    )

    result = run(
        employees=[ber, altro], shift_types=[moda],
        pinned_assignments={(1, 1, 5)},  # BER (id 1) pinnato sul turno 1 (MODA) il giorno 5 (lunedi')
    )

    moda_by_day = {}
    for a in result.assignments:
        moda_by_day.setdefault(a["day"], set()).add(a["employee_id"])

    # nessun giorno del mese deve mai avere piu' di 1 persona su MODA (il
    # fabbisogno e' 1): ne' nella settimana pinnata ne' nelle altre.
    for day, assignees in moda_by_day.items():
        assert len(assignees) == 1, f"giorno {day}: {assignees} (doveva restarne solo 1)"
    # la settimana 5-9 (lun-ven) contenente il giorno pinnato resta tutta a BER,
    # per via del vincolo di blocco settimanale rigido (stessa persona tutti i giorni)
    pinned_week_days = {dd: moda_by_day[dd] for dd in range(5, 10) if dd in moda_by_day}
    assert all(assignees == {1} for assignees in pinned_week_days.values())


def test_pinned_manual_assignment_on_ordinary_shift_not_duplicated():
    ber = emp(1, "BER", skills={"X"})
    altro = emp(2, "TRI", skills={"X"})
    turno = shift(1, "Turno", skill_required="X", requirements_by_weekday={wd: 1 for wd in range(7)})

    result = run(employees=[ber, altro], shift_types=[turno], pinned_assignments={(1, 1, 5)})

    day5_assignees = {a["employee_id"] for a in result.assignments if a["day"] == 5}
    assert day5_assignees == {1}


def test_pinned_assignment_for_employee_no_longer_in_the_list_is_ignored_not_crashed():
    # Un'assegnazione manuale puo' riferirsi a un dipendente nel frattempo
    # disattivato (quindi assente da 'employees'): deve solo essere ignorata
    # dal risolutore, non far esplodere la generazione con un errore.
    solo = emp(1, "Solo", skills=set())
    moda = ShiftTypeInput(
        id=1, name="Corsia A", weekly_block=True, weekly_block_strictness=10,
        days_set={0, 1, 2, 3, 4}, requirements_by_weekday={},
    )

    result = run(
        employees=[solo], shift_types=[moda],
        pinned_assignments={(999, 1, 5)},  # 999 non e' tra gli 'employees'
    )

    assert isinstance(result.assignments, list)  # non e' esploso
