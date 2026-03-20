from datetime import datetime
from app.extensions import db


class WebhookConfig(db.Model):
    """Webhook endpoint configuration for a project."""
    __tablename__ = "webhook_configs"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False)

    name = db.Column(db.String(100), nullable=False)
    url = db.Column(db.String(500), nullable=False)
    secret = db.Column(db.String(200))           # HMAC signing secret

    # Comma-separated event names: run.completed, run.failed, gate.approved, gate.rejected
    events = db.Column(db.String(500), default="run.completed")

    is_active = db.Column(db.Boolean, default=True)

    # Last delivery status
    last_triggered_at = db.Column(db.DateTime)
    last_status_code = db.Column(db.Integer)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by = db.Column(db.String(100))

    project = db.relationship("Project", backref=db.backref("webhooks", lazy="dynamic"))

    def event_list(self):
        return [e.strip() for e in (self.events or "").split(",") if e.strip()]

    def __repr__(self):
        return f"<WebhookConfig id={self.id} project={self.project_id} url={self.url[:40]}>"
