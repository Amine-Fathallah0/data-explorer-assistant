# ingest.py — (ré)ingestion complète de la Knowledge Base dans Qdrant.
#
# Idempotent : les ids sont content-adressés (uuid5(source::page_content), voir
# kb_meta._chunk_id), donc relancer ce script sur un KB inchangé réécrit exactement les
# mêmes points au lieu d'en créer des doublons.
#
# Le découpage, le parcours des fichiers et la normalisation des noms de table viennent
# TOUS de kb_meta — ce fichier n'en réimplémente aucun. C'était le cas avant (splitter
# recopié ici, `os.listdir` à plat, `.replace("Table : ", "")` en ligne) et c'est
# précisément ce qui rendait une divergence possible : un chunk ingéré sous un id que
# l'application ne sait pas recalculer devient invisible aux corrections.

import argparse
import os
import sys
import time

from dotenv import load_dotenv
from langchain_google_genai._common import GoogleGenerativeAIError

# La console Windows est en cp1252 par défaut : le moindre « → » ou emoji d'un fil d'Ariane
# fait planter le script sur un UnicodeEncodeError au moment du print, alors que
# l'ingestion elle-même n'a aucun problème. Le KB est intégralement en français et truffé
# de marqueurs (⚠️, 🔍) — on force donc la sortie en UTF-8.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_qdrant import QdrantVectorStore
from qdrant_client import QdrantClient, models

import db
import kb_meta
from config import EMBEDDING_DIM, EMBEDDING_MODEL, QDRANT_COLLECTION

# 1. CONFIGURATION
load_dotenv()
gemini_api_key = os.getenv("GEMINI_API_KEY")
qdrant_client = QdrantClient(
    url=os.getenv("QDRANT_URL"),
    api_key=os.getenv("QDRANT_API_KEY"),
    timeout=60,
)

parser = argparse.ArgumentParser(description="Ingestion de la Knowledge Base dans Qdrant.")
parser.add_argument(
    "--dry-run", action="store_true",
    help="Découpe et affiche le plan d'ingestion sans appeler l'API d'embedding ni écrire dans Qdrant.",
)
args = parser.parse_args()

# 2. CHARGEMENT + 3. CHUNKING
# Parcours RÉCURSIF : les fichiers vivent dans un sous-dossier par dataset
# (knowledge-base/bio_vente/…, knowledge-base/bio_refgeographie/…), jamais à plat. Un
# `os.listdir` sur la racine ne remonterait aucun fichier et l'ingestion se terminerait
# sur un « 0 chunk » silencieux.
all_chunks = []
ids = []

for source, path in kb_meta.iter_kb_files():
    text = kb_meta._normalize(kb_meta._read_raw(path))
    # Les ids viennent de chunk_markdown, jamais recalculés ici : c'est la même fonction que
    # celle utilisée par kb_edit/kb_create pour retrouver un point à mettre à jour, donc
    # aucun risque que les deux formules divergent.
    pairs = kb_meta.chunk_markdown(source, text)
    all_chunks.extend(doc for _cid, doc in pairs)
    ids.extend(cid for cid, _doc in pairs)
    print(f"{source} → {len(pairs)} chunks")

# Garde-fou : deux chunks ne doivent jamais partager un id, sinon l'un écrase l'autre dans
# Qdrant en silence (cf. kb_meta._chunk_id). C'est arrivé, on le vérifie désormais.
if len(set(ids)) != len(ids):
    doublons = {cid for cid in ids if ids.count(cid) > 1}
    raise SystemExit(
        f"Collision d'ids sur {len(doublons)} chunk(s) — ingestion annulée. "
        "Deux chunks partagent (fichier, titres, contenu) à l'identique."
    )

if not all_chunks:
    raise SystemExit(
        f"Aucun fichier .md trouvé sous {kb_meta.KB_FOLDER}. "
        "Vérifier que la Knowledge Base est bien en place (un sous-dossier par dataset)."
    )

datasets = sorted({c.metadata.get("dataset", "") or "(racine)" for c in all_chunks})
print(f"\nTotal : {len(all_chunks)} chunks prêts pour l'embedding")
print(f"Datasets : {', '.join(datasets)}")

# 3bis. RESTAURATION DU MARQUEUR DE CORRECTION
# Un re-ingest complet reconstruit les chunks depuis les .md : le CONTENU corrigé est bien
# là (les corrections sont écrites dans les fichiers), mais le marqueur `corrected` du
# payload, lui, serait perdu — et avec lui le préfixe [CORRECTION VALIDÉE] que rag.py pose
# dans le contexte. On le repose donc depuis le journal d'audit.
#
# La clé (table nettoyée, colonne ou section) vient de kb_meta.clean_table, la même
# fonction qu'utilise db.corrected_chunks_index() — plus de copie locale à garder
# synchronisée. Le repli sur `section` n'est pas théorique : les chunks de prose sous un
# `###` sans `####` (ex. « Logique métier au niveau de la table ») n'ont pas de `colonne`.
db.init_db()  # idempotent : couvre le cas où ingest tourne avant le premier lancement de l'app
corrections_index = db.corrected_chunks_index()

if corrections_index:
    nb_marques = 0
    for chunk in all_chunks:
        table = kb_meta.clean_table(chunk.metadata.get("table", ""))
        detail = (chunk.metadata.get("colonne") or chunk.metadata.get("section") or "").strip()
        entry = corrections_index.get((table, detail))
        if entry:
            chunk.metadata["corrected"] = True
            chunk.metadata["corrected_at"] = entry["date"]
            nb_marques += 1
    print(f"Marqueur de correction restauré sur {nb_marques} chunk(s) depuis le journal d'audit")

if args.dry_run:
    print("\n--dry-run : aucun embedding calculé, aucune écriture dans Qdrant.")
    print("\nAperçu des fils d'Ariane :")
    for chunk in all_chunks:
        print(f"  {chunk.metadata['breadcrumb']}")
    raise SystemExit(0)

# 4. EMBEDDING + STOCKAGE
embeddings = GoogleGenerativeAIEmbeddings(
    model=EMBEDDING_MODEL,
    google_api_key=gemini_api_key,
    output_dimensionality=EMBEDDING_DIM,
)
# La collection du POC Orange est distincte de celle du POC F1 : au premier lancement elle
# n'existe pas encore côté cluster, et le constructeur QdrantVectorStore ne la crée pas.
if not qdrant_client.collection_exists(QDRANT_COLLECTION):
    qdrant_client.create_collection(
        collection_name=QDRANT_COLLECTION,
        vectors_config=models.VectorParams(size=EMBEDDING_DIM, distance=models.Distance.COSINE),
    )
    print(f"Collection « {QDRANT_COLLECTION} » créée (dim={EMBEDDING_DIM}, cosine)")

vector_store = QdrantVectorStore(
    collection_name=QDRANT_COLLECTION,
    client=qdrant_client,
    embedding=embeddings,
)

# Le free tier Gemini plafonne à 100 requêtes d'embedding par minute et par modèle, et
# l'intégration en envoie une par document : un envoi en bloc du KB (une centaine de chunks,
# et ça ne fera que croître) dépasse le quota en plein milieu et laisse la collection
# partiellement remplie. On découpe donc en lots avec une pause entre chacun.
# Reprise sans risque : les ids sont content-adressés, donc relancer le script après un
# échec réécrit les mêmes points au lieu d'en créer de nouveaux.
EMBED_BATCH = int(os.getenv("EMBED_BATCH", "90"))
EMBED_PAUSE_S = float(os.getenv("EMBED_PAUSE_S", "61"))
MAX_RETRIES = 3

stored = 0
for start in range(0, len(all_chunks), EMBED_BATCH):
    batch_docs = all_chunks[start:start + EMBED_BATCH]
    batch_ids = ids[start:start + EMBED_BATCH]

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            vector_store.add_documents(batch_docs, ids=batch_ids)
            break
        except GoogleGenerativeAIError as e:
            # Seul le dépassement de quota est réessayable : une clé invalide ou un modèle
            # inconnu ne se résoudra pas en attendant, autant échouer tout de suite.
            if "429" not in str(e) and "RESOURCE_EXHAUSTED" not in str(e):
                raise
            if attempt == MAX_RETRIES:
                raise
            print(f"  quota atteint, nouvelle tentative dans {EMBED_PAUSE_S:.0f}s "
                  f"({attempt}/{MAX_RETRIES - 1})…")
            time.sleep(EMBED_PAUSE_S)

    stored += len(batch_docs)
    print(f"  {stored}/{len(all_chunks)} chunks envoyés")

    if start + EMBED_BATCH < len(all_chunks):
        time.sleep(EMBED_PAUSE_S)

print(f"\n{len(all_chunks)} chunks stockés dans Qdrant (collection « {QDRANT_COLLECTION} »)")

# 4bis. PURGE DES POINTS ORPHELINS
# Les upserts couvrent les ajouts et les modifications, jamais les suppressions : un chunk
# retiré du markdown (colonne supprimée, section renommée, header corrigé) laisse derrière
# lui un point que plus rien ne référence, et que le retriever continue pourtant de
# remonter — donc du contenu périmé injecté dans le contexte. On retire tout point dont
# l'id n'appartient pas au KB courant, ce qui rend la ré-ingestion réellement idempotente.
attendus = set(ids)
orphelins = []
offset = None
while True:
    points, offset = qdrant_client.scroll(
        collection_name=QDRANT_COLLECTION, limit=256, offset=offset,
        with_payload=False, with_vectors=False,
    )
    orphelins.extend(p.id for p in points if str(p.id) not in attendus)
    if offset is None:
        break

if orphelins:
    qdrant_client.delete(
        collection_name=QDRANT_COLLECTION,
        points_selector=models.PointIdsList(points=orphelins),
    )
    print(f"{len(orphelins)} point(s) orphelin(s) supprimé(s) (chunks disparus du markdown)")

# 5. VÉRIFICATION
# Recompte côté serveur plutôt que de se fier au retour du SDK local :
# add_documents() ne lève pas toujours d'erreur en cas d'upsert partiel.
collection_info = qdrant_client.get_collection(QDRANT_COLLECTION)
print(f"Points dans la collection Qdrant : {collection_info.points_count}")
if collection_info.points_count < len(all_chunks):
    print(
        f"ATTENTION : {len(all_chunks)} chunks envoyés mais {collection_info.points_count} "
        "points en base — upsert probablement partiel, relancer l'ingestion."
    )
