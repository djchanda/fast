from datetime import datetime
import secrets
from app.extensions import db


class ApiKey(db.Model):
    """API keys for programmatic access (API-first mode)."""
    __tablename__ = "api_keys"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    name = db.Column(db.String(100), nullable=False)
    key_hash = db.Column(db.String(256), nullable=False, unique=True)
    key_prefix = db.Column(db.String(10), nullable=False)   # first 8 chars for display

    # Permissions: comma-separated scopes
    scopes = db.Column(db.String(300), default="validate:read")

    is_active = db.Column(db.Boolean, default=True)
    expires_at = db.Column(db.DateTime)

    last_used_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by = db.Column(db.String(100))

    user = db.relationship("User", backref=db.backref("api_keys", lazy="dynamic"))

    @staticmethod
    def generate():
        """Generate a new API key and return (raw_key, prefix, hash)."""
        import hashlib
        raw = "fast_" + secrets.token_urlsafe(32)
        prefix = raw[:12]
        key_hash = hashlib.sha256(raw.encode()).hexdigest()
        return raw, prefix, key_hash

    def scope_list(self):
        return [s.strip() for s in (self.scopes or "").split(",") if s.strip()]

    def __repr__(self):
        return f"<ApiKey id={self.id} prefix={self.key_prefix} name={self.name}>"
