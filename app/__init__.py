from pathlib import Path

from flask import Flask

from app.config import Config
from app.db import close_db, init_db


def create_app(config_class=Config):
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent.parent / "templates"),
        static_folder=str(Path(__file__).parent.parent / "static"),
    )
    app.config.from_object(config_class)

    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)

    app.teardown_appcontext(close_db)

    from app.routes.auth import bp as auth_bp
    from app.routes.jobs import bp as jobs_bp
    from app.routes.ledger_routes import bp as ledger_bp
    from app.routes.mock import bp as mock_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(jobs_bp)
    app.register_blueprint(ledger_bp)
    app.register_blueprint(mock_bp)

    from app.services.ledger import header_summary

    @app.context_processor
    def inject_ledger_header():
        # 登录页与静态资源不连库；其余页面统一注入跨日账本页眉
        try:
            return {"ledger_header": header_summary()}
        except Exception:
            return {"ledger_header": None}

    init_db(app)
    return app
