import os
import json
from dotenv import load_dotenv
from flask import Flask
from app.extensions import db

load_dotenv()


def create_app():
    app = Flask(__name__, instance_relative_config=True)

    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "fast-dev-secret-change-me")
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///fast.db"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    @app.after_request
    def _fix_iframe_headers(resp):
        resp.headers["X-Frame-Options"] = "SAMEORIGIN"
        return resp

    # Initialize db with this app
    db.init_app(app)
    with app.app_context():
        db.create_all()

    # Add JSON filter for templates
    @app.template_filter("from_json")
    def from_json_filter(value):
        if not value:
            return []
        try:
            return json.loads(value)
        except Exception:
            return []

    @app.template_filter("tojson_pretty")
    def tojson_pretty(value):
        try:
            if isinstance(value, str):
                value = json.loads(value)
            return json.dumps(value, indent=2)
        except Exception:
            return str(value)

    @app.template_filter("est")
    def est_filter(dt, fmt="%Y-%m-%d at %H:%M"):
        """Convert a naive UTC datetime to Eastern Time and format it."""
        if dt is None:
            return ""
        from datetime import timezone
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        try:
            from zoneinfo import ZoneInfo
            et = dt.astimezone(ZoneInfo("America/New_York"))
        except Exception:
            from datetime import timedelta
            # Fall back to fixed UTC-5 (EST) if zoneinfo unavailable
            et = dt.astimezone(timezone(timedelta(hours=-5)))
        return et.strftime(fmt)

    # Import models only after init_app (safe)
    from app import models  # noqa: F401

    # Register blueprints
    from app.routes.web import web_bp
    app.register_blueprint(web_bp)

    from app.routes.api import api_bp
    app.register_blueprint(api_bp)

    with app.app_context():
        from sqlalchemy.orm import configure_mappers
        try:
            configure_mappers()
        except Exception:
            pass
        db.create_all()

        # Run lightweight column migrations for existing DBs
        _run_migrations()

        # Seed default compliance standards if none exist
        _seed_compliance_standards()

        # Seed default admin user if none exist
        _seed_default_admin()

    # Start background scheduler for scheduled runs
    from app.services.scheduler import init_scheduler
    init_scheduler(app)

    return app


def _run_migrations():
    """Apply additive column migrations that db.create_all() won't handle."""
    from sqlalchemy import text, inspect
    engine = db.engine
    inspector = inspect(engine)

    # projects: add account, area, environment
    existing = {c["name"] for c in inspector.get_columns("projects")}
    with engine.connect() as conn:
        for col, typedef in [
            ("account", "VARCHAR(200)"),
            ("area", "VARCHAR(100)"),
            ("environment", "VARCHAR(50)"),
        ]:
            if col not in existing:
                conn.execute(text(f"ALTER TABLE projects ADD COLUMN {col} {typedef}"))
        conn.commit()


def _seed_compliance_standards():
    """Add built-in compliance standards on first run."""
    from app.models.compliance_standard import ComplianceStandard, ComplianceRequirement

    if ComplianceStandard.query.count() > 0:
        return

    standards = [
        {
            "code": "WCAG21",
            "name": "WCAG 2.1 AA",
            "description": "Web Content Accessibility Guidelines 2.1 Level AA",
            "version": "2.1",
            "url": "https://www.w3.org/TR/WCAG21/",
            "requirements": [
                ("1.1.1", "Non-text Content", "Provide text alternatives for non-text content.", "A"),
                ("1.3.1", "Info and Relationships", "Information conveyed by presentation can be programmatically determined.", "A"),
                ("1.4.3", "Contrast (Minimum)", "Visual presentation of text has a contrast ratio of at least 4.5:1.", "AA"),
                ("2.4.2", "Page Titled", "Web pages have titles that describe topic or purpose.", "A"),
                ("3.1.1", "Language of Page", "Default human language of page can be programmatically determined.", "A"),
                ("3.3.2", "Labels or Instructions", "Labels or instructions provided when content requires user input.", "A"),
            ],
        },
        {
            "code": "SECTION508",
            "name": "Section 508",
            "description": "US Federal accessibility requirements for electronic and information technology.",
            "version": "2018",
            "url": "https://www.section508.gov/",
            "requirements": [
                ("1194.22(a)", "Text Equivalents", "Provide text equivalent for every non-text element.", "Required"),
                ("1194.22(n)", "Electronic Forms", "Forms shall allow people using assistive technology to access information, field elements, and functionality.", "Required"),
            ],
        },
        {
            "code": "INTERNAL",
            "name": "Internal Policy",
            "description": "Organization-specific internal form standards.",
            "version": "1.0",
            "url": "",
            "requirements": [
                ("POL-001", "Form Numbering", "All forms must have a unique identifier in the header and footer.", "Required"),
                ("POL-002", "Approval Signatures", "All forms require authorized signatures before submission.", "Required"),
                ("POL-003", "Version Control", "Form version must be clearly labeled.", "Required"),
            ],
        },
    ]

    for s in standards:
        std = ComplianceStandard(
            code=s["code"],
            name=s["name"],
            description=s["description"],
            version=s["version"],
            url=s["url"],
        )
        db.session.add(std)
        db.session.flush()  # get std.id

        for req in s["requirements"]:
            r = ComplianceRequirement(
                standard_id=std.id,
                code=req[0],
                title=req[1],
                description=req[2],
                level=req[3],
            )
            db.session.add(r)

    db.session.commit()


def _seed_default_admin():
    """Create a default admin user if no users exist."""
    from app.models.user import User

    if User.query.count() > 0:
        return

    admin = User(
        username="admin",
        email="admin@fast.local",
        display_name="Administrator",
        role="admin",
        is_active=True,
    )
    admin.set_password("admin")  # Change in production via env var ADMIN_PASSWORD
    db.session.add(admin)

    # Also add reviewer and viewer demo users
    for uname, role in [("reviewer", "reviewer"), ("viewer", "viewer")]:
        u = User(username=uname, email=f"{uname}@fast.local", display_name=uname.title(), role=role, is_active=True)
        u.set_password(uname)
        db.session.add(u)

    db.session.commit()
