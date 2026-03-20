from datetime import datetime
from app.extensions import db


class FindingReview(db.Model):
    """Per-finding review status for the approval workflow."""
    __tablename__ = "finding_reviews"

    id = db.Column(db.Integer, primary_key=True)

    run_result_id = db.Column(db.Integer, db.ForeignKey("run_results.id"), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False)

    # Unique key within a result — index in the findings array + category
    finding_index = db.Column(db.Integer, nullable=False)
    finding_category = db.Column(db.String(60))   # e.g. "spelling_errors"
    finding_description = db.Column(db.Text)

    # Workflow: open | in_review | resolved | false_positive
    status = db.Column(db.String(30), default="open", nullable=False)

    # Assignment
    assigned_to = db.Column(db.String(100))       # username
    assigned_by = db.Column(db.String(100))
    assigned_at = db.Column(db.DateTime)

    # Resolution
    resolved_by = db.Column(db.String(100))
    resolved_at = db.Column(db.DateTime)
    resolution_note = db.Column(db.Text)

    # Compliance tag IDs (comma-separated)
    compliance_tags = db.Column(db.Text)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    run_result = db.relationship("RunResult", backref=db.backref("finding_reviews", lazy="dynamic"))

    def __repr__(self):
        return f"<FindingReview id={self.id} rr={self.run_result_id} idx={self.finding_index} status={self.status}>"
