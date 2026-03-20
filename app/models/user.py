from datetime import datetime
from app.extensions import db
import hashlib
import os


class User(db.Model):
    """User with role-based access control."""
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), nullable=False, unique=True)
    email = db.Column(db.String(200), unique=True)
    display_name = db.Column(db.String(200))

    # Role: admin | reviewer | viewer
    role = db.Column(db.String(20), nullable=False, default="viewer")

    password_hash = db.Column(db.String(256))
    password_salt = db.Column(db.String(64))

    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime)

    # SSO / OIDC support
    sso_provider = db.Column(db.String(50))   # e.g. "google", "azure", None
    sso_subject = db.Column(db.String(200))    # provider's user id

    # White-label / org branding
    organization = db.Column(db.String(200))

    def set_password(self, plaintext: str):
        salt = os.urandom(32).hex()
        self.password_salt = salt
        self.password_hash = hashlib.sha256((salt + plaintext).encode()).hexdigest()

    def check_password(self, plaintext: str) -> bool:
        if not self.password_hash or not self.password_salt:
            return False
        check = hashlib.sha256((self.password_salt + plaintext).encode()).hexdigest()
        return check == self.password_hash

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_reviewer(self) -> bool:
        return self.role in ("admin", "reviewer")

    def __repr__(self):
        return f"<User id={self.id} username={self.username} role={self.role}>"
