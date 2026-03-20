from datetime import datetime
from app.extensions import db


class FalsePositive(db.Model):
    """Learned false positives — suppresses similar findings in future runs."""
    __tablename__ = "false_positives"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False)

    # Pattern to match future findings
    category = db.Column(db.String(60), nullable=False)    # finding category
    pattern = db.Column(db.Text, nullable=False)           # keyword/phrase to suppress
    match_mode = db.Column(db.String(20), default="contains")  # contains | exact | regex

    # Scope: project-wide or form-specific
    form_id = db.Column(db.Integer, db.ForeignKey("forms.id"), nullable=True)

    created_by = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Stats
    suppressed_count = db.Column(db.Integer, default=0)

    is_active = db.Column(db.Boolean, default=True)

    project = db.relationship("Project", backref=db.backref("false_positives", lazy="dynamic"))

    def __repr__(self):
        return f"<FalsePositive id={self.id} category={self.category} pattern={self.pattern[:30]}>"
