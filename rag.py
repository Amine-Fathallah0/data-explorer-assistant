import os
import re
from dotenv import load_dotenv
from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse
from langchain_google_genai._common import GoogleGenerativeAIError
from langchain_core.exceptions import ModelError


from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings
from langchain_qdrant import QdrantVectorStore
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnablePassthrough
from langchain_core.output_parsers import StrOutputParser
from operator import itemgetter

from config import (
    EMBEDDING_DIM, EMBEDDING_MODEL, QDRANT_COLLECTION,
    LLM_MODEL, LLM_FALLBACK_MODELS, LLM_THINKING_LEVEL, LLM_TIMEOUT,
    LLM_MAX_RETRIES,
)
from kb_meta import (
    _all_docs, _chunk_key, _clean_table, _column_token, breadcrumb, is_entity_header,
    table_aliases,
)

load_dotenv()
gemini_api_key = os.getenv("GEMINI_API_KEY")
qdrant_client =QdrantClient(
    url=os.getenv("QDRANT_URL"),
    api_key=os.getenv("QDRANT_API_KEY")
)

embeddings = GoogleGenerativeAIEmbeddings(
    model=EMBEDDING_MODEL,
    google_api_key=gemini_api_key,
    output_dimensionality=EMBEDDING_DIM,
)
vector_store = QdrantVectorStore(
    collection_name=QDRANT_COLLECTION,
    client=qdrant_client,
    embedding=embeddings,
)

# k=8 (contre 5 sur le POC F1) : le KB Orange documente deux datasets qui se référencent
# mutuellement (bio_vente → bio_refgeographie), et une question de jointure a besoin des
# deux côtés. Le budget de contexte reste modeste, les chunks étant courts.
retriever = vector_store.as_retriever(search_kwargs={"k": 8})

# ---------- Expansion par jointure (navigation inter-tables et inter-datasets) ----------
#
# Le retriever fait une similarité vectorielle plate sur tous les chunks : un chunk qui
# ÉNONCE une jointure peut entrer dans le top-k sans que les colonnes qu'il nomme y entrent
# elles aussi — le modèle reçoit alors la RÈGLE de jointure sans la définition des colonnes
# qu'elle utilise. C'est particulièrement coûteux ici, où les deux datasets se référencent
# mutuellement : `vente.IdGeoIrisEdo = geo.IdIris` traverse bio_vente -> bio_refgeographie,
# et la définition d'`IdIris` (avec son « non vérifié empiriquement ») vit dans l'autre
# fichier. Cette étape complète déterministiquement (aucun LLM, aucun embedding) les chunks
# récupérés avec les définitions de colonnes qu'ils référencent explicitement.
#
# Différence avec le POC F1 : là-bas, les relations étaient portées par des titres
# `## Relation 5 : results → status` et les colonnes citées entre backticks dans la prose.
# Ici les jointures sont écrites en SQL dans des blocs de code (`ON vente.IdGeoIrisEdo =
# geo.IdIris`) ou en prose FK (« Clé étrangère → `dwh_dim_ActeKpiHierarchise_t.IdActeCommercial` »).
# On ne parse donc plus le titre : on cherche les références qualifiées `x.y` dans le corps,
# blocs de code INCLUS — c'est là que vit l'information.

# Référence qualifiée `quelquechose.autrechose`. Les deux membres doivent commencer par une
# lettre : ça écarte d'office les nombres décimaux omniprésents dans ce KB (« ##.##% »,
# « ##.##% »), qui sinon pollueraient chaque chunk de statistiques.
_QUALIFIED_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b")
MAX_RELATION_EXPANSION = 6  # plafond : ne pas gonfler le contexte sans limite

# Index (alias -> tables, table -> colonnes) construit paresseusement et mémorisé pour la
# durée du process. Le KB tient en une centaine de chunks : le relire à chaque question
# serait acceptable, mais cet index-là est reconstruit à chaque appel d'expansion sinon.
_index_cache = None


def _build_index():
    """({alias_minuscule: {tables}}, {table_minuscule: {colonne_minuscule: Document}}).

    Tout est indexé en minuscules : le KB est incohérent sur la casse des identifiants
    (`idIris` dans le header de colonne, `geo.IdIris` dans le SQL de jointure,
    `GeoIris1.flagirisee` ailleurs). Un rapprochement sensible à la casse ne retrouverait
    aucune de ces trois écritures à partir des deux autres."""
    aliases = {}
    columns = {}
    for doc in _all_docs():
        table_raw = doc.metadata.get("table", "")
        if not is_entity_header(table_raw):
            continue
        table = _clean_table(table_raw)
        for alias in table_aliases(table_raw):
            aliases.setdefault(alias.lower(), set()).add(table)
        token = _column_token(doc.metadata.get("colonne", ""))
        if token:
            columns.setdefault(table.lower(), {}).setdefault(token.lower(), doc)
    return aliases, columns


def _get_index():
    global _index_cache
    if _index_cache is None:
        _index_cache = _build_index()
    return _index_cache


def _referenced_columns(doc, aliases, columns):
    """Les Documents de définition de colonne que `doc` référence explicitement via une
    écriture qualifiée `table.colonne`.

    Deux modes de résolution du membre gauche, dans cet ordre :
      1. c'est un nom de table ou un alias documenté (`vente`, `dwh_dim_GeoIris_t`) — cas
         direct ;
      2. c'est un alias SQL local, jamais documenté (`geo`, `f`, `d`, `raw`, `kpi`) — on
         retombe alors sur les tables effectivement nommées ailleurs dans le même chunk.
         C'est ce qui fait fonctionner `ON vente.IdGeoIrisEdo = geo.IdIris` : `geo` est
         opaque, mais le bloc SQL juste au-dessus écrit
         `` `bio_refgeographie.dwh_dim_GeoIris_t` geo ``.

    Les faux positifs possibles (une colonne homonyme dans une table citée au passage) sont
    sans gravité : le pire cas ajoute un chunk de définition en trop au contexte."""
    text = doc.page_content
    pairs = set(_QUALIFIED_RE.findall(text))

    # Tables réellement nommées dans ce chunk — support du mode 2.
    local_tables = {
        table
        for alias, tables in aliases.items()
        if re.search(r"\b" + re.escape(alias) + r"\b", text, re.IGNORECASE)
        for table in tables
    }

    found = []
    for left, right in pairs:
        col = right.lower()
        targets = aliases.get(left.lower()) or local_tables
        for table in targets:
            match = columns.get(table.lower(), {}).get(col)
            if match is not None:
                found.append(match)
    return found


def expand_with_relations(docs):
    """Complète `docs` (résultat du retriever) avec les définitions de colonnes que les
    chunks récupérés référencent explicitement en écriture qualifiée `table.colonne`.
    N'écarte ni ne réordonne rien — ajoute seulement en fin de liste, dédoublonné, plafonné
    à MAX_RELATION_EXPANSION chunks ajoutés."""
    seen = {_chunk_key(d.metadata) for d in docs}
    added = []
    aliases = columns = None  # index chargé paresseusement, seulement si utile

    for doc in docs:
        if "." not in doc.page_content:
            continue
        if aliases is None:
            aliases, columns = _get_index()
        for match in _referenced_columns(doc, aliases, columns):
            key = _chunk_key(match.metadata)
            if key in seen:
                continue
            seen.add(key)
            added.append(match)
            if len(added) >= MAX_RELATION_EXPANSION:
                return docs + added

    return docs + added


# ---------- Plafonnement du contexte ----------
#
# Le proxy Orange refuse toute requête dépassant 10 000 tokens, enveloppe de sortie
# RÉSERVÉE comprise (voir le commentaire sur max_tokens plus bas). Rien ne bornait le
# contexte jusqu'ici : une question qui tombe sur les gros chunks du KB (le tableau de
# colonnes de `dwh_fact_VenteAssuranceMateriel_t`, la procédure de concomitance, la vue
# d'ensemble) atteignait ~9 600 tokens et basculait en 429 selon le tirage — l'utilisateur
# voyait un 503 sur une question précise, systématiquement la même, ce qui ne ressemblait
# pas à une limite de débit et n'en était pas une.
#
# Le plafond est appliqué ICI, sur la liste de Documents, et non dans format_docs : app.py
# construit les citations depuis cette même liste (extract_sources). Tronquer plus bas
# afficherait en source des chunks jamais envoyés au modèle — exactement la promesse de
# traçabilité que ce POC est censé tenir.
PROXY_TOKEN_LIMIT = 10000
OUTPUT_RESERVE = 2000      # doit rester aligné sur max_tokens du client LLM
SAFETY_MARGIN = 400        # imprécision de l'estimation + gabarit du prompt (history, etc.)
CHARS_PER_TOKEN = 3.2      # estimation prudente pour du français accentué et du markdown


def _context_char_budget():
    """Nombre de caractères de contexte encore disponibles une fois le prompt système,
    l'enveloppe de sortie et une marge de sécurité décomptés du plafond du proxy."""
    tokens_prompt = len(SYSTEM_PROMPT) / CHARS_PER_TOKEN
    tokens_dispo = PROXY_TOKEN_LIMIT - OUTPUT_RESERVE - tokens_prompt - SAFETY_MARGIN
    return max(int(tokens_dispo * CHARS_PER_TOKEN), 2000)


def fit_to_budget(docs):
    """Tronque `docs` pour tenir dans le budget de contexte, en conservant l'ordre — donc
    les chunks les mieux classés par similarité d'abord, et les compléments d'expansion
    (ajoutés en fin de liste) sacrifiés en premier. Garde toujours au moins un chunk :
    mieux vaut une réponse sur un contexte partiel qu'un 503."""
    budget = _context_char_budget()
    gardes, total = [], 0
    for doc in docs:
        cout = len(doc.page_content) + 120  # + en-tête [Source : …] posé par _format_doc
        if gardes and total + cout > budget:
            break
        gardes.append(doc)
        total += cout
    return gardes


def retrieve_docs(question):
    """Point d'entrée unique de la récupération : similarité vectorielle (top-k),
    complétion déterministe par jointure, puis plafonnement au budget de contexte.
    Utilisé par app.py et par le bloc CLI ci-dessous — un seul endroit à faire évoluer si
    la stratégie de récupération change."""
    return fit_to_budget(expand_with_relations(retriever.invoke(question)))


SYSTEM_PROMPT = """Tu es un assistant expert en modélisation de données, spécialisé dans les datasets du Data Warehouse Orange documentés dans cette Knowledge Base (aujourd'hui `bio_vente` — ventes de terminaux et d'assurances — et `bio_refgeographie` — référentiel géographique). Ton rôle est d'expliquer le MODÈLE DE DONNÉES à des data engineers et data analysts.

RÈGLE ABSOLUE — PÉRIMÈTRE MÉTADONNÉES UNIQUEMENT :
Tu documentes, tu n'interroges pas. Tu réponds uniquement sur la signification des colonnes, les règles métier, les conventions de valeurs, et la logique de jointure entre tables. Tu n'as AUCUN accès aux données : tu n'exécutes jamais de requête, tu ne calcules jamais un chiffre, tu ne renvoies jamais une valeur lue dans une ligne, tu ne produis jamais de statistique « à jour ».
Si une question demande d'interroger les données (« combien de ventes en 2025 ? », « quelles sont les valeurs de cette colonne aujourd'hui ? », « donne-moi des exemples de lignes »), explique qu'elle sort de ton périmètre — qui est l'explication du modèle de données, pas son interrogation — et propose de la reformuler en une question sur la structure ou les règles métier si cela a du sens.

Si tu proposes des exemples de questions reformulées, liste-les sous un marqueur `SUGGESTIONS:` sur sa propre ligne, une suggestion par ligne suivante. N'utilise ce marqueur que dans ce cas précis (refus hors périmètre avec reformulation proposée) — jamais ailleurs.

RÈGLE — CHIFFRES DOCUMENTÉS vs INTERROGATION DES DONNÉES :
Le contexte contient des chiffres de cadrage déjà agrégés et validés au moment de la rédaction de la documentation (taux de remplissage, cardinalités, taux d'orphelins, volumétries, répartitions de valeurs codées). Ces chiffres CARACTÉRISENT la colonne : ils font partie de ton périmètre, tu peux et dois les citer quand ils éclairent la question.
Deux obligations quand tu les cites :
- toujours les présenter comme un constat daté de la documentation, jamais comme un état actuel du système (« ##.#% des lignes portent la sentinelle `ND` d'après la documentation », pas « il y a aujourd'hui ##.#% de… ») ;
- ne jamais en dériver un nouveau calcul, une extrapolation ou une projection. Tu restitues le chiffre documenté, tu n'en produis pas.
Ce qui reste hors périmètre : toute valeur mesurée pour une entité précise (un client, une vente, une commune), et tout chiffre qui n'est pas explicitement dans le contexte.

RÈGLE — RGPD, DONNÉES PERSONNELLES :
Ces tables portent des données client (identifiants de ligne mobile, espaces clients, géographie du domicile). Tu ne produis, n'inventes et ne reproduis JAMAIS une valeur ressemblant à une donnée personnelle réelle : pas de numéro de téléphone/MSISDN, pas d'IMEI, pas d'identifiant client, pas d'adresse. Si un exemple est nécessaire pour illustrer un format, décris le format en toutes lettres (« 10 chiffres commençant par 06 ou 07 ») plutôt que d'écrire un numéro. Les seules valeurs concrètes que tu as le droit de citer sont celles déjà présentes dans le contexte, et uniquement quand ce sont des codes de référentiel (codes canal, codes KPI, codes DO, codes maille, sentinelles) — jamais un identifiant d'entité.

RÈGLE — INCERTITUDE, LE POINT LE PLUS IMPORTANT DE CETTE KNOWLEDGE BASE :
Cette documentation est explicitement en cours de construction et distingue avec soin ce qui est vérifié de ce qui ne l'est pas. Tu dois transmettre cette distinction intacte, jamais la lisser.
- 🔍 ou « non vérifié empiriquement », « à confirmer », « hypothèse », « supposé », « non confirmé », « reste ouvert » : le point est INCERTAIN. Ne comble JAMAIS ce trou, ni depuis tes connaissances générales, ni par déduction depuis le reste du contexte. Dis explicitement que le point n'est pas vérifié, et reprends la réserve telle qu'elle est documentée. Une incertitude documentée est une information de premier ordre pour l'utilisateur, pas une lacune à masquer.
- ✅ « confirmé » / « vérifié » / « résolu » : le point est établi, tu peux l'affirmer.
- ⚠️ : piège de nommage, exception ou mise en garde — remonte-la systématiquement si elle touche au sujet de la question.
- ❌ : piste testée et REJETÉE. Ne la présente jamais comme une option valable.
- 🔴 : question ouverte prioritaire, non tranchée.
Si la question porte précisément sur un point marqué incertain, la bonne réponse commence par dire que ce n'est pas tranché. Ne jamais présenter une hypothèse comme un fait, même plausible. Ne jamais transformer « supposé complet et non-nul » en « complet et non-nul ».

RÈGLE — PORTÉE D'UNE VÉRIFICATION :
Les propriétés suivantes ne sont JAMAIS transférables d'une colonne, d'une table, d'un périmètre ou d'un environnement à un autre :
nullabilité · unicité · complétude et taux d'orphelins · grain · exhaustivité d'une liste de valeurs · stabilité dans le temps · taux de remplissage · environnement (PROD/QA).
Pour chacune de ces propriétés, tu dois nommer le périmètre exact sur lequel le contexte dit qu'elle a été vérifiée. Si le contexte ne l'énonce pas POUR L'ÉLÉMENT PRÉCIS sur lequel porte la question, écris explicitement qu'elle n'est pas vérifiée pour cet élément.
L'absence d'information sur une propriété n'est JAMAIS une confirmation de cette propriété. Un silence se rapporte comme un silence, jamais comme un « oui » hérité d'un voisin.
Quand deux éléments se ressemblent et qu'un seul a été vérifié, dis lequel l'a été et que l'autre ne l'est pas — ne réponds jamais sur les deux à la fois comme s'ils partageaient le même statut.

RÈGLE — FORMULES D'HÉRITAGE :
Les tournures « redondant avec X », « même logique que X », « même colonne que X », « même table cible que X », « même concept que X », « idem », « même définition que X » transfèrent UNIQUEMENT la définition métier de l'élément cité. À elles seules, elles ne transfèrent jamais une propriété de la liste ci-dessus, ni un marqueur ✅ posé sur X.
Le contexte contient au moins un cas où deux colonnes déclarées redondantes portent en réalité des contenus différents (un code technique d'un côté, un libellé en clair de l'autre) : une redondance annoncée dans cette documentation est une indication de sens, pas une garantie d'équivalence technique.

PRÉCÉDENCE — une mention explicite et locale l'emporte toujours sur ce défaut, dans les deux sens :
- si le contexte énonce POUR CETTE COLONNE que les valeurs sont les mêmes (a fortiori marquée ✅ ou accompagnée de la liste des valeurs), reprends-le comme un fait établi, sans y ajouter de réserve de ton cru ;
- si le contexte signale au contraire une divergence (⚠️), c'est cette divergence qui prime.
Le défaut de prudence ne s'applique QUE lorsque le contexte est muet sur le point demandé. Ne transforme jamais une prudence en refus de répondre : quand la documentation affirme quelque chose, ton rôle est de le restituer, pas de le relativiser.

RÈGLE — HISTORIQUE DE CONVERSATION :
Voici l'historique des échanges précédents dans cette conversation, s'il y en a. Utilise-le uniquement pour comprendre à quoi une question fait référence si elle est ambiguë (ex: "et pour X ?") — jamais comme source d'information factuelle à la place du contexte ci-dessous.

Historique :
{history}

RÈGLE — CORRECTIONS VALIDÉES :
Si un passage du contexte est précédé de "[CORRECTION VALIDÉE]", son FOND a été validé par un humain et fait autorité : en cas de contradiction avec un autre passage du contexte, c'est lui qui gagne, sans exception.
Cette priorité porte uniquement sur le FOND. La RÈGLE — FORMAT DE RÉPONSE s'applique à ce passage exactement comme aux autres : ne reprends jamais en prose une valeur déjà listée dans le tableau, même si le texte de la correction l'énumère, et ne mentionne jamais le marqueur lui-même dans ta réponse.

RÈGLE — CONTEXTE FOURNI UNIQUEMENT :
Réponds exclusivement à partir du contexte ci-dessous, extrait de la Knowledge Base. N'utilise jamais tes connaissances générales — ni sur les télécoms, ni sur la fibre, ni sur l'ARCEP, ni sur le découpage INSEE/IRIS, ni sur les conventions habituelles d'un Data Warehouse — pour compléter ou corriger ce contexte. Le fait qu'une convention te paraisse évidente (une colonne `Id*` serait une clé primaire non nulle, un `Code*` suivrait le standard officiel) ne t'autorise jamais à l'affirmer si le contexte ne le dit pas : c'est précisément ce genre de supposition que cette documentation cherche à ne pas propager.
Si le contexte ne contient pas explicitement les éléments nécessaires pour répondre à la question posée, dis-le clairement au début de ta réponse (par exemple : "Le contexte disponible ne couvre pas explicitement ce point, mais voici ce qui s'en rapproche le plus :") avant de donner ce que tu peux, plutôt que de répondre avec une fausse assurance.
La documentation se déclare elle-même non exhaustive à plusieurs endroits (dimensions non investiguées, colonnes dont la table cible n'a jamais été identifiée). Ne présente jamais une liste du contexte comme le schéma complet d'une table si le contexte la signale comme partielle.

RÈGLE — FORMAT DE RÉPONSE :
Sois concis. Ne répète jamais en prose une information déjà présentée dans un tableau — un tableau bien construit remplace la prose, il ne s'y ajoute pas.

Si la question porte sur une colonne spécifique, commence ta réponse par un tableau récapitulatif avec les colonnes suivantes : Nom | Type | Description courte | Nullable | Valeurs possibles.
- Pour "Description courte", utilise en priorité la définition officielle de la colonne (section Colonnes) — ne la remplace pas par une reformulation tirée d'une règle métier associée, même si celle-ci semble pertinente.
- Remplis "Nullable" et "Valeurs possibles" uniquement si le contexte les mentionne explicitement. Si le contexte ne précise pas si la colonne peut être NULL, écris "non précisé" plutôt que de deviner.
- Pour "Valeurs possibles" : si la colonne a un nombre limité de valeurs codées (ex : `CodeCanalDistribution`, `LibDo`, `Regroupement`), liste-les avec leur signification dans le tableau. Si elle accepte une plage numérique continue et mesurable (ex : dates, montants, prix), écris "valeurs continues, non énumérables". Si c'est un identifiant textuel libre ou un numéro non documenté comme liste fermée (ex : `CodeActe`, `NumeroDossierMobile`), écris "identifiant libre, non codifié" — réserve "valeurs continues" aux seules grandeurs numériques mesurables.
- Si la colonne porte une valeur sentinelle documentée (`-999`, `ND`, `NC`, `NA`), mentionne-la toujours dans "Valeurs possibles" avec son sens exact tel que documenté pour CETTE colonne. Le sens de `-999` varie d'une colonne à l'autre dans ce modèle (parfois un échec de résolution, parfois une information métier positive) — ne généralise jamais l'interprétation d'une colonne à une autre.
- Si la question ne porte que sur un seul aspect d'une colonne (ex : uniquement ses valeurs possibles, ou uniquement si elle est nullable), tu peux répondre uniquement sur cet aspect sans reconstruire le tableau complet.
- Le Type de la colonne est presque toujours indiqué entre parenthèses juste après son nom dans le contexte (ex : `CodeLigneDMC` (INTEGER)) — utilise systématiquement cette valeur pour le champ "Type". Si le contexte indique "type à confirmer" ou signale le type comme inféré et non vérifié, reporte cette réserve au lieu d'écrire un type sec. N'écris "non précisé" pour le Type que si aucune indication n'apparaît nulle part dans le contexte.
- Un taux de NULL documenté ("NULL sur ~#.#% des lignes", "0% NULL") renseigne "Nullable" — mais attention, le contexte avertit que « 0% NULL SQL » ne signifie pas « colonne complète » quand une sentinelle est utilisée. Ne conclus jamais à la complétude d'une colonne sur le seul taux de NULL si le contexte mentionne une sentinelle.
- Pour "Description courte", utilise en priorité la phrase de définition qui suit immédiatement le nom et le type de la colonne (généralement la toute première phrase du chunk correspondant), avant tout élément tiré d'une règle métier associée.
- Complète ensuite le tableau avec au maximum 2-3 puces courtes, une information par puce, si des règles métier importantes ne rentrent pas dans le tableau (ex : évolution historique, piège à connaître). Jamais de phrase liée par "car", "ce qui permet de", "afin de" — une puce, un fait, point.

Si la question porte sur une table entière, une relation entre tables, ou sort du périmètre : structure TOUJOURS ta réponse en puces courtes, jamais en prose liée. Une puce = une information technique directe, vocabulaire de data engineer (pas de vulgarisation). Si plusieurs éléments comparables sont listés, un tableau reste possible à la place des puces — mais jamais de paragraphe qui enchaîne plusieurs faits avec des connecteurs de cause ou de conséquence. Inclus les règles métier directement liées au sujet de la question, chacune sur sa propre puce, pas seulement une définition minimale.
RÈGLE — SYNTAXE MARKDOWN :
Utilise des backticks uniquement pour les noms techniques (colonnes, tables, valeurs de code), et referme-les toujours immédiatement — ne mélange jamais guillemets simples et backticks au sein d'une même liste de valeurs codées, choisis un seul style et applique-le à toute la liste. Pour toute liste à puces, place chaque élément sur sa propre ligne avec un tiret en début de ligne — n'utilise jamais d'astérisque au milieu d'une phrase.

RÈGLE — PAS DE REMPLISSAGE :
N'ajoute aucune phrase d'introduction ou de transition qui ne porte pas d'information nouvelle (ex : évite "Voici les informations demandées"). Tu peux terminer par une phrase courte proposant une aide complémentaire, uniquement si elle apporte une piste réellement nouvelle et pertinente à cette réponse précise — jamais une formule générique répétée par réflexe d'une réponse à l'autre. S'il n'y a rien de spécifique à proposer, ne termine par aucune phrase plutôt que d'utiliser une formule par défaut.

RÈGLE — SQL :
Le contexte contient des exemples de requêtes SQL de référence (jointures, requêtes de vérification, parfois explicitement marquées « jamais exécutée »). Tu peux expliquer en français la logique de la relation qu'elles décrivent, et nommer les colonnes de part et d'autre d'une jointure ainsi que les pièges de nommage, mais ne reproduis jamais le texte SQL brut dans ta réponse — décris la relation en langage naturel. Tu ne rédiges pas de requête, tu n'en exécutes aucune, et tu ne présentes jamais le résultat d'une requête de vérification comme connu si le contexte indique qu'elle n'a pas été lancée.

RÈGLE — CITATIONS :
Ne mentionne jamais explicitement d'où provient l'information (pas de "selon la table X", pas de nom de fichier, pas de numéro de section ou de règle). Réponds directement au contenu de la question — la source sera affichée séparément par l'interface.
Contexte :
{context}

Question :
{question}
"""
prompt = ChatPromptTemplate.from_template(SYSTEM_PROMPT)

def _make_llm(model):
    """Un client Gemini borné dans le temps. Les deux plafonds comptent autant que le nom du
    modèle : sans `timeout`, un appel pris dans une congestion n'est jamais abandonné, et
    avec le `max_retries` par défaut (6) une salve de 503 enchaîne six tentatives lentes
    avant de rendre la main — c'est ce qui transformait une lenteur en attente sans fin."""
    return ChatGoogleGenerativeAI(
        model=model,
        google_api_key=gemini_api_key,
        max_output_tokens=2000,
        timeout=LLM_TIMEOUT,
        max_retries=LLM_MAX_RETRIES,
        # Le raisonnement interne est le premier poste de latence ici (cf. LLM_THINKING_LEVEL
        # dans config.py), bien avant la taille de la sortie.
        thinking_config={"thinking_level": LLM_THINKING_LEVEL},
    )


# Repli en cascade plutôt qu'un modèle unique : l'API Gemini renvoie des 503 par modèle et
# par intermittence, pas de façon globale — au moment d'écrire, trois modèles sur huit
# refusaient tout appel pendant que d'autres répondaient normalement. `with_fallbacks`
# rattrape aussi bien le 503 que le dépassement de `timeout`, donc un modèle engorgé coûte
# au pire LLM_TIMEOUT avant de passer au suivant, au lieu de bloquer la requête.
# Le repli vaut pour TOUS les appels (chat et corrections) : `chain` et `llm_text` (app.py)
# partent tous deux d'ici.
llm = _make_llm(LLM_MODEL).with_fallbacks(
    [_make_llm(m) for m in LLM_FALLBACK_MODELS]
)


# Les erreurs LLM à rattraper côté Flask ne tiennent PAS dans le seul
# GoogleGenerativeAIError : selon le chemin, langchain_google_genai lève soit celui-là
# (embeddings), soit un GoogleAPIError qui n'en hérite pas (chat — sa chaîne d'héritage
# passe par langchain_core.exceptions.ModelError, sans rapport avec le type Google).
# Un `except GoogleGenerativeAIError` seul laissait donc passer les 503 du chat : Flask
# renvoyait une 500 brute au lieu du message d'indisponibilité prévu. On expose un tuple
# unique pour que tous les handlers couvrent les deux familles.
LLM_ERRORS = (GoogleGenerativeAIError, ModelError)

def _format_doc(doc):
    """Un bloc de contexte pour un chunk. Les chunks dont le payload porte `corrected`
    (posé à l'écriture par kb_edit, restauré par ingest.py depuis le journal d'audit) sont
    préfixés d'un marqueur : le contenu est déjà celui du KB, mais le marqueur donne au
    modèle la priorité à appliquer en cas de contradiction avec un autre passage."""
    # Fil d'Ariane `Dataset > Table > Section > Colonne` recalculé plutôt que lu depuis le
    # payload : un point ingéré avant l'ajout de cette métadonnée n'en porterait pas, et le
    # modèle perdrait alors le dataset d'appartenance — décisif ici, où `CodeRegion`,
    # `CodeAd` ou `DateMaj` existent à l'identique dans plusieurs tables et deux datasets.
    entete = f"[Source : {breadcrumb(doc.metadata)}]"
    marqueur = "[CORRECTION VALIDÉE]\n" if doc.metadata.get("corrected") else ""
    return f"{entete}\n{marqueur}{doc.page_content}"


def format_docs(docs):
    return "\n\n".join(_format_doc(doc) for doc in docs)

chain = (
    {
        "context": itemgetter("context"),
        "question": itemgetter("question"),
        "history": itemgetter("history"),
    }
    | prompt
    | llm
    | StrOutputParser()
)

if __name__ == "__main__":
    question = input("Question : ")

    try:
        docs_trouves = retrieve_docs(question)
        if not docs_trouves:
            print("Aucun chunk pertinent trouvé dans la Knowledge Base pour cette question.")
        else:
            contexte = format_docs(docs_trouves)
            print(chain.invoke({"question": question, "history": "", "context": contexte}))
    except (ResponseHandlingException, UnexpectedResponse) as e:
        print(f"Erreur Qdrant : {e}")
    except GoogleGenerativeAIError as e:
        print(f"Erreur API Gemini (embeddings, rate limit, réseau, etc.) : {e}")

