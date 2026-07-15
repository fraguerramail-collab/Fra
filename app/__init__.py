import os

import sqlalchemy as sa
from flask import Flask

from .models import db


def _add_missing_columns():
    """Aggiunge alle tabelle gia' esistenti le colonne nuove definite nei
    modelli ma assenti nel file .db (creato con una versione precedente
    dell'app). db.create_all() crea solo le tabelle mancanti, non altera
    quelle gia' presenti: questo evita di dover intervenire a mano sul
    database ogni volta che un modello guadagna un campo."""
    inspector = sa.inspect(db.engine)
    existing_tables = set(inspector.get_table_names())
    for table in db.metadata.tables.values():
        if table.name not in existing_tables:
            continue
        existing_cols = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing_cols:
                continue
            col_type = column.type.compile(db.engine.dialect)
            default_sql = ""
            if column.default is not None and getattr(column.default, "is_scalar", False):
                arg = column.default.arg
                default_sql = f" DEFAULT '{arg}'" if isinstance(arg, str) else f" DEFAULT {int(arg)}"
            with db.engine.begin() as conn:
                conn.execute(sa.text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}{default_sql}'))


def _drop_orphaned_not_null_columns():
    """Rimuove dalle tabelle esistenti le colonne presenti nel file .db ma
    non piu' definite nei modelli (es. un campo eliminato in un aggiornamento
    dell'app), quando sono NOT NULL senza un default: altrimenti ogni nuovo
    inserimento fallirebbe perche' quella colonna non riceve mai un valore."""
    inspector = sa.inspect(db.engine)
    existing_tables = set(inspector.get_table_names())
    for table in db.metadata.tables.values():
        if table.name not in existing_tables:
            continue
        model_cols = {c.name for c in table.columns}
        for col in inspector.get_columns(table.name):
            if col["name"] in model_cols or col["nullable"] or col["default"] is not None:
                continue
            try:
                with db.engine.begin() as conn:
                    conn.execute(sa.text(f'ALTER TABLE "{table.name}" DROP COLUMN "{col["name"]}"'))
            except Exception:
                # SQLite troppo vecchio per DROP COLUMN (serve 3.35+): ricrea la
                # tabella da zero secondo lo schema attuale, copiando i dati.
                _rebuild_table(table)


def _rebuild_table(table):
    tmp_name = f"{table.name}__rebuild"
    keep_cols = [c.name for c in table.columns]
    cols_csv = ", ".join(f'"{c}"' for c in keep_cols)
    with db.engine.begin() as conn:
        conn.execute(sa.text(f'ALTER TABLE "{table.name}" RENAME TO "{tmp_name}"'))
        table.create(conn)
        conn.execute(sa.text(f'INSERT INTO "{table.name}" ({cols_csv}) SELECT {cols_csv} FROM "{tmp_name}"'))
        conn.execute(sa.text(f'DROP TABLE "{tmp_name}"'))


def create_app(test_config=None, profile_slug="default", profile_name=None, instance_path=None):
    """Crea un'app Flask isolata su un proprio database SQLite.

    Ogni "profilo" (reparto) ha un file dati separato (data_<slug>.db): le
    modifiche fatte in un profilo non toccano gli altri. Vedi app/registry.py
    e run.py per come i profili vengono elencati/creati e serviti insieme.
    """
    app = Flask(__name__, instance_relative_config=True, instance_path=instance_path)
    os.makedirs(app.instance_path, exist_ok=True)

    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret-key-change-me"),
        SQLALCHEMY_DATABASE_URI="sqlite:///" + os.path.join(app.instance_path, f"data_{profile_slug}.db"),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        PROFILE_SLUG=profile_slug,
        PROFILE_NAME=profile_name or profile_slug,
    )
    if test_config:
        app.config.update(test_config)

    db.init_app(app)

    from . import routes

    app.register_blueprint(routes.bp)

    with app.app_context():
        db.create_all()
        _add_missing_columns()
        _drop_orphaned_not_null_columns()

    return app
