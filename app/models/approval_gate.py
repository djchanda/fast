from datetime import datetime
from app.extensions import db


class ApprovalGate(db.Model):
    """Approval gate: a run must pass this gate before being considered QA-cleared."""
    __tablename__ = "approval_gates"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False)
    run_id = db.Column(db.Integer, db.ForeignKey("runs.id"), nullable=False)

    # pending | approved | rejected
    status = db.Column(db.String(20), default="pending", nullable=False)

    # Criteria thresholds
    max_errors_allowed = db.Column(db.Integer, default=0)
    max_warnings_allowed = db.Column(db.Integer, default=5)

    # Who approved / rejected
    reviewed_by = db.Column(db.String(100))
    reviewed_at = db.Column(db.DateTime)
    review_note = db.Column(db.Text)

    # Require all findings resolved before approval
    require_all_resolved = db.Column(db.Boolean, default=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    run = db.relationship("Run", backref=db.backref("approval_gate", uselist=False))

    def __repr__(self):
        return f"<ApprovalGate id={self.id} run={self.run_id} status={self.status}>"
