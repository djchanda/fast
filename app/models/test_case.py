from datetime import datetime
from app.extensions import db


class TestCase(db.Model):
    __tablename__ = "test_cases"

    id = db.Column(db.Integer, primary_key=True)

    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False)

    # The form you are validating (actual)
    form_id = db.Column(db.Integer, db.ForeignKey("forms.id"), nullable=False)

    # For benchmark mode (golden copy / expected)
    benchmark_form_id = db.Column(db.Integer, db.ForeignKey("forms.id"), nullable=True)

    name = db.Column(db.String(200), nullable=False)

    # basic | specific | benchmark
    mode = db.Column(db.String(20), nullable=False, default="basic")

    # For specific / benchmark prompts / steps
    prompt_text = db.Column(db.Text, nullable=True)

    expected_json = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    project = db.relationship("Project", back_populates="test_cases")

    # Two relationships to same table => must declare foreign_keys explicitly
    form = db.relationship("Form", foreign_keys=[form_id], back_populates="test_cases")
    benchmark_form = db.relationship("Form", foreign_keys=[benchmark_form_id])

    def __repr__(self):
        return f"<TestCase id={self.id} name={self.name} mode={self.mode}>"
