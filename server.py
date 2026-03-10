import os
import json
import base64
import re
from datetime import datetime
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory, redirect, url_for, session
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from flask_bcrypt import Bcrypt
from authlib.integrations.flask_client import OAuth
from anthropic import Anthropic
from dotenv import load_dotenv
from models import db, User, Analysis

load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB max
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-change-in-prod")
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///slidereview.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# Fix postgres:// → postgresql:// (Railway uses old format)
if app.config["SQLALCHEMY_DATABASE_URI"].startswith("postgres://"):
    app.config["SQLALCHEMY_DATABASE_URI"] = app.config["SQLALCHEMY_DATABASE_URI"].replace("postgres://", "postgresql://", 1)

db.init_app(app)
bcrypt = Bcrypt(app)

login_manager = LoginManager(app)
login_manager.login_view = None  # We handle redirects manually via JSON

# Create tables on first request (works with gunicorn)
with app.app_context():
    db.create_all()

oauth = OAuth(app)
google = oauth.register(
    name="google",
    client_id=os.getenv("GOOGLE_CLIENT_ID"),
    client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

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
        raw = re.sub(r'^```(?:json)?\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
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
        db.session.commit()

        return jsonify(result)

    except json.JSONDecodeError as e:
        return jsonify({"error": f"Réponse JSON invalide : {str(e)}", "raw": raw}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/history", methods=["GET"])
@login_required
def get_history():
    analyses = (
        Analysis.query
        .filter_by(user_id=current_user.id)
        .order_by(Analysis.created_at.desc())
        .limit(20)
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


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    port = int(os.getenv("PORT", 8080))
    debug = os.getenv("FLASK_ENV") == "development"
    print(f"🚀 SlideReview démarré → http://localhost:{port}")
    app.run(debug=debug, host="0.0.0.0", port=port)
