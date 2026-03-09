from app.extensions import db
from datetime import datetime

class Project(db.Model):
    __tablename__ = "projects"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    forms = db.relationship("Form", back_populates="project", lazy="dynamic", cascade="all, delete-orphan")
    test_cases = db.relationship("TestCase", back_populates="project", lazy="dynamic", cascade="all, delete-orphan")
    runs = db.relationship("Run", back_populates="project", lazy="dynamic", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Project id={self.id} name={self.name}>"
