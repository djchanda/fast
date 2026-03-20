from datetime import datetime
from app.extensions import db


class AuditLog(db.Model):
    """Immutable audit trail of all system actions."""
    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)

    # Who did it
    username = db.Column(db.String(100))
    user_ip = db.Column(db.String(50))

    # What happened
    action = db.Column(db.String(100), nullable=False)   # e.g. "run.created", "finding.resolved"
    resource_type = db.Column(db.String(50))              # e.g. "project", "run", "finding"
    resource_id = db.Column(db.Integer)
    project_id = db.Column(db.Integer)

    # Detail payload (JSON string)
    detail = db.Column(db.Text)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f"<AuditLog id={self.id} action={self.action} by={self.username}>"
