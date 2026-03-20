from datetime import datetime
from app.extensions import db


class ScheduledRun(db.Model):
    """Cron-style scheduled test run configuration."""
    __tablename__ = "scheduled_runs"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False)

    name = db.Column(db.String(200), nullable=False)

    # Cron expression (e.g. "0 9 * * 1" = every Monday 9am)
    cron_expression = db.Column(db.String(100), nullable=False)

    # Comma-separated test case IDs to run
    testcase_ids = db.Column(db.Text, nullable=False)

    is_active = db.Column(db.Boolean, default=True)
    triggered_by = db.Column(db.String(100))

    last_run_at = db.Column(db.DateTime)
    next_run_at = db.Column(db.DateTime)
    last_run_id = db.Column(db.Integer)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    project = db.relationship("Project", backref=db.backref("scheduled_runs", lazy="dynamic"))

    def testcase_id_list(self):
        return [int(x.strip()) for x in (self.testcase_ids or "").split(",") if x.strip().isdigit()]

    def __repr__(self):
        return f"<ScheduledRun id={self.id} project={self.project_id} cron={self.cron_expression}>"
