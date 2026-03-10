import os
import json
import base64
import re
from datetime import datetime
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB max
client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

SKILL_PATH = Path(__file__).parent / "skills/slide-reviewer/SKILL.md"

# In-memory history (persists during session, resets on restart)
_history = []

# Load skill as system prompt
def get_system_prompt():
    if SKILL_PATH.exists():
        content = SKILL_PATH.read_text()
        # Strip YAML frontmatter
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


def load_history():
    return _history


def save_history(history):
    global _history
    _history = history


@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/analyze", methods=["POST"])
def analyze():
    if "image" not in request.files:
        return jsonify({"error": "Aucune image fournie"}), 400

    image_file = request.files["image"]
    image_bytes = image_file.read()
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    # Determine media type
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
        # Clean potential markdown fences
        raw = re.sub(r'^```(?:json)?\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
        result = json.loads(raw)

        # Save to history
        history = load_history()
        history.insert(0, {
            "id": datetime.now().isoformat(),
            "filename": image_file.filename,
            "timestamp": datetime.now().strftime("%d/%m/%Y %H:%M"),
            "verdict": result.get("verdict"),
            "global_score": result.get("global_score"),
            "slide_type": result.get("slide_type"),
            "scores": result.get("scores"),
        })
        save_history(history[:20])  # Keep last 20

        return jsonify(result)

    except json.JSONDecodeError as e:
        return jsonify({"error": f"Réponse JSON invalide : {str(e)}", "raw": raw}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/history", methods=["GET"])
def get_history():
    return jsonify(load_history())


@app.route("/templates", methods=["GET"])
def get_templates():
    templates = [
        {
            "id": "recommandation",
            "name": "Recommandation",
            "description": "Titre-action + 3 bullets quantifiés + call-to-action",
            "structure": ["Titre-action (conclusion)", "Bullet 1 : action + impact €", "Bullet 2 : action + impact €", "Bullet 3 : action + impact €", "Décision demandée"],
            "best_for": "Proposer des actions à un COMEX ou CFO",
            "icon": "🎯",
            "color": "#6366f1"
        },
        {
            "id": "etat_des_lieux",
            "name": "État des lieux",
            "description": "Situation actuelle avec données chiffrées + constat",
            "structure": ["Titre : constat chiffré", "Graphique ou KPIs clés", "3 insights factuels", "So what ?"],
            "best_for": "Cadrer un problème avant de proposer des solutions",
            "icon": "📊",
            "color": "#3b82f6"
        },
        {
            "id": "comparaison",
            "name": "Comparaison",
            "description": "Tableau 2 colonnes ou matrice de décision",
            "structure": ["Titre : ce que la comparaison révèle", "Option A vs Option B", "Critères de comparaison pondérés", "Recommandation en bas"],
            "best_for": "Aide à la décision entre plusieurs options",
            "icon": "⚖️",
            "color": "#8b5cf6"
        },
        {
            "id": "tendance",
            "name": "Tendance",
            "description": "Graphique temporel avec insight sur la trajectoire",
            "structure": ["Titre : ce que la tendance signifie", "Graphique temporel (ligne ou barres)", "Annotation du point clé", "Implication pour l'entreprise"],
            "best_for": "Montrer une évolution et son impact business",
            "icon": "📈",
            "color": "#10b981"
        },
        {
            "id": "processus",
            "name": "Processus",
            "description": "Étapes séquentielles avec responsables et délais",
            "structure": ["Titre : objectif du processus", "Étape 1 → Étape 2 → Étape 3", "Responsable + délai par étape", "Livrable final"],
            "best_for": "Plan d'action ou roadmap projet",
            "icon": "🔄",
            "color": "#f59e0b"
        },
        {
            "id": "kpis",
            "name": "KPIs Dashboard",
            "description": "Métriques clés avec statut RAG (Rouge/Orange/Vert)",
            "structure": ["Titre : performance globale", "4–6 KPIs avec valeur cible vs réel", "Code couleur RAG", "Actions prioritaires"],
            "best_for": "Revue de performance ou steering committee",
            "icon": "🎛️",
            "color": "#ef4444"
        },
        {
            "id": "minto",
            "name": "Pyramide Minto (SCQA)",
            "description": "Situation → Complication → Question → Réponse",
            "structure": ["Titre : Réponse (conclusion)", "Situation (contexte partagé)", "Complication (problème)", "Question (ce que le client se pose)", "Réponse détaillée"],
            "best_for": "Aligner l'audience avant d'aller dans le détail",
            "icon": "🔺",
            "color": "#ec4899"
        },
        {
            "id": "before_after",
            "name": "Avant / Après",
            "description": "Contraste visuel entre état actuel et état cible",
            "structure": ["Titre : ce que la transformation apporte", "Colonne Avant (état douloureux)", "Colonne Après (état cible)", "Effort / investissement requis"],
            "best_for": "Vendre une transformation ou un projet de changement",
            "icon": "✨",
            "color": "#14b8a6"
        }
    ]
    return jsonify(templates)


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    debug = os.getenv("FLASK_ENV") == "development"
    print(f"🚀 SlideReview démarré → http://localhost:{port}")
    app.run(debug=debug, host="0.0.0.0", port=port)
