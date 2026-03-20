from datetime import datetime
from app.extensions import db


class FindingComment(db.Model):
    """Discussion thread on a finding review."""
    __tablename__ = "finding_comments"

    id = db.Column(db.Integer, primary_key=True)
    finding_review_id = db.Column(db.Integer, db.ForeignKey("finding_reviews.id"), nullable=False)

    author = db.Column(db.String(100), nullable=False)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    finding_review = db.relationship("FindingReview", backref=db.backref("comments", lazy="dynamic"))

    def __repr__(self):
        return f"<FindingComment id={self.id} fr={self.finding_review_id} by={self.author}>"
