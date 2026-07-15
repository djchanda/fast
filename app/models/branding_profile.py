from datetime import datetime
from app.extensions import db


class BrandingProfile(db.Model):
    __tablename__ = "branding_profiles"

    id            = db.Column(db.Integer, primary_key=True)
    project_id    = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False, unique=True)
    company_name  = db.Column(db.String(200))
    tagline       = db.Column(db.String(300))
    primary_color = db.Column(db.String(7), default="#003087")
    logo_path     = db.Column(db.String(500))
    header_height = db.Column(db.Integer, default=60)
    footer_text   = db.Column(db.String(300))
    updated_at    = db.Column(db.DateTime, default=datetime.utcnow)

    project = db.relationship("Project", backref=db.backref("branding_profile", uselist=False))

    def __repr__(self):
        return f"<BrandingProfile project={self.project_id} company='{self.company_name}'>"
