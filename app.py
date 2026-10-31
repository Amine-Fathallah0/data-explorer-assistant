"""
app.py — serveur Flask pour l'interface Data Explorer, avec historique de conversation.

Importe directement chain, retrieve_docs, llm depuis rag.py sans les modifier.
La persistance des conversations vit dans db.py (SQLite).

Placer ce fichier + db.py + templates/ + static/ au même niveau que rag.py.
"""

import os
import re
import time
import uuid
from functools import wraps

import markdown as md
from flask import Flask, jsonify, redirect, render_template, request, session, url_for

from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse
from langchain_core.output_parsers import StrOutputParser

from rag import chain, retrieve_docs, llm, format_docs, LLM_ERRORS
import db
import kb_edit
import kb_create
import kb_meta
import kb_schema

app = Flask(__name__)

# Certaines réponses de gemini-3.1-flash-lite renvoient .content sous forme de liste de
# blocs structurés plutôt qu'une simple chaîne (selon le modèle/la réponse) — StrOutputParser
# sait normaliser ça dans la plupart des cas, contrairement à un accès direct à .content.
text_llm = llm | StrOutputParser()


def llm_text(prompt):
    """Invoque le LLM pour un prompt ponctuel (hors chaîne RAG) et renvoie du texte brut.
    Filet de sécurité : même StrOutputParser a été observé renvoyer une liste de blocs plutôt
    qu'une chaîne dans de rares cas — on normalise nous-mêmes plutôt que de planter en aval."""
    result = text_llm.invoke(prompt)
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        return "".join(
            block if isinstance(block, str) else block.get("text", "")
            for block in result
            if isinstance(block, str) or isinstance(block, dict)
        )
    return str(result)

SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    SECRET_KEY = "dev-secret-key-ne-pas-utiliser-en-production"
    print(
        "ATTENTION : variable d'environnement SECRET_KEY absente — utilisation d'une "
        "clé de secours de développement. Ne jamais utiliser cette clé en production."
    )
app.secret_key = SECRET_KEY

db.init_db()


def current_identity():
    """Identité utilisée pour scoper les conversations : le username si connecté,
    sinon un identifiant anonyme stable pour la session du navigateur (invité)."""
    return session.get("username") or session.setdefault("anon_id", str(uuid.uuid4()))


def tech_lead_required(f):
    """403 si la session n'a pas le rôle tech_lead — protège les routes de correction."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if session.get("role") != "tech_lead":
            return jsonify({"error": "Accès réservé aux tech leads."}), 403
        return f(*args, **kwargs)
    return wrapper


def extract_suggestions(markdown_text):
    """Sépare le texte principal du bloc SUGGESTIONS: éventuel (format défini dans le
    prompt : marqueur sur sa propre ligne, une suggestion par ligne suivante).
    Renvoie (texte_sans_suggestions, liste_de_suggestions)."""
    match = re.search(r"^SUGGESTIONS:\s*\n((?:.+\n?)*)", markdown_text, re.MULTILINE)
    if not match:
        return markdown_text, []
    bloc = match.group(1).strip()
    suggestions = [line.strip("-*• ").strip() for line in bloc.split("\n") if line.strip()]
    texte_principal = markdown_text[:match.start()].strip()
    return texte_principal, suggestions


def extract_sources(docs):
    """Construit la liste des sources (dataset › table › colonne/section) depuis les
    métadonnées des chunks récupérés — déterministe, ne dépend jamais du LLM. Chaque entrée
    porte, en plus du label affiché, l'identité structurée du chunk (source, table brute,
    section, colonne) nécessaire à /api/chunk pour en relire le contenu à la demande
    (panneau de visualisation de source).

    Le dataset fait partie du label : `CodeRegion`, `CodeAd`, `LibDo` et `DateMaj` existent
    dans plusieurs tables et dans les deux datasets — sans lui, deux sources distinctes
    s'affichent sous un libellé identique."""
    seen = set()
    result = []
    for doc in docs:
        dataset = doc.metadata.get("dataset") or kb_meta.dataset_of(doc.metadata.get("source") or "")
        table = kb_meta.clean_table(doc.metadata.get("table") or "")
        detail = (doc.metadata.get("colonne") or doc.metadata.get("section") or "").strip()
        label = " › ".join(p for p in (dataset, table, detail) if p)
        if not label or label in seen:
            continue
        seen.add(label)
        result.append({
            "label": label,
            "dataset": dataset,
            "table": table,
            "table_raw": doc.metadata.get("table") or "",
            "section": doc.metadata.get("section") or "",
            "colonne": doc.metadata.get("colonne") or "",
            "source": doc.metadata.get("source") or "",
        })
    return result


def extract_correctable_sources(docs):
    """Sources (table, colonne) éligibles à une correction — contrairement à
    extract_sources(), exclut les chunks de section sans colonne identifiée, puisque
    la table `corrections` est keyée sur (table_name, colonne)."""
    seen = set()
    result = []
    for doc in docs:
        table, colonne = db.extract_entity(doc)
        if table and colonne and (table, colonne) not in seen:
            seen.add((table, colonne))
            result.append({"table": table, "colonne": colonne})
    return result


def corrected_sources(docs):
    """Repère, parmi les chunks récupérés, ceux qui ont été réécrits par une correction
    (chunk primaire OU propagation), d'après le journal d'audit. Ne modifie plus les docs :
    le contenu corrigé est désormais celui de la Knowledge Base elle-même. Sert uniquement
    à afficher un repère ✓ discret sur la source correspondante en pied de réponse."""
    index = db.corrected_chunks_index()
    if not index:
        return []
    seen = set()
    result = []
    for doc in docs:
        table = kb_meta.clean_table(doc.metadata.get("table") or "")
        detail = (doc.metadata.get("colonne") or doc.metadata.get("section") or "").strip()
        entry = index.get((table, detail))
        if entry and (table, detail) not in seen:
            seen.add((table, detail))
            result.append({"table": table, "colonne": detail, "auteur": entry["auteur"]})
    return result


def generate_title(question, answer_text):
    """Génère un titre court (3-6 mots) à partir du premier échange.
    Appel LLM séparé et minimal, une seule fois par conversation — pas à chaque message."""
    prompt_titre = (
        "Résume la question suivante en un titre très court (3 à 6 mots), en français, "
        "sans guillemets, sans ponctuation finale, sans markdown. "
        f"Question : {question}\nRéponse partielle : {answer_text[:200]}\nTitre :"
    )
    try:
        titre = llm_text(prompt_titre).strip().strip('"').strip("«»")
        return titre[:60] if titre else question[:50]
    except Exception:
        # si la génération de titre échoue (rate limit, etc.), on retombe sur une troncature simple
        return question[:50]


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/dashboard")
def dashboard():
    # Réservé aux tech leads : la télémétrie expose le contenu des questions posées par
    # tous les utilisateurs, pas seulement les siennes. Redirige plutôt que 403 — pas de
    # page d'erreur dédiée dans cette app, et le lien est de toute façon masqué pour les
    # autres rôles (voir app.js/schema.js).
    if session.get("role") != "tech_lead":
        return redirect(url_for("index"))
    stats = db.get_dashboard_stats()
    return render_template("dashboard.html", stats=stats)


@app.route("/schema")
def schema():
    return render_template("schema.html")


@app.route("/api/schema", methods=["GET"])
def api_schema():
    return jsonify(kb_schema.build_schema_graph())


@app.route("/api/datasets", methods=["GET"])
def api_datasets():
    """Datasets présents dans la Knowledge Base et fichier qui déclare leurs tables.
    Alimente le sélecteur de dataset du formulaire de création de table — la liste vient
    du disque, jamais d'une constante à maintenir."""
    return jsonify([
        {"dataset": dataset, "source": source}
        for dataset, source in sorted(kb_create.list_dataset_sources().items())
    ])


@app.route("/api/chunk", methods=["GET"])
def api_chunk():
    """Contenu ACTUEL d'un chunk déjà identifié (panneau « informations fournies par »
    du chat, et panneau colonne du schéma) — relit toujours le .md en direct, donc
    reflète une correction appliquée après coup."""
    source = request.args.get("source", "")
    table_raw = request.args.get("table_raw", "")
    section = request.args.get("section", "")
    colonne = request.args.get("colonne", "")

    doc = kb_meta.find_chunk(source, table_raw, section, colonne)
    if doc is None:
        return jsonify({"error": "Chunk introuvable."}), 404

    table = kb_meta.clean_table(doc.metadata.get("table") or "")
    detail = (colonne or section).strip()
    entry = db.corrected_chunks_index().get((table, detail))

    return jsonify({
        "content": doc.page_content,
        "dataset": doc.metadata.get("dataset") or kb_meta.dataset_of(source),
        "table": table,
        "section": section,
        "colonne": colonne,
        "source": source,
        "corrected": bool(entry),
        "corrected_by": entry["auteur"] if entry else None,
    })


@app.route("/api/conversations", methods=["GET"])
def api_list_conversations():
    return jsonify(db.list_conversations(current_identity()))


@app.route("/api/conversations", methods=["POST"])
def api_create_conversation():
    return jsonify(db.create_conversation(current_identity()))


@app.route("/api/conversations/<conv_id>/messages", methods=["GET"])
def api_get_messages(conv_id):
    # Lecture seule ouverte au propriétaire ET à qui la conversation a été partagée (voir
    # db.can_view_conversation) — contrairement à la suppression, au renommage et à /ask,
    # qui restent strictement réservés au propriétaire (db.get_conversation_owner direct,
    # inchangés plus bas dans ce fichier).
    if not db.can_view_conversation(conv_id, current_identity()):
        return jsonify({"error": "Conversation introuvable."}), 404
    messages = db.get_messages(conv_id)
    for m in messages:
        if m["role"] == "assistant":
            texte_principal, suggestions = extract_suggestions(m["content"])
            m["content_html"] = md.markdown(texte_principal, extensions=["tables"])
            m["suggestions"] = suggestions
    return jsonify(messages)


@app.route("/api/conversations/<conv_id>", methods=["DELETE"])
def api_delete_conversation(conv_id):
    if db.get_conversation_owner(conv_id) != current_identity():
        return jsonify({"error": "Conversation introuvable."}), 404
    db.delete_conversation(conv_id)
    return jsonify({"deleted": True})


@app.route("/api/conversations/<conv_id>/title", methods=["PUT"])
def api_rename_conversation(conv_id):
    if db.get_conversation_owner(conv_id) != current_identity():
        return jsonify({"error": "Conversation introuvable."}), 404
    payload = request.get_json(silent=True) or {}
    titre = (payload.get("title") or "").strip()
    if not titre:
        return jsonify({"error": "Titre vide."}), 400
    db.set_title(conv_id, titre[:60])
    return jsonify({"title": titre[:60]})


@app.route("/api/conversations/<conv_id>/share", methods=["POST"])
def api_share_conversation(conv_id):
    """Partage `conv_id` en lecture seule avec un autre utilisateur enregistré. Réservé
    aux utilisateurs connectés (une conversation invitée n'a pas d'identité nommable
    qu'un tiers pourrait retrouver)."""
    if not session.get("username"):
        return jsonify({"error": "Connexion requise pour partager une conversation."}), 401
    payload = request.get_json(silent=True) or {}
    target = (payload.get("username") or "").strip()
    if not target:
        return jsonify({"error": "Nom d'utilisateur requis."}), 400
    if target == session["username"]:
        return jsonify({"error": "Impossible de partager une conversation avec vous-même."}), 400
    if not db.share_conversation(conv_id, session["username"], target):
        return jsonify({"error": "Conversation introuvable ou utilisateur inconnu."}), 404
    return jsonify(db.list_shares(conv_id, session["username"]))


@app.route("/api/conversations/<conv_id>/shares", methods=["GET"])
def api_list_conversation_shares(conv_id):
    if not session.get("username"):
        return jsonify({"error": "Connexion requise."}), 401
    shares = db.list_shares(conv_id, session["username"])
    if shares is None:
        return jsonify({"error": "Conversation introuvable."}), 404
    return jsonify(shares)


@app.route("/api/conversations/<conv_id>/share/<username>", methods=["DELETE"])
def api_revoke_conversation_share(conv_id, username):
    if not session.get("username"):
        return jsonify({"error": "Connexion requise."}), 401
    if not db.revoke_share(conv_id, session["username"], username):
        return jsonify({"error": "Conversation introuvable."}), 404
    return jsonify({"revoked": True})


@app.route("/api/conversations/shared", methods=["GET"])
def api_list_shared_conversations():
    """Conversations partagées avec l'utilisateur connecté — vide (pas d'erreur) pour une
    session invitée, qui ne peut jamais être destinataire d'un partage (voir
    api_share_conversation)."""
    if not session.get("username"):
        return jsonify([])
    return jsonify(db.list_shared_with_me(session["username"]))


@app.route("/api/session", methods=["GET"])
def api_session():
    if "username" in session:
        return jsonify({"username": session["username"], "role": session["role"]})
    return jsonify({"username": None, "role": None})


@app.route("/login", methods=["POST"])
def login():
    payload = request.get_json(silent=True) or {}
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""

    user = db.verify_user(username, password)
    if not user:
        return jsonify({"error": "Identifiants invalides."}), 401

    session["username"] = user["username"]
    session["role"] = user["role"]
    return jsonify(user)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"logged_out": True})


@app.route("/api/corrections/prepare", methods=["POST"])
@tech_lead_required
def api_prepare_correction():
    """Dry-run : reformule le chunk ciblé au style du KB et repère les chunks candidats
    (scan déterministe scopé sur (table, colonne), sans LLM). Les candidats sont renvoyés
    tels quels, sans réécriture proposée : c'est le tech lead qui juge et rédige en
    relecture. N'écrit RIEN — l'application effective se fait via /apply."""
    payload = request.get_json(silent=True) or {}
    table_name = (payload.get("table_name") or "").strip()
    colonne = (payload.get("colonne") or "").strip()
    texte_brut = (payload.get("texte_brut") or "").strip()

    if not table_name or not colonne or not texte_brut:
        return jsonify({"error": "table_name, colonne et texte_brut sont requis."}), 400

    try:
        result = kb_edit.propose_correction(table_name, colonne, texte_brut, llm_text)
        return jsonify(result)
    except LookupError as e:
        return jsonify({"error": str(e)}), 404
    except (ResponseHandlingException, UnexpectedResponse):
        return jsonify({
            "error": "Erreur de connexion à la base vectorielle Qdrant. Réessayez dans un instant."
        }), 503
    except LLM_ERRORS:
        return jsonify({
            "error": "Erreur de l'API Gemini (limite de requêtes atteinte ou problème réseau)."
        }), 503


@app.route("/api/corrections/apply", methods=["POST"])
@tech_lead_required
def api_apply_correction():
    """Applique les éditions confirmées : réécrit chaque chunk dans son .md + resynchronise
    Qdrant, puis journalise l'événement et chaque chunk touché (audit, sans annulation)."""
    payload = request.get_json(silent=True) or {}
    table_name = (payload.get("table_name") or "").strip()
    colonne = (payload.get("colonne") or "").strip()
    texte_brut = (payload.get("texte_brut") or "").strip()
    edits_in = payload.get("edits") or []

    if not table_name or not colonne or not texte_brut or not edits_in:
        return jsonify({"error": "table_name, colonne, texte_brut et au moins une édition sont requis."}), 400

    edits = []
    texte_valide_primary = None
    for e in edits_in:
        nouveau = (e.get("nouveau_texte") or "").strip()
        source = (e.get("source") or "").strip()
        table_raw = (e.get("table_raw") or "").strip()
        if not nouveau or not source or not table_raw:
            continue
        # Un candidat coché mais laissé tel quel est un vrai no-op : on ne réécrit pas le .md
        # à l'identique et on ne journalise pas une ligne d'audit sans modification. Seule
        # l'édition primaire est toujours appliquée (c'est la correction elle-même).
        if not e.get("is_primary") and nouveau == (e.get("ancien_texte") or "").strip():
            continue
        edits.append({
            "source": source,
            "table_raw": table_raw,
            "table": (e.get("table") or "").strip(),
            "section": (e.get("section") or "").strip(),
            "colonne": (e.get("colonne") or "").strip(),
            "nouveau_texte": nouveau,
            "is_primary": bool(e.get("is_primary")),
            "label": (e.get("label") or "").strip(),
        })
        if e.get("is_primary"):
            texte_valide_primary = nouveau

    if not edits:
        return jsonify({"error": "Aucune édition valide à appliquer."}), 400
    if texte_valide_primary is None:
        return jsonify({"error": "La correction principale doit faire partie des éditions."}), 400

    try:
        applied = kb_edit.apply_edits(edits)
    except LookupError as e:
        return jsonify({"error": f"Correction annulée par sécurité : {e}"}), 409
    except (ResponseHandlingException, UnexpectedResponse):
        return jsonify({
            "error": "Erreur de connexion à la base vectorielle Qdrant. Réessayez dans un instant."
        }), 503
    except LLM_ERRORS:
        return jsonify({
            "error": "Erreur de l'API Gemini (limite de requêtes atteinte ou problème réseau)."
        }), 503

    correction_id = db.save_correction(table_name, colonne, texte_brut, texte_valide_primary, session["username"])
    for a in applied:
        db.log_corrected_chunk(
            correction_id, a["source"], a["table"], a.get("section", ""), a.get("colonne", ""),
            a["ancien_texte"], a["nouveau_texte"], session["username"]
        )

    return jsonify({
        "count": len(applied),
        "applied": [{"label": a["label"], "is_primary": a["is_primary"]} for a in applied],
    })


@app.route("/api/propositions", methods=["GET"])
def api_list_propositions():
    """Propositions de définition, visibles de TOUS (c'est le principe : une hypothèse
    partagée immédiatement, mais étiquetée comme telle). Filtrable par table/colonne."""
    return jsonify(db.list_propositions(
        table_name=(request.args.get("table") or "").strip() or None,
        colonne=(request.args.get("colonne") or "").strip() or None,
        statut=(request.args.get("statut") or "").strip() or None,
    ))


@app.route("/api/propositions", methods=["POST"])
def api_create_proposition():
    """Soumet une proposition de définition pour une colonne à dimension inconnue.
    N'écrit ni dans la Knowledge Base ni dans Qdrant — uniquement dans SQLite.

    Connexion requise : la proposition est affichée à tous sous « Proposé par X », ce qui
    n'a de sens qu'avec une identité nommée. Une session invitée n'a qu'un identifiant
    anonyme, inexploitable comme signature."""
    if not session.get("username"):
        return jsonify({"error": "Connexion requise pour proposer une définition."}), 401
    payload = request.get_json(silent=True) or {}
    table_name = (payload.get("table") or "").strip()
    colonne = (payload.get("colonne") or "").strip()
    texte = (payload.get("texte") or "").strip()
    if not table_name or not colonne or not texte:
        return jsonify({"error": "table, colonne et texte sont requis."}), 400
    if len(texte) > 2000:
        return jsonify({"error": "Proposition trop longue (2000 caractères maximum)."}), 400
    return jsonify(db.create_proposition(table_name, colonne, texte, session["username"]))


@app.route("/api/propositions/<int:prop_id>/rejeter", methods=["POST"])
@tech_lead_required
def api_reject_proposition(prop_id):
    prop = db.set_proposition_statut(prop_id, "rejetee", session["username"])
    if prop is None:
        return jsonify({"error": "Proposition introuvable ou déjà traitée."}), 404
    return jsonify(prop)


@app.route("/api/propositions/<int:prop_id>/valider", methods=["POST"])
@tech_lead_required
def api_validate_proposition(prop_id):
    """Valide une proposition : elle devient alors — et alors seulement — un vrai chunk de
    la Knowledge Base, réécrit dans le .md et ré-embedé dans Qdrant.

    Réutilise intégralement le chemin d'écriture des corrections (kb_edit.apply_edits) et
    son journal d'audit (corrected_chunks), ce qui fait que le chunk hérite automatiquement
    du marqueur [CORRECTION VALIDÉE] posé par rag.format_docs. Aucun mécanisme d'auth ni
    d'écriture parallèle n'est introduit ici.

    Le passage de statut est fait AVANT l'écriture, et de façon conditionnelle
    (set_proposition_statut n'agit que depuis `en_attente`) : deux validations concurrentes
    ne peuvent donc pas écrire deux fois le même texte dans le markdown. En cas d'échec de
    l'écriture, le statut est remis en attente."""
    prop = db.get_proposition(prop_id)
    if prop is None:
        return jsonify({"error": "Proposition introuvable."}), 404
    if prop["statut"] != "en_attente":
        return jsonify({"error": f"Proposition déjà {prop['statut']}."}), 409

    doc = kb_edit.find_chunk_by_column_name(prop["table_name"], prop["colonne"])
    if doc is None:
        return jsonify({
            "error": f"La colonne « {prop['colonne']} » n'a pas de bloc dédié dans la "
                     "Knowledge Base (elle n'est décrite que dans un tableau markdown). "
                     "Ajoutez-lui d'abord une section propre pour pouvoir y écrire."
        }), 409

    if db.set_proposition_statut(prop_id, "validee", session["username"]) is None:
        return jsonify({"error": "Proposition déjà traitée entre-temps."}), 409

    meta = doc.metadata
    nouveau = (
        f"{doc.page_content.rstrip()}\n"
        f"- Définition validée (proposée par {prop['auteur']}) : {prop['texte']}"
    )
    edit = {
        "source": meta.get("source", ""),
        "table_raw": meta.get("table", ""),
        "table": kb_meta.clean_table(meta.get("table", "")),
        "section": meta.get("section", ""),
        "colonne": meta.get("colonne", ""),
        "nouveau_texte": nouveau,
        "is_primary": True,
        "label": f"{prop['table_name']} › {prop['colonne']}",
    }

    try:
        applied = kb_edit.apply_edits([edit])
    except LookupError as e:
        db.set_proposition_statut(prop_id, "en_attente", None, depuis="validee")
        return jsonify({"error": f"Validation annulée par sécurité : {e}"}), 409
    except (ResponseHandlingException, UnexpectedResponse):
        db.set_proposition_statut(prop_id, "en_attente", None, depuis="validee")
        return jsonify({"error": "Erreur de connexion à Qdrant. Réessayez dans un instant."}), 503
    except LLM_ERRORS:
        db.set_proposition_statut(prop_id, "en_attente", None, depuis="validee")
        return jsonify({"error": "Erreur de l'API Gemini (limite de requêtes ou réseau)."}), 503

    correction_id = db.save_correction(
        prop["table_name"], prop["colonne"], prop["texte"], nouveau, session["username"]
    )
    for a in applied:
        db.log_corrected_chunk(
            correction_id, a["source"], a["table"], a.get("section", ""), a.get("colonne", ""),
            a["ancien_texte"], a["nouveau_texte"], session["username"],
        )
    return jsonify({"proposition": db.get_proposition(prop_id), "chunks_ecrits": len(applied)})


@app.route("/api/corrections/mine", methods=["GET"])
@tech_lead_required
def api_list_my_corrections():
    return jsonify(db.list_corrections_by_author(session["username"]))


@app.route("/api/tables/prepare", methods=["POST"])
@tech_lead_required
def api_prepare_table():
    """Dry-run : valide `spec` et renvoie le markdown qui serait écrit + le nombre de
    chunks — n'écrit rien. Contrairement à /api/corrections/prepare, aucun appel LLM ni
    Qdrant (voir kb_create.preview_table), donc pas de branche 503 ici."""
    payload = request.get_json(silent=True) or {}
    spec = payload.get("spec") or {}
    try:
        return jsonify(kb_create.preview_table(spec))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/tables/publish", methods=["POST"])
@tech_lead_required
def api_publish_table():
    """Publie un brouillon existant (jamais une spec ad hoc non sauvegardée) : réécrit le
    fichier du dataset ciblé par la spec + resynchronise Qdrant, marque le brouillon publié,
    journalise dans created_tables."""
    payload = request.get_json(silent=True) or {}
    draft_id = (payload.get("draft_id") or "").strip()
    if not draft_id:
        return jsonify({"error": "draft_id requis."}), 400

    draft = db.get_draft(draft_id)
    if draft is None or draft["auteur"] != session["username"]:
        return jsonify({"error": "Brouillon introuvable."}), 404
    if draft["status"] == "published":
        return jsonify({"error": "Ce brouillon a déjà été publié."}), 409

    try:
        result = kb_create.create_table(draft["payload"])
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except LookupError as e:
        return jsonify({"error": f"Publication annulée par sécurité : {e}"}), 409
    except (ResponseHandlingException, UnexpectedResponse):
        return jsonify({
            "error": "Erreur de connexion à la base vectorielle Qdrant. Réessayez dans un instant."
        }), 503
    except LLM_ERRORS:
        return jsonify({
            "error": "Erreur de l'API Gemini (limite de requêtes atteinte ou problème réseau)."
        }), 503

    published = db.mark_draft_published(draft_id, session["username"])
    if published is None:
        # Course perdue entre le check ci-dessus et l'écriture (double clic, deux onglets) :
        # le .md/Qdrant sont déjà à jour (create_table a réussi), on journalise quand même,
        # mais on prévient que le statut du brouillon n'a pas pu être mis à jour proprement.
        db.log_created_table(
            draft_id, result["table_name"], result["source"],
            result["table_markdown"], result["chunks_added"], session["username"]
        )
        return jsonify({"error": "Table créée mais le brouillon était déjà marqué publié (double clic ?)."}), 409

    db.log_created_table(
        draft_id, result["table_name"], result["source"],
        result["table_markdown"], result["chunks_added"], session["username"]
    )
    return jsonify(result)


@app.route("/api/tables/drafts", methods=["GET"])
@tech_lead_required
def api_list_table_drafts():
    return jsonify(db.list_drafts_by_author(session["username"]))


@app.route("/api/tables/drafts", methods=["POST"])
@tech_lead_required
def api_create_table_draft():
    payload = request.get_json(silent=True) or {}
    return jsonify(db.create_draft(session["username"], payload.get("spec") or {}))


@app.route("/api/tables/drafts/<draft_id>", methods=["GET"])
@tech_lead_required
def api_get_table_draft(draft_id):
    draft = db.get_draft(draft_id)
    if draft is None or draft["auteur"] != session["username"]:
        return jsonify({"error": "Brouillon introuvable."}), 404
    return jsonify(draft)


@app.route("/api/tables/drafts/<draft_id>", methods=["PUT"])
@tech_lead_required
def api_update_table_draft(draft_id):
    payload = request.get_json(silent=True) or {}
    draft = db.update_draft(draft_id, session["username"], payload.get("spec") or {})
    if draft is None:
        return jsonify({"error": "Brouillon introuvable."}), 404
    return jsonify(draft)


@app.route("/api/tables/drafts/<draft_id>", methods=["DELETE"])
@tech_lead_required
def api_delete_table_draft(draft_id):
    if not db.delete_draft(draft_id, session["username"]):
        return jsonify({"error": "Brouillon introuvable ou déjà publié."}), 404
    return jsonify({"deleted": True})


@app.route("/ask", methods=["POST"])
def ask():
    payload = request.get_json(silent=True) or {}
    question = (payload.get("question") or "").strip()
    conversation_id = payload.get("conversation_id")

    if not question:
        return jsonify({"error": "La question est vide."}), 400
    if not conversation_id:
        return jsonify({"error": "conversation_id manquant."}), 400
    if db.get_conversation_owner(conversation_id) != current_identity():
        return jsonify({"error": "Conversation introuvable."}), 404

    try:
        docs_trouves = retrieve_docs(question)

        if not docs_trouves:
            reponse_html = "<p>Aucun chunk pertinent trouvé dans la Knowledge Base pour cette question.</p>"
            db.add_message(conversation_id, "user", question)
            db.add_message(conversation_id, "assistant", "Aucun chunk pertinent trouvé dans la Knowledge Base pour cette question.")
            return jsonify({
                "answer_html": reponse_html,
                "sources": [],
                "sources_corrigibles": [],
                "corrections_appliquees": []
            })

        contexte = format_docs(docs_trouves)
        corrections_appliquees = corrected_sources(docs_trouves)

        historique = db.build_history_string(conversation_id)
        chrono_debut = time.perf_counter()
        reponse_markdown = chain.invoke({"question": question, "history": historique, "context": contexte})
        response_time_ms = round((time.perf_counter() - chrono_debut) * 1000)
        texte_principal, suggestions = extract_suggestions(reponse_markdown)
        reponse_html = md.markdown(texte_principal, extensions=["tables"])
        sources = extract_sources(docs_trouves)
        sources_corrigibles = extract_correctable_sources(docs_trouves)

        db.add_message(conversation_id, "user", question)
        db.add_message(
            conversation_id, "assistant", reponse_markdown, sources,
            response_time_ms=response_time_ms, corrections_appliquees=corrections_appliquees,
            sources_corrigibles=sources_corrigibles
        )

        # génère le titre uniquement si c'est le premier échange de la conversation
        nouveau_titre = None
        messages_existants = db.get_messages(conversation_id)
        if len(messages_existants) == 2:  # cette question + cette réponse, rien avant
            nouveau_titre = generate_title(question, reponse_markdown)
            db.set_title(conversation_id, nouveau_titre)

        return jsonify({
            "answer_html": reponse_html,
            "sources": sources,
            "sources_corrigibles": sources_corrigibles,
            "suggestions": suggestions,
            "corrections_appliquees": corrections_appliquees,
            "new_title": nouveau_titre
        })

    except (ResponseHandlingException, UnexpectedResponse):
        return jsonify({
            "error": "Erreur de connexion à la base vectorielle Qdrant. Réessayez dans un instant."
        }), 503

    except LLM_ERRORS:
        return jsonify({
            "error": "Erreur de l'API Gemini (limite de requêtes atteinte ou problème réseau)."
        }), 503


if __name__ == "__main__":
    app.run(debug=True, port=5000)