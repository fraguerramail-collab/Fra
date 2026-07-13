import os

from flask import Flask

from .models import db


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

    return app
