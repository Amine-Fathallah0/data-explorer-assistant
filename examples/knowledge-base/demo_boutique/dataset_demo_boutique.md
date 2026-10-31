# Dataset `demo_boutique` — exemple fictif

> **Jeu de données entièrement fictif.** Fourni pour essayer l'application et pour la CI : il suit le gabarit de la Knowledge Base (titres `## Table :` / `## Dimension :`, `### Colonnes`, `#### \`colonne\` (TYPE)`), mais ne décrit aucun système réel.

## Table : `fact_commande` *(alias `cmd`)*

### Colonnes

#### `IdCommande` (STRING)
Identifiant unique d'une commande.
- Format : `CMD-` suivi de 8 chiffres
- ✅ Unicité confirmée : comptage total égal au comptage distinct.

#### `IdClient` (INTEGER)
Client ayant passé la commande.
- Jointure : `cmd.IdClient = dim_client.IdClient`

#### `IdMagasin` (INTEGER)
Magasin dans lequel la commande a été passée.
- ⚠️ Valeur sentinelle `-1` : commande passée en ligne, pas un magasin manquant.
- Jointure : `cmd.IdMagasin = dim_magasin.IdMagasin`

#### `IdPromotion` (INTEGER)
Promotion appliquée à la commande, le cas échéant.
- 🔍 Table cible non identifiée : les valeurs ne correspondent à aucune dimension documentée.

#### `MontantTTC` (NUMERIC)
Montant total de la commande, toutes taxes comprises.
- Plage valide : supérieur ou égal à 0

#### `CodeCanal` (STRING)
Canal de vente de la commande.
- Exemples : `WEB`, `MAG`
- 🔍 Liste des valeurs non vérifiée de façon exhaustive.

### Logique métier au niveau de la table

- **Grain :** une ligne par commande.
- **Règle 1 — Commandes en ligne :** `IdMagasin = -1` et `CodeCanal = WEB` décrivent la même commande web ; ne pas traiter `-1` comme une géographie à corriger.

---

## Dimension : `dim_client`

### Colonnes

#### `IdClient` (INTEGER)
Clé technique du client.

#### `Segment` (STRING)
Segment commercial du client.
- Exemples : `PARTICULIER`, `PRO`

---

## Dimension : `dim_magasin`

### Colonnes

#### `IdMagasin` (INTEGER)
Clé technique du magasin.

#### `Ville` (STRING)
Ville d'implantation du magasin.

---

## Inventaire des clés étrangères

| Table source | Colonne FK | Dimension cible | Statut |
|---|---|---|---|
| `fact_commande` | `IdClient` | `dim_client` | ✅ Documenté |
| `fact_commande` | `IdMagasin` | `dim_magasin` | ✅ Documenté |
| `fact_commande` | `IdPromotion` | — | ⚠️ Cible distincte, non identifiée |
