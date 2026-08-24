from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

WEEKDAY_NAMES = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]
SATURDAY, SUNDAY = 5, 6

TIME_BANDS = ["MATTINA", "POMERIGGIO", "NOTTE"]

AVAILABLE = "OK"
UNAVAILABLE = "NO"
FERIE = "FERIE"
MALATTIA = "MAL"
PARTIAL_PREFIX = "NO_"  # es. NO_MATTINA, NO_POMERIGGIO, NO_NOTTE, combinabili con "|"

PREFERENCE_TYPES = ["EVITA", "PREFERISCI", "RISERVA", "MAX_MESE"]


def parse_csv(value):
    return [x.strip() for x in (value or "").split(",") if x.strip()]


def parse_days_set(value):
    """'0,1,4' -> {0,1,4}. Vuoto = tutti i giorni."""
    return {int(x) for x in parse_csv(value)}


class Employee(db.Model):
    __tablename__ = "employee"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), nullable=False, unique=True)
    name = db.Column(db.String(120), nullable=False)
    category = db.Column(db.String(40), nullable=True)  # "fascia": es. STRUTTURATO, SP
    skills = db.Column(db.String(255), nullable=True)  # CSV: "PR,SR,BREAST"
    active = db.Column(db.Boolean, nullable=False, default=True)
    max_shifts_per_week = db.Column(db.Integer, nullable=True)
    notes = db.Column(db.String(255), nullable=True)
    night_shift_exempt = db.Column(db.Boolean, nullable=False, default=False)  # es. post-maternita': esclude
    # dai turni con fascia NOTTE (GN, reperibilita' notte...), attivabile/disattivabile quando serve

    availabilities = db.relationship(
        "Availability", back_populates="employee", cascade="all, delete-orphan"
    )
    assignments = db.relationship(
        "Assignment", back_populates="employee", cascade="all, delete-orphan"
    )

    def skill_list(self):
        return set(parse_csv(self.skills))

    def has_skill(self, skill_formula):
        """skill_formula puo' essere 'PR|SR' (OR). Vuoto = nessuna skill richiesta."""
        required = [s.strip() for s in (skill_formula or "").split("|") if s.strip()]
        if not required:
            return True
        return bool(self.skill_list() & set(required))


class ShiftType(db.Model):
    __tablename__ = "shift_type"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), nullable=False, unique=True)
    name = db.Column(db.String(80), nullable=False)
    group = db.Column(db.String(40), nullable=True)  # NOTTE, GUARDIA, CORSIA, AMBULATORIO, EXTRA, EXTRA_RN, SEDE...
    color = db.Column(db.String(7), nullable=False, default="#4f7cff")
    sort_order = db.Column(db.Integer, nullable=False, default=0)

    time_bands = db.Column(db.String(60), nullable=True)  # CSV tra TIME_BANDS
    skill_required = db.Column(db.String(120), nullable=True)  # "PR|SR"
    days_set = db.Column(db.String(30), nullable=True)  # CSV weekday int, vuoto = tutti i giorni
    excluded_categories = db.Column(db.String(120), nullable=True)  # CSV fasce escluse, es. "SP"

    exclusive_day = db.Column(db.Boolean, nullable=False, default=False)  # es. GN: nessun altro turno lo stesso giorno
    exclusive_day_exception_shift_type_id = db.Column(db.Integer, db.ForeignKey("shift_type.id"), nullable=True)
    # turno ammesso a coesistere lo stesso giorno anche se questo e' 'esclusivo', ma SOLO sabato/domenica
    # (es. GN esclusivo in settimana, ma nel weekend il ruolo B copre anche la reperibilita' dello stesso
    # giorno insieme alla notte); in settimana l'esclusivita' resta valida anche per il turno in eccezione
    requires_rest_next_day = db.Column(db.Boolean, nullable=False, default=False)  # smonto
    rest_exception_shift_type_id = db.Column(db.Integer, db.ForeignKey("shift_type.id"), nullable=True)
    min_gap_days = db.Column(db.Integer, nullable=False, default=0)  # distanza minima tra due occorrenze stesso turno
    min_gap_excludes_weekend = db.Column(db.Boolean, nullable=False, default=False)  # non conta sabato+domenica
    # dello stesso weekend (utile per reperibilita' che devono restare consecutive nel weekend)

    weekly_block = db.Column(db.Boolean, nullable=False, default=False)  # assegnato per settimana intera (corsia)
    weekly_block_strictness = db.Column(db.Integer, nullable=False, default=10)  # 0=flessibile .. 10=ferrea
    block_group = db.Column(db.String(40), nullable=True)  # raggruppa piu' turni weekly_block per l'equita'
    block_cooldown_weeks = db.Column(db.Integer, nullable=False, default=0)  # 0=nessun raffreddamento: chi
    # possiede una settimana su un turno di questo block_group non puo' possederne un'altra (in nessun turno
    # dello stesso gruppo) prima che passino tante settimane quante impostate qui (conta anche il mese prima)

    is_extra = db.Column(db.Boolean, nullable=False, default=False)  # attivabile solo su date specifiche
    balance_pool = db.Column(db.String(40), nullable=True)  # pool di equita' separato (es. punti economici extra)

    report_column = db.Column(db.String(40), nullable=True)  # turni diversi (giorni/orari) mostrati nella
    # stessa colonna in pianificazione/report/CSV (es. "Visite lun"+"Visite gio" -> stessa colonna "VISITE").
    # Restano turni distinti a tutti gli effetti per la generazione automatica: cambia solo la visualizzazione.

    rest_exception_shift_type = db.relationship(
        "ShiftType", remote_side=[id], foreign_keys=[rest_exception_shift_type_id]
    )
    requirements = db.relationship(
        "ShiftRequirement", back_populates="shift_type", cascade="all, delete-orphan"
    )

    def time_band_list(self):
        return set(parse_csv(self.time_bands))

    def days_set_list(self):
        return parse_days_set(self.days_set)

    def excluded_category_list(self):
        return set(parse_csv(self.excluded_categories))

    def requirement_for_weekday(self, weekday):
        for r in self.requirements:
            if r.weekday == weekday:
                return r.required_staff
        return 0

    def skill_override_for_weekday(self, weekday):
        for r in self.requirements:
            if r.weekday == weekday:
                return r.skill_override or ""
        return ""


class ShiftRequirement(db.Model):
    """Personale richiesto per un turno in un giorno della settimana (0=Lun). Non usato per i turni weekend a ruoli o extra."""

    __tablename__ = "shift_requirement"
    __table_args__ = (db.UniqueConstraint("shift_type_id", "weekday", name="uq_shift_weekday"),)

    id = db.Column(db.Integer, primary_key=True)
    shift_type_id = db.Column(db.Integer, db.ForeignKey("shift_type.id"), nullable=False)
    weekday = db.Column(db.Integer, nullable=False)
    required_staff = db.Column(db.Integer, nullable=False, default=0)
    skill_override = db.Column(db.String(120), nullable=True)  # se valorizzata, sostituisce skill_required SOLO quel giorno

    shift_type = db.relationship("ShiftType", back_populates="requirements")


class WeekendPatternRole(db.Model):
    """Definisce i ruoli del weekend (es. A/B/C/D): quale turno sab/dom copre ciascun ruolo e con quale skill."""

    __tablename__ = "weekend_pattern_role"

    id = db.Column(db.Integer, primary_key=True)
    role_code = db.Column(db.String(10), nullable=False)
    day = db.Column(db.String(3), nullable=False)  # 'SAB' o 'DOM'
    shift_type_id = db.Column(db.Integer, db.ForeignKey("shift_type.id"), nullable=False)
    skill_required = db.Column(db.String(120), nullable=True)  # se vuoto, usa skill_required del turno

    shift_type = db.relationship("ShiftType")

    def effective_skill(self):
        return self.skill_required or self.shift_type.skill_required


class Preference(db.Model):
    """Preferenza pesata: EVITA/PREFERISCI/RISERVA/MAX_MESE per medico+turno+giorni."""

    __tablename__ = "preference"

    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=True)  # null = tutti
    shift_type_id = db.Column(db.Integer, db.ForeignKey("shift_type.id"), nullable=True)  # null = tutti
    days_set = db.Column(db.String(30), nullable=True)  # CSV weekday int, null = tutti
    time_bands = db.Column(db.String(60), nullable=True)  # CSV fasce (MATTINA/POMERIGGIO/NOTTE), null = tutte
    pref_type = db.Column(db.String(20), nullable=False)  # EVITA/PREFERISCI/RISERVA/MAX_MESE
    weight = db.Column(db.Integer, nullable=False, default=1)  # per MAX_MESE: e' il limite mensile

    employee = db.relationship("Employee")
    shift_type = db.relationship("ShiftType")

    def days_set_list(self):
        return parse_days_set(self.days_set)

    def time_bands_list(self):
        return parse_csv(self.time_bands)


class SkillRule(db.Model):
    """Regola trasversale legata a una skill oppure a una categoria/fascia
    (non a un turno specifico). Una riga usa l'uno O l'altro campo, non
    entrambi.

    BLOCK: chi ha questa skill (o questa categoria, es. 'specializzando')
    non e' assegnabile a NESSUN turno in questo giorno della settimana
    (es. impegnato in un'attivita' fuori app, oppure escluso dai weekend).
    RESERVE: tra chi ha questa skill, almeno 'min_free' restano liberi
    (non assegnati a nulla) in questo giorno della settimana (solo skill).
    """

    __tablename__ = "skill_rule"

    id = db.Column(db.Integer, primary_key=True)
    skill = db.Column(db.String(60), nullable=False, default="")  # vuota se la regola e' per categoria
    category = db.Column(db.String(60), nullable=True)
    weekday = db.Column(db.Integer, nullable=False)  # 0=Lunedì .. 6=Domenica
    mode = db.Column(db.String(10), nullable=False)  # BLOCK / RESERVE
    min_free = db.Column(db.Integer, nullable=False, default=1)


class Settings(db.Model):
    __tablename__ = "settings"

    id = db.Column(db.Integer, primary_key=True)
    max_consecutive_work_days = db.Column(db.Integer, nullable=False, default=6)
    last_year = db.Column(db.Integer, nullable=True)  # ultimo mese/anno usato, per riproporlo di default
    last_month = db.Column(db.Integer, nullable=True)

    @classmethod
    def get(cls):
        settings = cls.query.first()
        if settings is None:
            settings = cls()
            db.session.add(settings)
            db.session.commit()
        return settings


class LockedMonth(db.Model):
    """Mese 'finalizzato': blocca modifiche manuali e rigenerazione automatica
    dei turni per quel mese, cosi' i dati usati come memoria dai mesi
    successivi (smonti, settimane di corsia, distanze minime...) non possono
    piu' essere alterati per sbaglio."""

    __tablename__ = "locked_month"
    __table_args__ = (db.UniqueConstraint("year", "month", name="uq_locked_month"),)

    id = db.Column(db.Integer, primary_key=True)
    year = db.Column(db.Integer, nullable=False)
    month = db.Column(db.Integer, nullable=False)

    @classmethod
    def is_locked(cls, year, month):
        return cls.query.filter_by(year=year, month=month).first() is not None


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
    status = db.Column(db.String(40), nullable=False, default=AVAILABLE)  # OK/NO/FERIE/MAL/NO_MATTINA|NO_NOTTE...

    employee = db.relationship("Employee", back_populates="availabilities")

    def availability_info(self):
        """Ritorna (bloccato_tutto_il_giorno: bool, fasce_bloccate: set)."""
        if self.status == AVAILABLE:
            return False, set()
        if self.status in (UNAVAILABLE, FERIE, MALATTIA):
            return True, set()
        bands = set()
        for token in self.status.split("|"):
            token = token.strip()
            if token.startswith(PARTIAL_PREFIX):
                bands.add(token[len(PARTIAL_PREFIX):])
        return False, bands


class Suppression(db.Model):
    """Sopprime un turno obbligatorio in una data specifica (es. attivita' sospesa)."""

    __tablename__ = "suppression"

    id = db.Column(db.Integer, primary_key=True)
    shift_type_id = db.Column(db.Integer, db.ForeignKey("shift_type.id"), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    month = db.Column(db.Integer, nullable=False)
    day = db.Column(db.Integer, nullable=False)
    reason = db.Column(db.String(255), nullable=True)
    active = db.Column(db.Boolean, nullable=False, default=True)

    shift_type = db.relationship("ShiftType")


class ExtraShiftActivation(db.Model):
    """Attiva un turno extra (is_extra=True) in una data specifica."""

    __tablename__ = "extra_shift_activation"

    id = db.Column(db.Integer, primary_key=True)
    shift_type_id = db.Column(db.Integer, db.ForeignKey("shift_type.id"), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    month = db.Column(db.Integer, nullable=False)
    day = db.Column(db.Integer, nullable=False)
    reason = db.Column(db.String(255), nullable=True)
    active = db.Column(db.Boolean, nullable=False, default=True)

    shift_type = db.relationship("ShiftType")


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
