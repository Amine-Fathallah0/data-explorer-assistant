"""
kb_schema.py — dérive un graphe {tables, colonnes, relations} depuis la Knowledge Base
markdown, pour l'API /api/schema (vue graphique du modèle de données, façon graphe Obsidian).

Réutilise le découpage de kb_meta.py (mêmes headers `## table` / `#### colonne`) plutôt que
de reparser le markdown à sa façon : la doc reste la seule source de vérité, aucune liste de
tables/colonnes/relations à maintenir séparément.

Les relations sont extraites des écritures qualifiées `table.colonne = table.colonne`
trouvées dans le corps des chunks — qu'elles soient en prose entre backticks
(« Clé de jointure : `vente.IdActeKpi = dwh_dim_ActeKpiHierarchise_t.IdActeCommercial` »)
ou dans un bloc SQL (`ON vente.IdGeoIrisEdo = geo.IdIris`). Le POC F1 n'acceptait que la
première forme, entièrement entourée de backticks ; la documentation Orange écrit ses
jointures en SQL avec des alias, donc les deux formes sont désormais reconnues et les alias
résolus (voir _resolve).
"""

import re

from kb_meta import (
    _all_docs, _clean_table, _column_token, dataset_of, is_entity_header, table_aliases,
)

_COLUMN_TYPE_RE = re.compile(r"`[^`]+`\s*\(([^)]+)\)")
# Backticks optionnels autour de chaque membre : couvre `a.b = c.d`, a.b = c.d, et les
# formes mixtes rencontrées dans le SQL du KB.
_JOIN_RE = re.compile(
    r"`?\b([A-Za-z_]\w*)\.(\w+)\b`?\s*=\s*`?\b([A-Za-z_]\w*)\.(\w+)\b`?"
)
# `bio_refgeographie.dwh_dim_GeoIris_t` geo  /  `...` AS GeoIris1  -> alias local -> table
_SQL_ALIAS_RE = re.compile(
    r"`[^`]*?(\w+)`\s+(?:AS\s+)?(\w+)\b", re.IGNORECASE
)


# --- Colonnes documentées SANS header `####` ---
# Les deux datasets Orange n'écrivent pas leurs colonnes de la même façon. `dwh_dim_GeoIris_t`
# et `dwh_fact_Vente_t` donnent un `#### `col` (TYPE)` par colonne — le splitter en fait un
# chunk et le graphe les voit. Mais les dimensions et `dwh_fact_VenteAssuranceMateriel_t` les
# listent en tableau markdown, en puces, ou en une seule ligne de prose ; sans les motifs
# ci-dessous, ces entités apparaissaient dans le graphe avec zéro colonne.
# Lecture seule : rien n'est réécrit dans le KB, on se contente de comprendre les deux styles.
_MD_ROW_RE = re.compile(r"^\|\s*`(\w+)`\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|", re.M)
_BULLET_RE = re.compile(r"^-\s*`(\w+)`\s*(?:\(([^)]*)\))?\s*[—:-]?\s*(.*)$", re.M)
_INLINE_RE = re.compile(r"`(\w+)`\s*\(([^)]*)\)")


def _column_type(colonne_header):
    m = _COLUMN_TYPE_RE.search(colonne_header or "")
    return m.group(1).strip() if m else ""


def _first_line(text):
    return (text or "").strip().split("\n")[0].strip()


def _implicit_columns(text):
    """[(nom, type, description)] pour un chunk de section « Colonnes… » qui ne porte aucun
    header `####`. Essaie les trois écritures présentes dans le KB, dans l'ordre du plus
    structuré au moins structuré, et s'arrête à la première qui donne un résultat — pour ne
    pas ramasser en puces les lignes de commentaire d'un tableau déjà correctement lu."""
    rows = [(n, t, d) for n, t, d in _MD_ROW_RE.findall(text) if n.lower() != "colonne"]
    if rows:
        return rows
    bullets = _BULLET_RE.findall(text)
    if bullets:
        return bullets
    return [(n, t, "") for n, t in _INLINE_RE.findall(text)]


def _build_alias_map(docs):
    """{alias_minuscule: nom_de_table} pour tous les noms et alias déclarés en header
    (`Table : \\`dwh_fact_Vente_t\\` *(alias \\`vente\\`)*` donne les deux). Minuscules :
    le KB écrit `flagirisee` dans le SQL et `FlagIrisee` dans le header."""
    alias_map = {}
    for doc in docs:
        table_raw = doc.metadata.get("table", "")
        if not is_entity_header(table_raw):
            continue
        table = _clean_table(table_raw)
        for alias in table_aliases(table_raw):
            alias_map.setdefault(alias.lower(), table)
    return alias_map


def _local_aliases(text):
    """Alias SQL déclarés dans CE chunk : {alias_minuscule: table}. Extrait de
    `` `bio_refgeographie.dwh_dim_GeoIris_t` geo `` et de `` `...` AS GeoIris1 ``.
    Sans ça, `ON vente.IdGeoIrisEdo = geo.IdIris` produirait une relation vers une table
    fantôme nommée « geo »."""
    return {
        alias.lower(): table
        for table, alias in _SQL_ALIAS_RE.findall(text)
        if alias.lower() not in ("as", "on", "and", "join", "left", "from")
    }


def _resolve(token, alias_map, local, known_tables):
    """Nom de table réel derrière `token`, ou None si le token ne désigne aucune table
    DOCUMENTÉE (préfixe de dataset `bio_refgeographie.`, variable `${REFGEOGRAPHIE}`, alias
    jamais déclaré, ou table tierce citée dans un exemple mais absente du KB — ex.
    `raw_fact_temp_MesureFinanciere_t`, qui n'appartient à aucun des datasets documentés).

    Renvoyer None est le comportement voulu : mieux vaut omettre une arête du graphe que
    d'en inventer une vers un nœud fantôme. Le contrôle d'appartenance à `known_tables`
    s'applique aux trois modes de résolution, pas seulement au dernier."""
    key = (token or "").lower()
    resolved = local.get(key) or alias_map.get(key) or token
    return resolved if resolved in known_tables else None


# ---------- Inventaire des clés étrangères (source de vérité déclarative) ----------
#
# L'inférence par motif `table.colonne = table.colonne` dans la prose ne voit une jointure
# que là où quelqu'un a pris la peine de l'écrire. Elle ratait donc toutes les FK de
# `dwh_fact_VenteAssuranceMateriel_t` dont l'exemple SQL n'existe que pour `vente`, alors
# que la colonne est présente sur les deux tables — et surtout, elle est incapable de
# représenter une FK dont la cible est INCONNUE, puisqu'il n'y a aucune égalité à matcher.
#
# La section `## Inventaire des clés étrangères` déclare chaque FK avec son statut exact.
# On la parse directement : ce qui n'y est pas listé n'est pas dessiné, ce qui y est listé
# l'est avec le bon niveau de certitude.
_INVENTORY_HEADER = "Inventaire des clés étrangères"
_ROW_RE = re.compile(r"^\|(.+)\|\s*$", re.M)
_VIA_RE = re.compile(r"\(\s*via\s+`?(\w+)`?\s*\)", re.I)
_TICKED_RE = re.compile(r"`([^`]+)`")

# Trois statuts, volontairement distincts (voir docstring de parse_fk_inventory).
STATUS_DOCUMENTED = "documented"          # ✅ cible connue et nommée
STATUS_DISTINCT_UNKNOWN = "distinct_unknown"  # ⚠️ cible prouvée distincte mais jamais nommée
STATUS_UNKNOWN = "unknown"                # ❌ rien d'établi


def _cell_ident(cell):
    """Identifiant nu d'une cellule : premier segment entre backticks, sans préfixe de
    dataset (`bio_refgeographie.dwh_dim_GeoIris_t` -> `dwh_dim_GeoIris_t`)."""
    m = _TICKED_RE.search(cell or "")
    token = (m.group(1) if m else (cell or "")).strip()
    return token.split(".")[-1].strip()


def _classify(statut):
    if "✅" in statut:
        return STATUS_DOCUMENTED
    if "⚠️" in statut or "⚠" in statut:
        return STATUS_DISTINCT_UNKNOWN
    return STATUS_UNKNOWN


def parse_fk_inventory():
    """Lit la section `## Inventaire des clés étrangères` et renvoie une liste de dicts :
        {source, source_column, target, target_column, status, note}
    `target` vaut None dès que la cible n'est pas nommée (statuts ⚠️ et ❌).

    Les trois statuts ne sont PAS interchangeables et ne doivent jamais être fusionnés à
    l'affichage : ✅ affirme une cible précise, ⚠️ affirme qu'une cible distincte existe
    sans pouvoir la nommer (c'est une information positive, chèrement acquise par des tests
    d'orphelins), ❌ n'affirme rien du tout. Confondre ⚠️ et ❌ effacerait précisément le
    travail d'investigation que cette documentation enregistre."""
    rows = []
    for doc in _all_docs():
        if _INVENTORY_HEADER not in (doc.metadata.get("table") or ""):
            continue
        for m in _ROW_RE.finditer(doc.page_content):
            cells = [c.strip() for c in m.group(1).split("|")]
            if len(cells) < 4:
                continue
            src, col, cible, statut = cells[0], cells[1], cells[2], cells[3]
            source, source_column = _cell_ident(src), _cell_ident(col)
            # En-tête du tableau et ligne de séparation `|---|---|`
            if not source or not source_column or set(source) <= set("-: "):
                continue
            if source_column.lower() in ("colonne fk", "colonne"):
                continue

            status = _classify(statut)
            target = target_column = None
            via_explicite = False
            if status == STATUS_DOCUMENTED:
                target = _cell_ident(cible)
                via = _VIA_RE.search(cible)
                via_explicite = bool(via)
                # Sans mention « via », on PROPOSE le nom de la colonne source ; c'est à
                # l'appelant de le valider contre les colonnes réelles de la cible avant
                # de l'afficher (voir build_schema_graph).
                target_column = via.group(1) if via else source_column
            rows.append({
                "source": source, "source_column": source_column,
                "target": target, "target_column": target_column,
                "via_explicite": via_explicite,
                "status": status, "note": statut,
            })
    return rows


def build_schema_graph():
    """Renvoie {"tables": [{"name", "dataset", "columns": [{"name","type","description"}]}],
    "relations": [{"source","source_column","target","target_column"}]}.

    Une table existe dès qu'un chunk `## Table : x` ou `## Dimension : x` est rencontré ;
    ses colonnes sont les chunks `#### \\`col\\` (TYPE)` sous ce header. Le sens FK -> PK
    d'une relation vient de l'ordre d'écriture de l'égalité (membre gauche = source), ce qui
    correspond à la convention du KB (`vente.IdActeKpi = dim.IdActeCommercial`)."""
    docs = _all_docs()
    alias_map = _build_alias_map(docs)

    tables = {}
    for doc in docs:
        table_raw = doc.metadata.get("table")
        # Les `##` des fichiers relationship_*.md sont des titres de section (« Cas 1 :
        # source dispose du code IRIS complet », « Contexte »), pas des tables du modèle :
        # seuls les headers `Table :` / `Dimension :` déclarent une entité. Le scan des
        # relations, lui, s'applique quand même à ces chunks — c'est précisément là que les
        # jointures sont documentées.
        if not is_entity_header(table_raw):
            continue
        table = _clean_table(table_raw)
        if table not in tables:
            tables[table] = {
                "name": table,
                "dataset": dataset_of(doc.metadata.get("source", "")),
                "columns": [],
            }

        colonne_header = doc.metadata.get("colonne")
        if colonne_header:
            col_name = _column_token(colonne_header)
            if col_name and not any(c["name"] == col_name for c in tables[table]["columns"]):
                tables[table]["columns"].append({
                    "name": col_name,
                    "type": _column_type(colonne_header),
                    "description": _first_line(doc.page_content),
                    # Texte brut du header `#### ` (ex : "`CodeActe` (STRING)") — c'est
                    # cette valeur exacte, pas `name` seul, qu'attend /api/corrections/prepare
                    # en `colonne` (voir kb_edit.find_primary). Le panneau d'édition du schéma
                    # (feature colonne éditable) en a besoin pour rouvrir la bonne correction.
                    "colonne_raw": colonne_header,
                })
        elif "colonne" in (doc.metadata.get("section") or "").lower():
            # Section « Colonnes » sans sous-header : colonnes listées en tableau/puces/prose.
            # `colonne_raw` reste vide — ces colonnes n'ont pas de chunk propre, donc pas de
            # cible de correction adressable ; l'UI doit masquer l'édition dans ce cas.
            for name, col_type, description in _implicit_columns(doc.page_content):
                if any(c["name"] == name for c in tables[table]["columns"]):
                    continue
                tables[table]["columns"].append({
                    "name": name,
                    "type": (col_type or "").strip(),
                    "description": (description or "").strip(),
                    "colonne_raw": "",
                })

    # --- Relations : l'inventaire fait autorité ---
    inventory = parse_fk_inventory()
    tables_inventoriees = {r["source"] for r in inventory}

    relations = []
    unresolved = []   # FK sans cible nommée : ⚠️ (cible distincte prouvée) et ❌ (rien)
    seen_relations = set()

    for r in inventory:
        if r["status"] != STATUS_DOCUMENTED:
            # Pas de cible à dessiner. Le rendu distingue les deux cas (voir schema.js) :
            # ⚠️ obtient un nœud fantôme, ❌ n'obtient rien qu'un marqueur sur la colonne.
            unresolved.append({
                "table": r["source"], "column": r["source_column"],
                "status": r["status"], "note": r["note"],
            })
            continue

        cible = r["target"]
        # Une cible peut ne pas être documentée par un header `## Table :` (cas de
        # `dwh_dim_TrancheTarifNetCommerce_t`, connue seulement par l'inventaire). On la
        # crée en nœud vide plutôt que de jeter l'arête : la jointure, elle, est confirmée.
        if cible not in tables:
            tables[cible] = {"name": cible, "dataset": "", "columns": [], "documented": False}

        # Résolution de la colonne cible, en deux régimes :
        #  - « (via `X`) » explicite dans l'inventaire : on fait confiance au document, en
        #    récupérant simplement la casse réelle de la colonne si on la connaît (le KB
        #    écrit `idIris` dans le header et `IdIris` dans les exemples de jointure) ;
        #  - pas de « via » : on n'accepte le nom de la colonne source que s'il existe
        #    vraiment sur la cible (cas `IdCanalDistribution`, même nom des deux côtés).
        #    Sinon on n'invente rien — `IdTranchePrixUtilise` pointe vers une dimension dont
        #    la clé s'appelle `IdTrancheTarifNet`, la reprendre telle quelle afficherait une
        #    colonne qui n'existe pas.
        cols_cible = {c["name"].lower(): c["name"] for c in tables[cible]["columns"]}
        col_cible = r["target_column"]
        if col_cible:
            reel = cols_cible.get(col_cible.lower())
            if reel:
                col_cible = reel
            elif not r.get("via_explicite"):
                col_cible = None

        key = (r["source"], r["source_column"], cible, col_cible)
        if key in seen_relations:
            continue
        seen_relations.add(key)
        relations.append({
            "source": r["source"], "source_column": r["source_column"],
            "target": cible, "target_column": col_cible,
            "status": STATUS_DOCUMENTED, "note": r["note"],
        })

    # --- Repli : inférence en texte libre, UNIQUEMENT pour les tables hors inventaire ---
    # Les datasets sans section d'inventaire (aujourd'hui bio_refgeographie) continuent de
    # fonctionner comme avant. Dès qu'une table est inventoriée, l'inventaire est la seule
    # source pour elle : c'est ce qui garantit que rien n'est dessiné « en plus » de ce
    # qui est déclaré, ni omis parce que la prose ne l'a pas répété.
    known_tables = set(tables)
    for doc in docs:
        local = _local_aliases(doc.page_content)
        for m in _JOIN_RE.finditer(doc.page_content):
            t1_raw, c1, t2_raw, c2 = (g.strip() for g in m.groups())
            t1 = _resolve(t1_raw, alias_map, local, known_tables)
            t2 = _resolve(t2_raw, alias_map, local, known_tables)
            if not t1 or not t2 or t1 == t2:
                continue
            if t1 in tables_inventoriees or t2 in tables_inventoriees:
                continue
            # Une même jointure est souvent écrite dans les deux sens selon le document
            # (`vente.IdActeKpi = dim.IdActeCommercial` dans la fiche de la dimension,
            # `ON kpi.IdActeCommercial = f.IdActeKpi` dans le SQL du fichier de relations).
            # On garde la première rencontrée et on neutralise sa symétrique, sinon le
            # graphe affiche deux arêtes opposées pour une seule relation. L'ordre de
            # parcours étant celui des fichiers triés, c'est l'écriture fait -> dimension
            # (celle des fiches de colonnes) qui gagne — le bon sens FK -> PK.
            key = (t1, c1, t2, c2)
            if key in seen_relations:
                continue
            seen_relations.add(key)
            seen_relations.add((t2, c2, t1, c1))
            relations.append({
                "source": t1, "source_column": c1,
                "target": t2, "target_column": c2,
                "status": STATUS_DOCUMENTED, "note": "",
            })

    for t in tables.values():
        t.setdefault("documented", True)

    return {
        "tables": sorted(tables.values(), key=lambda t: (t["dataset"], t["name"])),
        "relations": relations,
        "unresolved": unresolved,
    }
