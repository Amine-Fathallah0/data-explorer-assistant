/**
 * schema.js — graphe interactif du schéma de données (façon graphe Obsidian).
 *
 * Tables = noeuds simples reliés par les relations (jointures). Cliquer une table affiche
 * ses colonnes dans le panneau latéral (plutôt que de la déplier dans le graphe même —
 * plus robuste et plus lisible sur un schéma à beaucoup de tables) : les colonnes utilisées
 * dans une relation y sont mises en évidence, et les flèches connectées à la table cliquée
 * sont surlignées dans le graphe pendant que le reste s'estompe (effet loupe/fisheye léger,
 * façon "focus + contexte", sans dépendre d'une extension d'expand/collapse fragile).
 * Données depuis /api/schema — dérivées de la Knowledge Base markdown (kb_schema.py).
 *
 * Un tech lead peut aussi corriger une définition de colonne directement depuis ce panneau
 * (✎ à côté du nom de colonne) : le panneau de correction inline est le même composant que
 * celui du chat (voir static/correction.js), monté ici dans #schema-correction-mount plutôt
 * que dans le panneau de source du chat — la correction sous-jacente est identique
 * (keyée sur table + colonne), seul l'endroit d'où on la déclenche change.
 */
(function () {
  cytoscape.use(cytoscapeFcose);

  const rootStyle = getComputedStyle(document.documentElement);
  const cssVar = (name, fallback) => {
    const v = rootStyle.getPropertyValue(name);
    return v && v.trim() ? v.trim() : fallback;
  };

  // Palette dérivée du système existant de l'app (custom.css), pas de couleurs ad hoc :
  // f1-amber est déjà le langage établi pour "mis en avant / important" (question-tag,
  // correction-inline, panel-label...) — on le réutilise pour la table cliquée et les
  // relations actives. bs-info marque les tables voisines : bs-primary de Boosted est en
  // réalité l'orange de marque Orange (#f16e00), quasi identique à f1-amber une fois rendu
  // — les deux se confondaient à l'écran. bs-info (bleu, inutilisé ailleurs dans l'app)
  // donne un vrai contraste chaud/froid. f1-red reste réservé à "destructif/live" (live-dot,
  // bouton supprimer) ailleurs dans l'app — pas une sémantique qui convient à une relation.
  const COLORS = {
    tableBorder: cssVar('--bs-border-color', '#dee2e6'),
    tableBg: cssVar('--bs-body-bg', '#ffffff'),
    text: cssVar('--bs-body-color', '#212529'),
    edge: cssVar('--bs-secondary-color', '#6c757d'),
    accent: cssVar('--f1-amber', '#F2A900'),
    related: cssVar('--bs-info', '#4170d8'),
  };

  const cyContainer = document.getElementById('schema-cy');
  const emptyMsg = document.getElementById('schema-empty');
  const detailsPlaceholder = document.getElementById('schema-details-placeholder');
  const detailsContent = document.getElementById('schema-details-content');
  const detailsTitle = document.getElementById('schema-details-title');
  const detailsType = document.getElementById('schema-details-type');
  const columnList = document.getElementById('schema-column-list');
  const searchInput = document.getElementById('schema-search');
  const searchSuggestions = document.getElementById('schema-search-suggestions');
  const correctionMount = document.getElementById('schema-correction-mount');
  const correctionTemplate = document.getElementById('correction-inline-template');
  const builderMount = document.getElementById('schema-builder-mount');
  const builderTemplate = document.getElementById('table-builder-template');
  const newTableBtn = document.getElementById('new-table-btn');
  const myDraftsBtn = document.getElementById('my-drafts-btn');
  const myDraftsModalEl = document.getElementById('my-drafts-modal');
  const myDraftsList = document.getElementById('my-drafts-list');

  const MAX_SEARCH_SUGGESTIONS = 20;

  // Rôle de l'utilisateur courant — seul un tech lead voit le ✎ sur une colonne (rendu
  // dynamique, dépend de currentRole ci-dessous). Les boutons "+ Nouvelle table"/"Mes
  // brouillons"/"Télémétrie" ne sont eux PAS gérés en JS : ils sont omis du HTML lui-même
  // par le serveur (voir templates/schema.html, `{% if session.get('role') == 'tech_lead' %}`)
  // pour qu'ils n'apparaissent jamais, même dans le DOM, à un rôle non autorisé — pas
  // seulement masqués visuellement. /schema n'a pas de UI de connexion propre : un tech
  // lead doit se connecter depuis le chat au préalable (le cookie de session est partagé),
  // donc l'état au chargement de CETTE page reflète toujours la session en cours.
  let currentRole = null;
  fetch('/api/session')
    .then((res) => res.json())
    .then((data) => { currentRole = data.role; })
    .catch(() => { currentRole = null; });

  // Table actuellement affichée dans le panneau (et le graphe/index qui la décrivent) —
  // mémorisés pour pouvoir rafraîchir juste la liste de colonnes après une correction,
  // sans recharger /api/schema en entier pour un seul champ modifié.
  let currentGraph = null;
  let currentRelationIndex = null;
  let currentTableName = null;
  // FK sans cible nommee (statuts ⚠️/❌ de l'inventaire), indexees table -> colonne.
  let currentUnresolvedIndex = {};

  // Identifiant du noeud fantôme d'une FK ⚠️ : une cible dont on a PROUVÉ l'existence et la
  // distinction, mais dont personne ne connaît le nom. Un noeud par (table, colonne) — deux
  // FK non identifiées ne pointent pas forcément vers la même dimension, les fusionner
  // affirmerait une chose que rien n'établit.
  const phantomId = (table, column) => `__phantom__${table}__${column}`;

  function buildElements(graph) {
    const elements = [];
    graph.tables.forEach((t) => {
      elements.push({
        group: 'nodes',
        data: { id: t.name, label: t.name, columnCount: t.columns.length },
        classes: 'table-node' + (t.documented === false ? ' undocumented-node' : ''),
      });
    });
    graph.relations.forEach((r, i) => {
      elements.push({
        group: 'edges',
        data: {
          id: `rel-${i}`,
          source: r.source,
          target: r.target,
          sourceColumn: r.source_column,
          targetColumn: r.target_column,
          // Sans colonne cible nommée, on affiche la seule colonne source plutôt qu'une
          // égalité bancale « X = undefined ».
          label: r.target_column ? `${r.source_column} = ${r.target_column}` : r.source_column,
        },
        classes: 'relation-edge',
      });
    });

    // Cas ⚠️ UNIQUEMENT. Les FK ❌ ne produisent volontairement AUCUN élément de graphe :
    // il n'y a rien vers quoi tirer une flèche, et dessiner un noeud « inconnu » pour elles
    // laisserait croire qu'une cible identifiée existe. Elles sont signalées à l'intérieur
    // du noeud de la table de faits, sur la ligne de la colonne (voir renderColumnList).
    (graph.unresolved || []).forEach((u) => {
      if (u.status !== 'distinct_unknown') return;
      const id = phantomId(u.table, u.column);
      elements.push({
        group: 'nodes',
        data: { id, label: 'cible inconnue', phantomFor: `${u.table}.${u.column}` },
        classes: 'unknown-target-node',
      });
      elements.push({
        group: 'edges',
        data: { id: `unk-${id}`, source: u.table, target: id, sourceColumn: u.column, label: u.column },
        classes: 'unknown-target-edge',
      });
    });
    return applyParallelBends(elements);
  }

  // Écartement initial des arêtes parallèles. Deux FK entre les MÊMES tables (cas
  // `IdGeoIrisEdo` et `IdGeoClient`, tous deux vers `dwh_dim_GeoIris_t`) se superposent
  // parfaitement si on les laisse droites : un seul trait visible, et deux libellés empilés
  // au même endroit — c'est ce qui rendait « IdGeoIrisEdo = idIris » illisible sur le rendu.
  // On les décale symétriquement de part et d'autre de la ligne théorique.
  const BEND_STEP = 62;

  function applyParallelBends(elements) {
    const groupes = {};
    elements.forEach((el) => {
      if (el.group !== 'edges') return;
      // Clé non orientée : deux arêtes en sens inverse entre les mêmes tables se
      // superposent tout autant que deux arêtes de même sens.
      const cle = [el.data.source, el.data.target].sort().join('::');
      (groupes[cle] = groupes[cle] || []).push(el);
    });
    Object.values(groupes).forEach((arr) => {
      arr.forEach((el, i) => {
        // Une arête seule reste parfaitement droite (bend = 0) : on ne courbe que ce qui en
        // a besoin, sinon tout le graphe ondule sans raison.
        el.data.bend = arr.length === 1 ? 0 : (i - (arr.length - 1) / 2) * BEND_STEP;
      });
    });
    return elements;
  }

  /** Recalcule l'écartement par défaut sur un graphe déjà monté — après ajout d'une arête
   *  qui peut se retrouver parallèle à une existante (publication d'un brouillon de table).
   *  N'écrase pas un écartement choisi à la main : seules les arêtes restées à leur valeur
   *  par défaut sont réparties, sinon publier une table déferait le rangement de l'utilisateur. */
  function restaggerParallelEdges(cy) {
    const vus = new Set();
    cy.edges().forEach((edge) => {
      const arr = edge.parallelEdges();
      const cle = arr.map((e) => e.id()).sort().join('|');
      if (vus.has(cle)) return;
      vus.add(cle);
      if (arr.some((e) => e.scratch('_bendManuel'))) return;
      arr.forEach((e, i) => {
        e.data('bend', arr.length === 1 ? 0 : (i - (arr.length - 1) / 2) * BEND_STEP);
      });
    });
  }

  /**
   * Rend les arêtes déplaçables à la souris : on tire un lien pour l'écarter d'un noeud ou
   * d'un libellé qui le gêne. Implémenté directement sur l'API cytoscape (point de contrôle
   * `data(bend)` d'une unbundled-bezier) plutôt qu'avec une extension d'édition de courbure,
   * qui aurait ajouté deux dépendances à vendorer pour le même résultat.
   *
   * Le déplacement est perpendiculaire à la ligne source→cible : c'est le seul degré de
   * liberté utile ici (écarter le trait), et il garde la courbe symétrique donc lisible.
   * Double-clic sur une arête : retour à l'écartement calculé par défaut.
   */
  function enableEdgeBending(cy) {
    let drag = null;

    cy.on('mousedown', 'edge', (evt) => {
      const edge = evt.target;
      drag = { edge, defaut: edge.data('bend') || 0 };
      // Sans ça, tirer une arête ferait aussi glisser tout le canevas sous le curseur.
      cy.userPanningEnabled(false);
      cy.boxSelectionEnabled(false);
      cyContainer.style.cursor = 'grabbing';
    });

    cy.on('mousemove', (evt) => {
      if (!drag) return;
      const s = drag.edge.source().position();
      const t = drag.edge.target().position();
      const dx = t.x - s.x;
      const dy = t.y - s.y;
      const longueur = Math.hypot(dx, dy) || 1;
      // Normale unitaire à la ligne, et projection du curseur dessus depuis le milieu :
      // la courbure suit exactement le doigt, sans dérive quand on bouge le long de l'axe.
      const nx = -dy / longueur;
      const ny = dx / longueur;
      const milieu = { x: (s.x + t.x) / 2, y: (s.y + t.y) / 2 };
      const p = evt.position;
      drag.edge.data('bend', (p.x - milieu.x) * nx + (p.y - milieu.y) * ny);
      drag.edge.scratch('_bendManuel', true);
    });

    const relacher = () => {
      if (!drag) return;
      drag = null;
      cy.userPanningEnabled(true);
      cy.boxSelectionEnabled(true);
      cyContainer.style.cursor = '';
    };
    cy.on('mouseup', relacher);
    // Le curseur peut quitter le canevas en plein glissement : sans ce filet, l'arête
    // resterait collée au pointeur au retour dans la zone.
    cyContainer.addEventListener('mouseleave', relacher);

    cy.on('dbltap', 'edge', (evt) => {
      const arr = evt.target.parallelEdges();
      arr.forEach((e, i) => {
        e.data('bend', arr.length === 1 ? 0 : (i - (arr.length - 1) / 2) * BEND_STEP);
        e.scratch('_bendManuel', false);
      });
    });

    cy.on('mouseover', 'edge', () => { if (!drag) cyContainer.style.cursor = 'grab'; });
    cy.on('mouseout', 'edge', () => { if (!drag) cyContainer.style.cursor = ''; });
  }

  // Index (table -> colonne -> {status, note}) des FK sans cible nommée, pour décorer les
  // lignes de colonnes du panneau latéral en O(1).
  function buildUnresolvedIndex(graph) {
    const index = {};
    (graph.unresolved || []).forEach((u) => {
      (index[u.table] = index[u.table] || {})[u.column] = u;
    });
    return index;
  }

  // Index tableName -> { asSource: Map(colonne -> relation), asTarget: Map(colonne -> relation) },
  // pour savoir en O(1) quelles colonnes d'une table sont impliquées dans une relation
  // (et vers/depuis quelle autre table) quand on affiche le panneau de détail.
  function buildRelationIndex(graph) {
    const index = {};
    graph.tables.forEach((t) => {
      index[t.name] = { asSource: new Map(), asTarget: new Map() };
    });
    graph.relations.forEach((r) => {
      index[r.source].asSource.set(r.source_column, r);
      index[r.target].asTarget.set(r.target_column, r);
    });
    return index;
  }

  const style = [
    {
      selector: 'node.table-node',
      style: {
        shape: 'round-rectangle',
        'background-color': COLORS.tableBg,
        'border-width': 2,
        'border-color': COLORS.tableBorder,
        label: 'data(label)',
        'text-valign': 'center',
        'text-halign': 'center',
        'font-family': 'Barlow Condensed, sans-serif',
        'font-weight': 600,
        'font-size': 17,
        'text-transform': 'uppercase',
        color: COLORS.text,
        // Le noeud s'adapte à son libellé au lieu d'une largeur fixe. Les noms de ce modèle
        // vont jusqu'à `dwh_fact_VenteAssuranceMateriel_t` : à 140px fixes, le texte
        // débordait largement hors du rectangle et se superposait aux arêtes voisines.
        // Pas de text-wrap : `wrap` ne coupe que sur les espaces, or ces identifiants n'en
        // ont aucun — la seule mise en forme qui marche est de laisser la boîte s'élargir.
        width: 'label',
        height: 'label',
        padding: '14px',
      },
    },
    {
      selector: 'node.table-node.schema-focused',
      style: { 'border-color': COLORS.accent, 'border-width': 3 },
    },
    {
      selector: 'node.table-node.schema-neighbor',
      style: { 'border-color': COLORS.related, 'border-width': 2 },
    },
    {
      selector: 'edge.relation-edge',
      style: {
        // unbundled-bezier + point de contrôle piloté par `data(bend)` : c'est ce qui rend
        // les arêtes déplaçables (voir enableEdgeBending) ET ce qui sépare automatiquement
        // les arêtes parallèles. `dwh_fact_Vente_t` et `dwh_fact_VenteAssuranceMateriel_t`
        // ont chacune DEUX FK vers `dwh_dim_GeoIris_t` (`IdGeoIrisEdo` et `IdGeoClient`) :
        // superposées, leurs libellés se masquaient l'un l'autre.
        'curve-style': 'unbundled-bezier',
        'control-point-distances': 'data(bend)',
        'control-point-weights': 0.5,
        width: 2,
        'line-color': COLORS.edge,
        'target-arrow-color': COLORS.edge,
        'target-arrow-shape': 'triangle',
        'arrow-scale': 1,
        label: 'data(label)',
        'font-family': 'IBM Plex Mono, monospace',
        'font-size': 13,
        'font-weight': 600,
        color: COLORS.text,
        // Lisibilité des libellés d'arête. Trois choix délibérés :
        //  - pastille opaque bordée : le texte ne se lit plus par-dessus la flèche ni
        //    par-dessus une autre arête qui passe derrière ;
        //  - PAS d'autorotate : un libellé pivoté suit la pente de la flèche et finit par
        //    la traverser en diagonale, illisible dès que deux arêtes se croisent. Un texte
        //    toujours horizontal reste lisible quel que soit l'angle ;
        //  - text-wrap/max-width : les noms de colonnes de ce modèle sont longs
        //    (`IdOrganisationO3HistoriseeTraitement`), on les passe à la ligne au lieu de
        //    les tronquer — une troncature masquerait justement le suffixe distinctif.
        'text-background-color': '#ffffff',
        'text-background-opacity': 0.96,
        'text-background-padding': 4,
        'text-background-shape': 'roundrectangle',
        'text-border-opacity': 1,
        'text-border-width': 1,
        'text-border-color': COLORS.tableBorder,
        'text-rotation': 'none',
        'text-wrap': 'wrap',
        'text-max-width': 150,
        'text-events': 'yes',
      },
    },
    // --- Cas ⚠️ : cible prouvée distincte, jamais nommée ---
    // Gris et pointillés pour ne pas se confondre avec une dimension documentée, mais bien
    // PRÉSENT : l'information « il existe une cible spécifique et différente » a été
    // établie par des tests d'orphelins, elle mérite d'être affichée.
    {
      selector: 'node.unknown-target-node',
      style: {
        shape: 'round-rectangle',
        'background-color': '#f2f2f2',
        'border-width': 2,
        'border-color': COLORS.edge,
        'border-style': 'dashed',
        label: 'data(label)',
        'text-valign': 'center',
        'text-halign': 'center',
        'font-family': 'Barlow Condensed, sans-serif',
        'font-size': 14,
        'font-style': 'italic',
        color: COLORS.edge,
        width: 120,
        height: 52,
      },
    },
    {
      selector: 'edge.unknown-target-edge',
      style: {
        'curve-style': 'unbundled-bezier',
        'control-point-distances': 'data(bend)',
        'control-point-weights': 0.5,
        width: 2,
        'line-style': 'dashed',
        'line-color': COLORS.edge,
        'target-arrow-color': COLORS.edge,
        'target-arrow-shape': 'triangle',
        'arrow-scale': 1,
        label: 'data(label)',
        'font-family': 'IBM Plex Mono, monospace',
        'font-size': 12,
        color: COLORS.edge,
        'text-background-color': '#ffffff',
        'text-background-opacity': 0.96,
        'text-background-padding': 4,
        'text-background-shape': 'roundrectangle',
        'text-rotation': 'none',
        'text-wrap': 'wrap',
        'text-max-width': 150,
      },
    },
    // Table connue seulement par l'inventaire des FK (aucun bloc `## Table :` la décrivant) :
    // la jointure est confirmée, la documentation de la cible manque. Bordure pointillée
    // discrète — à ne pas confondre avec le gris du cas ⚠️, où c'est la CIBLE qui est inconnue.
    {
      selector: 'node.table-node.undocumented-node',
      style: { 'border-style': 'dotted', 'border-color': COLORS.edge },
    },
    {
      selector: 'edge.relation-edge.schema-relation-highlighted',
      style: {
        'line-color': COLORS.accent,
        'target-arrow-color': COLORS.accent,
        color: '#8a5c00',
        width: 3.5,
        'z-index': 10,
      },
    },
    {
      selector: '.schema-dimmed',
      style: { opacity: 0.15 },
    },
    {
      selector: '.schema-search-match',
      style: { 'border-color': COLORS.accent, 'border-width': 3 },
    },
    // Table en cours de création (feature "+ Nouvelle table") : pointillés pour la
    // distinguer sans ambiguïté d'une table déjà publiée, tant qu'elle n'existe encore
    // que dans le brouillon en cours d'édition.
    {
      selector: 'node.table-node.draft-node',
      style: { 'border-style': 'dashed', 'border-color': COLORS.accent, 'border-width': 3 },
    },
    {
      selector: 'edge.draft-relation-edge',
      style: {
        'line-style': 'dashed',
        'line-color': COLORS.accent,
        'target-arrow-color': COLORS.accent,
        'target-arrow-shape': 'triangle',
        'font-family': 'IBM Plex Mono, monospace',
        'font-size': 12,
        color: COLORS.text,
        'text-background-color': '#ffffff',
        'text-background-opacity': 1,
        'text-background-padding': 3,
      },
    },
  ];

  // ---------- Panneau de correction (colonne, tech lead uniquement) ----------

  const correctionMountNode = correctionTemplate.content.firstElementChild.cloneNode(true);
  correctionMount.appendChild(correctionMountNode);

  // Noeud du panneau de création de table — cloné ici (pas besoin de cy/graph pour ça),
  // mais createTableBuilder() n'est appelé qu'une fois le graphe cytoscape prêt (voir
  // initGraph plus bas) puisqu'il en a besoin pour placer/relier le noeud brouillon.
  const builderMountNode = builderTemplate.content.firstElementChild.cloneNode(true);
  builderMount.appendChild(builderMountNode);
  let tableBuilder = null;

  function showColumnListView() {
    correctionMountNode.classList.add('hidden');
    builderMountNode.classList.add('hidden');
    if (currentTableName) {
      detailsContent.classList.remove('hidden');
      detailsPlaceholder.classList.add('hidden');
    } else {
      clearDetails();
    }
  }

  const correctionPanel = createCorrectionPanel(correctionMountNode, {
    onClose: showColumnListView,
    onApplied: () => {
      if (currentGraph && currentRelationIndex && currentTableName) {
        renderColumnList(currentGraph, currentRelationIndex, currentTableName);
      }
    },
  });

  function openColumnCorrection(tableName, colonneRaw) {
    detailsContent.classList.add('hidden');
    detailsPlaceholder.classList.add('hidden');
    correctionPanel.open(tableName, colonneRaw);
  }

  function openTableBuilder(draft) {
    if (!tableBuilder) return;
    detailsContent.classList.add('hidden');
    detailsPlaceholder.classList.add('hidden');
    tableBuilder.open(draft);
  }

  // newTableBtn n'existe dans le DOM que pour un tech lead (voir templates/schema.html) —
  // absent (null) pour tout autre rôle.
  if (newTableBtn) newTableBtn.addEventListener('click', () => openTableBuilder());

  // ---------- Mes brouillons (reprendre / supprimer) ----------

  const myDraftsModal = new boosted.Modal(myDraftsModalEl);

  async function loadMyDrafts() {
    myDraftsList.innerHTML = '<p class="text-secondary small mb-0">Chargement...</p>';
    try {
      const res = await fetch('/api/tables/drafts');
      const drafts = await res.json();
      renderMyDrafts(drafts);
    } catch (err) {
      myDraftsList.innerHTML = '<p class="text-danger small mb-0">Connexion au serveur impossible.</p>';
    }
  }

  function renderMyDrafts(drafts) {
    myDraftsList.innerHTML = '';
    if (!drafts.length) {
      myDraftsList.innerHTML = '<p class="text-secondary small mb-0">Aucun brouillon pour l\'instant.</p>';
      return;
    }

    const list = document.createElement('div');
    list.className = 'list-group';

    drafts.forEach((d) => {
      const isPublished = d.status === 'published';
      const item = document.createElement('div');
      item.className = 'list-group-item d-flex align-items-start justify-content-between gap-2';
      item.innerHTML = `
        <div class="min-width-0">
          <code>${d.table_name || '(sans nom)'}</code>
          ${isPublished ? '<span class="badge text-bg-secondary ms-2">Publié</span>' : ''}
          <p class="text-secondary small mb-0 mt-1">${d.payload.columns.length} colonne(s)</p>
        </div>
        <div class="d-flex gap-2 flex-shrink-0">
          ${isPublished ? '' : '<button type="button" class="btn btn-outline-secondary btn-sm" data-draft-resume>Reprendre</button>'}
          ${isPublished ? '' : '<button type="button" class="btn btn-outline-secondary btn-sm" data-draft-delete>Supprimer</button>'}
        </div>`;

      const resumeBtn = item.querySelector('[data-draft-resume]');
      if (resumeBtn) {
        resumeBtn.addEventListener('click', () => {
          myDraftsModal.hide();
          openTableBuilder(d);
        });
      }
      const deleteBtn = item.querySelector('[data-draft-delete]');
      if (deleteBtn) {
        deleteBtn.addEventListener('click', async () => {
          if (!confirm(`Supprimer le brouillon « ${d.table_name || '(sans nom)'} » ?`)) return;
          await fetch(`/api/tables/drafts/${d.id}`, { method: 'DELETE' });
          loadMyDrafts();
        });
      }
      list.appendChild(item);
    });

    myDraftsList.appendChild(list);
  }

  // Même chose pour myDraftsBtn : absent du DOM pour tout rôle non-tech-lead.
  if (myDraftsBtn) {
    myDraftsBtn.addEventListener('click', () => {
      myDraftsModal.show();
      loadMyDrafts();
    });
  }

  // ---------- Propositions de définition (colonnes ⚠️ / ❌) ----------

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (ch) => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
    ));
  }

  /** Une proposition affichée : toujours accompagnée de sa provenance et de son statut.
   *  Le badge « non vérifié » n'est pas décoratif — c'est ce qui distingue une hypothèse
   *  d'utilisateur du contenu vérifié de la Knowledge Base, et il ne disparaît qu'une fois
   *  la proposition validée par un tech lead. */
  function propositionCard(p) {
    const el = document.createElement('div');
    el.className = 'schema-proposition schema-proposition-' + p.statut;
    const badge = p.statut === 'validee'
      ? `<span class="schema-prop-badge schema-prop-badge-ok">✓ Validée par ${escapeHtml(p.valide_par)}</span>`
      : p.statut === 'rejetee'
        ? `<span class="schema-prop-badge schema-prop-badge-ko">Rejetée</span>`
        : `<span class="schema-prop-badge schema-prop-badge-pending">Proposé par ${escapeHtml(p.auteur)}, non vérifié</span>`;
    el.innerHTML = `${badge}<p class="schema-prop-text">${escapeHtml(p.texte)}</p>`;

    if (currentRole === 'tech_lead' && p.statut === 'en_attente') {
      const actions = document.createElement('div');
      actions.className = 'schema-prop-actions';
      [['valider', 'Valider', 'btn-outline-success'], ['rejeter', 'Rejeter', 'btn-outline-secondary']]
        .forEach(([action, label, cls]) => {
          const btn = document.createElement('button');
          btn.type = 'button';
          btn.className = `btn btn-sm ${cls}`;
          btn.textContent = label;
          btn.addEventListener('click', async () => {
            btn.disabled = true;
            const res = await fetch(`/api/propositions/${p.id}/${action}`, { method: 'POST' });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
              btn.disabled = false;
              const err = document.createElement('p');
              err.className = 'schema-prop-error';
              err.textContent = data.error || 'Échec.';
              el.appendChild(err);
              return;
            }
            // Une validation réécrit le markdown ET Qdrant : le graphe lui-même ne change
            // pas, mais la définition de la colonne si. On recharge la liste seule.
            refreshPropositions(el.closest('.schema-prop-zone'), p.table_name, p.colonne);
          });
          actions.appendChild(btn);
        });
      el.appendChild(actions);
    }
    return el;
  }

  async function refreshPropositions(zone, tableName, columnName) {
    if (!zone) return;
    const list = zone.querySelector('.schema-prop-list');
    list.innerHTML = '';
    try {
      const res = await fetch(
        `/api/propositions?table=${encodeURIComponent(tableName)}&colonne=${encodeURIComponent(columnName)}`
      );
      const props = res.ok ? await res.json() : [];
      if (!props.length) {
        const p = document.createElement('p');
        p.className = 'schema-prop-empty';
        p.textContent = 'Aucune proposition pour cette colonne.';
        list.appendChild(p);
      } else {
        props.forEach((p) => list.appendChild(propositionCard(p)));
      }
    } catch (e) {
      /* réseau : on laisse la zone vide plutôt que d'afficher une erreur bloquante */
    }
  }

  /** Zone dépliable sous une colonne non résolue : propositions existantes + formulaire. */
  function buildPropositionZone(tableName, columnName, unresolved) {
    const zone = document.createElement('div');
    zone.className = 'schema-prop-zone hidden';

    const explain = document.createElement('p');
    explain.className = 'schema-prop-explain';
    explain.textContent = unresolved.status === 'distinct_unknown'
      ? "Cible confirmée distincte des dimensions connues, mais jamais identifiée. Une définition proposée ici n'entrera dans la Knowledge Base qu'après validation."
      : "Aucune dimension cible identifiée pour cette colonne. Une définition proposée ici n'entrera dans la Knowledge Base qu'après validation.";
    zone.appendChild(explain);

    const list = document.createElement('div');
    list.className = 'schema-prop-list';
    zone.appendChild(list);

    const form = document.createElement('form');
    form.className = 'schema-prop-form';
    form.innerHTML = `
      <textarea class="form-control form-control-sm" rows="3"
                placeholder="Proposer une définition pour ${escapeHtml(columnName)}…"></textarea>
      <button class="btn btn-sm btn-outline-primary" type="submit">Proposer</button>
      <p class="schema-prop-error hidden"></p>`;
    const textarea = form.querySelector('textarea');
    const errEl = form.querySelector('.schema-prop-error');

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const texte = textarea.value.trim();
      if (!texte) return;
      errEl.classList.add('hidden');
      const res = await fetch('/api/propositions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ table: tableName, colonne: columnName, texte }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        errEl.textContent = data.error || 'Échec de l’envoi.';
        errEl.classList.remove('hidden');
        return;
      }
      textarea.value = '';
      refreshPropositions(zone, tableName, columnName);
    });

    zone.appendChild(form);
    return zone;
  }

  function renderColumnList(graph, relationIndex, tableName) {
    const table = graph.tables.find((t) => t.name === tableName);
    if (!table) return;

    currentGraph = graph;
    currentRelationIndex = relationIndex;
    currentTableName = tableName;

    detailsPlaceholder.classList.add('hidden');
    detailsContent.classList.remove('hidden');
    detailsTitle.textContent = table.name;
    detailsType.textContent = `${table.columns.length} colonne${table.columns.length > 1 ? 's' : ''}`;

    const rel = relationIndex[tableName];
    columnList.innerHTML = '';

    table.columns.forEach((c) => {
      const asSourceRel = rel.asSource.get(c.name);
      const asTargetRel = rel.asTarget.get(c.name);

      const row = document.createElement('div');
      row.className = 'schema-column-row' + (asSourceRel || asTargetRel ? ' schema-column-row-fk' : '');

      const head = document.createElement('div');
      head.className = 'schema-column-row-head';
      const nameEl = document.createElement('span');
      nameEl.className = 'schema-column-name';
      nameEl.textContent = c.name;
      head.appendChild(nameEl);

      if (currentRole === 'tech_lead' && c.colonne_raw) {
        const editBtn = document.createElement('button');
        editBtn.type = 'button';
        editBtn.className = 'schema-column-edit-btn';
        editBtn.title = 'Corriger cette colonne';
        editBtn.setAttribute('aria-label', `Corriger ${c.name}`);
        editBtn.textContent = '✎';
        editBtn.addEventListener('click', () => openColumnCorrection(tableName, c.colonne_raw));
        head.appendChild(editBtn);
      }

      const typeEl = document.createElement('span');
      typeEl.className = 'schema-column-type';
      typeEl.textContent = c.type || '';
      head.appendChild(typeEl);
      row.appendChild(head);

      if (c.description) {
        const descEl = document.createElement('p');
        descEl.className = 'schema-column-desc';
        descEl.textContent = c.description;
        row.appendChild(descEl);
      }
      if (asSourceRel) {
        const relEl = document.createElement('span');
        relEl.className = 'schema-column-rel';
        relEl.textContent = `→ ${asSourceRel.target}.${asSourceRel.target_column}`;
        row.appendChild(relEl);
      }
      if (asTargetRel) {
        const relEl = document.createElement('span');
        relEl.className = 'schema-column-rel';
        relEl.textContent = `← référencée par ${asTargetRel.source}.${asTargetRel.source_column}`;
        row.appendChild(relEl);
      }

      // FK dont la cible n'est pas nommée : le marqueur vit ICI, sur la ligne de la colonne,
      // à l'intérieur de la table de faits — pas sous forme de noeud dans le graphe, puisqu'il
      // n'y a aucune cible vers laquelle tirer une flèche.
      const unresolved = (currentUnresolvedIndex[tableName] || {})[c.name];
      if (unresolved) {
        const distinct = unresolved.status === 'distinct_unknown';
        row.classList.add(distinct ? 'schema-column-row-distinct' : 'schema-column-row-unknown');

        const badge = document.createElement('button');
        badge.type = 'button';
        badge.className = 'schema-fk-badge ' + (distinct ? 'schema-fk-badge-distinct' : 'schema-fk-badge-unknown');
        badge.textContent = distinct ? '⚠' : '?';
        badge.title = distinct
          ? 'Cible confirmée distincte, jamais identifiée — proposer une définition'
          : 'Dimension cible inconnue — proposer une définition';
        badge.setAttribute('aria-label', badge.title);
        head.insertBefore(badge, typeEl);

        const zone = buildPropositionZone(tableName, c.name, unresolved);
        row.appendChild(zone);
        badge.addEventListener('click', () => {
          const opening = zone.classList.contains('hidden');
          zone.classList.toggle('hidden', !opening);
          badge.classList.toggle('schema-fk-badge-open', opening);
          if (opening) refreshPropositions(zone, tableName, c.name);
        });
      }

      columnList.appendChild(row);
    });
  }

  function highlightColumnRow(columnName) {
    const rows = columnList.querySelectorAll('.schema-column-row');
    rows.forEach((row) => {
      const nameEl = row.querySelector('.schema-column-name');
      if (!nameEl || nameEl.textContent !== columnName) return;
      row.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      row.classList.add('schema-column-row-flash');
      setTimeout(() => row.classList.remove('schema-column-row-flash'), 1200);
    });
  }

  function clearDetails() {
    // Ne pas révéler le placeholder derrière un panneau déjà ouvert (correction ou
    // création de table) — sinon un tap sur le canevas vide pendant qu'un de ces
    // panneaux est ouvert ferait apparaître le placeholder en double-fond.
    if (correctionPanel.isOpen() || (tableBuilder && tableBuilder.isOpen())) return;
    detailsContent.classList.add('hidden');
    detailsPlaceholder.classList.remove('hidden');
    currentTableName = null;
  }

  fetch('/api/schema')
    .then((res) => {
      if (!res.ok) throw new Error('bad status');
      return res.json();
    })
    .then((graph) => {
      if (!graph.tables || !graph.tables.length) {
        emptyMsg.textContent = 'Aucune table à afficher dans la Knowledge Base.';
        emptyMsg.classList.remove('hidden');
        return;
      }
      initGraph(graph);
    })
    .catch((err) => {
      // Remonter l'erreur en console. Ce catch avalait silencieusement toute exception de
      // initGraph : une simple ReferenceError dans le code de rendu s'affichait comme
      // « Impossible de charger le schéma », ce qui envoie chercher la panne du côté de
      // l'API alors que le serveur répond parfaitement. Le message distingue désormais les
      // deux cas, et la trace reste consultable.
      console.error('[schema] échec du rendu du graphe :', err);
      emptyMsg.textContent = 'Impossible d’afficher le schéma (voir la console).';
      emptyMsg.classList.remove('hidden');
    });

  // Position de repli pour une table nouvellement fusionnée dans le graphe qui n'est PAS
  // celle que le tech lead venait de publier (cas marginal : une autre publication
  // concurrente serait apparue entre-temps) — centre de la zone visible, cohérent avec
  // table_builder.js's draftNodeCenterPosition() pour un nouveau noeud brouillon.
  function draftFallbackPosition(cy) {
    const ext = cy.extent();
    return { x: (ext.x1 + ext.x2) / 2, y: (ext.y1 + ext.y2) / 2 };
  }

  function initGraph(graph) {
    let relationIndex = buildRelationIndex(graph);
    currentUnresolvedIndex = buildUnresolvedIndex(graph);

    const cy = cytoscape({
      container: cyContainer,
      elements: buildElements(graph),
      style,
      wheelSensitivity: 3,
      minZoom: 0.15,
      maxZoom: 2.5,
    });

    cy.layout({
      name: 'fcose',
      animate: false,
      randomize: true,
      fit: true,
      padding: 60,
      nodeDimensionsIncludeLabels: true,
      nodeRepulsion: 9000,
      idealEdgeLength: 160,
    }).run();

    // ---------- Panneau de création de table (voir static/table_builder.js) ----------
    // Construit ici (pas plus haut) : le builder a besoin de `cy` pour placer/relier le
    // noeud brouillon, et de `graph` pour peupler le mode liaison avec les tables réelles.

    tableBuilder = createTableBuilder(builderMountNode, cy, graph, {
      onClose: showColumnListView,
      onPublished: reloadGraphPreservingLayout,
    });

    // Après publication : fusionne la/les table(s) et relation(s) nouvellement publiées
    // dans le graphe existant SANS relancer le layout fcose (qui redistribuerait tous les
    // noeuds déjà arrangés par le tech lead — une régression visuelle juste au moment du
    // succès). Le nouveau noeud est placé exactement là où était le noeud brouillon.
    function reloadGraphPreservingLayout({ tableName, position }) {
      fetch('/api/schema')
        .then((res) => res.json())
        .then((freshGraph) => {
          const existingNames = new Set(graph.tables.map((t) => t.name));
          const newTables = freshGraph.tables.filter((t) => !existingNames.has(t.name));

          newTables.forEach((t) => {
            graph.tables.push(t);
            const pos = t.name === tableName ? position : draftFallbackPosition(cy);
            cy.add({ group: 'nodes', data: { id: t.name, label: t.name, columnCount: t.columns.length }, classes: 'table-node', position: pos });
          });

          const existingRelKeys = new Set(
            graph.relations.map((r) => `${r.source}.${r.source_column}=${r.target}.${r.target_column}`)
          );
          const newRelations = freshGraph.relations.filter(
            (r) => !existingRelKeys.has(`${r.source}.${r.source_column}=${r.target}.${r.target_column}`)
          );
          newRelations.forEach((r) => {
            graph.relations.push(r);
            cy.add({
              group: 'edges',
              data: {
                id: `rel-${graph.relations.length}-${r.source}-${r.target}`,
                source: r.source, target: r.target,
                sourceColumn: r.source_column, targetColumn: r.target_column,
                label: r.target_column ? `${r.source_column} = ${r.target_column}` : r.source_column,
                bend: 0,
              },
              classes: 'relation-edge',
            });
          });
          // Re-répartir : la nouvelle arête peut être parallèle à une existante.
          restaggerParallelEdges(cy);

          relationIndex = buildRelationIndex(graph);
          currentUnresolvedIndex = buildUnresolvedIndex(graph);
        })
        .catch(() => { /* la table est déjà publiée côté serveur — un échec ici n'est qu'un raffraîchissement manqué */ });
    }

    function focusTable(tableName) {
      // Le panneau latéral est occupé par un panneau de correction/création ouvert :
      // ne pas le faire disparaître sous un autre contenu tant qu'il n'est pas fermé.
      if (correctionPanel.isOpen() || (tableBuilder && tableBuilder.isOpen())) return;
      const node = cy.getElementById(tableName);
      if (node.empty()) return;
      // Les noeuds fantomes (cible ⚠️ inconnue) n'ont ni colonnes ni fiche : on les
      // met en evidence dans le graphe, mais le panneau lateral reste sur la table
      // de faits d'ou part la FK — c'est la seule entite reellement documentee.
      if (node.hasClass('unknown-target-node')) {
        cy.elements().removeClass('schema-dimmed schema-focused schema-neighbor schema-relation-highlighted schema-search-match');
        cy.elements().difference(node.closedNeighborhood()).addClass('schema-dimmed');
        node.addClass('schema-focused');
        return;
      }

      const neighborhood = node.closedNeighborhood();

      cy.elements().removeClass(
        'schema-dimmed schema-focused schema-neighbor schema-relation-highlighted schema-search-match'
      );
      cy.elements().difference(neighborhood).addClass('schema-dimmed');
      node.addClass('schema-focused');
      node.connectedEdges().addClass('schema-relation-highlighted');
      neighborhood.nodes().difference(node).addClass('schema-neighbor');

      // Effet loupe léger : recadre sur la table cliquée et son voisinage direct plutôt que
      // sur tout le graphe — donne une sensation de "focus" façon fisheye sans avoir à
      // déplacer individuellement chaque noeud voisin (coûteux et fragile à grande échelle).
      // Durée courte et easing "out" : un recadrage doit se sentir immédiat, pas flotter.
      cy.stop(true);
      cy.animate({ fit: { eles: neighborhood, padding: 90 }, duration: 150, easing: 'ease-out' });

      renderColumnList(graph, relationIndex, tableName);
    }

    function clearFocus() {
      cy.elements().removeClass(
        'schema-dimmed schema-focused schema-neighbor schema-relation-highlighted'
      );
      cy.stop(true);
      cy.animate({ fit: { eles: cy.elements(), padding: 60 }, duration: 150, easing: 'ease-out' });
      clearDetails();
    }

    // Après la création de `cy` — l'appeler plus haut lèverait un ReferenceError (zone
    // morte du `const cy`), attrapé par le .catch du fetch, qui affichait « Impossible de
    // charger le schéma » alors que l'API répondait parfaitement.
    enableEdgeBending(cy);

    cy.on('tap', 'node.table-node', (evt) => focusTable(evt.target.id()));
    cy.on('tap', (evt) => {
      if (evt.target === cy) clearFocus();
    });

    let searchTimer = null;
    searchInput.addEventListener('input', () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => {
        runSearch(cy, graph, searchInput.value.trim(), (tableName, columnName) => {
          focusTable(tableName);
          if (columnName) highlightColumnRow(columnName);
        });
      }, 150);
    });

    searchInput.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') searchSuggestions.classList.add('hidden');
    });
    searchInput.addEventListener('blur', () => {
      // Délai court : laisse le mousedown d'une suggestion s'exécuter avant la fermeture
      // (voir renderSearchSuggestions — un click seul arriverait après le blur).
      setTimeout(() => searchSuggestions.classList.add('hidden'), 120);
    });
  }

  // Recherche : au lieu de seulement recadrer/surligner les tables qui matchent, affiche
  // aussi la liste de ce qui a matché (tables ET colonnes individuelles) sous le champ —
  // sur un schéma à beaucoup de tables, deviner quel noeud a matché en zoomant seul est
  // un cycle essai-erreur. `onSelect(tableName, columnName|null)` est appelé au clic sur
  // une suggestion (columnName seulement pour un match de colonne, pour surligner la ligne
  // correspondante une fois le panneau de la table ouvert).
  function runSearch(cy, graph, query, onSelect) {
    cy.elements().removeClass(
      'schema-dimmed schema-focused schema-neighbor schema-relation-highlighted schema-search-match'
    );
    if (!query) {
      searchSuggestions.classList.add('hidden');
      clearDetails();
      return;
    }

    const needle = query.toLowerCase();
    const suggestions = [];
    const matchingTableNames = [];

    graph.tables.forEach((t) => {
      const tableMatches = t.name.toLowerCase().includes(needle);
      if (tableMatches) {
        matchingTableNames.push(t.name);
        suggestions.push({ kind: 'table', table: t.name });
      }
      t.columns.forEach((c) => {
        if (c.name.toLowerCase().includes(needle)) {
          if (!tableMatches) matchingTableNames.push(t.name);
          suggestions.push({ kind: 'column', table: t.name, column: c.name, type: c.type });
        }
      });
    });

    renderSearchSuggestions(suggestions.slice(0, MAX_SEARCH_SUGGESTIONS), onSelect);

    if (!matchingTableNames.length) return;

    const matches = cy.collection(matchingTableNames.map((name) => cy.getElementById(name)));
    matches.addClass('schema-search-match');
    cy.stop(true);
    cy.animate({ fit: { eles: matches, padding: 80 }, duration: 150, easing: 'ease-out' });
  }

  function renderSearchSuggestions(suggestions, onSelect) {
    searchSuggestions.innerHTML = '';
    if (!suggestions.length) {
      searchSuggestions.classList.add('hidden');
      return;
    }

    suggestions.forEach((s) => {
      const item = document.createElement('button');
      item.type = 'button';
      item.className = 'schema-search-suggestion';
      if (s.kind === 'table') {
        item.innerHTML = `<span class="schema-search-suggestion-table">${s.table}</span>`;
      } else {
        item.innerHTML =
          `<span class="schema-search-suggestion-table">${s.table}</span>` +
          `<span class="schema-search-suggestion-sep">›</span>` +
          `<span class="schema-search-suggestion-column">${s.column}</span>` +
          (s.type ? `<span class="schema-search-suggestion-type">${s.type}</span>` : '');
      }
      // mousedown + preventDefault (pas click) : s'exécute avant le blur du champ de
      // recherche, qui sinon fermerait ce dropdown avant que le clic ne l'atteigne.
      item.addEventListener('mousedown', (e) => {
        e.preventDefault();
        searchSuggestions.classList.add('hidden');
        onSelect(s.table, s.kind === 'column' ? s.column : null);
      });
      searchSuggestions.appendChild(item);
    });

    searchSuggestions.classList.remove('hidden');
  }
})();
