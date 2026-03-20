from datetime import datetime
from app.extensions import db


class ComplianceStandard(db.Model):
    """A compliance framework/standard definition (e.g. WCAG 2.1, Section 508)."""
    __tablename__ = "compliance_standards"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), nullable=False, unique=True)   # e.g. "WCAG21"
    name = db.Column(db.String(200), nullable=False)               # e.g. "WCAG 2.1 AA"
    description = db.Column(db.Text)
    version = db.Column(db.String(20))
    url = db.Column(db.String(500))                                # link to standard
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    requirements = db.relationship(
        "ComplianceRequirement", backref="standard", lazy="dynamic", cascade="all, delete-orphan"
    )

    def __repr__(self):
        return f"<ComplianceStandard code={self.code} name={self.name}>"


class ComplianceRequirement(db.Model):
    """A specific requirement within a compliance standard."""
    __tablename__ = "compliance_requirements"

    id = db.Column(db.Integer, primary_key=True)
    standard_id = db.Column(db.Integer, db.ForeignKey("compliance_standards.id"), nullable=False)
    code = db.Column(db.String(50), nullable=False)    # e.g. "1.1.1"
    title = db.Column(db.String(300), nullable=False)
    description = db.Column(db.Text)
    level = db.Column(db.String(10))                   # A, AA, AAA, etc.

    def __repr__(self):
        return f"<ComplianceRequirement {self.standard_id}.{self.code}: {self.title[:40]}>"
