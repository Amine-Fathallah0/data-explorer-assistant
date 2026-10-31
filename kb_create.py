"""
kb_create.py — création de nouvelles tables dans la Knowledge Base.

Contrairement à kb_edit.py (qui réécrit le corps d'un header EXISTANT et refuse tout
changement de structure), ce module INSÈRE un nouveau bloc `## Table : x` complet.
L'entrée est structurée (spec dict : nom, grain, colonnes, règles), jamais du markdown
brut fourni par l'utilisateur — le rendu (render_table_markdown) est donc déterministe et
garantit que le résultat respecte le gabarit de la KB (voir knowledge-base/dataset_f1_ergast.md),
sans dépendre d'un LLM ni d'une relecture de style.

Les relations sont exprimées de la même façon que partout ailleurs dans la KB : une puce
"- Jointure : `thistable.thiscol = othertable.othercol`" dans le corps de la colonne
concernée — un format que kb_schema reconnaît pour construire le graphe de /api/schema.
Une clé étrangère ne peut cibler qu'une table/colonne déjà publiée (validate_table_spec),
jamais une autre table en brouillon ni du texte libre. La cible peut appartenir à un autre
dataset : c'est le cas normal ici (`dwh_fact_Vente_t` référence
`bio_refgeographie.dwh_dim_GeoIris_t`).

CIBLE D'ÉCRITURE — la Knowledge Base est désormais multi-datasets (un sous-dossier par
dataset). Il n'y a donc plus un fichier unique où écrire : `spec["dataset"]` désigne le
dataset d'accueil, et resolve_dataset_source() le traduit en chemin de fichier. Le client
n'envoie JAMAIS un nom de fichier — seulement un nom de dataset, validé contre la liste
réellement présente sur disque (voir list_dataset_sources), pour ne pas ouvrir de surface
d'injection de chemin.

Écriture en deux phases comme kb_edit.apply_edits : tout est validé (validate_table_spec
puis _plan_creation) AVANT la moindre écriture disque/Qdrant.
"""

import re
from datetime import datetime, timezone

from rag import vector_store
from kb_meta import (
    KB_FOLDER, _read_raw, _normalize, _write_raw, chunk_markdown, _clean_table,
    dataset_of, is_entity_header, iter_kb_files,
)
from kb_schema import build_schema_graph

_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_H2_RE = re.compile(r"^##[ \t]+(.+?)[ \t]*$", re.M)


def list_dataset_sources():
    """{dataset: source} — pour chaque dataset, le fichier .md qui déclare ses tables.

    Un dataset contient plusieurs fichiers (`dataset_*.md`, `relationship_*.md`) mais un
    seul déclare des entités via `## Table :` / `## Dimension :` : c'est celui-là qu'une
    création doit étendre, jamais le fichier de relations. Détecté par lecture du contenu
    plutôt que par convention de nommage — le KB n'en a pas de stricte."""
    sources = {}
    for source, path in iter_kb_files():
        dataset = dataset_of(source)
        if not dataset or dataset in sources:
            continue
        text = _normalize(_read_raw(path))
        if any(is_entity_header(m.group(1)) for m in _H2_RE.finditer(text)):
            sources[dataset] = source
    return sources


def resolve_dataset_source(spec):
    """Chemin relatif du fichier à étendre pour `spec`, ou None si le dataset demandé
    n'existe pas. Si un seul dataset est présent dans le KB, il est pris par défaut —
    ça garde l'appel simple quand il n'y a pas d'ambiguïté."""
    sources = list_dataset_sources()
    dataset = (spec.get("dataset") or "").strip()
    if not dataset and len(sources) == 1:
        return next(iter(sources.values()))
    return sources.get(dataset)


# ---------- Validation ----------

def validate_table_spec(spec, schema_graph=None):
    """Renvoie la liste de TOUS les problèmes de `spec` (liste vide = valide) — pas la
    première erreur seule, pour qu'un formulaire visuel puisse tout afficher d'un coup.
    `schema_graph` : passer le graphe déjà chargé par l'appelant pour éviter une relecture
    redondante de la KB ; sinon recalculé ici via build_schema_graph()."""
    errors = []
    graph = schema_graph if schema_graph is not None else build_schema_graph()
    existing_tables = {t["name"]: t for t in graph["tables"]}
    existing_lower = {t.lower() for t in existing_tables}

    # Le dataset d'accueil est validé contre les sous-dossiers réellement présents : c'est
    # ce contrôle qui garantit qu'aucun chemin arbitraire ne remonte du client jusqu'à
    # _write_raw.
    sources = list_dataset_sources()
    dataset = (spec.get("dataset") or "").strip()
    if not dataset and len(sources) != 1:
        errors.append(
            "Le dataset d'accueil est requis (choix possibles : " + ", ".join(sorted(sources)) + ")."
        )
    elif dataset and dataset not in sources:
        errors.append(
            f"Dataset inconnu « {dataset} » (choix possibles : " + ", ".join(sorted(sources)) + ")."
        )

    name = (spec.get("name") or "").strip()
    if not name:
        errors.append("Le nom de la table est requis.")
    elif not _NAME_RE.match(name):
        errors.append(
            "Le nom de la table doit être un identifiant valide (lettres, chiffres, "
            "underscore, ne commence pas par un chiffre)."
        )
    elif name.lower() in existing_lower:
        errors.append(f"Une table nommée « {name} » existe déjà.")

    grain = (spec.get("grain") or "").strip()
    if not grain:
        errors.append("Le grain (logique métier au niveau de la table) est requis.")

    columns = spec.get("columns") or []
    if not columns:
        errors.append("Au moins une colonne est requise.")

    seen_col_names = set()
    for i, col in enumerate(columns):
        label = f"Colonne {i + 1}"
        col_name = (col.get("name") or "").strip()
        if not col_name:
            errors.append(f"{label} : le nom est requis.")
        elif not _NAME_RE.match(col_name):
            errors.append(f"{label} (« {col_name} ») : nom de colonne invalide.")
        elif col_name in seen_col_names:
            errors.append(f"{label} : nom de colonne « {col_name} » dupliqué.")
        else:
            seen_col_names.add(col_name)

        col_type = (col.get("type") or "").strip()
        if not col_type:
            errors.append(f"{label} : le type est requis.")
        elif "`" in col_type or ")" in col_type:
            errors.append(f"{label} : le type ne peut contenir ni « ` » ni « ) ».")

        if not (col.get("description") or "").strip():
            errors.append(f"{label} : la description est requise.")

        fk = col.get("fk")
        if fk:
            fk_table = (fk.get("table") or "").strip()
            fk_column = (fk.get("column") or "").strip()
            if fk_table == name:
                errors.append(f"{label} : une colonne ne peut pas référencer sa propre table.")
            elif fk_table not in existing_tables:
                errors.append(f"{label} : la table cible « {fk_table} » n'existe pas.")
            elif not any(c["name"] == fk_column for c in existing_tables[fk_table]["columns"]):
                errors.append(f"{label} : la colonne cible « {fk_table}.{fk_column} » n'existe pas.")

    for i, rule in enumerate(spec.get("rules") or []):
        title = (rule.get("title") or "").strip()
        text = (rule.get("text") or "").strip()
        if bool(title) != bool(text):
            errors.append(f"Règle {i + 1} : titre et texte doivent être renseignés ensemble (ou vides ensemble).")

    return errors


# ---------- Rendu markdown (pur, sans I/O) ----------

def _render_column_block(table_name, col):
    name = col["name"].strip()
    col_type = col["type"].strip()
    description = col["description"].strip()
    lines = [f"#### `{name}` ({col_type})", description]

    examples = [e.strip() for e in (col.get("examples") or []) if e and e.strip()]
    if examples:
        lines.append("- Exemples : " + ", ".join(f"`{e}`" for e in examples))

    fmt = (col.get("format") or "").strip()
    if fmt:
        lines.append(f"- Format : {fmt}")

    valid_range = (col.get("valid_range") or "").strip()
    if valid_range:
        lines.append(f"- Plage valide : {valid_range}")

    warning = (col.get("warning") or "").strip()
    if warning:
        lines.append(f"- ⚠️ {warning}")

    fk = col.get("fk")
    if fk:
        fk_table = fk["table"].strip()
        fk_column = fk["column"].strip()
        lines.append(f"- Jointure : `{table_name}.{name} = {fk_table}.{fk_column}`")

    return "\n".join(lines)


def render_table_markdown(spec):
    """Rend le bloc `## Table : ...` complet dans le gabarit exact de la KB. Fonction
    pure (aucune écriture) — appeler validate_table_spec(spec) avant, cette fonction ne
    revalide rien. Se termine par '---\\n' (séparateur entre tables, comme dans le reste
    du fichier)."""
    name = spec["name"].strip()
    grain = spec["grain"].strip()
    columns = spec["columns"]
    rules = [
        r for r in (spec.get("rules") or [])
        if (r.get("title") or "").strip() and (r.get("text") or "").strip()
    ]

    business_lines = [f"- **Grain :** {grain}"]
    for i, rule in enumerate(rules):
        business_lines.append(f"- **Règle {i + 1} — {rule['title'].strip()} :** {rule['text'].strip()}")

    parts = [
        f"## Table : {name}", "",
        "### Colonnes", "",
        "\n\n".join(_render_column_block(name, col) for col in columns), "",
        "### Logique métier au niveau de la table", "",
        "\n".join(business_lines), "",
        "---",
    ]
    return "\n".join(parts) + "\n"


# ---------- Localisation du point d'insertion ----------

def locate_insertion_point(text):
    """Offset où insérer un nouveau bloc `## Table : ...` dans `text` (déjà normalisé LF) :
    juste après le DERNIER bloc d'entité (`## Table :` ou `## Dimension :`), c'est-à-dire au
    premier header `##` qui le suit, ou en fin de fichier s'il n'y en a pas.

    « Après la dernière entité » et non « avant la première section non-entité » : les
    documents Orange intercalent des sections de prose entre leurs tables (`## Géographie —
    voir dimension externe` sépare une dimension de la table de faits suivante). L'ancienne
    règle y aurait inséré la nouvelle table au milieu du fichier, avant des tables
    existantes. Lève LookupError si le fichier ne déclare aucune entité."""
    last_entity_end = None
    for m in _H2_RE.finditer(text):
        if is_entity_header(m.group(1).strip()):
            last_entity_end = None  # une entité de plus : l'ancien point de fin ne vaut plus
        elif last_entity_end is None:
            last_entity_end = m.start()
    if not any(is_entity_header(m.group(1).strip()) for m in _H2_RE.finditer(text)):
        raise LookupError("Aucun header '## Table :' ou '## Dimension :' trouvé — insertion impossible.")
    return last_entity_end if last_entity_end is not None else len(text)


# ---------- Écriture (markdown + Qdrant) ----------

def _plan_creation(source, spec):
    """Prépare (sans effet de bord) l'insertion d'une nouvelle table dans `source`.
    Renvoie {path, out_text, to_add_docs, to_add_ids, table_markdown} ou lève LookupError
    si le nom existe déjà (revérifié directement sur ce fichier — garde-fou TOCTOU contre
    une publication concurrente depuis la dernière validation) ou si l'insertion produirait
    un résultat incohérent.

    Contrairement à kb_edit._plan_file, qui exige que l'ENSEMBLE des clés de chunk reste
    identique avant/après (une correction ne peut jamais ajouter/retirer un header), une
    création DOIT en ajouter. L'invariant vérifié ici est donc plus faible mais toujours
    sûr : tout id de chunk présent avant doit rester présent après. Les ids sont
    content-adressés (uuid5(source, page_content), voir kb_meta._chunk_id) — un id inchangé
    garantit donc un contenu inchangé, ce qui suffit à prouver qu'aucun chunk existant n'a
    été altéré, sans interdire l'apparition de nouvelles clés."""
    path = KB_FOLDER / source
    raw = _read_raw(path)
    uses_crlf = "\r\n" in raw
    old_text = _normalize(raw)
    before = dict(chunk_markdown(source, old_text))

    name_clean = spec["name"].strip()
    for _cid, doc in before.items():
        if _clean_table(doc.metadata.get("table", "")) == name_clean:
            raise LookupError(f"Une table nommée « {name_clean} » existe déjà dans {source}.")

    insert_at = locate_insertion_point(old_text)
    table_md = render_table_markdown(spec)

    prefix = old_text[:insert_at]
    if not prefix.endswith("\n\n"):
        prefix = prefix.rstrip("\n") + "\n\n"
    new_text = prefix + table_md.rstrip("\n") + "\n\n" + old_text[insert_at:]

    after = dict(chunk_markdown(source, new_text))
    if not set(before) <= set(after):
        raise LookupError(f"L'insertion altérerait des chunks existants de {source} — annulé.")

    to_add_ids = [cid for cid in after if cid not in before]
    to_add_docs = [after[cid] for cid in to_add_ids]
    if not to_add_docs:
        raise LookupError("L'insertion n'a produit aucun nouveau chunk — gabarit invalide.")

    now_iso = datetime.now(timezone.utc).isoformat()
    for doc in to_add_docs:
        doc.metadata["created"] = True
        doc.metadata["created_at"] = now_iso

    return {
        "path": path,
        "out_text": new_text.replace("\n", "\r\n") if uses_crlf else new_text,
        "to_add_docs": to_add_docs,
        "to_add_ids": to_add_ids,
        "table_markdown": table_md,
    }


def preview_table(spec):
    """Dry-run : valide `spec` et renvoie {table_markdown, chunk_count, dataset, source} —
    n'écrit ni le .md ni Qdrant. Contrairement à kb_edit.propose_correction, aucun appel
    LLM : la reformulation « au style du KB » n'a pas lieu d'être puisque l'entrée est déjà
    structurée (le rendu est déterministe). Lève ValueError (messages joints par '; ')
    si `spec` est invalide."""
    errors = validate_table_spec(spec)
    if errors:
        raise ValueError("; ".join(errors))
    source = resolve_dataset_source(spec)
    table_md = render_table_markdown(spec)
    return {
        "table_markdown": table_md,
        # Le découpage dépend de `source` (il en dérive dataset et breadcrumb), donc on
        # compte les chunks sur le fichier réellement ciblé, pas sur un nom générique.
        "chunk_count": len(chunk_markdown(source, table_md)),
        "dataset": dataset_of(source),
        "source": source,
    }


def create_table(spec):
    """Valide, écrit le nouveau bloc dans le fichier du dataset ciblé, puis ajoute les
    chunks correspondants dans Qdrant (uniquement des ajouts — une création ne supprime
    jamais de point existant, contrairement à une correction). Lève ValueError si `spec`
    est invalide, LookupError si l'insertion est refusée par sécurité (nom déjà pris,
    structure incohérente). Ces deux erreurs sont levées AVANT toute écriture."""
    errors = validate_table_spec(spec)
    if errors:
        raise ValueError("; ".join(errors))
    source = resolve_dataset_source(spec)
    plan = _plan_creation(source, spec)

    _write_raw(plan["path"], plan["out_text"])
    vector_store.add_documents(plan["to_add_docs"], ids=plan["to_add_ids"])

    return {
        "table_markdown": plan["table_markdown"],
        "chunks_added": len(plan["to_add_ids"]),
        "table_name": spec["name"].strip(),
        "dataset": dataset_of(source),
        "source": source,
    }
