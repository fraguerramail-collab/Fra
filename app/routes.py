import calendar
import csv
import io
import json
from datetime import date, timedelta

from flask import Blueprint, flash, redirect, render_template, request, send_file, url_for

from .models import (
    AVAILABLE,
    Assignment,
    Availability,
    Employee,
    ExtraShiftActivation,
    PREFERENCE_TYPES,
    Preference,
    SATURDAY,
    SUNDAY,
    Settings,
    ShiftRequirement,
    ShiftType,
    SkillRule,
    Suppression,
    TIME_BANDS,
    WEEKDAY_NAMES,
    WeekendPatternRole,
    db,
    parse_csv,
)
from .scheduler import (
    EmployeeInput,
    PreferenceInput,
    ShiftTypeInput,
    SkillRuleInput,
    WeekendRoleInput,
    generate_schedule,
)

bp = Blueprint("main", __name__)

MONTH_NAMES = [
    "", "Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno",
    "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre",
]

AVAILABILITY_OPTIONS = [
    ("OK", "Disp."),
    ("NO", "No (tutto il giorno)"),
    ("FERIE", "Ferie"),
    ("MAL", "Malattia"),
    ("NO_MATTINA", "No mattina"),
    ("NO_POMERIGGIO", "No pomeriggio"),
    ("NO_NOTTE", "No notte"),
    ("NO_MATTINA|NO_POMERIGGIO", "No mattina+pom."),
    ("NO_MATTINA|NO_NOTTE", "No mattina+notte"),
    ("NO_POMERIGGIO|NO_NOTTE", "No pom.+notte"),
]


FERIALI = {0, 1, 2, 3, 4}

# Catalogo di riferimento per un reparto di chirurgia generale, basato sulle
# regole reali condivise dall'utente (script Apps Script + turni di luglio +
# spiegazione del pattern weekend). Il campo skill_required di GG/R1G/R2URG/PO
# resta un segnaposto (vuoto = nessuna skill) finche' l'utente non conferma i
# codici skill esatti da usare.
SEED_SHIFT_TYPES = [
    dict(code="GN", name="Guardia Notte", group="NOTTE", color="#7c5cbf",
         time_bands="NOTTE", skill_required="GN", days_set="",
         requires_rest_next_day=True, min_gap_days=2,
         notes="Non esclusivo: nel weekend (ruolo B) coesiste con R2_URG lo stesso giorno.",
         required_by_weekday={d: 1 for d in range(7)}),
    dict(code="R1N", name="Reperibilità Notte 1", group="NOTTE", color="#9b8ad6",
         time_bands="NOTTE", skill_required="PR", days_set="",
         required_by_weekday={d: 1 for d in range(7)}),
    dict(code="R2N", name="Reperibilità Notte 2", group="NOTTE", color="#9b8ad6",
         time_bands="NOTTE", skill_required="SR", days_set="",
         required_by_weekday={d: 1 for d in range(7)}),
    dict(code="GG", name="Guardia Giorno", group="GUARDIA", color="#4f7cff",
         time_bands="MATTINA,POMERIGGIO", skill_required="GG", days_set="",
         required_by_weekday={0: 1, 1: 1, 2: 1, 3: 1, 4: 1, 6: 1},
         notes="Il sabato NON ha GG intera: e' spezzata in GG_AM+GG_PM (vedi ruoli weekend)."),
    dict(code="GG_AM", name="Guardia Giorno Sab. mattina (8-14)", group="GUARDIA", color="#4f7cff",
         time_bands="MATTINA", skill_required="", days_set="5",
         required_by_weekday={5: 1}),
    dict(code="GG_PM", name="Guardia Giorno Sab. pomeriggio (14-20)", group="GUARDIA", color="#4f7cff",
         time_bands="POMERIGGIO", skill_required="", days_set="5",
         required_by_weekday={5: 1}),
    dict(code="R1G", name="Reperibilità Giorno 1", group="GUARDIA", color="#7c9bff",
         time_bands="POMERIGGIO", skill_required="R1_G", days_set="",
         required_by_weekday={d: 1 for d in range(7)}),
    dict(code="R2URG", name="Reperibilità Urgenza", group="GUARDIA", color="#7c9bff",
         time_bands="MATTINA,POMERIGGIO", skill_required="SR", days_set="5,6",
         is_extra=True,
         notes="Coperta ogni weekend dai ruoli A-D (skill di ruolo secondo Pattern_Weekend). Per un "
               "festivo infrasettimanale (es. 10 agosto), attivala dalla pagina Extra sulla data "
               "specifica: essendo un turno extra funziona su qualsiasi giorno, non solo sab/dom."),
    dict(code="PO", name="Preospedalizzazione", group="CORSIA", color="#f2994a",
         time_bands="MATTINA", skill_required="PO", days_set=",".join(map(str, FERIALI)),
         required_by_weekday={d: 1 for d in FERIALI},
         notes="No weekend. Sospeso in luglio/agosto: usare una soppressione sull'intervallo di date."),
    dict(code="MODA", name="Corsia A", group="CORSIA", color="#1a9c5c",
         time_bands="MATTINA,POMERIGGIO", skill_required="MODA", days_set=",".join(map(str, FERIALI)),
         weekly_block=True, block_group="CORSIA"),
    dict(code="MODB", name="Corsia B", group="CORSIA", color="#1a9c5c",
         time_bands="MATTINA,POMERIGGIO", skill_required="MODB", days_set=",".join(map(str, FERIALI)),
         weekly_block=True, block_group="CORSIA"),
    dict(code="BREAST", name="Ambulatorio Breast", group="CORSIA", color="#e05780",
         time_bands="MATTINA", skill_required="BREAST", days_set="0,2",
         notes="Non richiede assegnazione tramite l'app: chi ha skill BREAST e' protetto da altri "
               "turni lun/mer e riservato (almeno 1 libero) mar/gio/ven, vedi Regole > regole skill."),
    dict(code="MEDIC", name="Ambulatorio Medicazioni", group="AMBULATORIO", color="#f2994a",
         time_bands="MATTINA", skill_required="MED", days_set="0,2,4",
         required_by_weekday={0: 1, 2: 1, 4: 1}),
    dict(code="VISLUN", name="Visite (Lunedì)", group="AMBULATORIO", color="#f2994a",
         time_bands="MATTINA", skill_required="VIS", days_set="0",
         required_by_weekday={0: 1}),
    dict(code="VISGIO", name="Visite (Giovedì)", group="AMBULATORIO", color="#f2994a",
         time_bands="MATTINA", skill_required="VIS", days_set="3",
         required_by_weekday={3: 1}),
    dict(code="CHIR", name="Chirurgia Ambulatoriale", group="AMBULATORIO", color="#f2994a",
         time_bands="POMERIGGIO", skill_required="CHIR", days_set="0",
         required_by_weekday={0: 1},
         notes="Da confermare chi e' abilitato (nota del reparto)."),
    dict(code="PROCT", name="Ambulatorio Proctologia", group="AMBULATORIO", color="#f2994a",
         time_bands="MATTINA", skill_required="PROCTO", days_set="1,3",
         required_by_weekday={1: 1, 3: 1}),
    dict(code="GOMSUP", name="GOM Superiore", group="GOM", color="#b5750f",
         time_bands="MATTINA", skill_required="GOM_SUP", days_set="4",
         required_by_weekday={4: 1},
         notes="Giorno da confermare (nota del reparto)."),
    dict(code="GOMINF", name="GOM Inferiore", group="GOM", color="#b5750f",
         time_bands="POMERIGGIO", skill_required="GOM_INF", days_set="2,3",
         required_by_weekday={2: 1, 3: 1},
         notes="Giorno da confermare (nota del reparto)."),
    dict(code="CDP", name="CDP", group="EXTRA", color="#999999",
         time_bands="MATTINA", skill_required="", is_extra=True),
    dict(code="ABB", name="ABB", group="EXTRA", color="#999999",
         time_bands="MATTINA,POMERIGGIO", skill_required="", is_extra=True),
]


def load_example_catalog():
    """Carica il catalogo di esempio (Chirurgia Generale a degenza) in un
    profilo vuoto. Va chiamata esplicitamente (pagina Home / pulsante), non
    all'avvio: un nuovo profilo puo' essere un reparto completamente diverso
    (es. blocco operatorio) per cui questo catalogo non avrebbe senso.
    """
    if ShiftType.query.count() > 0:
        return False

    created = {}
    for spec in SEED_SHIFT_TYPES:
        required_by_weekday = spec.pop("required_by_weekday", {})
        spec.pop("notes", None)
        st = ShiftType(sort_order=len(created), **spec)
        db.session.add(st)
        db.session.flush()
        for weekday, count in required_by_weekday.items():
            db.session.add(ShiftRequirement(shift_type_id=st.id, weekday=weekday, required_staff=count))
        created[st.code] = st

    # Smonto: dopo GN, riposo il giorno dopo; eccezione sab->dom con R2N
    # (stessa logica della regola "GN sabato + R2_N domenica" dello script).
    created["GN"].rest_exception_shift_type_id = created["R2N"].id

    # Pattern weekend reale, dal foglio "Pattern_Weekend" del reparto (4 figure
    # A/B/C/D) e verificato riga per riga sul turnario di luglio 2026 reale.
    # Ogni riga: (giorno, codice turno, skill richiesta per QUEL ruolo).
    WEEKEND_PATTERN = {
        "A": [("SAB", "GG_AM", "PR"), ("SAB", "R1G", "PR"), ("SAB", "R1N", "PR"),
              ("DOM", "R1G", "PR"), ("DOM", "R1N", "PR")],
        "B": [("SAB", "GG_PM", "PR|SR"), ("SAB", "R2N", "PR|SR"),
              ("DOM", "R2URG", "PR|SR"), ("DOM", "GN", "PR|SR")],
        "C": [("SAB", "R2URG", "GG"), ("DOM", "GG", "GG")],
        "D": [("SAB", "GN", "PR|SR"), ("DOM", "R2N", "PR|SR")],
    }
    for role_code, entries in WEEKEND_PATTERN.items():
        for day, st_code, skill in entries:
            db.session.add(
                WeekendPatternRole(
                    role_code=role_code, day=day, shift_type_id=created[st_code].id, skill_required=skill,
                )
            )

    # Chi ha skill BREAST: bloccato da ogni altro turno lun/mer (dedicato
    # all'ambulatorio Breast), riservato (almeno 1 libero) mar/gio/ven.
    for weekday in (0, 2):
        db.session.add(SkillRule(skill="BREAST", weekday=weekday, mode="BLOCK"))
    for weekday in (1, 3, 4):
        db.session.add(SkillRule(skill="BREAST", weekday=weekday, mode="RESERVE", min_free=1))

    Settings.get()
    db.session.commit()
    return True


def _current_year_month():
    today = date.today()
    year = request.args.get("anno", type=int) or today.year
    month = request.args.get("mese", type=int) or today.month
    return year, month


def _build_employee_inputs(employees):
    return [
        EmployeeInput(
            id=e.id, name=e.name, category=e.category or "", skills=e.skill_list(),
            max_shifts_per_week=e.max_shifts_per_week,
        )
        for e in employees
    ]


def _build_shift_type_inputs(shift_types):
    return {
        st.id: ShiftTypeInput(
            id=st.id, name=st.name,
            time_bands=set(parse_csv(st.time_bands)), skill_required=st.skill_required or "",
            skill_by_weekday={r.weekday: r.skill_override for r in st.requirements if r.skill_override},
            days_set=st.days_set_list(), excluded_categories=st.excluded_category_list(),
            exclusive_day=st.exclusive_day, requires_rest_next_day=st.requires_rest_next_day,
            rest_exception_shift_type_id=st.rest_exception_shift_type_id, min_gap_days=st.min_gap_days,
            weekly_block=st.weekly_block, weekly_block_strictness=st.weekly_block_strictness,
            block_group=st.block_group, is_extra=st.is_extra,
            balance_pool=st.balance_pool,
            requirements_by_weekday={r.weekday: r.required_staff for r in st.requirements},
        )
        for st in shift_types
    }


def _last_weekend_days(year, month):
    num_days = calendar.monthrange(year, month)[1]
    first_weekday = calendar.monthrange(year, month)[0]
    sat = None
    for day in range(num_days, 0, -1):
        weekday = (day - 1 + first_weekday) % 7
        if weekday == SATURDAY:
            sat = day
            break
    if sat is None:
        return []
    days = [sat]
    if sat + 1 <= num_days:
        days.append(sat + 1)
    return days


def _prev_month(year, month):
    return (year - 1, 12) if month == 1 else (year, month - 1)


# ---------------------------------------------------------------- dashboard
@bp.route("/")
def index():
    n_employees = Employee.query.filter_by(active=True).count()
    n_shift_types = ShiftType.query.count()
    return render_template("index.html", n_employees=n_employees, n_shift_types=n_shift_types)


@bp.route("/carica-esempio", methods=["POST"])
def load_example():
    if load_example_catalog():
        flash("Catalogo di esempio (Chirurgia Generale a degenza) caricato: adattalo alle tue esigenze.", "success")
    else:
        flash("Questo reparto ha gia' dei tipi di turno configurati: il catalogo di esempio non e' stato caricato.", "warning")
    return redirect(url_for("main.index"))


# ---------------------------------------------------------------- dipendenti
@bp.route("/dipendenti", methods=["GET", "POST"])
def employees():
    if request.method == "POST":
        code = request.form.get("code", "").strip().upper()
        name = request.form.get("name", "").strip()
        if code and name:
            if Employee.query.filter_by(code=code).first():
                flash(f"Esiste gia' un dipendente con codice '{code}'.", "warning")
                return redirect(url_for("main.employees"))
            emp = Employee(
                code=code, name=name,
                category=request.form.get("category", "").strip() or None,
                skills=request.form.get("skills", "").strip() or None,
                max_shifts_per_week=request.form.get("max_shifts_per_week", type=int),
            )
            db.session.add(emp)
            db.session.commit()
            flash(f"Dipendente '{name}' aggiunto.", "success")
        return redirect(url_for("main.employees"))

    people = Employee.query.order_by(Employee.name).all()
    return render_template("employees.html", employees=people, known_skills=_known_skills())


@bp.route("/dipendenti/<int:employee_id>/modifica", methods=["POST"])
def edit_employee(employee_id):
    emp = Employee.query.get_or_404(employee_id)
    new_code = request.form.get("code", emp.code).strip().upper() or emp.code
    if new_code != emp.code and Employee.query.filter_by(code=new_code).first():
        flash(f"Esiste gia' un dipendente con codice '{new_code}'.", "warning")
        return redirect(url_for("main.employees"))
    emp.code = new_code
    emp.name = request.form.get("name", emp.name).strip() or emp.name
    emp.category = request.form.get("category", "").strip() or None
    emp.skills = request.form.get("skills", "").strip() or None
    emp.active = request.form.get("active") == "on"
    emp.max_shifts_per_week = request.form.get("max_shifts_per_week", type=int)
    emp.notes = request.form.get("notes", "").strip() or None
    db.session.commit()
    flash(f"Dipendente '{emp.name}' aggiornato.", "success")
    return redirect(url_for("main.employees"))


@bp.route("/dipendenti/<int:employee_id>/elimina", methods=["POST"])
def delete_employee(employee_id):
    emp = Employee.query.get_or_404(employee_id)
    db.session.delete(emp)
    db.session.commit()
    flash(f"Dipendente '{emp.name}' eliminato.", "success")
    return redirect(url_for("main.employees"))


# ---------------------------------------------------------------- tipi di turno
def _read_shift_type_form(st):
    st.name = request.form.get("name", st.name).strip() or st.name
    st.group = request.form.get("group", "").strip() or None
    st.color = request.form.get("color", st.color)
    st.time_bands = ",".join(request.form.getlist("time_bands"))
    st.skill_required = request.form.get("skill_required", "").strip() or None
    days = request.form.getlist("days_set")
    st.days_set = ",".join(days)
    st.excluded_categories = request.form.get("excluded_categories", "").strip() or None
    st.exclusive_day = request.form.get("exclusive_day") == "on"
    st.requires_rest_next_day = request.form.get("requires_rest_next_day") == "on"
    rest_exc = request.form.get("rest_exception_shift_type_id", type=int)
    st.rest_exception_shift_type_id = rest_exc or None
    st.min_gap_days = request.form.get("min_gap_days", type=int) or 0
    st.weekly_block = request.form.get("weekly_block") == "on"
    st.weekly_block_strictness = request.form.get("weekly_block_strictness", type=int)
    if st.weekly_block_strictness is None:
        st.weekly_block_strictness = 10
    st.block_group = request.form.get("block_group", "").strip() or None
    st.is_extra = request.form.get("is_extra") == "on"
    st.balance_pool = request.form.get("balance_pool", "").strip() or None


@bp.route("/turni", methods=["GET", "POST"])
def shift_types():
    if request.method == "POST":
        code = request.form.get("code", "").strip().upper()
        name = request.form.get("name", "").strip()
        if code and name:
            if ShiftType.query.filter_by(code=code).first():
                flash(f"Esiste gia' un turno con codice '{code}'.", "warning")
                return redirect(url_for("main.shift_types"))
            st = ShiftType(code=code, name=name, sort_order=ShiftType.query.count())
            _read_shift_type_form(st)
            db.session.add(st)
            db.session.flush()
            for weekday in range(7):
                db.session.add(ShiftRequirement(shift_type_id=st.id, weekday=weekday, required_staff=0))
            db.session.commit()
            flash(f"Turno '{name}' creato.", "success")
        return redirect(url_for("main.shift_types"))

    types = ShiftType.query.order_by(ShiftType.sort_order).all()
    return render_template(
        "shift_types.html", shift_types=types, weekday_names=WEEKDAY_NAMES, time_bands=TIME_BANDS
    )


@bp.route("/turni/<int:shift_type_id>/modifica", methods=["POST"])
def edit_shift_type(shift_type_id):
    st = ShiftType.query.get_or_404(shift_type_id)
    new_code = request.form.get("code", st.code).strip().upper() or st.code
    if new_code != st.code and ShiftType.query.filter_by(code=new_code).first():
        flash(f"Esiste gia' un turno con codice '{new_code}'.", "warning")
        return redirect(url_for("main.shift_types"))
    st.code = new_code
    _read_shift_type_form(st)

    req_by_wd = {r.weekday: r for r in st.requirements}
    for weekday in range(7):
        value = request.form.get(f"required_{weekday}", type=int) or 0
        skill_override = request.form.get(f"skill_override_{weekday}", "").strip() or None
        if weekday in req_by_wd:
            req_by_wd[weekday].required_staff = value
            req_by_wd[weekday].skill_override = skill_override
        else:
            db.session.add(
                ShiftRequirement(
                    shift_type_id=st.id, weekday=weekday, required_staff=value, skill_override=skill_override,
                )
            )

    db.session.commit()
    flash(f"Turno '{st.name}' aggiornato.", "success")
    return redirect(url_for("main.shift_types"))


@bp.route("/turni/<int:shift_type_id>/duplica", methods=["POST"])
def duplicate_shift_type(shift_type_id):
    src = ShiftType.query.get_or_404(shift_type_id)

    base_code = f"{src.code}_COPY"
    new_code = base_code
    n = 2
    while ShiftType.query.filter_by(code=new_code).first():
        new_code = f"{base_code}{n}"
        n += 1

    clone = ShiftType(
        code=new_code, name=f"{src.name} (copia)", group=src.group, color=src.color,
        sort_order=ShiftType.query.count(),
        time_bands=src.time_bands, skill_required=src.skill_required, days_set=src.days_set,
        excluded_categories=src.excluded_categories, exclusive_day=src.exclusive_day,
        requires_rest_next_day=src.requires_rest_next_day,
        rest_exception_shift_type_id=src.rest_exception_shift_type_id, min_gap_days=src.min_gap_days,
        weekly_block=src.weekly_block, weekly_block_strictness=src.weekly_block_strictness,
        block_group=src.block_group, is_extra=src.is_extra, balance_pool=src.balance_pool,
    )
    db.session.add(clone)
    db.session.flush()
    for r in src.requirements:
        db.session.add(
            ShiftRequirement(
                shift_type_id=clone.id, weekday=r.weekday, required_staff=r.required_staff,
                skill_override=r.skill_override,
            )
        )
    db.session.commit()
    flash(f"Turno '{src.name}' duplicato come '{clone.name}' (codice {clone.code}) — modificalo qui sotto.", "success")
    return redirect(url_for("main.shift_types"))


@bp.route("/turni/<int:shift_type_id>/elimina", methods=["POST"])
def delete_shift_type(shift_type_id):
    st = ShiftType.query.get_or_404(shift_type_id)
    db.session.delete(st)
    db.session.commit()
    flash(f"Turno '{st.name}' eliminato.", "success")
    return redirect(url_for("main.shift_types"))


@bp.route("/turni/<int:shift_type_id>/sposta", methods=["POST"])
def move_shift_type(shift_type_id):
    direction = request.form.get("direction")
    types = ShiftType.query.order_by(ShiftType.sort_order).all()
    idx = next((i for i, st in enumerate(types) if st.id == shift_type_id), None)
    if idx is not None:
        swap_idx = idx - 1 if direction == "up" else idx + 1
        if 0 <= swap_idx < len(types):
            types[idx].sort_order, types[swap_idx].sort_order = (
                types[swap_idx].sort_order,
                types[idx].sort_order,
            )
            db.session.commit()
    return redirect(url_for("main.shift_types"))


@bp.route("/turni/<int:shift_type_id>/riordina", methods=["POST"])
def reorder_shift_type(shift_type_id):
    target_pos = request.form.get("position", type=int)
    types = ShiftType.query.order_by(ShiftType.sort_order).all()
    idx = next((i for i, st in enumerate(types) if st.id == shift_type_id), None)
    if target_pos is not None and idx is not None:
        item = types.pop(idx)
        target_idx = max(0, min(len(types), target_pos - 1))
        types.insert(target_idx, item)
        for i, st in enumerate(types):
            st.sort_order = i
        db.session.commit()
    return redirect(url_for("main.shift_types"))


# ---------------------------------------------------------------- weekend a ruoli
@bp.route("/weekend", methods=["GET", "POST"])
def weekend_pattern():
    if request.method == "POST":
        role_code = request.form.get("role_code", "").strip().upper()
        day = request.form.get("day")
        shift_type_id = request.form.get("shift_type_id", type=int)
        if role_code and day in ("SAB", "DOM") and shift_type_id:
            db.session.add(
                WeekendPatternRole(
                    role_code=role_code, day=day, shift_type_id=shift_type_id,
                    skill_required=request.form.get("skill_required", "").strip() or None,
                )
            )
            db.session.commit()
            flash(f"Ruolo weekend {role_code} ({day}) aggiunto.", "success")
        return redirect(url_for("main.weekend_pattern"))

    roles = WeekendPatternRole.query.order_by(WeekendPatternRole.role_code, WeekendPatternRole.day).all()
    types = ShiftType.query.order_by(ShiftType.sort_order).all()
    return render_template("weekend_pattern.html", roles=roles, shift_types=types)


@bp.route("/weekend/<int:role_id>/elimina", methods=["POST"])
def delete_weekend_role(role_id):
    role = WeekendPatternRole.query.get_or_404(role_id)
    db.session.delete(role)
    db.session.commit()
    flash("Ruolo weekend eliminato.", "success")
    return redirect(url_for("main.weekend_pattern"))


# ---------------------------------------------------------------- preferenze
@bp.route("/preferenze", methods=["GET", "POST"])
def preferences():
    if request.method == "POST":
        pref_type = request.form.get("pref_type")
        if pref_type in PREFERENCE_TYPES:
            days = request.form.getlist("days_set")
            db.session.add(
                Preference(
                    employee_id=request.form.get("employee_id", type=int) or None,
                    shift_type_id=request.form.get("shift_type_id", type=int) or None,
                    days_set=",".join(days) or None,
                    pref_type=pref_type,
                    weight=request.form.get("weight", type=int) or 1,
                )
            )
            db.session.commit()
            flash("Preferenza aggiunta.", "success")
        return redirect(url_for("main.preferences"))

    prefs = Preference.query.all()
    people = Employee.query.order_by(Employee.name).all()
    types = ShiftType.query.order_by(ShiftType.sort_order).all()
    return render_template(
        "preferences.html", preferences=prefs, employees=people, shift_types=types,
        pref_types=PREFERENCE_TYPES, weekday_names=WEEKDAY_NAMES,
    )


@bp.route("/preferenze/<int:pref_id>/modifica", methods=["POST"])
def edit_preference(pref_id):
    pref = Preference.query.get_or_404(pref_id)
    pref.weight = request.form.get("weight", type=int)
    if pref.weight is None:
        pref.weight = 1
    db.session.commit()
    flash("Peso aggiornato.", "success")
    return redirect(url_for("main.preferences"))


@bp.route("/preferenze/<int:pref_id>/elimina", methods=["POST"])
def delete_preference(pref_id):
    pref = Preference.query.get_or_404(pref_id)
    db.session.delete(pref)
    db.session.commit()
    flash("Preferenza eliminata.", "success")
    return redirect(url_for("main.preferences"))


def _known_skills():
    """Tutte le skill in uso da qualche parte (dipendenti o skill_required dei
    turni), per suggerirle nel form delle regole skill invece di lasciar
    scrivere un codice libero che puo' non corrispondere a nulla."""
    skills = set()
    for e in Employee.query.all():
        skills.update(parse_csv(e.skills))
    for st in ShiftType.query.all():
        for token in (st.skill_required or "").split("|"):
            token = token.strip()
            if token:
                skills.add(token)
    return sorted(skills)


# ---------------------------------------------------------------- regole
@bp.route("/regole", methods=["GET", "POST"])
def rules():
    settings = Settings.get()
    if request.method == "POST":
        settings.max_consecutive_work_days = request.form.get("max_consecutive_work_days", type=int) or 6
        db.session.commit()
        flash("Regole salvate.", "success")
        return redirect(url_for("main.rules"))
    skill_rules = SkillRule.query.order_by(SkillRule.skill, SkillRule.weekday).all()

    active_skills = set()
    for e in Employee.query.filter_by(active=True).all():
        active_skills.update(parse_csv(e.skills))
    rules_with_status = [(r, r.skill not in active_skills) for r in skill_rules]

    return render_template(
        "rules.html", settings=settings, rules_with_status=rules_with_status,
        weekday_names=WEEKDAY_NAMES, known_skills=_known_skills(),
    )


@bp.route("/regole/skill", methods=["POST"])
def add_skill_rule():
    skill = request.form.get("skill", "").strip()
    weekday = request.form.get("weekday", type=int)
    mode = request.form.get("mode", "").strip()
    min_free = request.form.get("min_free", type=int) or 1
    if skill and weekday is not None and mode in ("BLOCK", "RESERVE"):
        db.session.add(SkillRule(skill=skill, weekday=weekday, mode=mode, min_free=min_free))
        db.session.commit()
        flash("Regola skill aggiunta.", "success")
    return redirect(url_for("main.rules"))


@bp.route("/regole/skill/<int:rule_id>/elimina", methods=["POST"])
def delete_skill_rule(rule_id):
    rule = SkillRule.query.get_or_404(rule_id)
    db.session.delete(rule)
    db.session.commit()
    flash("Regola skill eliminata.", "success")
    return redirect(url_for("main.rules"))


# ---------------------------------------------------------------- disponibilità
@bp.route("/disponibilita", methods=["GET", "POST"])
def availability():
    year, month = _current_year_month()
    num_days = calendar.monthrange(year, month)[1]
    people = Employee.query.filter_by(active=True).order_by(Employee.name).all()

    valid_statuses = {value for value, _label in AVAILABILITY_OPTIONS}

    if request.method == "POST":
        for emp in people:
            for day in range(1, num_days + 1):
                status = request.form.get(f"a_{emp.id}_{day}", AVAILABLE)
                if status not in valid_statuses:
                    status = AVAILABLE
                existing = Availability.query.filter_by(
                    employee_id=emp.id, year=year, month=month, day=day
                ).first()
                if existing:
                    existing.status = status
                elif status != AVAILABLE:
                    db.session.add(
                        Availability(employee_id=emp.id, year=year, month=month, day=day, status=status)
                    )
        db.session.commit()
        flash("Disponibilità salvate.", "success")
        return redirect(url_for("main.availability", anno=year, mese=month))

    existing_rows = Availability.query.filter_by(year=year, month=month).all()
    avail_map = {(r.employee_id, r.day): r.status for r in existing_rows}

    first_weekday = calendar.monthrange(year, month)[0]
    day_weekday_names = [WEEKDAY_NAMES[(first_weekday + d) % 7][:2] for d in range(num_days)]

    return render_template(
        "availability.html", employees=people, year=year, month=month, month_name=MONTH_NAMES[month],
        num_days=num_days, day_range=range(1, num_days + 1), day_weekday_names=day_weekday_names,
        avail_map=avail_map, options=AVAILABILITY_OPTIONS, month_names=MONTH_NAMES,
    )


# ---------------------------------------------------------------- soppressioni
@bp.route("/soppressioni", methods=["GET", "POST"])
def suppressions():
    year, month = _current_year_month()

    if request.method == "POST":
        shift_type_ids = request.form.getlist("shift_type_ids", type=int)
        day = request.form.get("day", type=int)
        end_year = request.form.get("end_year", type=int) or year
        end_month = request.form.get("end_month", type=int) or month
        end_day = request.form.get("end_day", type=int) or day
        reason = request.form.get("reason", "").strip() or None

        if shift_type_ids and day:
            start_date = date(year, month, day)
            end_date = date(end_year, end_month, end_day)
            if end_date < start_date:
                flash("La data di fine non puo' essere prima della data di inizio.", "warning")
                return redirect(url_for("main.suppressions", anno=year, mese=month))

            total_created = 0
            total_skipped = 0
            for shift_type_id in shift_type_ids:
                shift_type = ShiftType.query.get(shift_type_id)
                if shift_type is None:
                    continue
                current = start_date
                while current <= end_date:
                    weekday = current.weekday()  # 0=Lun .. 6=Dom, coerente con days_set
                    days_set = shift_type.days_set_list()
                    normally_scheduled = (not days_set or weekday in days_set) and (
                        shift_type.requirement_for_weekday(weekday) > 0 or shift_type.is_extra
                    )
                    if normally_scheduled:
                        exists = Suppression.query.filter_by(
                            shift_type_id=shift_type_id, year=current.year, month=current.month, day=current.day
                        ).first()
                        if not exists:
                            db.session.add(
                                Suppression(
                                    shift_type_id=shift_type_id, year=current.year, month=current.month,
                                    day=current.day, reason=reason,
                                )
                            )
                            total_created += 1
                    else:
                        total_skipped += 1
                    current += timedelta(days=1)

            db.session.commit()
            if total_created:
                msg = f"{total_created} soppressione/i aggiunta/e su {len(shift_type_ids)} turno/i."
                if total_skipped:
                    msg += f" ({total_skipped} combinazioni giorno/turno ignorate perche' non previste.)"
                flash(msg, "success")
            else:
                flash("Nessuna soppressione aggiunta: nessuno dei turni scelti era previsto nei giorni indicati.", "warning")
        return redirect(url_for("main.suppressions", anno=year, mese=month))

    items = Suppression.query.filter_by(year=year, month=month).order_by(Suppression.day).all()
    types = ShiftType.query.order_by(ShiftType.sort_order).all()
    num_days = calendar.monthrange(year, month)[1]
    return render_template(
        "suppressions.html", items=items, shift_types=types, year=year, month=month,
        month_name=MONTH_NAMES[month], month_names=MONTH_NAMES, day_range=range(1, num_days + 1),
    )


@bp.route("/soppressioni/<int:item_id>/elimina", methods=["POST"])
def delete_suppression(item_id):
    item = Suppression.query.get_or_404(item_id)
    year, month = item.year, item.month
    db.session.delete(item)
    db.session.commit()
    flash("Soppressione eliminata.", "success")
    return redirect(url_for("main.suppressions", anno=year, mese=month))


# ---------------------------------------------------------------- turni extra
@bp.route("/extra", methods=["GET", "POST"])
def extra_shifts():
    year, month = _current_year_month()

    if request.method == "POST":
        shift_type_id = request.form.get("shift_type_id", type=int)
        day = request.form.get("day", type=int)
        if shift_type_id and day:
            db.session.add(
                ExtraShiftActivation(
                    shift_type_id=shift_type_id, year=year, month=month, day=day,
                    reason=request.form.get("reason", "").strip() or None,
                )
            )
            db.session.commit()
            flash("Turno extra attivato.", "success")
        return redirect(url_for("main.extra_shifts", anno=year, mese=month))

    items = ExtraShiftActivation.query.filter_by(year=year, month=month).order_by(ExtraShiftActivation.day).all()
    types = ShiftType.query.filter_by(is_extra=True).order_by(ShiftType.sort_order).all()
    num_days = calendar.monthrange(year, month)[1]
    return render_template(
        "extra_shifts.html", items=items, shift_types=types, year=year, month=month,
        month_name=MONTH_NAMES[month], month_names=MONTH_NAMES, day_range=range(1, num_days + 1),
    )


@bp.route("/extra/<int:item_id>/elimina", methods=["POST"])
def delete_extra_shift(item_id):
    item = ExtraShiftActivation.query.get_or_404(item_id)
    year, month = item.year, item.month
    db.session.delete(item)
    db.session.commit()
    flash("Turno extra rimosso.", "success")
    return redirect(url_for("main.extra_shifts", anno=year, mese=month))


# ---------------------------------------------------------------- pianificazione
@bp.route("/pianificazione", methods=["GET"])
def schedule():
    year, month = _current_year_month()
    num_days = calendar.monthrange(year, month)[1]
    first_weekday = calendar.monthrange(year, month)[0]

    people = Employee.query.filter_by(active=True).order_by(Employee.name).all()
    types = ShiftType.query.order_by(ShiftType.sort_order).all()

    assignments = Assignment.query.filter_by(year=year, month=month).all()
    grid = {}
    for a in assignments:
        grid.setdefault((a.day, a.shift_type_id), []).append(a)

    day_weekday_names = [WEEKDAY_NAMES[(first_weekday + d) % 7] for d in range(num_days)]
    edit_mode = request.args.get("modifica") == "1"
    employees_json = json.dumps([{"id": e.id, "code": e.code, "name": e.name} for e in people])

    return render_template(
        "schedule.html", employees=people, shift_types=types, year=year, month=month,
        month_name=MONTH_NAMES[month], month_names=MONTH_NAMES, day_range=range(1, num_days + 1),
        day_weekday_names=day_weekday_names, grid=grid, edit_mode=edit_mode, employees_json=employees_json,
    )


@bp.route("/pianificazione/genera", methods=["POST"])
def generate():
    year = request.form.get("anno", type=int)
    month = request.form.get("mese", type=int)

    settings = Settings.get()
    people = Employee.query.filter_by(active=True).order_by(Employee.name).all()
    types = ShiftType.query.order_by(ShiftType.sort_order).all()

    employee_inputs = _build_employee_inputs(people)
    shift_type_inputs_by_id = _build_shift_type_inputs(types)

    weekend_role_inputs = [
        WeekendRoleInput(
            role_code=r.role_code, day=r.day, shift_type_id=r.shift_type_id,
            skill_required=r.effective_skill() or "",
        )
        for r in WeekendPatternRole.query.all()
    ]

    preference_inputs = [
        PreferenceInput(
            employee_id=p.employee_id, shift_type_id=p.shift_type_id, days_set=p.days_set_list(),
            pref_type=p.pref_type, weight=p.weight,
        )
        for p in Preference.query.all()
    ]

    availability_rows = Availability.query.filter_by(year=year, month=month).all()
    availability_map = {(r.employee_id, r.day): r.availability_info() for r in availability_rows}

    suppression_set = {
        (s.shift_type_id, s.day)
        for s in Suppression.query.filter_by(year=year, month=month, active=True).all()
    }

    extra_activations = {}
    for e in ExtraShiftActivation.query.filter_by(year=year, month=month, active=True).all():
        extra_activations.setdefault(e.day, []).append(e.shift_type_id)

    prev_year, prev_month = _prev_month(year, month)
    prev_num_days = calendar.monthrange(prev_year, prev_month)[1]
    prev_last_day_assignments = Assignment.query.filter_by(
        year=prev_year, month=prev_month, day=prev_num_days
    ).all()
    prev_month_last_shifts = {}
    for a in prev_last_day_assignments:
        prev_month_last_shifts.setdefault(a.employee_id, []).append(a.shift_type_id)

    weekend_shift_ids = {r.shift_type_id for r in weekend_role_inputs}
    prev_weekend_days = _last_weekend_days(prev_year, prev_month)
    prev_month_weekend_count = {}
    if prev_weekend_days and weekend_shift_ids:
        prev_weekend_assignments = Assignment.query.filter(
            Assignment.year == prev_year,
            Assignment.month == prev_month,
            Assignment.day.in_(prev_weekend_days),
            Assignment.shift_type_id.in_(weekend_shift_ids),
        ).all()
        for a in prev_weekend_assignments:
            prev_month_weekend_count[a.employee_id] = 1

    skill_rule_inputs = [
        SkillRuleInput(skill=r.skill, weekday=r.weekday, mode=r.mode, min_free=r.min_free)
        for r in SkillRule.query.all()
    ]

    result = generate_schedule(
        year=year, month=month, employees=employee_inputs,
        shift_types=list(shift_type_inputs_by_id.values()), weekend_roles=weekend_role_inputs,
        preferences=preference_inputs, availability=availability_map, suppressions=suppression_set,
        extra_activations=extra_activations, prev_month_last_shifts=prev_month_last_shifts,
        prev_month_weekend_count=prev_month_weekend_count,
        max_consecutive_work_days=settings.max_consecutive_work_days,
        skill_rules=skill_rule_inputs,
    )

    Assignment.query.filter_by(year=year, month=month, auto_generated=True).delete()
    existing_keys = {
        (a.employee_id, a.shift_type_id, a.day)
        for a in Assignment.query.filter_by(year=year, month=month).all()
    }
    skipped_duplicates = 0
    for a in result.assignments:
        key = (a["employee_id"], a["shift_type_id"], a["day"])
        if key in existing_keys:
            # coincide con un'assegnazione manuale gia' presente per la stessa
            # persona/turno/giorno: non reinserirla (violerebbe l'unicita').
            skipped_duplicates += 1
            continue
        existing_keys.add(key)
        db.session.add(
            Assignment(
                employee_id=a["employee_id"], shift_type_id=a["shift_type_id"], year=year, month=month,
                day=a["day"], auto_generated=True,
            )
        )
    db.session.commit()

    if skipped_duplicates:
        flash(
            f"{skipped_duplicates} assegnazione/i generate coincidevano con turni gia' inseriti "
            "a mano per la stessa persona/turno/giorno: lasciate quelle manuali.", "warning",
        )

    if result.warnings:
        flash(f"Turni generati con {len(result.warnings)} avviso/i:", "warning")
        for w in result.warnings[:30]:
            flash(w, "warning-detail")
    else:
        flash("Turni generati correttamente, nessun avviso.", "success")

    return redirect(url_for("main.schedule", anno=year, mese=month))


@bp.route("/pianificazione/salva", methods=["POST"])
def save_schedule():
    year = request.form.get("anno", type=int)
    month = request.form.get("mese", type=int)
    num_days = calendar.monthrange(year, month)[1]

    types = ShiftType.query.all()

    # una cella lasciata identica a prima resta con lo stesso stato (manuale/
    # automatica) che aveva; solo le celle effettivamente cambiate diventano
    # manuali, cosi' la distinzione visiva sopravvive al salvataggio.
    old_cells = {}
    for a in Assignment.query.filter_by(year=year, month=month).all():
        old_cells.setdefault((a.day, a.shift_type_id), {})[a.employee_id] = a.auto_generated

    Assignment.query.filter_by(year=year, month=month).delete()
    for day in range(1, num_days + 1):
        for st in types:
            selected_ids = [int(x) for x in request.form.getlist(f"slot_{day}_{st.id}")]
            old_cell = old_cells.get((day, st.id), {})
            unchanged = set(selected_ids) == set(old_cell.keys())
            for emp_id in selected_ids:
                db.session.add(
                    Assignment(
                        employee_id=emp_id, shift_type_id=st.id, year=year, month=month, day=day,
                        auto_generated=bool(unchanged and old_cell.get(emp_id)),
                    )
                )
    db.session.commit()
    flash("Pianificazione salvata.", "success")
    return redirect(url_for("main.schedule", anno=year, mese=month, modifica=1))


@bp.route("/pianificazione/pulisci", methods=["POST"])
def clear_schedule():
    year = request.form.get("anno", type=int)
    month = request.form.get("mese", type=int)
    n = Assignment.query.filter_by(year=year, month=month).delete()
    db.session.commit()
    flash(f"Pianificazione di {MONTH_NAMES[month]} {year} cancellata ({n} assegnazione/i rimossa/e).", "success")
    return redirect(url_for("main.schedule", anno=year, mese=month, modifica=1))


@bp.route("/pianificazione/pulisci-auto", methods=["POST"])
def clear_auto_schedule():
    year = request.form.get("anno", type=int)
    month = request.form.get("mese", type=int)
    n = Assignment.query.filter_by(year=year, month=month, auto_generated=True).delete()
    db.session.commit()
    flash(f"{n} assegnazione/i automatica/che cancellata/e (quelle manuali restano).", "success")
    return redirect(url_for("main.schedule", anno=year, mese=month, modifica=1))


@bp.route("/pianificazione/esporta.csv")
def export_schedule_csv():
    year, month = _current_year_month()
    num_days = calendar.monthrange(year, month)[1]
    types = ShiftType.query.order_by(ShiftType.sort_order).all()

    assignments = Assignment.query.filter_by(year=year, month=month).all()
    grid = {}
    for a in assignments:
        grid.setdefault((a.day, a.shift_type_id), []).append(a.employee.name)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Giorno"] + [st.name for st in types])
    for day in range(1, num_days + 1):
        row = [day]
        for st in types:
            row.append(", ".join(grid.get((day, st.id), [])))
        writer.writerow(row)

    mem = io.BytesIO(output.getvalue().encode("utf-8-sig"))
    filename = f"turni_{year}_{month:02d}.csv"
    return send_file(mem, mimetype="text/csv", as_attachment=True, download_name=filename)


# ---------------------------------------------------------------- report
@bp.route("/report")
def report():
    year, month = _current_year_month()
    people = Employee.query.filter_by(active=True).order_by(Employee.name).all()
    types = ShiftType.query.order_by(ShiftType.sort_order).all()
    assignments = Assignment.query.filter_by(year=year, month=month).all()

    counts = {emp.id: {"total": 0, "per_type": {}} for emp in people}
    for a in assignments:
        if a.employee_id not in counts:
            continue
        counts[a.employee_id]["total"] += 1
        counts[a.employee_id]["per_type"][a.shift_type_id] = (
            counts[a.employee_id]["per_type"].get(a.shift_type_id, 0) + 1
        )

    rows = []
    for emp in people:
        c = counts[emp.id]
        alerts = []
        for st in types:
            n = c["per_type"].get(st.id, 0)
            if st.min_gap_days > 0 and n > 1:
                alerts.append(f"{st.code}={n}")
        if c["total"] == 0:
            alerts.append("nessun incarico")
        rows.append({"employee": emp, "counts": c, "alerts": alerts})

    rows.sort(key=lambda r: -r["counts"]["total"])

    return render_template(
        "report.html", rows=rows, shift_types=types, year=year, month=month,
        month_name=MONTH_NAMES[month], month_names=MONTH_NAMES,
    )


@bp.route("/guida")
def guide():
    return render_template("guide.html")
