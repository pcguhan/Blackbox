from datetime import datetime

from models import db


class AccessAllowlistEntry(db.Model):
    """An email address eligible for Exclusive Access."""

    __tablename__ = 'access_allowlist'

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    added_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
