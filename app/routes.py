import calendar
import csv
import io
from datetime import date

from flask import Blueprint, flash, redirect, render_template, request, send_file, url_for

from .models import (
    AVAILABLE,
    AVAILABILITY_STATUSES,
    Assignment,
    Availability,
    EmployeeQualification,
    Employee,
    ForbiddenSequence,
    PREFERRED_OFF,
    Settings,
    ShiftRequirement,
    ShiftType,
    UNAVAILABLE,
    WEEKDAY_NAMES,
    db,
)
from .scheduler import EmployeeInput, ShiftTypeInput, generate_schedule

bp = Blueprint("main", __name__)

MONTH_NAMES = [
    "", "Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno",
    "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre",
]


def seed_defaults():
    """Crea dati di esempio minimi al primo avvio (solo se il DB è vuoto)."""
    if ShiftType.query.count() > 0:
        return

    defaults = [
        ("Mattina", "M", "07:00", "14:00", "#4f7cff"),
        ("Pomeriggio", "P", "14:00", "21:00", "#f2994a"),
        ("Notte", "N", "21:00", "07:00", "#7c5cbf"),
    ]
    for i, (name, code, start, end, color) in enumerate(defaults):
        st = ShiftType(name=name, code=code, start_time=start, end_time=end, color=color, sort_order=i)
        db.session.add(st)
        db.session.flush()
        for weekday in range(7):
            required = 1 if weekday < 5 else 1
            db.session.add(ShiftRequirement(shift_type_id=st.id, weekday=weekday, required_staff=required))

    Settings.get()
    db.session.commit()


def _current_year_month():
    today = date.today()
    year = request.args.get("anno", type=int) or today.year
    month = request.args.get("mese", type=int) or today.month
    return year, month


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
        name = request.form.get("name", "").strip()
        if name:
            max_shifts = request.form.get("max_shifts_per_week", type=int)
            emp = Employee(name=name, max_shifts_per_week=max_shifts)
            db.session.add(emp)
            db.session.flush()
            for st in ShiftType.query.all():
                db.session.add(EmployeeQualification(employee_id=emp.id, shift_type_id=st.id, qualified=True))
            db.session.commit()
            flash(f"Dipendente '{name}' aggiunto.", "success")
        return redirect(url_for("main.employees"))

    people = Employee.query.order_by(Employee.name).all()
    return render_template("employees.html", employees=people)


@bp.route("/dipendenti/<int:employee_id>/modifica", methods=["POST"])
def edit_employee(employee_id):
    emp = Employee.query.get_or_404(employee_id)
    emp.name = request.form.get("name", emp.name).strip() or emp.name
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
@bp.route("/turni", methods=["GET", "POST"])
def shift_types():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        code = request.form.get("code", "").strip() or name[:2].upper()
        if name:
            st = ShiftType(
                name=name,
                code=code,
                start_time=request.form.get("start_time", "00:00"),
                end_time=request.form.get("end_time", "00:00"),
                color=request.form.get("color", "#4f7cff"),
                sort_order=ShiftType.query.count(),
            )
            db.session.add(st)
            db.session.flush()
            for weekday in range(7):
                db.session.add(ShiftRequirement(shift_type_id=st.id, weekday=weekday, required_staff=0))
            for emp in Employee.query.all():
                db.session.add(EmployeeQualification(employee_id=emp.id, shift_type_id=st.id, qualified=True))
            db.session.commit()
            flash(f"Turno '{name}' creato.", "success")
        return redirect(url_for("main.shift_types"))

    types = ShiftType.query.order_by(ShiftType.sort_order).all()
    return render_template("shift_types.html", shift_types=types, weekday_names=WEEKDAY_NAMES)


@bp.route("/turni/<int:shift_type_id>/modifica", methods=["POST"])
def edit_shift_type(shift_type_id):
    st = ShiftType.query.get_or_404(shift_type_id)
    st.name = request.form.get("name", st.name).strip() or st.name
    st.code = request.form.get("code", st.code).strip() or st.code
    st.start_time = request.form.get("start_time", st.start_time)
    st.end_time = request.form.get("end_time", st.end_time)
    st.color = request.form.get("color", st.color)

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


# ---------------------------------------------------------------- qualifiche
@bp.route("/qualifiche", methods=["GET", "POST"])
def qualifications():
    people = Employee.query.filter_by(active=True).order_by(Employee.name).all()
    types = ShiftType.query.order_by(ShiftType.sort_order).all()

    if request.method == "POST":
        for emp in people:
            for st in types:
                qualified = request.form.get(f"q_{emp.id}_{st.id}") == "on"
                existing = EmployeeQualification.query.filter_by(employee_id=emp.id, shift_type_id=st.id).first()
                if existing:
                    existing.qualified = qualified
                else:
                    db.session.add(
                        EmployeeQualification(employee_id=emp.id, shift_type_id=st.id, qualified=qualified)
                    )
        db.session.commit()
        flash("Qualifiche aggiornate.", "success")
        return redirect(url_for("main.qualifications"))

    qual_map = {}
    for emp in people:
        qual_map[emp.id] = emp.qualified_shift_type_ids()

    return render_template("qualifications.html", employees=people, shift_types=types, qual_map=qual_map)


# ---------------------------------------------------------------- regole
@bp.route("/regole", methods=["GET", "POST"])
def rules():
    settings = Settings.get()
    types = ShiftType.query.order_by(ShiftType.sort_order).all()

    if request.method == "POST":
        settings.max_consecutive_work_days = request.form.get("max_consecutive_work_days", type=int) or 6
        settings.require_weekly_day_off = request.form.get("require_weekly_day_off") == "on"

        prev_id = request.form.get("prev_shift_type_id", type=int)
        next_id = request.form.get("next_shift_type_id", type=int)
        if request.form.get("action") == "add_sequence" and prev_id and next_id:
            exists = ForbiddenSequence.query.filter_by(
                prev_shift_type_id=prev_id, next_shift_type_id=next_id
            ).first()
            if not exists:
                db.session.add(ForbiddenSequence(prev_shift_type_id=prev_id, next_shift_type_id=next_id))
                flash("Regola di sequenza aggiunta.", "success")

        db.session.commit()
        return redirect(url_for("main.rules"))

    sequences = ForbiddenSequence.query.all()
    return render_template("rules.html", settings=settings, shift_types=types, sequences=sequences)


@bp.route("/regole/sequenze/<int:sequence_id>/elimina", methods=["POST"])
def delete_sequence(sequence_id):
    seq = ForbiddenSequence.query.get_or_404(sequence_id)
    db.session.delete(seq)
    db.session.commit()
    flash("Regola di sequenza eliminata.", "success")
    return redirect(url_for("main.rules"))


# ---------------------------------------------------------------- disponibilità
@bp.route("/disponibilita", methods=["GET", "POST"])
def availability():
    year, month = _current_year_month()
    num_days = calendar.monthrange(year, month)[1]
    people = Employee.query.filter_by(active=True).order_by(Employee.name).all()

    if request.method == "POST":
        for emp in people:
            for day in range(1, num_days + 1):
                status = request.form.get(f"a_{emp.id}_{day}", AVAILABLE)
                if status not in AVAILABILITY_STATUSES:
                    status = AVAILABLE
                existing = Availability.query.filter_by(
                    employee_id=emp.id, year=year, month=month, day=day
                ).first()
                if existing:
                    existing.status = status
                else:
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
        "availability.html",
        employees=people,
        year=year,
        month=month,
        month_name=MONTH_NAMES[month],
        num_days=num_days,
        day_range=range(1, num_days + 1),
        day_weekday_names=day_weekday_names,
        avail_map=avail_map,
        statuses=AVAILABILITY_STATUSES,
        month_names=MONTH_NAMES,
    )


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

    qual_map = {emp.id: emp.qualified_shift_type_ids() for emp in people}

    return render_template(
        "schedule.html",
        employees=people,
        shift_types=types,
        year=year,
        month=month,
        month_name=MONTH_NAMES[month],
        month_names=MONTH_NAMES,
        day_range=range(1, num_days + 1),
        day_weekday_names=day_weekday_names,
        grid=grid,
        qual_map=qual_map,
    )


@bp.route("/pianificazione/genera", methods=["POST"])
def generate():
    year = request.form.get("anno", type=int)
    month = request.form.get("mese", type=int)

    settings = Settings.get()
    people = Employee.query.filter_by(active=True).order_by(Employee.name).all()
    types = ShiftType.query.order_by(ShiftType.sort_order).all()

    employee_inputs = [
        EmployeeInput(
            id=emp.id,
            name=emp.name,
            max_shifts_per_week=emp.max_shifts_per_week,
            qualified_shift_type_ids=emp.qualified_shift_type_ids(),
        )
        for emp in people
    ]
    shift_type_inputs = [
        ShiftTypeInput(
            id=st.id,
            name=st.name,
            requirements_by_weekday={r.weekday: r.required_staff for r in st.requirements},
            sort_order=st.sort_order,
        )
        for st in types
    ]

    availability_rows = Availability.query.filter_by(year=year, month=month).all()
    availability_map = {(r.employee_id, r.day): r.status for r in availability_rows}

    forbidden = {(f.prev_shift_type_id, f.next_shift_type_id) for f in ForbiddenSequence.query.all()}

    result = generate_schedule(
        year=year,
        month=month,
        employees=employee_inputs,
        shift_types=shift_type_inputs,
        availability=availability_map,
        forbidden_sequences=forbidden,
        max_consecutive_work_days=settings.max_consecutive_work_days,
        require_weekly_day_off=settings.require_weekly_day_off,
    )

    Assignment.query.filter_by(year=year, month=month, auto_generated=True).delete()
    for a in result.assignments:
        db.session.add(
            Assignment(
                employee_id=a["employee_id"],
                shift_type_id=a["shift_type_id"],
                year=year,
                month=month,
                day=a["day"],
                auto_generated=True,
            )
        )
    db.session.commit()

    if result.warnings:
        flash(
            f"Turni generati con {len(result.warnings)} avviso/i di copertura insufficiente:", "warning"
        )
        for w in result.warnings[:20]:
            flash(w, "warning-detail")
    else:
        flash("Turni generati correttamente, copertura completa.", "success")

    return redirect(url_for("main.schedule", anno=year, mese=month))


@bp.route("/pianificazione/salva", methods=["POST"])
def save_schedule():
    year = request.form.get("anno", type=int)
    month = request.form.get("mese", type=int)
    num_days = calendar.monthrange(year, month)[1]

    people = Employee.query.filter_by(active=True).all()
    types = ShiftType.query.all()

    Assignment.query.filter_by(year=year, month=month).delete()
    for day in range(1, num_days + 1):
        for st in types:
            selected_ids = request.form.getlist(f"slot_{day}_{st.id}")
            for emp_id in selected_ids:
                db.session.add(
                    Assignment(
                        employee_id=int(emp_id),
                        shift_type_id=st.id,
                        year=year,
                        month=month,
                        day=day,
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
            names = grid.get((day, st.id), [])
            row.append(", ".join(names))
        writer.writerow(row)

    mem = io.BytesIO(output.getvalue().encode("utf-8-sig"))
    filename = f"turni_{year}_{month:02d}.csv"
    return send_file(mem, mimetype="text/csv", as_attachment=True, download_name=filename)
