from datetime import datetime
from app.extensions import db


class Form(db.Model):
    __tablename__ = "forms"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False)

    name = db.Column(db.String(200), nullable=False)
    file_path = db.Column(db.String(500), nullable=False)

    original_filename = db.Column(db.String(255))
    stored_filename = db.Column(db.String(255))
    size_bytes = db.Column(db.Integer)
    version = db.Column(db.String(20), default="v1")

    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)

    project = db.relationship("Project", back_populates="forms")

    # IMPORTANT: specify foreign_keys to avoid ambiguity with benchmark_form_id
    test_cases = db.relationship(
        "TestCase",
        back_populates="form",
        foreign_keys="TestCase.form_id",
        lazy="dynamic",
    )

    def __repr__(self):
        return f"<Form id={self.id} name={self.name}>"
