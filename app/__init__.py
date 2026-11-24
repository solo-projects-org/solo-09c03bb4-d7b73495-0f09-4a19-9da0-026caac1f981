"""Flask 应用工厂。"""

from __future__ import annotations

import os

from flask import Flask

from . import db


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("FUND_SECRET_KEY", "dev-secret-change-me"),
        DATABASE=os.environ.get(
            "FUND_DB_PATH", os.path.join(app.instance_path, "fund.db")
        ),
        UPLOAD_FOLDER=os.environ.get(
            "FUND_UPLOAD_DIR", os.path.join(app.instance_path, "uploads")
        ),
        MAX_CONTENT_LENGTH=16 * 1024 * 1024,
    )
    if config:
        app.config.update(config)

    os.makedirs(os.path.dirname(app.config["DATABASE"]), exist_ok=True)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

    db.init_app(app)

    from .routes import bp

    app.register_blueprint(bp)
    return app
