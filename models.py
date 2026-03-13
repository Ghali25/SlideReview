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

    # Subscription
    trial_count = db.Column(db.Integer, default=5)
    subscription_plan = db.Column(db.String(50), nullable=True)    # 'starter' | 'pro'
    subscription_status = db.Column(db.String(50), nullable=True)  # 'active' | 'canceled'
    stripe_customer_id = db.Column(db.String(255), nullable=True)
    stripe_subscription_id = db.Column(db.String(255), nullable=True)
    is_admin = db.Column(db.Boolean, default=False)

    analyses = db.relationship("Analysis", backref="user", lazy=True)

    STARTER_MONTHLY_LIMIT = 30

    @property
    def plan_level(self):
        if self.subscription_status == 'active':
            return self.subscription_plan
        return 'free'

    @property
    def monthly_analyses_count(self):
        """Nombre d'analyses effectuées ce mois-ci (pour le plafond Starter)."""
        now = datetime.utcnow()
        start = datetime(now.year, now.month, 1)
        return Analysis.query.filter(
            Analysis.user_id == self.id,
            Analysis.created_at >= start
        ).count()

    @property
    def can_analyze(self):
        level = self.plan_level
        if level == 'pro':
            return True
        if level == 'starter':
            return self.monthly_analyses_count < self.STARTER_MONTHLY_LIMIT
        # free
        return (self.trial_count or 0) > 0


class Deck(db.Model):
    __tablename__ = "decks"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    filename = db.Column(db.String(255), nullable=True)
    slides_count = db.Column(db.Integer, default=0)
    global_score = db.Column(db.Integer, nullable=True)
    global_verdict = db.Column(db.String(50), nullable=True)
    summary_json = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    analyses = db.relationship("Analysis", backref="deck", lazy=True,
                               foreign_keys="Analysis.deck_id")

    def to_dict(self):
        import json
        return {
            "id": self.id,
            "filename": self.filename,
            "slides_count": self.slides_count,
            "global_score": self.global_score,
            "global_verdict": self.global_verdict,
            "summary": json.loads(self.summary_json) if self.summary_json else {},
            "created_at": self.created_at.strftime("%d/%m/%Y %H:%M") if self.created_at else None,
        }


class Analysis(db.Model):
    __tablename__ = "analyses"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    deck_id = db.Column(db.Integer, db.ForeignKey("decks.id"), nullable=True)
    filename = db.Column(db.String(255), nullable=True)
    timestamp = db.Column(db.String(50), nullable=True)
    verdict = db.Column(db.String(50), nullable=True)
    global_score = db.Column(db.Integer, nullable=True)
    slide_type = db.Column(db.String(50), nullable=True)
    scores_json = db.Column(db.Text, nullable=True)   # JSON string
    result_json = db.Column(db.Text, nullable=True)   # Full result JSON
    thumbnail   = db.Column(db.Text, nullable=True)   # base64 JPEG data URL
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

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
            "thumbnail": self.thumbnail,
        }

    def to_full_dict(self):
        import json
        d = self.to_dict()
        d["result"] = json.loads(self.result_json) if self.result_json else {}
        return d
