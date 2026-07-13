import calendar
import csv
import io
from datetime import date

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
# regole reali condivise dall'utente (script Apps Script + turni di luglio).
# Alcuni campi (skill_required per GG/R1_G, uso di PO/R2_URG) sono marcati
# come "da confermare" nelle note e vanno rivisti con dati reali.
SEED_SHIFT_TYPES = [
    dict(code="GN", name="Guardia Notte", group="NOTTE", color="#7c5cbf", priority=10,
         time_bands="NOTTE", skill_required="GN", days_set="",
         exclusive_day=True, requires_rest_next_day=True, min_gap_days=2,
         required_by_weekday={d: 1 for d in range(7)}),
    dict(code="R1N", name="Reperibilità Notte 1", group="NOTTE", color="#9b8ad6", priority=11,
         time_bands="NOTTE", skill_required="PR", days_set="",
         required_by_weekday={d: 1 for d in range(7)}),
    dict(code="R2N", name="Reperibilità Notte 2", group="NOTTE", color="#9b8ad6", priority=12,
         time_bands="NOTTE", skill_required="SR", days_set="",
         required_by_weekday={d: 1 for d in range(7)}),
    dict(code="GG", name="Guardia Giorno", group="GUARDIA", color="#4f7cff", priority=20,
         time_bands="MATTINA,POMERIGGIO", skill_required="", days_set="",
         required_by_weekday={d: 1 for d in range(7)},
         notes="Skill richiesta da confermare"),
    dict(code="R1G", name="Reperibilità Giorno 1", group="GUARDIA", color="#7c9bff", priority=21,
         time_bands="POMERIGGIO", skill_required="PR", days_set="",
         required_by_weekday={d: 1 for d in range(7)}),
    dict(code="MODA", name="Corsia A", group="CORSIA", color="#1a9c5c", priority=40,
         time_bands="MATTINA,POMERIGGIO", skill_required="", days_set=",".join(map(str, FERIALI)),
         weekly_block=True, block_group="CORSIA"),
    dict(code="MODB", name="Corsia B", group="CORSIA", color="#1a9c5c", priority=41,
         time_bands="MATTINA,POMERIGGIO", skill_required="", days_set=",".join(map(str, FERIALI)),
         weekly_block=True, block_group="CORSIA"),
    dict(code="BREAST", name="Ambulatorio Breast", group="AMBULATORIO", color="#e05780", priority=30,
         time_bands="MATTINA", skill_required="BREAST", days_set=",".join(map(str, FERIALI)),
         required_by_weekday={0: 2, 1: 1, 2: 2, 3: 1, 4: 1}),
    dict(code="MEDIC", name="Ambulatorio Medic", group="AMBULATORIO", color="#f2994a", priority=55,
         time_bands="MATTINA", skill_required="", days_set=",".join(map(str, FERIALI)),
         required_by_weekday={d: 1 for d in FERIALI}),
    dict(code="VISLUN", name="Visite (Lunedì)", group="AMBULATORIO", color="#f2994a", priority=56,
         time_bands="MATTINA", skill_required="", days_set="0",
         required_by_weekday={0: 1}),
    dict(code="VISGIO", name="Visite (Giovedì)", group="AMBULATORIO", color="#f2994a", priority=57,
         time_bands="MATTINA", skill_required="", days_set="3",
         required_by_weekday={3: 1}),
    dict(code="CHIR", name="Ambulatorio Chir", group="AMBULATORIO", color="#f2994a", priority=58,
         time_bands="MATTINA", skill_required="", days_set=",".join(map(str, FERIALI)),
         required_by_weekday={d: 1 for d in FERIALI}),
    dict(code="PROCT", name="Ambulatorio Proct", group="AMBULATORIO", color="#f2994a", priority=59,
         time_bands="MATTINA", skill_required="", days_set=",".join(map(str, FERIALI)),
         required_by_weekday={d: 1 for d in FERIALI}),
    dict(code="GOMSUP", name="GOM Superiore", group="GOM", color="#b5750f", priority=60,
         time_bands="MATTINA", skill_required="", days_set=",".join(map(str, FERIALI)),
         required_by_weekday={d: 1 for d in FERIALI}),
    dict(code="GOMINF", name="GOM Inferiore", group="GOM", color="#b5750f", priority=61,
         time_bands="MATTINA", skill_required="", days_set=",".join(map(str, FERIALI)),
         required_by_weekday={d: 1 for d in FERIALI}),
    dict(code="CDP", name="CDP", group="EXTRA", color="#999999", priority=90,
         time_bands="MATTINA", skill_required="", is_extra=True),
    dict(code="ABB", name="ABB", group="EXTRA", color="#999999", priority=91,
         time_bands="MATTINA,POMERIGGIO", skill_required="", is_extra=True),
]


def seed_defaults():
    if ShiftType.query.count() > 0:
        return

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
    db.session.add(
        WeekendPatternRole(role_code="A", day="SAB", shift_type_id=created["GN"].id, skill_required="GN")
    )
    db.session.add(
        WeekendPatternRole(role_code="A", day="DOM", shift_type_id=created["R2N"].id, skill_required="SR")
    )

    Settings.get()
    db.session.commit()


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
            id=st.id, name=st.name, priority=st.priority,
            time_bands=set(parse_csv(st.time_bands)), skill_required=st.skill_required or "",
            days_set=st.days_set_list(), excluded_categories=st.excluded_category_list(),
            exclusive_day=st.exclusive_day, requires_rest_next_day=st.requires_rest_next_day,
            rest_exception_shift_type_id=st.rest_exception_shift_type_id, min_gap_days=st.min_gap_days,
            weekly_block=st.weekly_block, block_group=st.block_group, is_extra=st.is_extra,
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
    return render_template("employees.html", employees=people)


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
    st.priority = request.form.get("priority", type=int) or 100
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
        if weekday in req_by_wd:
            req_by_wd[weekday].required_staff = value
        else:
            db.session.add(ShiftRequirement(shift_type_id=st.id, weekday=weekday, required_staff=value))

    db.session.commit()
    flash(f"Turno '{st.name}' aggiornato.", "success")
    return redirect(url_for("main.shift_types"))


@bp.route("/turni/<int:shift_type_id>/elimina", methods=["POST"])
def delete_shift_type(shift_type_id):
    st = ShiftType.query.get_or_404(shift_type_id)
    db.session.delete(st)
    db.session.commit()
    flash(f"Turno '{st.name}' eliminato.", "success")
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


@bp.route("/preferenze/<int:pref_id>/elimina", methods=["POST"])
def delete_preference(pref_id):
    pref = Preference.query.get_or_404(pref_id)
    db.session.delete(pref)
    db.session.commit()
    flash("Preferenza eliminata.", "success")
    return redirect(url_for("main.preferences"))


# ---------------------------------------------------------------- regole
@bp.route("/regole", methods=["GET", "POST"])
def rules():
    settings = Settings.get()
    if request.method == "POST":
        settings.max_consecutive_work_days = request.form.get("max_consecutive_work_days", type=int) or 6
        db.session.commit()
        flash("Regole salvate.", "success")
        return redirect(url_for("main.rules"))
    return render_template("rules.html", settings=settings)


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
        shift_type_id = request.form.get("shift_type_id", type=int)
        day = request.form.get("day", type=int)
        if shift_type_id and day:
            db.session.add(
                Suppression(
                    shift_type_id=shift_type_id, year=year, month=month, day=day,
                    reason=request.form.get("reason", "").strip() or None,
                )
            )
            db.session.commit()
            flash("Soppressione aggiunta.", "success")
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
        grid.setdefault((a.day, a.shift_type_id), []).append(a.employee)

    day_weekday_names = [WEEKDAY_NAMES[(first_weekday + d) % 7] for d in range(num_days)]
    edit_mode = request.args.get("modifica") == "1"

    return render_template(
        "schedule.html", employees=people, shift_types=types, year=year, month=month,
        month_name=MONTH_NAMES[month], month_names=MONTH_NAMES, day_range=range(1, num_days + 1),
        day_weekday_names=day_weekday_names, grid=grid, edit_mode=edit_mode,
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

    result = generate_schedule(
        year=year, month=month, employees=employee_inputs,
        shift_types=list(shift_type_inputs_by_id.values()), weekend_roles=weekend_role_inputs,
        preferences=preference_inputs, availability=availability_map, suppressions=suppression_set,
        extra_activations=extra_activations, prev_month_last_shifts=prev_month_last_shifts,
        prev_month_weekend_count=prev_month_weekend_count,
        max_consecutive_work_days=settings.max_consecutive_work_days,
    )

    Assignment.query.filter_by(year=year, month=month, auto_generated=True).delete()
    for a in result.assignments:
        db.session.add(
            Assignment(
                employee_id=a["employee_id"], shift_type_id=a["shift_type_id"], year=year, month=month,
                day=a["day"], auto_generated=True,
            )
        )
    db.session.commit()

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

    Assignment.query.filter_by(year=year, month=month).delete()
    for day in range(1, num_days + 1):
        for st in types:
            selected_ids = request.form.getlist(f"slot_{day}_{st.id}")
            for emp_id in selected_ids:
                db.session.add(
                    Assignment(
                        employee_id=int(emp_id), shift_type_id=st.id, year=year, month=month, day=day,
                        auto_generated=False,
                    )
                )
    db.session.commit()
    flash("Pianificazione salvata.", "success")
    return redirect(url_for("main.schedule", anno=year, mese=month))


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
