from datetime import datetime
from app.extensions import db


class RunResult(db.Model):
    __tablename__ = "run_results"

    id = db.Column(db.Integer, primary_key=True)

    run_id = db.Column(db.Integer, db.ForeignKey("runs.id"), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False)

    test_case_id = db.Column(db.Integer, db.ForeignKey("test_cases.id"), nullable=False)
    form_id = db.Column(db.Integer, db.ForeignKey("forms.id"), nullable=True)

    mode = db.Column(db.String(20))

    status = db.Column(db.String(20), default="unknown")

    passed = db.Column(db.Integer, default=0)
    warnings = db.Column(db.Integer, default=0)
    errors = db.Column(db.Integer, default=0)

    result_json = db.Column(db.Text)
    summary_text = db.Column(db.Text)

    error_message = db.Column(db.Text)
    duration = db.Column(db.Float)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Visual comparison fields
    original_pdf_path = db.Column(db.String(500))
    expected_pdf_path = db.Column(db.String(500))
    visual_diff_images = db.Column(db.Text)  # store json list

    # NEW: path to generated html report
    report_html_path = db.Column(db.String(500))

    run = db.relationship("Run", back_populates="results")
    test_case = db.relationship("TestCase")

    def __repr__(self):
        return f"<RunResult id={self.id} run={self.run_id} tc={self.test_case_id} status={self.status}>"
