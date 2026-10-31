"""
db.py — persistance SQLite pour l'historique des conversations.

Un fichier conversations.db est créé automatiquement au même endroit que ce module,
au premier lancement. Aucune configuration supplémentaire nécessaire.
"""

import sqlite3
import uuid
import json
from datetime import datetime, timezone
from pathlib import Path

from werkzeug.security import generate_password_hash, check_password_hash

# kb_meta ne dépend ni de ce module, ni de Qdrant, ni du LLM (voir son docstring) : l'importer
# ici ne crée aucun cycle et évite de recopier la normalisation des noms de table.
import kb_meta

DB_PATH = Path(__file__).parent / "conversations.db"

# Nombre d'échanges (paire question/réponse) envoyés comme historique au prompt.
# Plafonné pour éviter que le prompt grossisse sans limite au fil d'une longue conversation.
MAX_HISTORY_EXCHANGES = 3

# Plafonds de VOLUME de l'historique (voir build_history_string). Le compte d'échanges seul
# ne borne rien : trois échanges de réponses longues pèsent plus que tout le reste du prompt.
# rag.HISTORY_RESERVE doit rester aligné sur MAX_HISTORY_CHARS.
MAX_HISTORY_MESSAGE_CHARS = 500
MAX_HISTORY_CHARS = 2500


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY,
            title TEXT,
            created_at TEXT NOT NULL,
            username TEXT
        )
    """)
    # migration : les conversations créées avant le Verdict 4 n'ont pas de propriétaire —
    # elles deviennent invisibles (aucune identité ne vaut NULL), c'est voulu : on ne peut
    # pas deviner rétroactivement à qui elles appartenaient.
    existing_conv_columns = {row["name"] for row in conn.execute("PRAGMA table_info(conversations)")}
    if "username" not in existing_conv_columns:
        conn.execute("ALTER TABLE conversations ADD COLUMN username TEXT")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            sources TEXT,
            suggestions TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (conversation_id) REFERENCES conversations(id)
        )
    """)
    # migration : les bases créées avant l'ajout des suggestions n'ont pas cette colonne
    existing_columns = {row["name"] for row in conn.execute("PRAGMA table_info(messages)")}
    if "suggestions" not in existing_columns:
        conn.execute("ALTER TABLE messages ADD COLUMN suggestions TEXT")
    # migration : les lignes créées avant le chronométrage restent à NULL (durée inconnue)
    if "response_time_ms" not in existing_columns:
        conn.execute("ALTER TABLE messages ADD COLUMN response_time_ms INTEGER")
    # migration : les lignes créées avant le Verdict 4 n'ont pas ces colonnes
    if "corrections_appliquees" not in existing_columns:
        conn.execute("ALTER TABLE messages ADD COLUMN corrections_appliquees TEXT")
    if "sources_corrigibles" not in existing_columns:
        conn.execute("ALTER TABLE messages ADD COLUMN sources_corrigibles TEXT")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('user', 'tech_lead')),
            created_at TEXT NOT NULL
        )
    """)
    # Partage d'une conversation avec un autre utilisateur, en lecture seule : `shared_with`
    # peut alors lister/consulter la conversation (voir can_view_conversation), mais reste
    # soumis aux mêmes vérifications strictes de propriétaire pour /ask, le renommage et la
    # suppression (aucun changement à ces contrôles-là — seule la lecture s'ouvre).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversation_shares (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL,
            shared_by TEXT NOT NULL,
            shared_with TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(conversation_id, shared_with)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS propositions_colonnes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            table_name TEXT NOT NULL,
            colonne TEXT NOT NULL,
            texte TEXT NOT NULL,
            auteur TEXT NOT NULL,
            date TEXT NOT NULL,
            statut TEXT NOT NULL DEFAULT 'en_attente'
                CHECK(statut IN ('en_attente', 'validee', 'rejetee')),
            valide_par TEXT,
            valide_le TEXT
        )
    """)
    # Propositions de définition soumises par les utilisateurs sur les colonnes dont la
    # dimension cible est inconnue (statuts ⚠️/❌ de l'inventaire des FK).
    #
    # Elles vivent DÉLIBÉRÉMENT ici et nulle part ailleurs : une proposition n'est jamais
    # embarquée dans Qdrant, donc jamais récupérable comme contexte, donc jamais citable
    # par l'assistant comme un fait. C'est la garantie centrale du dispositif — la KB reste
    # un corpus vérifié, et une hypothèse d'utilisateur ne peut pas s'y glisser par accident.
    # Elle n'entre dans la Knowledge Base qu'après validation explicite par un tech lead,
    # via le même chemin d'écriture que les corrections (kb_edit), avec la même trace.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS corrections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            table_name TEXT NOT NULL,
            colonne TEXT NOT NULL,
            texte_brut TEXT NOT NULL,
            texte_valide TEXT NOT NULL,
            auteur TEXT NOT NULL,
            date TEXT NOT NULL,
            UNIQUE(table_name, colonne)
        )
    """)
    # Journal d'audit : une ligne par chunk réellement réécrit dans la Knowledge Base
    # (le chunk primaire ET chaque chunk de propagation). Sert à la fois de trace
    # (qui/quand/avant/après, sans bouton d'annulation) et de source du repère ✓
    # discret affiché en pied de réponse pour les chunks corrigés.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS corrected_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            correction_id INTEGER,
            source_file TEXT NOT NULL,
            table_name TEXT NOT NULL,
            section TEXT,
            colonne TEXT,
            ancien_texte TEXT NOT NULL,
            nouveau_texte TEXT NOT NULL,
            auteur TEXT NOT NULL,
            date TEXT NOT NULL
        )
    """)
    # Brouillons de nouvelles tables (feature « créer une table ») : payload_json contient
    # la spec structurée (voir kb_create.py), potentiellement incomplète/invalide tant que
    # le brouillon n'est pas publié — aucune validation à l'écriture ici, seulement à la
    # publication (kb_create.validate_table_spec). Pas de UNIQUE(table_name) : deux
    # brouillons peuvent transitoirement partager un nom de travail, seule la publication
    # tranche contre l'état réel de la KB.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS table_drafts (
            id TEXT PRIMARY KEY,
            auteur TEXT NOT NULL,
            table_name TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft', 'published')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            published_at TEXT
        )
    """)
    # Journal d'audit des tables effectivement publiées — append-only, comme
    # corrected_chunks. Stocke le markdown littéral écrit (pas de "avant" à diffuser ici,
    # contrairement à une correction, donc une seule colonne de texte suffit).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS created_tables (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            draft_id TEXT,
            table_name TEXT NOT NULL,
            source_file TEXT NOT NULL,
            table_markdown TEXT NOT NULL,
            chunk_count INTEGER NOT NULL,
            auteur TEXT NOT NULL,
            date TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def create_conversation(owner):
    conn = get_db()
    conv_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO conversations (id, title, created_at, username) VALUES (?, ?, ?, ?)",
        (conv_id, None, now, owner)
    )
    conn.commit()
    conn.close()
    return {"id": conv_id, "title": None, "created_at": now}


def list_conversations(owner):
    conn = get_db()
    rows = conn.execute(
        "SELECT id, title, created_at FROM conversations WHERE username = ? ORDER BY created_at DESC",
        (owner,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_conversation_owner(conv_id):
    conn = get_db()
    row = conn.execute("SELECT username FROM conversations WHERE id = ?", (conv_id,)).fetchone()
    conn.close()
    return row["username"] if row else None


def delete_conversation(conv_id):
    conn = get_db()
    conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conv_id,))
    conn.execute("DELETE FROM conversation_shares WHERE conversation_id = ?", (conv_id,))
    conn.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
    conn.commit()
    conn.close()


def set_title(conv_id, title):
    conn = get_db()
    conn.execute("UPDATE conversations SET title = ? WHERE id = ?", (title, conv_id))
    conn.commit()
    conn.close()


# ---------- Partage de conversations (lecture seule) ----------
# Le renommage, la suppression et /ask restent strictement réservés au propriétaire
# (get_conversation_owner direct, inchangé) — seule la LECTURE s'ouvre à `shared_with`,
# via can_view_conversation.


def username_exists(username):
    conn = get_db()
    row = conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()
    return row is not None


def share_conversation(conv_id, owner, shared_with):
    """Partage `conv_id` (dont `owner` doit être propriétaire) avec `shared_with`, en
    lecture seule. Renvoie False si `owner` n'en est pas le propriétaire ou si
    `shared_with` n'est pas un utilisateur enregistré ; True sinon. Idempotent : partager
    deux fois la même conversation avec le même utilisateur est un no-op (contrainte
    UNIQUE(conversation_id, shared_with))."""
    if get_conversation_owner(conv_id) != owner:
        return False
    if not username_exists(shared_with):
        return False
    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO conversation_shares (conversation_id, shared_by, shared_with, created_at) "
        "VALUES (?, ?, ?, ?)",
        (conv_id, owner, shared_with, now)
    )
    conn.commit()
    conn.close()
    return True


def list_shares(conv_id, owner):
    """Utilisateurs avec qui `conv_id` est partagée. Renvoie None si `owner` n'en est pas
    le propriétaire (jamais 403 côté app.py : on ne révèle pas l'existence d'une
    conversation qui n'est pas la sienne)."""
    if get_conversation_owner(conv_id) != owner:
        return None
    conn = get_db()
    rows = conn.execute(
        "SELECT shared_with, created_at FROM conversation_shares WHERE conversation_id = ? ORDER BY created_at ASC",
        (conv_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def revoke_share(conv_id, owner, shared_with):
    """Retire un partage. Renvoie False si `owner` n'est pas propriétaire de `conv_id`
    (le retrait est alors un no-op silencieux, même si le partage n'existait pas)."""
    if get_conversation_owner(conv_id) != owner:
        return False
    conn = get_db()
    conn.execute(
        "DELETE FROM conversation_shares WHERE conversation_id = ? AND shared_with = ?",
        (conv_id, shared_with)
    )
    conn.commit()
    conn.close()
    return True


def list_shared_with_me(username):
    """Conversations partagées avec `username`, les plus récemment partagées en premier —
    alimente la section « Partagées avec moi » de la sidebar."""
    conn = get_db()
    rows = conn.execute("""
        SELECT c.id, c.title, c.created_at, s.shared_by AS owner
        FROM conversation_shares s
        JOIN conversations c ON c.id = s.conversation_id
        WHERE s.shared_with = ?
        ORDER BY s.created_at DESC
    """, (username,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def can_view_conversation(conv_id, identity):
    """True si `identity` est le propriétaire de `conv_id` OU si la conversation lui a été
    partagée. Utilisé uniquement pour la lecture des messages — voir note en tête de
    section pour ce qui reste strictement réservé au propriétaire."""
    owner = get_conversation_owner(conv_id)
    if owner is None:
        return False
    if owner == identity:
        return True
    conn = get_db()
    row = conn.execute(
        "SELECT 1 FROM conversation_shares WHERE conversation_id = ? AND shared_with = ?",
        (conv_id, identity)
    ).fetchone()
    conn.close()
    return row is not None


def add_message(conv_id, role, content, sources=None, suggestions=None, response_time_ms=None,
                 corrections_appliquees=None, sources_corrigibles=None):
    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO messages (conversation_id, role, content, sources, suggestions, created_at, "
        "response_time_ms, corrections_appliquees, sources_corrigibles) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            conv_id, role, content,
            json.dumps(sources) if sources else None,
            json.dumps(suggestions) if suggestions else None,
            now,
            response_time_ms,
            json.dumps(corrections_appliquees) if corrections_appliquees else None,
            json.dumps(sources_corrigibles) if sources_corrigibles else None,
        )
    )
    conn.commit()
    conn.close()


def get_messages(conv_id):
    conn = get_db()
    rows = conn.execute(
        "SELECT role, content, sources, suggestions, created_at, response_time_ms, "
        "corrections_appliquees, sources_corrigibles "
        "FROM messages WHERE conversation_id = ? ORDER BY id ASC",
        (conv_id,)
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        item = dict(r)
        item["sources"] = json.loads(item["sources"]) if item["sources"] else []
        item["suggestions"] = json.loads(item["suggestions"]) if item["suggestions"] else []
        item["corrections_appliquees"] = json.loads(item["corrections_appliquees"]) if item["corrections_appliquees"] else []
        item["sources_corrigibles"] = json.loads(item["sources_corrigibles"]) if item["sources_corrigibles"] else []
        result.append(item)
    return result


def build_history_string(conv_id):
    """Construit l'historique formaté pour le prompt, plafonné aux N derniers échanges ET
    en volume de caractères.

    Le plafond en volume n'est pas cosmétique : le proxy LLM d'Orange refuse toute requête
    au-delà de 10 000 tokens, et une réponse d'assistant peut à elle seule en faire 2 000.
    Trois échanges recopiés intégralement suffisaient donc à faire dépasser le prompt dès
    le deuxième ou troisième tour d'une conversation — l'utilisateur voyait un 503 qui
    n'avait rien d'une limite de débit.

    Tronquer est sans perte fonctionnelle : le prompt système n'autorise l'historique que
    pour lever une ambiguïté de référence (« et pour X ? »), jamais comme source de faits —
    ceux-ci viennent exclusivement du contexte récupéré. Le début de chaque message suffit
    à cet usage, et les échanges les plus anciens sont sacrifiés avant les récents."""
    messages = get_messages(conv_id)
    # garde les N derniers échanges = 2*N derniers messages (user+assistant alternés)
    recent = messages[-(MAX_HISTORY_EXCHANGES * 2):]
    if not recent:
        return ""

    lines = []
    total = 0
    for m in reversed(recent):  # du plus récent au plus ancien
        prefix = "Utilisateur" if m["role"] == "user" else "Assistant"
        contenu = (m["content"] or "").strip()
        if len(contenu) > MAX_HISTORY_MESSAGE_CHARS:
            contenu = contenu[:MAX_HISTORY_MESSAGE_CHARS].rstrip() + " […]"
        ligne = f"{prefix} : {contenu}"
        if lines and total + len(ligne) > MAX_HISTORY_CHARS:
            break
        lines.append(ligne)
        total += len(ligne)
    return "\n".join(reversed(lines))


# ---------- Authentification ----------


def create_user(username, password, role="user"):
    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
        (username, generate_password_hash(password), role, now)
    )
    conn.commit()
    conn.close()


def verify_user(username, password):
    conn = get_db()
    row = conn.execute(
        "SELECT username, password_hash, role FROM users WHERE username = ?",
        (username,)
    ).fetchone()
    conn.close()
    if not row or not check_password_hash(row["password_hash"], password):
        return None
    return {"username": row["username"], "role": row["role"]}


# ---------- Corrections humaines (Verdict 4) ----------


def extract_entity(doc):
    """Normalise l'entité (table, colonne) ciblée par un chunk depuis ses métadonnées.

    Le nettoyage du nom de table passe par kb_meta.clean_table — la MÊME fonction
    qu'utilisent app.py, ingest.py et corrected_chunks_index() ci-dessous. C'était
    auparavant un `.replace("Table : ", "")` recopié dans chacun de ces fichiers, ce qui ne
    savait traiter ni les headers `## Dimension : …` ni les noms entre backticks des
    documents Orange (`## Table : \\`dwh_fact_Vente_t\\` *(alias \\`vente\\`)*`)."""
    table = kb_meta.clean_table(doc.metadata.get("table") or "")
    colonne = (doc.metadata.get("colonne") or "").strip()
    return table, colonne


# ---------- Propositions de définition de colonne ----------


def create_proposition(table_name, colonne, texte, auteur):
    """Enregistre une proposition en attente et la renvoie. Aucune écriture KB/Qdrant."""
    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO propositions_colonnes (table_name, colonne, texte, auteur, date, statut) "
        "VALUES (?, ?, ?, ?, ?, 'en_attente')",
        (table_name, colonne, texte, auteur, now),
    )
    conn.commit()
    prop_id = cur.lastrowid
    conn.close()
    return {
        "id": prop_id, "table_name": table_name, "colonne": colonne, "texte": texte,
        "auteur": auteur, "date": now, "statut": "en_attente",
        "valide_par": None, "valide_le": None,
    }


def list_propositions(table_name=None, colonne=None, statut=None):
    """Propositions filtrées, les plus récentes d'abord. Sans filtre de statut, renvoie
    tout : l'UI affiche les propositions en attente à TOUS les utilisateurs (avec leur
    badge « non vérifié »), c'est le principe même de la fonctionnalité."""
    clauses, params = [], []
    if table_name:
        clauses.append("table_name = ?")
        params.append(table_name)
    if colonne:
        clauses.append("colonne = ?")
        params.append(colonne)
    if statut:
        clauses.append("statut = ?")
        params.append(statut)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, table_name, colonne, texte, auteur, date, statut, valide_par, valide_le "
        f"FROM propositions_colonnes{where} ORDER BY id DESC",
        params,
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_proposition(prop_id):
    conn = get_db()
    row = conn.execute(
        "SELECT id, table_name, colonne, texte, auteur, date, statut, valide_par, valide_le "
        "FROM propositions_colonnes WHERE id = ?",
        (prop_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def set_proposition_statut(prop_id, statut, valide_par, depuis="en_attente"):
    """Fait passer une proposition à `statut`, uniquement si elle est actuellement à
    `depuis`. Renvoie la proposition mise à jour, ou None si la transition n'était pas
    permise.

    La transition conditionnelle en SQL (et non un test lu avant l'UPDATE) est ce qui
    empêche un double clic de valider deux fois — donc d'écrire deux fois le même texte
    dans le markdown. `depuis` permet aussi le retour arrière `validee -> en_attente`
    quand l'écriture KB échoue après le changement de statut."""
    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "UPDATE propositions_colonnes SET statut = ?, valide_par = ?, valide_le = ? "
        "WHERE id = ? AND statut = ?",
        (statut, valide_par, now, prop_id, depuis),
    )
    conn.commit()
    conn.close()
    return get_proposition(prop_id) if cur.rowcount else None


def get_correction(table_name, colonne):
    conn = get_db()
    row = conn.execute(
        "SELECT table_name, colonne, texte_brut, texte_valide, auteur, date "
        "FROM corrections WHERE table_name = ? AND colonne = ?",
        (table_name, colonne)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def save_correction(table_name, colonne, texte_brut, texte_valide, auteur):
    """Enregistre (ou met à jour) l'événement de correction pour la colonne ciblée et
    renvoie son id — utilisé pour rattacher les chunks réécrits dans corrected_chunks."""
    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        INSERT INTO corrections (table_name, colonne, texte_brut, texte_valide, auteur, date)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(table_name, colonne) DO UPDATE SET
            texte_brut = excluded.texte_brut,
            texte_valide = excluded.texte_valide,
            auteur = excluded.auteur,
            date = excluded.date
    """, (table_name, colonne, texte_brut, texte_valide, auteur, now))
    row = conn.execute(
        "SELECT id FROM corrections WHERE table_name = ? AND colonne = ?",
        (table_name, colonne)
    ).fetchone()
    conn.commit()
    conn.close()
    return row["id"]


def log_corrected_chunk(correction_id, source_file, table_name, section, colonne,
                        ancien_texte, nouveau_texte, auteur):
    """Journalise un chunk effectivement réécrit dans la Knowledge Base (primaire ou
    propagation). Pas de contrainte d'unicité : chaque correction laisse sa propre trace,
    y compris si le même chunk est corrigé plusieurs fois au fil du temps."""
    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        INSERT INTO corrected_chunks
            (correction_id, source_file, table_name, section, colonne,
             ancien_texte, nouveau_texte, auteur, date)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (correction_id, source_file, table_name, section, colonne,
          ancien_texte, nouveau_texte, auteur, now))
    conn.commit()
    conn.close()


def corrected_chunks_index():
    """Renvoie {(table_name, detail): {'auteur', 'date'}} pour repérer, en pied de réponse,
    les sources issues d'un chunk corrigé. `detail` = colonne si présente, sinon section —
    de façon à matcher le libellé « table › detail » des puces de source. La correction la
    plus récente pour une clé donnée l'emporte (parcours par id croissant, dernier écrit)."""
    conn = get_db()
    rows = conn.execute(
        "SELECT table_name, section, colonne, auteur, date FROM corrected_chunks ORDER BY id ASC"
    ).fetchall()
    conn.close()
    index = {}
    for r in rows:
        detail = (r["colonne"] or r["section"] or "").strip()
        index[(r["table_name"], detail)] = {"auteur": r["auteur"], "date": r["date"]}
    return index


def delete_correction(table_name, colonne):
    """Supprime une correction — utile pour annuler un test sans laisser de trace.
    N'affecte que la table `corrections` en local ; Qdrant n'a jamais été touché."""
    conn = get_db()
    conn.execute(
        "DELETE FROM corrections WHERE table_name = ? AND colonne = ?",
        (table_name, colonne)
    )
    conn.commit()
    conn.close()


def list_corrections_by_author(auteur):
    """Corrections faites par un tech lead donné, les plus récentes en premier —
    alimente le panneau "Mes corrections" où il peut revenir sur ses propres modifs."""
    conn = get_db()
    rows = conn.execute(
        "SELECT table_name, colonne, texte_brut, texte_valide, auteur, date "
        "FROM corrections WHERE auteur = ? ORDER BY date DESC",
        (auteur,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------- Brouillons de nouvelles tables (feature création de table) ----------
# Un brouillon vit tant qu'il n'est pas publié : payload_json porte la spec structurée
# (voir kb_create.py) telle quelle, sans validation à l'écriture — seule la publication
# (kb_create.validate_table_spec, appelée depuis /api/tables/publish) valide pour de bon.


def _draft_row_to_dict(row):
    item = dict(row)
    item["payload"] = json.loads(item.pop("payload_json"))
    return item


def create_draft(auteur, payload):
    """Crée un nouveau brouillon à partir de `payload` (peut être incomplet). Renvoie le
    brouillon créé (avec son id généré)."""
    conn = get_db()
    draft_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    table_name = (payload.get("name") or "").strip()
    conn.execute(
        "INSERT INTO table_drafts (id, auteur, table_name, payload_json, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'draft', ?, ?)",
        (draft_id, auteur, table_name, json.dumps(payload), now, now)
    )
    conn.commit()
    conn.close()
    return get_draft(draft_id)


def update_draft(draft_id, auteur, payload):
    """Remplace la spec d'un brouillon existant. Renvoie None si introuvable ou si
    `auteur` n'en est pas l'auteur (jamais 403 dans app.py : on ne révèle pas l'existence
    d'un brouillon qui n'est pas le sien)."""
    existing = get_draft(draft_id)
    if existing is None or existing["auteur"] != auteur:
        return None
    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()
    table_name = (payload.get("name") or "").strip()
    conn.execute(
        "UPDATE table_drafts SET table_name = ?, payload_json = ?, updated_at = ? WHERE id = ?",
        (table_name, json.dumps(payload), now, draft_id)
    )
    conn.commit()
    conn.close()
    return get_draft(draft_id)


def get_draft(draft_id):
    conn = get_db()
    row = conn.execute(
        "SELECT id, auteur, table_name, payload_json, status, created_at, updated_at, published_at "
        "FROM table_drafts WHERE id = ?",
        (draft_id,)
    ).fetchone()
    conn.close()
    return _draft_row_to_dict(row) if row else None


def list_drafts_by_author(auteur):
    """Brouillons d'un tech lead, les plus récemment modifiés en premier — alimente le
    panneau "Mes brouillons"."""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, auteur, table_name, payload_json, status, created_at, updated_at, published_at "
        "FROM table_drafts WHERE auteur = ? ORDER BY updated_at DESC",
        (auteur,)
    ).fetchall()
    conn.close()
    return [_draft_row_to_dict(r) for r in rows]


def delete_draft(draft_id, auteur):
    """Supprime un brouillon. Renvoie False s'il est introuvable, pas possédé par
    `auteur`, ou déjà publié — un brouillon publié devient un enregistrement d'audit
    immuable, pas un brouillon qu'on peut encore jeter."""
    existing = get_draft(draft_id)
    if existing is None or existing["auteur"] != auteur or existing["status"] == "published":
        return False
    conn = get_db()
    conn.execute("DELETE FROM table_drafts WHERE id = ?", (draft_id,))
    conn.commit()
    conn.close()
    return True


def mark_draft_published(draft_id, auteur):
    """Marque un brouillon comme publié. Renvoie None s'il est introuvable, pas possédé,
    ou déjà publié — ce dernier cas est un garde-fou anti double-publication (double clic,
    deux onglets), en plus de la revérification déjà faite par kb_create._plan_creation
    sur le nom de table lui-même."""
    existing = get_draft(draft_id)
    if existing is None or existing["auteur"] != auteur or existing["status"] == "published":
        return None
    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE table_drafts SET status = 'published', published_at = ?, updated_at = ? WHERE id = ?",
        (now, now, draft_id)
    )
    conn.commit()
    conn.close()
    return get_draft(draft_id)


def log_created_table(draft_id, table_name, source_file, table_markdown, chunk_count, auteur):
    """Journalise une table effectivement publiée — append-only, comme
    log_corrected_chunk(). Stocke le markdown littéral écrit."""
    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO created_tables (draft_id, table_name, source_file, table_markdown, chunk_count, auteur, date) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (draft_id, table_name, source_file, table_markdown, chunk_count, auteur, now)
    )
    conn.commit()
    creation_id = cur.lastrowid
    conn.close()
    return creation_id


# ---------- Statistiques pour le dashboard de télémétrie ----------

# Nombre de points conservés pour la tendance récente du temps de réponse.
RECENT_TREND_WINDOW = 12

# Nombre de tranches (bins) pour l'histogramme de distribution des temps de réponse.
DISTRIBUTION_BINS = 6


def _mean(values):
    return sum(values) / len(values) if values else None


def _normalize_heights(values, floor=12):
    """Ramène une liste de valeurs à des hauteurs en % (avec un plancher pour
    que même les petites valeurs restent visibles dans une sparkline)."""
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi == lo:
        return [70 for _ in values]
    return [floor + (v - lo) / (hi - lo) * (100 - floor) for v in values]


def _response_time_histogram(values, bins=DISTRIBUTION_BINS):
    """Répartit les temps de réponse (ms) en tranches égales pour un histogramme."""
    if not values:
        return []
    lo, hi = min(values), max(values)
    if lo == hi:
        return [{"label": f"{lo / 1000:.1f}s", "count": len(values), "height_pct": 100}]
    width = (hi - lo) / bins
    counts = [0] * bins
    for v in values:
        idx = min(int((v - lo) / width), bins - 1)
        counts[idx] += 1
    max_count = max(counts)
    result = []
    for i, count in enumerate(counts):
        start_s = (lo + i * width) / 1000
        end_s = (lo + (i + 1) * width) / 1000
        result.append({
            "label": f"{start_s:.1f}–{end_s:.1f}s",
            "count": count,
            "height_pct": round(count / max_count * 100) if max_count else 0,
        })
    return result


def _is_off_track(content):
    """Une réponse est « hors piste » si aucun chunk pertinent n'a été trouvé, ou si
    le LLM a refusé de répondre car la question sortait du périmètre (bloc SUGGESTIONS)."""
    return content.startswith("Aucun chunk pertinent trouvé") or "SUGGESTIONS:" in content


def get_dashboard_stats():
    """Calcule les statistiques agrégées affichées sur /dashboard."""
    conn = get_db()
    total_conversations = conn.execute("SELECT COUNT(*) AS c FROM conversations").fetchone()["c"]
    total_questions = conn.execute("SELECT COUNT(*) AS c FROM messages WHERE role = 'user'").fetchone()["c"]
    assistant_rows = conn.execute(
        "SELECT content, response_time_ms FROM messages WHERE role = 'assistant' ORDER BY id ASC"
    ).fetchall()
    user_rows = conn.execute(
        "SELECT content FROM messages WHERE role = 'user'"
    ).fetchall()
    conn.close()

    response_times = [r["response_time_ms"] for r in assistant_rows if r["response_time_ms"] is not None]
    word_counts = [len(r["content"].split()) for r in assistant_rows]
    char_counts = [len(r["content"]) for r in assistant_rows]
    off_track_count = sum(1 for r in assistant_rows if _is_off_track(r["content"]))

    recent_times = response_times[-RECENT_TREND_WINDOW:]
    older_times = response_times[:-RECENT_TREND_WINDOW] if len(response_times) > RECENT_TREND_WINDOW else []
    recent_avg = _mean(recent_times)
    older_avg = _mean(older_times) if older_times else None
    trend_delta_ms = (recent_avg - older_avg) if (recent_avg is not None and older_avg is not None) else None

    sparkline_heights = _normalize_heights(recent_times)
    sparkline = [
        {"value_ms": v, "height_pct": h}
        for v, h in zip(recent_times, sparkline_heights)
    ]

    questions = [r["content"] for r in user_rows]
    longest_question = max(questions, key=len) if questions else None
    shortest_question = min(questions, key=len) if questions else None

    return {
        "total_conversations": total_conversations,
        "total_questions": total_questions,
        "total_assistant_messages": len(assistant_rows),
        "avg_response_time_ms": _mean(response_times),
        "response_time_min_ms": min(response_times) if response_times else None,
        "response_time_max_ms": max(response_times) if response_times else None,
        "response_time_trend_delta_ms": trend_delta_ms,
        "response_time_sparkline": sparkline,
        "response_time_distribution": _response_time_histogram(response_times),
        "avg_response_words": _mean(word_counts),
        "avg_response_chars": _mean(char_counts),
        "off_track_count": off_track_count,
        "off_track_pct": (off_track_count / len(assistant_rows) * 100) if assistant_rows else None,
        "longest_question": {"text": longest_question, "length": len(longest_question)} if longest_question else None,
        "shortest_question": {"text": shortest_question, "length": len(shortest_question)} if shortest_question else None,
    }