from datetime import datetime
from app.extensions import db


class Run(db.Model):
    __tablename__ = "runs"

    id = db.Column(db.Integer, primary_key=True)

    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False)

    # execution stats
    status = db.Column(db.String(20), default="pending")
    total = db.Column(db.Integer, default=0)
    passed = db.Column(db.Integer, default=0)
    warnings = db.Column(db.Integer, default=0)
    errors = db.Column(db.Integer, default=0)

    # who ran it
    triggered_by = db.Column(db.String(200))

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    project = db.relationship("Project", back_populates="runs")
    results = db.relationship(
        "RunResult",
        back_populates="run",
        lazy="dynamic",
        cascade="all, delete-orphan",
    )

    def __repr__(self):
        return f"<Run id={self.id} project={self.project_id} status={self.status}>"
