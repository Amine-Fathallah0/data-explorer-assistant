"""
kb_edit.py — écriture directe des corrections dans la Knowledge Base.

Ancien modèle (retiré) : une correction validée était stockée en base et réinjectée
en mémoire à chaque question (overlay), sans jamais toucher le .md ni Qdrant. La
correction n'avait d'effet que si le chunk concerné était justement récupéré, et
n'influençait pas la recherche vectorielle (embedding de l'ancien texte).

Nouveau modèle : une correction validée
  1. réécrit le corps exact du chunk dans le fichier .md source (source de vérité),
  2. resynchronise Qdrant chunk par chunk — supprime l'ancien point, ré-embed le
     nouveau, avec le MÊME schéma d'id (uuid5(source::page_content)) que ingest.py.
     Un futur re-ingest complet reste donc idempotent, sans doublon.

Recherche des chunks impactés (candidats) : scan déterministe, sans LLM ni embedding.
Le scan est scopé sur (table, colonne) et non sur la colonne seule — corriger
`dwh_fact_Vente_t.DateMaj` ne doit pas remonter `dwh_dim_GeoIris_t.DateMaj` ni
`dwh_dim_CanalDistribution_t.DateMaj`. Le KB Orange rend ce point critique : `CodeRegion`,
`CodeAd`, `LibDo`, `CodeDo`, `Regroupement` et `DateMaj` sont documentés à l'identique dans
plusieurs tables et dans deux datasets différents.
Dans le fichier du chunk corrigé, un candidat doit appartenir à la MÊME table ; dans les
autres fichiers (ex. relationship_*.md, dont les titres `##` sont des cas de jointure et non
des tables), le rapprochement se fait sur le seul nom de colonne — les faux positifs y sont
assumés et tranchés par la relecture humaine.

Aucun jugement automatique de conflit : les candidats sont présentés tels quels au tech
lead, qui décide et rédige lui-même chaque modification. propose_correction() est un
dry-run (aucune écriture), apply_edits() écrit.
"""

import re
from datetime import datetime, timezone

from qdrant_client import models

from config import QDRANT_COLLECTION as COLLECTION
from rag import qdrant_client, vector_store
from kb_meta import (
    KB_FOLDER, _read_raw, _normalize, _write_raw, _chunk_id, chunk_markdown,
    _all_docs, _clean_table, _chunk_key, _column_token,
)


# ---------- Prompts LLM ----------

# Reformule la correction du tech lead au style du chunk (remplacement complet du corps).
REFORMULATION_PROMPT = """Tu reformules une correction de documentation technique pour
qu'elle s'intègre stylistiquement à une Knowledge Base existante.

Chunk original de référence (structure et ton à respecter) :
---
{chunk_original}
---

Correction du tech lead (fait exact qui fait autorité — ne JAMAIS en changer le sens) :
---
{texte_brut}
---

Réécris la correction en respectant strictement la structure et le ton du chunk de
référence (même format, même registre technique direct, mêmes conventions de backticks).
Reformule uniquement la FORME. Si le fond de la correction contredit le chunk original,
le fond du tech lead gagne toujours — ne lisse jamais une contradiction factuelle voulue.

Le résultat doit être un remplacement COMPLET et AUTONOME du chunk original — conserve
tous les faits du chunk de référence qui ne sont pas contredits par la correction
(valeurs codées, avertissements, règles associées), et intègre le nouveau fait du tech
lead à l'endroit qui a le plus de sens. Ne produis jamais un texte qui ne contiendrait
QUE le nouveau fait en isolant du reste — le résultat remplace entièrement l'ancien
chunk en mémoire, il doit donc rester complet.

N'ajoute AUCun titre markdown (pas de ligne commençant par #). Ne renvoie que le corps."""

# Il n'y a plus de prompt de propagation : le tri « ce passage devient-il faux ? » n'est plus
# délégué au LLM (un appel par candidat, coûteux et faillible) mais à la relecture humaine.


# ---------- Localisation du corps d'un chunk dans le markdown ----------
# Lecture, écriture, découpage, _clean_table/_column_token/_chunk_key : voir kb_meta.py
# (partagé avec rag.py et kb_create.py).

def _find_header(text, level, header_text, start=0, end=None):
    """(line_start, body_start) du header exact au niveau donné dans [start, end), ou None.
    body_start = index juste après le saut de ligne de la ligne de titre."""
    end = len(text) if end is None else end
    pat = re.compile(r"^" + ("#" * level) + r"[ \t]+" + re.escape(header_text) + r"[ \t]*\r?$", re.M)
    m = pat.search(text, start, end)
    if not m:
        return None
    nl = text.find("\n", m.end())
    body_start = len(text) if nl == -1 else nl + 1
    return (m.start(), body_start)


def _next_boundary(text, from_idx, max_level, end):
    """Début de la prochaine ligne (dans [from_idx, end)) qui ferme le bloc courant :
    un header de niveau <= max_level, ou une règle horizontale '---'. Sinon `end`."""
    pat = re.compile(r"^(#{1," + str(max_level) + r"}[ \t]|---[ \t]*\r?$)", re.M)
    m = pat.search(text, from_idx, end)
    return m.start() if m else end


def locate_body_span(text, table_raw, section, colonne):
    """(body_start, body_end) du corps du chunk (table_raw, section, colonne) dans `text`
    (normalisé LF). Scopé hiérarchiquement (table > section > colonne) car un même nom de
    colonne peut exister sous plusieurs tables. Lève LookupError si introuvable."""
    n = len(text)

    if not table_raw:
        # les chunks sans table (préambule sous un H1) ne sont pas localisables de façon fiable
        raise LookupError("Chunk sans table : non localisable dans le markdown.")

    h = _find_header(text, 2, table_raw)
    if not h:
        raise LookupError(f"Header de table introuvable : {table_raw!r}")
    _tbl_line, tbl_body_start = h
    tbl_end = _next_boundary(text, tbl_body_start, 2, n)

    if section:
        h = _find_header(text, 3, section, tbl_body_start, tbl_end)
        if not h:
            raise LookupError(f"Header de section introuvable : {section!r}")
        _sec_line, sec_body_start = h
        sec_end = _next_boundary(text, sec_body_start, 3, tbl_end)
    else:
        sec_body_start, sec_end = tbl_body_start, tbl_end

    if colonne:
        h = _find_header(text, 4, colonne, sec_body_start, sec_end)
        if not h:
            raise LookupError(f"Header de colonne introuvable : {colonne!r}")
        _col_line, body_start = h
        body_end = _next_boundary(text, body_start, 4, sec_end)
    else:
        body_start = sec_body_start
        body_end = _next_boundary(text, sec_body_start, 4, sec_end)

    return body_start, body_end


def _replace_body(text, body_start, body_end, new_body):
    """Remplace le corps [body_start, body_end) par new_body en préservant les espaces
    de début/fin (sauts de ligne autour) de la région d'origine."""
    region = text[body_start:body_end]
    lead = region[: len(region) - len(region.lstrip())]
    trail = region[len(region.rstrip()):]
    return text[:body_start] + lead + new_body.strip() + trail + text[body_end:]


# ---------- Recherche du chunk primaire + des candidats ----------

def find_primary(table_clean, colonne):
    """Le Document du chunk (table_clean, colonne) exact, ou None."""
    for doc in _all_docs():
        meta = doc.metadata
        if _clean_table(meta.get("table", "")) == table_clean and meta.get("colonne", "").strip() == colonne:
            return doc
    return None


def find_chunk_by_column_name(table_clean, column_name):
    """Le Document de la colonne `column_name` sous `table_clean`, retrouvé par NOM DE
    COLONNE et non par texte de header.

    find_primary() exige le header brut exact (« `CodeActe` (STRING) »), dont dispose le
    panneau de correction parce qu'il vient du graphe. La validation d'une proposition, elle,
    part du diagramme et ne connaît que le nom nu de la colonne. Comparaison insensible à la
    casse : le KB écrit `idIris` en header et `IdIris` dans les jointures.

    Renvoie None si la colonne n'a pas de chunk propre — c'est le cas de toutes celles qui
    ne sont documentées que dans un tableau markdown (`dwh_fact_VenteAssuranceMateriel_t`).
    L'appelant doit traiter ce cas explicitement : il n'y a alors aucun corps de chunk où
    écrire, et inventer un emplacement produirait une structure que le garde-fou de
    _plan_file refuserait de toute façon."""
    cible = (column_name or "").strip().lower()
    if not cible:
        return None
    for doc in _all_docs():
        meta = doc.metadata
        if _clean_table(meta.get("table", "")) != table_clean:
            continue
        if _column_token(meta.get("colonne", "")).lower() == cible:
            return doc
    return None


def find_candidates(table_clean, colonne, primary_source, exclude_key):
    """Chunks susceptibles d'être impactés par la correction de (table_clean, colonne).
    Scan déterministe, sans LLM ni embedding, avec un périmètre différent selon le fichier :

    - fichier du chunk corrigé : le candidat doit appartenir à la MÊME table ET mentionner
      la colonne. C'est ce qui évite de remonter le `DateMaj` de `dwh_dim_GeoInsee_t` quand
      on corrige celui de `dwh_dim_GeoIris_t` — la colonne seule ne suffit pas à identifier
      une entité, et le KB Orange est plein d'homonymes inter-tables.
    - autres fichiers : rapprochement sur le seul nom de colonne. Leurs titres `##` sont des
      cas de jointure (« Cas 1 : source dispose du code IRIS complet »), pas des tables : un
      filtre par table n'y matcherait rien. Les faux positifs sont assumés et tranchés par la
      relecture humaine.

    Exclus dans tous les cas : le chunk primaire, les chunks sans table (préambule non
    localisable) et les chunks contenant du code/SQL (bruit pour le relecteur)."""
    token = _column_token(colonne)
    if not token:
        return []
    pat = re.compile(r"\b" + re.escape(token) + r"\b")
    results = []
    for doc in _all_docs():
        meta = doc.metadata
        if not meta.get("table"):
            continue  # préambule non localisable : jamais réécrit
        if _chunk_key(meta) == exclude_key:
            continue
        if "```" in doc.page_content:
            continue  # chunks de code/SQL de référence : jamais réécrits automatiquement
        if meta.get("source") == primary_source and _clean_table(meta.get("table", "")) != table_clean:
            continue  # même fichier mais autre table : homonymie de colonne, pas un candidat
        if pat.search(doc.page_content):
            results.append(doc)
    return results


def _edit_dict(doc, ancien, nouveau, is_primary):
    meta = doc.metadata
    table_raw = meta.get("table", "")
    section = meta.get("section", "")
    colonne = meta.get("colonne", "")
    detail = colonne or section
    table_clean = _clean_table(table_raw)
    return {
        "source": meta.get("source", ""),
        "table_raw": table_raw,
        "table": table_clean,
        "section": section,
        "colonne": colonne,
        "label": f"{table_clean} › {detail}" if detail else table_clean,
        "ancien_texte": ancien,
        "nouveau_texte": nouveau,
        "is_primary": is_primary,
    }


# ---------- Dry-run (aucune écriture) ----------

def propose_correction(table_clean, colonne, texte_brut, llm_text):
    """Prépare une correction SANS rien écrire. Renvoie :
        {"primary": <edit>, "candidates": [<edit>, ...]}
    - primary : le chunk ciblé, reformulé au style du KB (seul appel LLM de la fonction).
    - candidates : les chunks susceptibles d'être impactés (scan déterministe scopé sur
      (table, colonne)). Ils sont renvoyés TELS QUELS — `nouveau_texte` est identique à
      `ancien_texte` : aucune réécriture n'est proposée, c'est le tech lead qui juge en
      relecture si le passage doit changer et rédige lui-même la modification.
    `llm_text` : fonction (prompt:str) -> str (fournie par app.py).
    Lève LookupError si aucun chunk ne correspond."""
    primary = find_primary(table_clean, colonne)
    if primary is None:
        raise LookupError("Aucun chunk ne correspond à cette table/colonne dans la Knowledge Base.")

    reformule = llm_text(
        REFORMULATION_PROMPT.replace("{chunk_original}", primary.page_content).replace("{texte_brut}", texte_brut)
    ).strip()
    primary_edit = _edit_dict(primary, primary.page_content, reformule, is_primary=True)

    candidats = find_candidates(
        table_clean, colonne, primary.metadata.get("source", ""), _chunk_key(primary.metadata)
    )
    candidates = [
        _edit_dict(doc, doc.page_content, doc.page_content, is_primary=False)
        for doc in candidats
    ]

    return {"primary": primary_edit, "candidates": candidates}


# ---------- Écriture (markdown + Qdrant) ----------

def _plan_file(source, file_edits):
    """Prépare (sans aucun effet de bord) la réécriture d'un fichier. Renvoie un plan
    {path, out_text, to_delete, to_add_docs, applied} ou lève LookupError si un span est
    introuvable, si deux spans se chevauchent, ou si la réécriture changerait la structure
    des titres. Rien n'est écrit ici — la validation de TOUS les fichiers précède toute
    écriture, pour qu'une correction multi-fichiers ne puisse pas s'appliquer à moitié."""
    path = KB_FOLDER / source
    raw = _read_raw(path)
    uses_crlf = "\r\n" in raw
    old_text = _normalize(raw)

    # 1. localiser tous les spans sur le texte d'origine (offsets figés)
    located = []
    for e in file_edits:
        start, end = locate_body_span(old_text, e["table_raw"], e.get("section", ""), e.get("colonne", ""))
        located.append((start, end, e, old_text[start:end]))

    # 2. refus si chevauchement (sécurité)
    ordered = sorted(located, key=lambda t: t[0])
    for i in range(1, len(ordered)):
        if ordered[i][0] < ordered[i - 1][1]:
            raise LookupError(f"Spans de correction qui se chevauchent dans {source} — annulé.")

    # 3. appliquer du bas vers le haut (les offsets amont restent valides)
    new_text = old_text
    for start, end, e, _orig in sorted(located, key=lambda t: t[0], reverse=True):
        new_text = _replace_body(new_text, start, end, e["nouveau_texte"])

    # 4. la structure des chunks (titres) doit rester identique — sinon réécriture douteuse
    before = dict(chunk_markdown(source, old_text))
    after = dict(chunk_markdown(source, new_text))
    if {_chunk_key(d.metadata) for d in before.values()} != {_chunk_key(d.metadata) for d in after.values()}:
        raise LookupError(f"La réécriture modifierait la structure des chunks de {source} (titres) — annulé.")

    before_ids, after_ids = set(before), set(after)
    to_add_ids = list(after_ids - before_ids)
    to_add_docs = [after[i] for i in to_add_ids]

    # Marqueur de provenance dans le payload Qdrant : permet de filtrer/repérer un chunk
    # corrigé au moment de la recherche (défense en profondeur, cf. le préfixe
    # [CORRECTION VALIDÉE] posé par rag.format_docs). Métadonnée purement additive : elle
    # n'entre ni dans l'id uuid5 (calculé sur le seul page_content) ni dans _chunk_key,
    # donc ni le rapprochement d'ids ni le garde-fou de structure n'en dépendent.
    now_iso = datetime.now(timezone.utc).isoformat()
    for doc in to_add_docs:
        doc.metadata["corrected"] = True
        doc.metadata["corrected_at"] = now_iso

    return {
        "path": path,
        "out_text": new_text.replace("\n", "\r\n") if uses_crlf else new_text,
        "to_delete": list(before_ids - after_ids),
        "to_add_docs": to_add_docs,
        "to_add_ids": to_add_ids,
        "applied": [{**e, "ancien_texte": orig} for _s, _e, e, orig in located],
    }


def apply_edits(edits):
    """Écrit chaque édition dans son .md puis resynchronise Qdrant chunk par chunk (ne
    ré-embed / ne supprime QUE les chunks changés, avec le schéma d'id de ingest.py).
    `edits` : liste de dicts {source, table_raw, section, colonne, nouveau_texte}.
    Transactionnel au niveau localisation/structure : TOUS les fichiers sont planifiés et
    validés avant la moindre écriture. Renvoie la liste des éditions appliquées, chacune
    avec `ancien_texte` relu du fichier (valeur d'audit fiable). Lève LookupError si un
    chunk est introuvable ou si une réécriture modifierait la structure — sans rien écrire."""
    by_file = {}
    for e in edits:
        by_file.setdefault(e["source"], []).append(e)

    # Phase 1 — planifier et valider tous les fichiers (aucun effet de bord).
    plans = [_plan_file(source, file_edits) for source, file_edits in by_file.items()]

    # Phase 2 — commit : écrire les .md (source de vérité) puis Qdrant.
    applied = []
    for plan in plans:
        _write_raw(plan["path"], plan["out_text"])
        if plan["to_delete"]:
            qdrant_client.delete(
                collection_name=COLLECTION,
                points_selector=models.PointIdsList(points=plan["to_delete"]),
            )
        if plan["to_add_ids"]:
            vector_store.add_documents(plan["to_add_docs"], ids=plan["to_add_ids"])
        applied.extend(plan["applied"])

    return applied
