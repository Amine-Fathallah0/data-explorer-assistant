"""
kb_meta.py — lecture et découpage PURS de la Knowledge Base : aucune écriture, aucune
dépendance à Qdrant ou à la base SQLite.

Existe pour être importé des DEUX côtés sans créer de cycle :
  - kb_edit.py (écriture des corrections) importe `from rag import qdrant_client, ...`
  - rag.py (expansion de la récupération vers les chunks référencés par une relation,
    voir expand_with_relations) a besoin de la même logique de lecture/parsing.
Si rag.py important directement kb_edit.py, kb_edit -> rag -> kb_edit boucle. Ce module
n'importe ni l'un ni l'autre : les deux peuvent l'importer librement.

C'est aussi le point unique de vérité pour :
  - le PARCOURS du KB (récursif, un sous-dossier par dataset) — voir iter_kb_files ;
  - le DÉCOUPAGE et les ids uuid5 — voir chunk_markdown ;
  - la NORMALISATION des noms de table — voir clean_table.
ingest.py, rag.py, db.py, app.py, kb_edit.py, kb_create.py et kb_schema.py passent tous
par ici. C'est délibéré : ces trois logiques étaient auparavant recopiées à la main dans
plusieurs fichiers, et une copie qui diverge ne lève aucune erreur — elle fait juste rater
silencieusement le rapprochement des chunks (corrections invisibles, doublons Qdrant).
"""

import os
import re
import uuid
import warnings
from pathlib import Path

from dotenv import load_dotenv
from langchain_text_splitters import MarkdownHeaderTextSplitter

load_dotenv()

_DEFAULT_KB_FOLDER = Path(__file__).parent / "knowledge-base"


def _resolve_kb_folder():
    """Dossier racine de la Knowledge Base, surchargeable par INGEST_DATA_PATH.

    Un chemin RELATIF est résolu depuis le dossier de ce module, pas depuis le répertoire
    courant. C'est la différence qui compte : `INGEST_DATA_PATH=poc_rag_f1\\knowledge-base`
    ne fonctionnait que si l'on lançait le script depuis le dossier PARENT du projet, et
    donnait un KB vide (donc zéro chunk, sans erreur) depuis n'importe où ailleurs. Avec
    cette résolution, la valeur attendue est simplement `knowledge-base`, ou un chemin
    absolu si le KB vit en dehors du projet.

    Un dossier absent n'interrompt pas l'import — app.py et rag.py importent ce module au
    démarrage, et échouer ici rendrait l'application inutilisable au lieu de simplement
    dégradée — mais il émet un avertissement, parce que le symptôme sans avertissement (des
    réponses « aucun chunk pertinent », un graphe de schéma vide) ne pointe vers rien."""
    raw = (os.getenv("INGEST_DATA_PATH") or "").strip().strip('"').strip("'")
    if not raw:
        return _DEFAULT_KB_FOLDER
    path = Path(raw)
    if not path.is_absolute():
        path = Path(__file__).parent / path
    if not path.is_dir():
        warnings.warn(
            f"INGEST_DATA_PATH pointe vers {path} qui n'existe pas — repli sur "
            f"{_DEFAULT_KB_FOLDER}. Un chemin relatif est résolu depuis le dossier du "
            "projet (ex. « knowledge-base »), pas depuis le répertoire courant.",
            RuntimeWarning,
            stacklevel=2,
        )
        return _DEFAULT_KB_FOLDER
    return path


KB_FOLDER = _resolve_kb_folder()

# Headers de découpage. `##` porte l'entité (table, dimension, ou titre de section pour les
# fichiers de relations), `###` la sous-section, `####` la colonne.
HEADERS = [("##", "table"), ("###", "section"), ("####", "colonne")]
_splitter = MarkdownHeaderTextSplitter(headers_to_split_on=HEADERS)

# Un header `##` désigne une vraie entité du modèle de données quand il est préfixé ainsi.
# `Dimension :` est apparu avec les données Orange (dataset_bio_vente.md documente ses
# dimensions au même niveau que ses tables de faits) — sans ce préfixe dans la liste, les
# dimensions étaient parsées comme de simples sections et disparaissaient du graphe.
_ENTITY_PREFIXES = ("Table :", "Dimension :")
_BACKTICK_RE = re.compile(r"`([^`]+)`")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _read_raw(path):
    """Lit sans traduction de fin de ligne (newline='') pour préserver les octets."""
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


def _write_raw(path, text):
    """Écrit sans traduction de fin de ligne — préserve le style (CRLF/LF) choisi par
    l'appelant (voir _normalize). Partagé par kb_edit.py et kb_create.py : deux écrivains,
    une seule implémentation, pour éviter qu'une copie diverge silencieusement de l'autre."""
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def _normalize(text):
    """Ramène en LF, comme le faisait ingest.py (open en mode texte universel). Le
    découpage et les ids uuid5 doivent être calculés sur ce texte normalisé."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _chunk_id(source, page_content, table="", section="", colonne=""):
    """Id déterministe d'un chunk : uuid5 sur (fichier + chemin de titres + contenu).

    Le chemin de titres fait partie de la graine, alors que le contenu seul suffirait
    « en théorie ». Il ne suffit pas en pratique : `bio_refgeographie_Docs.md` définit
    `CodeIrisActu` et `CodeInseeActu` avec un corps rigoureusement identique au caractère
    près (même explication de l'actualisation INSEE). Sur la seule paire (source, contenu),
    les deux colonnes reçoivent le même uuid5, le second upsert écrase le premier, et une
    colonne documentée devient introuvable — sans aucune erreur, ni à l'ingestion ni à la
    recherche. Le compteur de points côté serveur est le seul endroit où ça se voyait.

    La propriété qui compte est préservée : l'id ne dépend que du contenu du KB, jamais
    d'un tirage aléatoire ni de l'ordre d'ingestion. Ré-ingérer un KB inchangé réécrit
    exactement les mêmes points, sans doublon."""
    graine = f"{source}::{table}::{section}::{colonne}::{page_content}"
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, graine))


# ---------- Parcours du KB ----------

def iter_kb_files():
    """[(source, path)] pour tous les .md du KB, RÉCURSIVEMENT.

    `source` est le chemin relatif à KB_FOLDER en style posix (`bio_vente/dataset_bio_vente.md`),
    jamais le seul nom de fichier : les datasets sont rangés un par sous-dossier et deux
    d'entre eux peuvent parfaitement contenir un `relationship_*.md` homonyme. Ce chemin
    relatif est la clé utilisée partout (metadata `source`, id uuid5, `KB_FOLDER / source`
    côté écriture) — les séparateurs `/` sont acceptés tels quels par pathlib sous Windows.

    Trié pour que l'ordre d'ingestion soit reproductible d'une exécution à l'autre."""
    return sorted(
        ((p.relative_to(KB_FOLDER).as_posix(), p) for p in KB_FOLDER.rglob("*.md")),
        key=lambda t: t[0],
    )


def dataset_of(source):
    """'bio_vente/dataset_bio_vente.md' -> 'bio_vente'. Chaîne vide pour un fichier posé à
    la racine du KB (pas de dataset déductible du chemin)."""
    parts = Path(source).parts
    return parts[0] if len(parts) > 1 else ""


# ---------- Normalisation des noms d'entité ----------

def is_entity_header(table_raw):
    """True si ce header `##` déclare une table ou une dimension du modèle de données, par
    opposition à un titre de section (`Vue d'ensemble du dataset`, `Cas 1 : Géographie…`,
    `Relations`)."""
    return (table_raw or "").strip().startswith(_ENTITY_PREFIXES)


def clean_table(raw):
    """Nom d'entité nu depuis le texte brut d'un header `##`.

        'Table : dwh_dim_GeoIris_t'                        -> 'dwh_dim_GeoIris_t'
        'Table : `dwh_fact_Vente_t` *(alias `vente`)*'     -> 'dwh_fact_Vente_t'
        'Dimension : `dwh_dim_CanalDistribution_t`'        -> 'dwh_dim_CanalDistribution_t'
        'Cas 1 : Géographie — `IdGeoIrisEdo`'              -> inchangé (pas une entité)

    La metadata `table` est le texte brut du titre, pas un identifiant propre — d'où ce
    nettoyage. Un header qui n'est pas une déclaration d'entité est renvoyé tel quel
    (seulement `strip()`), pour rester utilisable comme libellé d'affichage.

    Point unique de vérité : db.extract_entity, db.corrected_chunks_index, ingest.py et
    app.py appellent tous cette fonction. Ces clés (table nettoyée, colonne ou section)
    doivent être construites à l'identique partout, sinon le repérage des chunks corrigés
    échoue silencieusement."""
    raw = (raw or "").strip()
    if not is_entity_header(raw):
        return raw
    body = raw.split(":", 1)[1].strip()
    m = _BACKTICK_RE.search(body)
    if m:
        return m.group(1).strip()
    m = _IDENT_RE.match(body)
    return m.group(0) if m else body


# Conservé : nom historique utilisé par les imports existants (rag, kb_edit, kb_create,
# kb_schema). Même fonction, pas une copie.
_clean_table = clean_table


def table_aliases(table_raw):
    """Tous les noms sous lesquels une entité peut être désignée dans le texte du KB.

    'Table : `dwh_fact_Vente_t` *(alias `vente`)*' -> {'dwh_fact_Vente_t', 'vente'}

    Les documents Orange utilisent massivement l'alias court dans les exemples de jointure
    (`vente.IdActeKpi = …`) alors que le header porte le nom physique. Sans ces alias, un
    rapprochement fait sur le seul nom de table ne retrouve jamais la table de faits."""
    if not is_entity_header(table_raw):
        return set()
    body = (table_raw or "").split(":", 1)[1]
    names = {t.strip() for t in _BACKTICK_RE.findall(body) if _IDENT_RE.fullmatch(t.strip())}
    names.add(clean_table(table_raw))
    return {n for n in names if n}


def _column_token(colonne):
    """Extrait le nom de colonne nu d'un header type '`dob` (DATE)' -> 'dob'."""
    m = _BACKTICK_RE.search(colonne or "")
    if m:
        return m.group(1).strip()
    return (colonne or "").split()[0].strip("`") if colonne else ""


column_token = _column_token


# ---------- Découpage ----------

def breadcrumb(meta):
    """'bio_vente > dwh_fact_Vente_t > Colonnes > `CodeActe` (STRING)'.

    Le fil d'Ariane sert deux choses : il est injecté en en-tête de chaque bloc de contexte
    envoyé au LLM (rag._format_doc), et il est affiché comme libellé de source dans l'UI.
    Le dataset y figure explicitement parce qu'il n'apparaît nulle part dans les headers
    découpés — il n'est porté que par le `#` de titre de fichier, que le splitter ignore,
    et par le nom du sous-dossier."""
    parts = [
        meta.get("dataset", ""),
        clean_table(meta.get("table", "")),
        meta.get("section", ""),
        meta.get("colonne", ""),
    ]
    return " > ".join(p for p in parts if p)


def chunk_markdown(source, normalized_text):
    """Découpe un markdown (déjà normalisé LF) : mêmes headers, même metadata, même id
    partout dans l'application. Renvoie [(id, Document)].

    Métadonnées posées : `source` (chemin relatif au KB), `dataset` (sous-dossier),
    `breadcrumb` (fil d'Ariane lisible). L'id est content-adressé
    (uuid5(source::page_content)) : ré-ingérer un KB inchangé réécrit exactement les mêmes
    points, sans doublon."""
    out = []
    dataset = dataset_of(source)
    for doc in _splitter.split_text(normalized_text):
        meta = doc.metadata
        meta["source"] = source
        meta["dataset"] = dataset
        meta["breadcrumb"] = breadcrumb(meta)
        cid = _chunk_id(
            source, doc.page_content,
            meta.get("table", ""), meta.get("section", ""), meta.get("colonne", ""),
        )
        out.append((cid, doc))
    return out


def _all_docs():
    """Tous les chunks de tous les .md du KB (Documents avec metadata). Relit les fichiers
    à chaque appel — pour un KB de quelques fichiers, le coût est négligeable (pas besoin
    de cache), et ça garantit qu'on ne lit jamais un état périmé après une correction."""
    docs = []
    for source, path in iter_kb_files():
        text = _normalize(_read_raw(path))
        for _cid, doc in chunk_markdown(source, text):
            docs.append(doc)
    return docs


def _chunk_key(meta):
    return (
        meta.get("source", ""),
        meta.get("table", ""),
        meta.get("section", ""),
        meta.get("colonne", ""),
    )


def find_chunk(source, table_raw, section, colonne):
    """Le Document dont la clé (source, table brute, section, colonne) correspond
    exactement à celle passée, ou None. Même primitive de correspondance que
    kb_edit._chunk_key / rag.expand_with_relations — sert à relire le contenu
    ACTUEL d'un chunk déjà identifié (ex : panneau de visualisation de source),
    sans dépendre d'un id Qdrant qui peut changer après une correction."""
    key = (source or "", table_raw or "", section or "", colonne or "")
    for doc in _all_docs():
        if _chunk_key(doc.metadata) == key:
            return doc
    return None
