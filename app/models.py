from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

WEEKDAY_NAMES = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]

AVAILABLE = "AVAILABLE"
UNAVAILABLE = "UNAVAILABLE"
PREFERRED_OFF = "PREFERRED_OFF"
AVAILABILITY_STATUSES = [AVAILABLE, UNAVAILABLE, PREFERRED_OFF]


class Employee(db.Model):
    __tablename__ = "employee"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=True)
    max_shifts_per_week = db.Column(db.Integer, nullable=True)
    notes = db.Column(db.String(255), nullable=True)

    qualifications = db.relationship(
        "EmployeeQualification", back_populates="employee", cascade="all, delete-orphan"
    )
    availabilities = db.relationship(
        "Availability", back_populates="employee", cascade="all, delete-orphan"
    )
    assignments = db.relationship(
        "Assignment", back_populates="employee", cascade="all, delete-orphan"
    )

    def qualified_shift_type_ids(self):
        return {q.shift_type_id for q in self.qualifications if q.qualified}


class ShiftType(db.Model):
    __tablename__ = "shift_type"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    code = db.Column(db.String(10), nullable=False)
    start_time = db.Column(db.String(5), nullable=False, default="00:00")
    end_time = db.Column(db.String(5), nullable=False, default="00:00")
    color = db.Column(db.String(7), nullable=False, default="#4f7cff")
    sort_order = db.Column(db.Integer, nullable=False, default=0)

    requirements = db.relationship(
        "ShiftRequirement", back_populates="shift_type", cascade="all, delete-orphan"
    )

    def requirement_for_weekday(self, weekday):
        for r in self.requirements:
            if r.weekday == weekday:
                return r.required_staff
        return 0


class ShiftRequirement(db.Model):
    """Numero di persone richieste per un tipo di turno in un dato giorno della settimana (0=Lunedì)."""

    __tablename__ = "shift_requirement"
    __table_args__ = (db.UniqueConstraint("shift_type_id", "weekday", name="uq_shift_weekday"),)

    id = db.Column(db.Integer, primary_key=True)
    shift_type_id = db.Column(db.Integer, db.ForeignKey("shift_type.id"), nullable=False)
    weekday = db.Column(db.Integer, nullable=False)  # 0=Lunedì .. 6=Domenica
    required_staff = db.Column(db.Integer, nullable=False, default=0)

    shift_type = db.relationship("ShiftType", back_populates="requirements")


class EmployeeQualification(db.Model):
    """Se un dipendente è abilitato a coprire un certo tipo di turno."""

    __tablename__ = "employee_qualification"
    __table_args__ = (db.UniqueConstraint("employee_id", "shift_type_id", name="uq_emp_shift"),)

    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=False)
    shift_type_id = db.Column(db.Integer, db.ForeignKey("shift_type.id"), nullable=False)
    qualified = db.Column(db.Boolean, nullable=False, default=True)

    employee = db.relationship("Employee", back_populates="qualifications")
    shift_type = db.relationship("ShiftType")


class ForbiddenSequence(db.Model):
    """Vieta che un turno 'next' venga assegnato il giorno dopo un turno 'prev' per lo stesso dipendente."""

    __tablename__ = "forbidden_sequence"
    __table_args__ = (db.UniqueConstraint("prev_shift_type_id", "next_shift_type_id", name="uq_seq"),)

    id = db.Column(db.Integer, primary_key=True)
    prev_shift_type_id = db.Column(db.Integer, db.ForeignKey("shift_type.id"), nullable=False)
    next_shift_type_id = db.Column(db.Integer, db.ForeignKey("shift_type.id"), nullable=False)

    prev_shift_type = db.relationship("ShiftType", foreign_keys=[prev_shift_type_id])
    next_shift_type = db.relationship("ShiftType", foreign_keys=[next_shift_type_id])


class Settings(db.Model):
    """Riga singola con le regole globali di generazione."""

    __tablename__ = "settings"

    id = db.Column(db.Integer, primary_key=True)
    max_consecutive_work_days = db.Column(db.Integer, nullable=False, default=6)
    require_weekly_day_off = db.Column(db.Boolean, nullable=False, default=True)

    @classmethod
    def get(cls):
        settings = cls.query.first()
        if settings is None:
            settings = cls()
            db.session.add(settings)
            db.session.commit()
        return settings


class Availability(db.Model):
    __tablename__ = "availability"
    __table_args__ = (
        db.UniqueConstraint("employee_id", "year", "month", "day", name="uq_availability"),
    )

    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    month = db.Column(db.Integer, nullable=False)
    day = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(20), nullable=False, default=UNAVAILABLE)

    employee = db.relationship("Employee", back_populates="availabilities")


class Assignment(db.Model):
    __tablename__ = "assignment"
    __table_args__ = (
        db.UniqueConstraint(
            "employee_id", "year", "month", "day", "shift_type_id", name="uq_assignment"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=False)
    shift_type_id = db.Column(db.Integer, db.ForeignKey("shift_type.id"), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    month = db.Column(db.Integer, nullable=False)
    day = db.Column(db.Integer, nullable=False)
    auto_generated = db.Column(db.Boolean, nullable=False, default=True)

    employee = db.relationship("Employee", back_populates="assignments")
    shift_type = db.relationship("ShiftType")
