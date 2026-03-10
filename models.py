from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin

db = SQLAlchemy()


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=True)  # null for Google-only users
    name = db.Column(db.String(255), nullable=True)
    google_id = db.Column(db.String(255), unique=True, nullable=True)
    avatar_url = db.Column(db.String(512), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    analyses = db.relationship("Analysis", backref="user", lazy=True)


class Analysis(db.Model):
    __tablename__ = "analyses"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    filename = db.Column(db.String(255), nullable=True)
    timestamp = db.Column(db.String(50), nullable=True)
    verdict = db.Column(db.String(50), nullable=True)
    global_score = db.Column(db.Integer, nullable=True)
    slide_type = db.Column(db.String(50), nullable=True)
    scores_json = db.Column(db.Text, nullable=True)   # JSON string
    result_json = db.Column(db.Text, nullable=True)   # Full result JSON
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        import json
        return {
            "id": self.id,
            "filename": self.filename,
            "timestamp": self.timestamp,
            "verdict": self.verdict,
            "global_score": self.global_score,
            "slide_type": self.slide_type,
            "scores": json.loads(self.scores_json) if self.scores_json else {},
        }
