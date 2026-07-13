import os

from flask import Flask

from .models import db


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    os.makedirs(app.instance_path, exist_ok=True)

    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret-key-change-me"),
        SQLALCHEMY_DATABASE_URI="sqlite:///" + os.path.join(app.instance_path, "turni.db"),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    if test_config:
        app.config.update(test_config)

    db.init_app(app)

    from . import routes

    app.register_blueprint(routes.bp)

    with app.app_context():
        db.create_all()
        routes.seed_defaults()

    return app
