"""
config.py — constantes partagées par les modules qui parlent à Qdrant.

Existe pour une seule raison : le nom de collection était dupliqué en trois endroits
(ingest.py, rag.py, kb_edit.py). Une divergence entre ces copies ne casse rien au
démarrage — elle écrit simplement les corrections dans une collection que le retriever
ne lit pas, silencieusement. Une constante unique, surchargeable par variable
d'environnement pour pouvoir tester sur une collection jetable sans toucher au code.
"""

import os

from dotenv import load_dotenv

load_dotenv()

# Collection Qdrant du POC Orange. Le POC F1 utilisait "F1-Intern" : collection distincte,
# donc les deux jeux de données peuvent cohabiter sur le même cluster free tier.
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "Orange-DataExplorer")

# Dimension des embeddings Gemini demandée à l'API (gemini-embedding-001 accepte une
# troncature). Doit être identique à l'ingestion et à la recherche, sinon Qdrant refuse
# le vecteur de requête.
EMBEDDING_MODEL = "models/gemini-embedding-001"
EMBEDDING_DIM = 768

# ---------- Modèle de génération ----------
# Le POC tapait "gemini-3.5-flash-lite" en dur dans rag.py. Remonté ici (et surchargeable
# par variable d'environnement) parce que ce nom doit pouvoir changer sans toucher au code.
#
# Le coût dominant d'un appel n'est PAS la sortie mais le raisonnement interne : sur le
# prompt de reformulation (340 tokens en entrée, ~53 en sortie), les modèles mesurés
# brûlaient 900 à 1700 tokens de "thinking". D'où les deux réglages ci-dessous — le modèle
# et le niveau de raisonnement — qui comptent autant l'un que l'autre.
LLM_MODEL = os.getenv("LLM_MODEL", "gemini-3-flash-preview")

# Modèles de repli, essayés dans l'ordre quand le principal échoue (503, timeout). Le but
# n'est pas la qualité de sortie — tous sont du même calibre — mais de ne jamais laisser une
# congestion côté Google bloquer une correction : l'API renvoie des 503 modèle par modèle et
# par intermittence, pas globalement. L'ancien modèle ferme la marche : lent (~20 s, et
# insensible au réglage de raisonnement), mais il répondait quand d'autres renvoyaient 503.
LLM_FALLBACK_MODELS = [
    m.strip() for m in os.getenv(
        "LLM_FALLBACK_MODELS", "gemini-3.6-flash,gemini-3.5-flash-lite"
    ).split(",") if m.strip()
]

# Niveau de raisonnement. La reformulation d'un chunk est une tâche de RÉÉCRITURE, pas de
# résolution : le raisonnement long n'y améliore rien et se paie en secondes d'attente.
# Mesuré sur le prompt réel : "low" fait tomber l'appel de ~10 s à ~6,5 s. Les modèles qui
# ne connaissent pas ce réglage (gemini-3.5-flash-lite) l'ignorent sans renvoyer d'erreur.
LLM_THINKING_LEVEL = os.getenv("LLM_THINKING_LEVEL", "low")

# Plafond en secondes d'UN appel au LLM. Sans ce plafond (défaut langchain : None), un appel
# parti dans une congestion Gemini n'est jamais abandonné : le bouton « Préparer la
# correction » reste sur « Analyse du KB... » indéfiniment, sans message d'erreur.
LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "30"))

# Reprises par modèle. Le défaut langchain est 6 : sur une salve de 503, un seul clic
# enchaîne six tentatives lentes avant de rendre la main, ce qui transforme une lenteur en
# attente de plusieurs minutes. 1 reprise suffit pour absorber un 503 isolé ; au-delà, on
# passe au modèle de repli, ce qui est à la fois plus rapide et plus susceptible d'aboutir.
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "1"))
