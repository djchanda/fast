from datetime import datetime
from app.extensions import db


class JiraConfig(db.Model):
    """Per-project Jira Cloud integration configuration."""

    __tablename__ = "jira_configs"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(
        db.Integer, db.ForeignKey("projects.id"), nullable=False, unique=True
    )
    jira_url = db.Column(db.String(300), nullable=False)        # https://company.atlassian.net
    email = db.Column(db.String(200), nullable=False)           # Atlassian account email
    api_token = db.Column(db.Text, nullable=False)              # Atlassian API token
    jira_project_key = db.Column(db.String(20), nullable=False) # e.g. "QA", "FAST"
    issue_type = db.Column(db.String(50), default="Bug")
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by = db.Column(db.String(100))

    project = db.relationship("Project", backref=db.backref("jira_config", uselist=False))

    def issue_url(self, issue_key: str) -> str:
        """Return the full URL to a Jira issue."""
        return f"{self.jira_url.rstrip('/')}/browse/{issue_key}"
