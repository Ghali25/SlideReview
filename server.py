import os
import json
import base64
import re
import stripe
from datetime import datetime
from functools import wraps
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory, redirect, url_for, session
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from flask_bcrypt import Bcrypt
from flask_mail import Mail, Message
from authlib.integrations.flask_client import OAuth
from anthropic import Anthropic
from dotenv import load_dotenv
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature
from models import db, User, Analysis

load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB max
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-change-in-prod")
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///slidereview.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# Flask-Mail
app.config["MAIL_SERVER"] = os.getenv("MAIL_SERVER", "smtp.gmail.com")
app.config["MAIL_PORT"] = int(os.getenv("MAIL_PORT", 587))
app.config["MAIL_USE_TLS"] = os.getenv("MAIL_USE_TLS", "true").lower() == "true"
app.config["MAIL_USERNAME"] = os.getenv("MAIL_USERNAME", "")
app.config["MAIL_PASSWORD"] = os.getenv("MAIL_PASSWORD", "")
app.config["MAIL_DEFAULT_SENDER"] = os.getenv("MAIL_DEFAULT_SENDER", os.getenv("MAIL_USERNAME", "noreply@slidereview.app"))

# Fix postgres:// → postgresql:// (Railway uses old format)
if app.config["SQLALCHEMY_DATABASE_URI"].startswith("postgres://"):
    app.config["SQLALCHEMY_DATABASE_URI"] = app.config["SQLALCHEMY_DATABASE_URI"].replace("postgres://", "postgresql://", 1)

db.init_app(app)
bcrypt = Bcrypt(app)
mail = Mail(app)

login_manager = LoginManager(app)
login_manager.login_view = None  # We handle redirects manually via JSON

# Create tables on first request (works with gunicorn)
with app.app_context():
    db.create_all()
    # Auto-promote admin user if ADMIN_EMAIL is set
    admin_email = os.getenv("ADMIN_EMAIL", "").strip().lower()
    if admin_email:
        admin_user = User.query.filter_by(email=admin_email).first()
        if admin_user and not admin_user.is_admin:
            admin_user.is_admin = True
            db.session.commit()

oauth = OAuth(app)
google = oauth.register(
    name="google",
    client_id=os.getenv("GOOGLE_CLIENT_ID"),
    client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICES = {
    "starter": os.getenv("STRIPE_STARTER_PRICE_ID", ""),
    "pro": os.getenv("STRIPE_PRO_PRICE_ID", ""),
}

def get_reset_serializer():
    return URLSafeTimedSerializer(app.config["SECRET_KEY"], salt="password-reset")

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            return jsonify({"error": "Accès refusé"}), 403
        return f(*args, **kwargs)
    return decorated

def price_to_plan(price_id):
    for plan, pid in STRIPE_PRICES.items():
        if pid and pid == price_id:
            return plan
    return None

SKILL_PATH = Path(__file__).parent / "skills/slide-reviewer/SKILL.md"


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


def get_system_prompt():
    if SKILL_PATH.exists():
        content = SKILL_PATH.read_text()
        content = re.sub(r'^---.*?---\s*', '', content, flags=re.DOTALL)
        return content.strip()
    return "You are a senior consulting partner reviewing presentation slides."


SYSTEM_PROMPT = get_system_prompt()

ANALYSIS_PROMPT = """Analyse cette slide en tant que Senior Partner de cabinet de conseil.

Réponds UNIQUEMENT avec un objet JSON valide, sans markdown, sans backticks, respectant exactement ce schéma :

{
  "verdict": "PRÊT POUR LE CLIENT" | "À RETRAVAILLER" | "REFAIRE",
  "global_score": <entier 0-100>,
  "five_second_test": "<ce qu'un dirigeant comprend en lisant uniquement le titre pendant 5 secondes>",
  "slide_type": "recommandation" | "etat_des_lieux" | "comparaison" | "tendance" | "processus" | "kpis" | "minto" | "before_after",
  "scores": {
    "structure": <entier 0-100>,
    "design": <entier 0-100>,
    "impact": <entier 0-100>,
    "message": <entier 0-100>
  },
  "dimensions": {
    "structure": {
      "positifs": ["<point>", ...],
      "problemes": ["<problème spécifique>", ...],
      "recommandation": "<action concrète>"
    },
    "design": {
      "positifs": ["<point>", ...],
      "problemes": ["<problème spécifique>", ...],
      "recommandation": "<action concrète>"
    },
    "impact": {
      "positifs": ["<point>", ...],
      "problemes": ["<problème spécifique>", ...],
      "recommandation": "<action concrète>"
    },
    "reformulation": {
      "titre_actuel": "<titre exact de la slide>",
      "titre_propose": "<nouveau titre en phrase complète avec conclusion>",
      "corps_reformule": "<reformulation du corps en 3 bullets max>",
      "supprimer": ["<élément à supprimer>", ...]
    }
  },
  "template_matches": ["<type1>", "<type2>"],
  "annotations": [
    {
      "zone": "top" | "center" | "bottom" | "left" | "right" | "top-left" | "top-right",
      "type": "error" | "warning" | "ok",
      "label": "<label court>",
      "detail": "<explication>"
    }
  ]
}"""


# ─── AUTH ROUTES ────────────────────────────────────────────────────────────

@app.route("/auth/register", methods=["POST"])
def register():
    data = request.get_json()
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")
    name = data.get("name", "").strip()

    if not email or not password:
        return jsonify({"error": "Email et mot de passe requis"}), 400
    if len(password) < 6:
        return jsonify({"error": "Mot de passe trop court (6 caractères min)"}), 400
    if User.query.filter_by(email=email).first():
        return jsonify({"error": "Cet email est déjà utilisé"}), 409

    user = User(
        email=email,
        name=name or email.split("@")[0],
        password_hash=bcrypt.generate_password_hash(password).decode("utf-8"),
    )
    db.session.add(user)
    db.session.commit()
    login_user(user, remember=True)
    return jsonify({"ok": True, "user": {"name": user.name, "email": user.email, "avatar": user.avatar_url}})


@app.route("/auth/login", methods=["POST"])
def login():
    data = request.get_json()
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")

    user = User.query.filter_by(email=email).first()
    if not user or not user.password_hash:
        return jsonify({"error": "Email ou mot de passe incorrect"}), 401
    if not bcrypt.check_password_hash(user.password_hash, password):
        return jsonify({"error": "Email ou mot de passe incorrect"}), 401

    login_user(user, remember=True)
    return jsonify({"ok": True, "user": {"name": user.name, "email": user.email, "avatar": user.avatar_url}})


@app.route("/auth/logout")
def logout():
    logout_user()
    return jsonify({"ok": True})


@app.route("/auth/google")
def auth_google():
    redirect_uri = url_for("auth_google_callback", _external=True)
    return google.authorize_redirect(redirect_uri)


@app.route("/auth/google/callback")
def auth_google_callback():
    token = google.authorize_access_token()
    userinfo = token.get("userinfo") or google.userinfo()

    google_id = userinfo["sub"]
    email = userinfo["email"].lower()
    name = userinfo.get("name", email.split("@")[0])
    avatar = userinfo.get("picture")

    user = User.query.filter_by(google_id=google_id).first()
    if not user:
        user = User.query.filter_by(email=email).first()
        if user:
            user.google_id = google_id
            user.avatar_url = avatar
        else:
            user = User(email=email, name=name, google_id=google_id, avatar_url=avatar)
            db.session.add(user)
    db.session.commit()
    login_user(user, remember=True)
    return redirect("/")


@app.route("/me")
def me():
    if current_user.is_authenticated:
        return jsonify({
            "authenticated": True,
            "user": {
                "name": current_user.name,
                "email": current_user.email,
                "avatar": current_user.avatar_url,
                "trial_count": current_user.trial_count,
                "plan_level": current_user.plan_level,
                "subscription_plan": current_user.subscription_plan,
                "is_admin": current_user.is_admin,
            }
        })
    return jsonify({"authenticated": False})


# ─── APP ROUTES ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/analyze", methods=["POST"])
@login_required
def analyze():
    if not current_user.can_analyze:
        return jsonify({"error": "quota_exceeded", "upgrade_url": "/pricing"}), 402

    if "image" not in request.files:
        return jsonify({"error": "Aucune image fournie"}), 400

    image_file = request.files["image"]
    image_bytes = image_file.read()
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    filename = image_file.filename.lower()
    if filename.endswith(".png"):
        media_type = "image/png"
    elif filename.endswith((".jpg", ".jpeg")):
        media_type = "image/jpeg"
    elif filename.endswith(".webp"):
        media_type = "image/webp"
    else:
        media_type = "image/png"

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": image_b64,
                            },
                        },
                        {"type": "text", "text": ANALYSIS_PROMPT},
                    ],
                }
            ],
        )

        raw = response.content[0].text.strip()

        # Robust JSON extraction: handle markdown blocks and extra text
        # Try to extract JSON from ```json ... ``` block first
        json_block = re.search(r'```(?:json)?\s*([\s\S]*?)```', raw)
        if json_block:
            raw = json_block.group(1).strip()
        else:
            # No code block — find the first { and last } to isolate JSON object
            start = raw.find('{')
            end = raw.rfind('}')
            if start != -1 and end != -1:
                raw = raw[start:end + 1]

        result = json.loads(raw)

        analysis = Analysis(
            user_id=current_user.id,
            filename=image_file.filename,
            timestamp=datetime.now().strftime("%d/%m/%Y %H:%M"),
            verdict=result.get("verdict"),
            global_score=result.get("global_score"),
            slide_type=result.get("slide_type"),
            scores_json=json.dumps(result.get("scores", {})),
            result_json=json.dumps(result),
        )
        db.session.add(analysis)

        # Decrement trial count for free users
        if current_user.plan_level == 'free':
            current_user.trial_count = max(0, (current_user.trial_count or 0) - 1)

        db.session.commit()

        return jsonify(result)

    except json.JSONDecodeError as e:
        return jsonify({"error": f"Réponse JSON invalide : {str(e)}", "raw": raw}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/history", methods=["GET"])
@login_required
def get_history():
    if current_user.plan_level != 'pro':
        return jsonify({"error": "plan_required", "required_plan": "pro"}), 402
    analyses = (
        Analysis.query
        .filter_by(user_id=current_user.id)
        .order_by(Analysis.created_at.desc())
        .limit(50)
        .all()
    )
    return jsonify([a.to_dict() for a in analyses])


@app.route("/templates", methods=["GET"])
def get_templates():
    templates = [
        {"id": "recommandation", "name": "Recommandation", "description": "Titre-action + 3 bullets quantifiés + call-to-action", "structure": ["Titre-action (conclusion)", "Bullet 1 : action + impact €", "Bullet 2 : action + impact €", "Bullet 3 : action + impact €", "Décision demandée"], "best_for": "Proposer des actions à un COMEX ou CFO", "icon": "🎯", "color": "#6366f1"},
        {"id": "etat_des_lieux", "name": "État des lieux", "description": "Situation actuelle avec données chiffrées + constat", "structure": ["Titre : constat chiffré", "Graphique ou KPIs clés", "3 insights factuels", "So what ?"], "best_for": "Cadrer un problème avant de proposer des solutions", "icon": "📊", "color": "#3b82f6"},
        {"id": "comparaison", "name": "Comparaison", "description": "Tableau 2 colonnes ou matrice de décision", "structure": ["Titre : ce que la comparaison révèle", "Option A vs Option B", "Critères de comparaison pondérés", "Recommandation en bas"], "best_for": "Aide à la décision entre plusieurs options", "icon": "⚖️", "color": "#8b5cf6"},
        {"id": "tendance", "name": "Tendance", "description": "Graphique temporel avec insight sur la trajectoire", "structure": ["Titre : ce que la tendance signifie", "Graphique temporel (ligne ou barres)", "Annotation du point clé", "Implication pour l'entreprise"], "best_for": "Montrer une évolution et son impact business", "icon": "📈", "color": "#10b981"},
        {"id": "processus", "name": "Processus", "description": "Étapes séquentielles avec responsables et délais", "structure": ["Titre : objectif du processus", "Étape 1 → Étape 2 → Étape 3", "Responsable + délai par étape", "Livrable final"], "best_for": "Plan d'action ou roadmap projet", "icon": "🔄", "color": "#f59e0b"},
        {"id": "kpis", "name": "KPIs Dashboard", "description": "Métriques clés avec statut RAG (Rouge/Orange/Vert)", "structure": ["Titre : performance globale", "4–6 KPIs avec valeur cible vs réel", "Code couleur RAG", "Actions prioritaires"], "best_for": "Revue de performance ou steering committee", "icon": "🎛️", "color": "#ef4444"},
        {"id": "minto", "name": "Pyramide Minto (SCQA)", "description": "Situation → Complication → Question → Réponse", "structure": ["Titre : Réponse (conclusion)", "Situation (contexte partagé)", "Complication (problème)", "Question (ce que le client se pose)", "Réponse détaillée"], "best_for": "Aligner l'audience avant d'aller dans le détail", "icon": "🔺", "color": "#ec4899"},
        {"id": "before_after", "name": "Avant / Après", "description": "Contraste visuel entre état actuel et état cible", "structure": ["Titre : ce que la transformation apporte", "Colonne Avant (état douloureux)", "Colonne Après (état cible)", "Effort / investissement requis"], "best_for": "Vendre une transformation ou un projet de changement", "icon": "✨", "color": "#14b8a6"},
    ]
    return jsonify(templates)


# ─── FORGOT / RESET PASSWORD ─────────────────────────────────────────────────

@app.route("/auth/forgot-password", methods=["POST"])
def forgot_password():
    data = request.get_json()
    email = data.get("email", "").strip().lower()
    user = User.query.filter_by(email=email).first()
    # Always return OK to avoid email enumeration
    if user and user.password_hash:
        token = get_reset_serializer().dumps(email)
        base_url = request.host_url.rstrip("/")
        reset_url = f"{base_url}/auth/reset-password?token={token}"
        try:
            msg = Message(
                subject="Réinitialisation de votre mot de passe — SlideReview",
                recipients=[email],
                html=f"""
                <div style="font-family:Inter,sans-serif;max-width:500px;margin:0 auto;padding:32px 24px">
                  <h2 style="color:#1a1d23;margin-bottom:8px">SlideReview</h2>
                  <p style="color:#6b7280;margin-bottom:24px">Réinitialisation du mot de passe</p>
                  <p style="color:#1a1d23">Bonjour,</p>
                  <p style="color:#1a1d23">Vous avez demandé à réinitialiser votre mot de passe. Cliquez sur le bouton ci-dessous :</p>
                  <a href="{reset_url}" style="display:inline-block;margin:24px 0;padding:12px 24px;background:#7c6af7;color:#fff;border-radius:8px;text-decoration:none;font-weight:600">
                    Réinitialiser mon mot de passe
                  </a>
                  <p style="color:#9ca3af;font-size:13px">Ce lien expire dans 1 heure. Si vous n'avez pas fait cette demande, ignorez cet email.</p>
                </div>
                """,
            )
            mail.send(msg)
        except Exception as e:
            app.logger.error(f"Mail error: {e}")
    return jsonify({"ok": True})


@app.route("/auth/reset-password", methods=["GET"])
def reset_password_page():
    return send_from_directory(".", "reset_password.html")


@app.route("/auth/reset-password", methods=["POST"])
def reset_password():
    data = request.get_json()
    token = data.get("token", "")
    password = data.get("password", "")
    if len(password) < 6:
        return jsonify({"error": "Mot de passe trop court (6 caractères min)"}), 400
    try:
        email = get_reset_serializer().loads(token, max_age=3600)
    except SignatureExpired:
        return jsonify({"error": "Le lien a expiré. Recommencez."}), 400
    except BadSignature:
        return jsonify({"error": "Lien invalide."}), 400

    user = User.query.filter_by(email=email).first()
    if not user:
        return jsonify({"error": "Utilisateur introuvable."}), 404

    user.password_hash = bcrypt.generate_password_hash(password).decode("utf-8")
    db.session.commit()
    login_user(user, remember=True)
    return jsonify({"ok": True, "user": {"name": user.name, "email": user.email, "avatar": user.avatar_url, "plan_level": user.plan_level, "trial_count": user.trial_count}})


# ─── ADMIN ────────────────────────────────────────────────────────────────────

@app.route("/admin")
@login_required
@admin_required
def admin_panel():
    return send_from_directory(".", "admin.html")


@app.route("/admin/stats")
@login_required
@admin_required
def admin_stats():
    total_users = User.query.count()
    paying_users = User.query.filter_by(subscription_status="active").count()
    total_analyses = Analysis.query.count()
    starter_users = User.query.filter_by(subscription_plan="starter", subscription_status="active").count()
    pro_users = User.query.filter_by(subscription_plan="pro", subscription_status="active").count()
    return jsonify({
        "total_users": total_users,
        "paying_users": paying_users,
        "starter_users": starter_users,
        "pro_users": pro_users,
        "total_analyses": total_analyses,
    })


@app.route("/admin/users")
@login_required
@admin_required
def admin_users():
    users = User.query.order_by(User.created_at.desc()).all()
    result = []
    for u in users:
        result.append({
            "id": u.id,
            "name": u.name,
            "email": u.email,
            "plan_level": u.plan_level,
            "subscription_plan": u.subscription_plan,
            "subscription_status": u.subscription_status,
            "trial_count": u.trial_count,
            "is_admin": u.is_admin,
            "analyses_count": len(u.analyses),
            "created_at": u.created_at.strftime("%d/%m/%Y") if u.created_at else "",
        })
    return jsonify(result)


@app.route("/admin/users/<int:user_id>/plan", methods=["POST"])
@login_required
@admin_required
def admin_set_plan(user_id):
    data = request.get_json()
    plan = data.get("plan", "free")
    user = User.query.get_or_404(user_id)
    if plan == "free":
        user.subscription_plan = None
        user.subscription_status = None
    else:
        user.subscription_plan = plan
        user.subscription_status = "active"
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/admin/users/<int:user_id>/reset-trials", methods=["POST"])
@login_required
@admin_required
def admin_reset_trials(user_id):
    user = User.query.get_or_404(user_id)
    user.trial_count = 5
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/admin/analyses")
@login_required
@admin_required
def admin_analyses():
    analyses = (
        Analysis.query
        .order_by(Analysis.created_at.desc())
        .limit(100)
        .all()
    )
    result = []
    for a in analyses:
        result.append({
            "id": a.id,
            "filename": a.filename,
            "verdict": a.verdict,
            "global_score": a.global_score,
            "slide_type": a.slide_type,
            "timestamp": a.timestamp,
            "user_email": a.user.email if a.user else "?",
        })
    return jsonify(result)


# ─── PRICING & STRIPE ────────────────────────────────────────────────────────

@app.route("/pricing")
def pricing():
    return send_from_directory(".", "pricing.html")


@app.route("/subscribe/<plan>", methods=["POST"])
@login_required
def subscribe(plan):
    if plan not in STRIPE_PRICES:
        return jsonify({"error": "Plan invalide"}), 400
    price_id = STRIPE_PRICES[plan]
    if not price_id:
        return jsonify({"error": "Stripe non configuré"}), 500

    if not current_user.stripe_customer_id:
        customer = stripe.Customer.create(
            email=current_user.email,
            name=current_user.name,
            metadata={"user_id": current_user.id},
        )
        current_user.stripe_customer_id = customer.id
        db.session.commit()

    base_url = request.host_url.rstrip("/")
    checkout = stripe.checkout.Session.create(
        customer=current_user.stripe_customer_id,
        payment_method_types=["card"],
        line_items=[{"price": price_id, "quantity": 1}],
        mode="subscription",
        success_url=f"{base_url}/?subscribed=1",
        cancel_url=f"{base_url}/pricing",
    )
    return jsonify({"url": checkout.url})


@app.route("/subscription/portal")
@login_required
def subscription_portal():
    if not current_user.stripe_customer_id:
        return redirect("/pricing")
    base_url = request.host_url.rstrip("/")
    portal = stripe.billing_portal.Session.create(
        customer=current_user.stripe_customer_id,
        return_url=f"{base_url}/",
    )
    return redirect(portal.url)


@app.route("/webhook/stripe", methods=["POST"])
def stripe_webhook():
    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError):
        return jsonify({"error": "Invalid signature"}), 400

    if event["type"] in ("customer.subscription.created", "customer.subscription.updated"):
        sub = event["data"]["object"]
        user = User.query.filter_by(stripe_customer_id=sub["customer"]).first()
        if user:
            price_id = sub["items"]["data"][0]["price"]["id"]
            user.subscription_plan = price_to_plan(price_id) or user.subscription_plan
            user.subscription_status = "active" if sub["status"] == "active" else sub["status"]
            user.stripe_subscription_id = sub["id"]
            db.session.commit()

    elif event["type"] == "customer.subscription.deleted":
        sub = event["data"]["object"]
        user = User.query.filter_by(stripe_customer_id=sub["customer"]).first()
        if user:
            user.subscription_status = "canceled"
            user.subscription_plan = None
            db.session.commit()

    return jsonify({"ok": True})


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    port = int(os.getenv("PORT", 8080))
    debug = os.getenv("FLASK_ENV") == "development"
    print(f"🚀 SlideReview démarré → http://localhost:{port}")
    app.run(debug=debug, host="0.0.0.0", port=port)
