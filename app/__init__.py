import os
import json
from dotenv import load_dotenv
from flask import Flask
from app.extensions import db

load_dotenv()

def create_app():
    app = Flask(__name__, instance_relative_config=True)

    app.config["SECRET_KEY"] = "dev"
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///fast.db"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    @app.after_request
    def _fix_iframe_headers(resp):
        # allow embedding reports in iframe on same site
        resp.headers["X-Frame-Options"] = "SAMEORIGIN"
        return resp

    # Initialize db with this app
    db.init_app(app)
    with app.app_context():
        db.create_all()

    # Add JSON filter for templates
    @app.template_filter('from_json')
    def from_json_filter(value):
        if not value:
            return []
        try:
            return json.loads(value)
        except:
            return []

    # Import models only after init_app (safe)
    from app import models  # noqa: F401

    # Register routes after init_app
    from app.routes.web import web_bp
    app.register_blueprint(web_bp)

    with app.app_context():
        # Force registry configuration
        from sqlalchemy.orm import configure_mappers
        try:
            configure_mappers()
        except Exception:
            pass
        db.create_all()

    return app
