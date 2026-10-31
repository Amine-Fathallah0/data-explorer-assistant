/**
 * table_builder.js — panneau de création de table, monté dans #schema-builder-mount
 * (voir schema.js). Une table en cours de création est aussi placée comme noeud (en
 * pointillés, classe .draft-node) directement sur le graphe cytoscape déjà affiché sur
 * /schema : ses relations se choisissent en cliquant une table/colonne RÉELLE sur ce
 * graphe (mode liaison), jamais via un champ texte libre — la cible d'une clé étrangère
 * doit toujours être une table/colonne déjà publiée (revalidé côté serveur par
 * kb_create.validate_table_spec).
 *
 * Publication en deux temps, comme le flux prepare/apply de correction.js : "Aperçu
 * Markdown" appelle /api/tables/prepare (dry-run, aucune écriture) ; "Enregistrer et
 * publier" enchaîne un enregistrement du brouillon puis /api/tables/publish, pour que la
 * publication ne puisse jamais publier une édition non sauvegardée.
 */

function _tbEscapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

function _tbUuid() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
  // Repli simple si crypto.randomUUID indisponible (contexte non sécurisé) — un id de
  // corrélation client, jamais persisté tel quel comme identifiant serveur.
  return "id-" + Math.random().toString(36).slice(2) + Date.now().toString(36);
}

/**
 * Monte un panneau de création dans `rootEl` (le noeud cloné depuis le <template> hôte,
 * déjà inséré dans le DOM). `cy` : l'instance cytoscape déjà initialisée par schema.js.
 * `graph` : le graphe {tables, relations} déjà chargé (pour peupler le mode liaison).
 * `onClose()` : appelé à chaque fermeture (Fermer, ✕, ou après publication) — laisse la
 * page hôte revenir à sa vue de lecture. `onPublished({tableName, position})` : appelé
 * juste après une publication réussie et le nettoyage du noeud brouillon, `position`
 * étant la position écran du noeud brouillon au moment de la publication — pour que la
 * page hôte puisse y placer le vrai noeud sans relancer tout le layout (voir schema.js).
 * Renvoie { open(draft), close(), isOpen() }.
 */
function createTableBuilder(rootEl, cy, graph, { onPublished, onClose } = {}) {
  const closeBtn = rootEl.querySelector("[data-tb-close]");
  const closeBtn2 = rootEl.querySelector("[data-tb-close-btn]");
  const nameInput = rootEl.querySelector("[data-tb-name]");
  const datasetSelect = rootEl.querySelector("[data-tb-dataset]");
  const grainInput = rootEl.querySelector("[data-tb-grain]");
  const columnsWrap = rootEl.querySelector("[data-tb-columns]");
  const addColumnBtn = rootEl.querySelector("[data-tb-add-column]");
  const rulesWrap = rootEl.querySelector("[data-tb-rules]");
  const addRuleBtn = rootEl.querySelector("[data-tb-add-rule]");
  const linkBanner = rootEl.querySelector("[data-tb-link-banner]");
  const linkCancelBtn = rootEl.querySelector("[data-tb-link-cancel]");
  const targetPicker = rootEl.querySelector("[data-tb-target-picker]");
  const previewBtn = rootEl.querySelector("[data-tb-preview]");
  const previewOutput = rootEl.querySelector("[data-tb-preview-output]");
  const errorEl = rootEl.querySelector("[data-tb-error]");
  const successEl = rootEl.querySelector("[data-tb-success]");
  const saveBtn = rootEl.querySelector("[data-tb-save]");
  const publishBtn = rootEl.querySelector("[data-tb-publish]");

  const columnRowTemplate = document.getElementById("table-builder-column-row-template");
  const ruleRowTemplate = document.getElementById("table-builder-rule-row-template");

  let draftState = null;   // { id, name, grain, columns: [...], rules: [...] }
  let draftNodeId = null;  // id du noeud cytoscape du brouillon, stable tant que le panneau est ouvert
  let mode = "idle";       // "idle" | "linking"
  let linkingLocalId = null;

  // Datasets d'accueil possibles, chargés une fois à l'ouverture du panneau. La Knowledge
  // Base a un sous-dossier par dataset : le serveur exige donc de savoir dans lequel écrire
  // (kb_create.resolve_dataset_source), il n'y a plus de fichier unique par défaut.
  let datasets = [];

  async function loadDatasets() {
    if (datasets.length) return;
    try {
      const res = await fetch("/api/datasets");
      if (res.ok) datasets = await res.json();
    } catch (e) {
      datasets = [];
    }
    datasetSelect.innerHTML = datasets
      .map((d) => `<option value="${_tbEscapeHtml(d.dataset)}">${_tbEscapeHtml(d.dataset)}</option>`)
      .join("");
    if (draftState) datasetSelect.value = draftState.dataset || (datasets[0] && datasets[0].dataset) || "";
  }

  function freshState() {
    return { id: null, name: "", dataset: "", grain: "", columns: [], rules: [] };
  }

  function freshColumn() {
    return {
      localId: _tbUuid(), name: "", type: "", description: "",
      examples: [], format: "", valid_range: "", warning: "", fk: null,
    };
  }

  function freshRule() {
    return { localId: _tbUuid(), title: "", text: "" };
  }

  // ---------- Construction de la spec envoyée au serveur ----------

  function buildSpecPayload() {
    return {
      name: draftState.name,
      dataset: draftState.dataset,
      grain: draftState.grain,
      columns: draftState.columns.map((c) => ({
        name: c.name, type: c.type, description: c.description,
        examples: c.examples, format: c.format, valid_range: c.valid_range,
        warning: c.warning, fk: c.fk,
      })),
      rules: draftState.rules.map((r) => ({ title: r.title, text: r.text })),
    };
  }

  // ---------- Cytoscape : noeud brouillon + arêtes de relation en aperçu ----------

  function draftNodeCenterPosition() {
    const ext = cy.extent();
    return { x: (ext.x1 + ext.x2) / 2, y: (ext.y1 + ext.y2) / 2 };
  }

  function addDraftNode() {
    cy.add({
      group: "nodes",
      data: { id: draftNodeId, label: draftState.name || "(nouvelle table)" },
      classes: "table-node draft-node",
      position: draftNodeCenterPosition(),
    });
  }

  function updateDraftNodeLabel() {
    const node = cy.getElementById(draftNodeId);
    if (!node.empty()) node.data("label", draftState.name || "(nouvelle table)");
  }

  function fkEdgeId(localId) {
    return `draft-fk-${localId}`;
  }

  function drawFkEdge(col) {
    const edgeId = fkEdgeId(col.localId);
    const existing = cy.getElementById(edgeId);
    if (!existing.empty()) existing.remove();
    if (!col.fk) return;
    const targetNode = cy.getElementById(col.fk.table);
    if (targetNode.empty()) return; // table cible pas visible sur ce graphe (ne devrait pas arriver)
    cy.add({
      group: "edges",
      data: {
        id: edgeId, source: draftNodeId, target: col.fk.table,
        label: `${col.name || "?"} = ${col.fk.column}`,
      },
      classes: "draft-relation-edge",
    });
  }

  function removeFkEdge(localId) {
    const edge = cy.getElementById(fkEdgeId(localId));
    if (!edge.empty()) edge.remove();
  }

  function removeDraftNodeAndEdges() {
    if (!draftNodeId) return;
    draftState.columns.forEach((c) => removeFkEdge(c.localId));
    const node = cy.getElementById(draftNodeId);
    if (!node.empty()) node.remove();
  }

  // Handler persistant, ajouté une seule fois à la construction du panneau (pas à chaque
  // entrée/sortie du mode liaison) : gated par `mode`, plus simple et plus sûr qu'un
  // ajout/retrait dynamique du listener sur chaque bascule (Échap, annuler, cible choisie
  // sont trois sorties différentes du mode liaison — un retrait manqué sur l'une d'elles
  // ferait fuiter des listeners qui s'accumulent silencieusement).
  cy.on("tap", "node.table-node:not(.draft-node)", (evt) => {
    if (mode !== "linking") return;
    openTargetPicker(evt.target.id());
  });

  // ---------- Mode liaison ----------

  function enterLinkingMode(localId) {
    mode = "linking";
    linkingLocalId = localId;
    linkBanner.classList.remove("hidden");
    columnsWrap.classList.add("hidden");
    addColumnBtn.classList.add("hidden");
    cy.container().classList.add("schema-linking-mode");
  }

  function exitLinkingMode() {
    mode = "idle";
    linkingLocalId = null;
    linkBanner.classList.add("hidden");
    targetPicker.classList.add("hidden");
    targetPicker.innerHTML = "";
    columnsWrap.classList.remove("hidden");
    addColumnBtn.classList.remove("hidden");
    cy.container().classList.remove("schema-linking-mode");
  }

  function openTargetPicker(tableName) {
    const table = graph.tables.find((t) => t.name === tableName);
    if (!table) return;

    columnsWrap.classList.add("hidden");
    addColumnBtn.classList.add("hidden");
    targetPicker.innerHTML = "";

    const heading = document.createElement("p");
    heading.className = "small mb-2";
    heading.innerHTML = `Lier vers une colonne de <code>${_tbEscapeHtml(tableName)}</code> :`;
    targetPicker.appendChild(heading);

    table.columns.forEach((col) => {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "table-builder-target-column";
      item.innerHTML =
        `<span class="table-builder-target-column-name">${_tbEscapeHtml(col.name)}</span>` +
        `<span class="table-builder-target-column-type">${_tbEscapeHtml(col.type || "")}</span>`;
      item.addEventListener("click", () => pickTargetColumn(tableName, col.name));
      targetPicker.appendChild(item);
    });

    targetPicker.classList.remove("hidden");
  }

  function pickTargetColumn(tableName, columnName) {
    const col = draftState.columns.find((c) => c.localId === linkingLocalId);
    if (col) {
      col.fk = { table: tableName, column: columnName };
      drawFkEdge(col);
      renderColumnRow(col);
    }
    exitLinkingMode();
  }

  linkCancelBtn.addEventListener("click", exitLinkingMode);

  // ---------- Colonnes ----------

  const columnRowEls = new Map(); // localId -> DOM row

  function renderColumnRow(col) {
    let row = columnRowEls.get(col.localId);
    if (!row) {
      row = columnRowTemplate.content.firstElementChild.cloneNode(true);
      columnRowEls.set(col.localId, row);
      columnsWrap.appendChild(row);

      row.querySelector("[data-col-name]").addEventListener("input", (e) => {
        col.name = e.target.value;
        drawFkEdge(col); // le label de l'arête inclut le nom de colonne
      });
      row.querySelector("[data-col-type]").addEventListener("input", (e) => { col.type = e.target.value; });
      row.querySelector("[data-col-desc]").addEventListener("input", (e) => { col.description = e.target.value; });
      row.querySelector("[data-col-examples]").addEventListener("input", (e) => {
        col.examples = e.target.value.split(",").map((s) => s.trim()).filter(Boolean);
      });
      row.querySelector("[data-col-format]").addEventListener("input", (e) => { col.format = e.target.value; });
      row.querySelector("[data-col-valid-range]").addEventListener("input", (e) => { col.valid_range = e.target.value; });
      row.querySelector("[data-col-warning]").addEventListener("input", (e) => { col.warning = e.target.value; });

      row.querySelector("[data-col-link-btn]").addEventListener("click", () => enterLinkingMode(col.localId));
      row.querySelector("[data-col-unlink-btn]").addEventListener("click", () => {
        col.fk = null;
        removeFkEdge(col.localId);
        renderColumnRow(col);
      });
      row.querySelector("[data-col-remove]").addEventListener("click", () => removeColumn(col.localId));
    }

    row.querySelector("[data-col-name]").value = col.name;
    row.querySelector("[data-col-type]").value = col.type;
    row.querySelector("[data-col-desc]").value = col.description;
    row.querySelector("[data-col-examples]").value = col.examples.join(", ");
    row.querySelector("[data-col-format]").value = col.format;
    row.querySelector("[data-col-valid-range]").value = col.valid_range;
    row.querySelector("[data-col-warning]").value = col.warning;

    const badge = row.querySelector("[data-col-fk-badge]");
    const linkBtn = row.querySelector("[data-col-link-btn]");
    const unlinkBtn = row.querySelector("[data-col-unlink-btn]");
    if (col.fk) {
      badge.textContent = `→ ${col.fk.table}.${col.fk.column}`;
      badge.classList.remove("hidden");
      linkBtn.classList.add("hidden");
      unlinkBtn.classList.remove("hidden");
    } else {
      badge.classList.add("hidden");
      linkBtn.classList.remove("hidden");
      unlinkBtn.classList.add("hidden");
    }
  }

  function addColumn() {
    const col = freshColumn();
    draftState.columns.push(col);
    renderColumnRow(col);
    columnRowEls.get(col.localId).querySelector("[data-col-name]").focus();
  }

  function removeColumn(localId) {
    removeFkEdge(localId);
    draftState.columns = draftState.columns.filter((c) => c.localId !== localId);
    const row = columnRowEls.get(localId);
    if (row) row.remove();
    columnRowEls.delete(localId);
  }

  addColumnBtn.addEventListener("click", addColumn);

  // ---------- Règles métier ----------

  const ruleRowEls = new Map();

  function renderRuleRow(rule) {
    let row = ruleRowEls.get(rule.localId);
    if (!row) {
      row = ruleRowTemplate.content.firstElementChild.cloneNode(true);
      ruleRowEls.set(rule.localId, row);
      rulesWrap.appendChild(row);

      row.querySelector("[data-rule-title]").addEventListener("input", (e) => { rule.title = e.target.value; });
      row.querySelector("[data-rule-text]").addEventListener("input", (e) => { rule.text = e.target.value; });
      row.querySelector("[data-rule-remove]").addEventListener("click", () => removeRule(rule.localId));
    }
    row.querySelector("[data-rule-title]").value = rule.title;
    row.querySelector("[data-rule-text]").value = rule.text;
  }

  function addRule() {
    const rule = freshRule();
    draftState.rules.push(rule);
    renderRuleRow(rule);
    ruleRowEls.get(rule.localId).querySelector("[data-rule-title]").focus();
  }

  function removeRule(localId) {
    draftState.rules = draftState.rules.filter((r) => r.localId !== localId);
    const row = ruleRowEls.get(localId);
    if (row) row.remove();
    ruleRowEls.delete(localId);
  }

  addRuleBtn.addEventListener("click", addRule);

  // ---------- Aperçu / enregistrement / publication ----------

  function resetMessages() {
    errorEl.classList.add("hidden");
    successEl.classList.add("hidden");
  }

  function showError(msg) {
    errorEl.textContent = msg;
    errorEl.classList.remove("hidden");
  }

  previewBtn.addEventListener("click", async () => {
    resetMessages();
    previewOutput.classList.add("hidden");
    try {
      const res = await fetch("/api/tables/prepare", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ spec: buildSpecPayload() }),
      });
      const data = await res.json();
      if (!res.ok || data.error) {
        showError(data.error || "Aperçu impossible.");
        return;
      }
      previewOutput.textContent = data.table_markdown;
      previewOutput.classList.remove("hidden");
    } catch (err) {
      showError("Connexion au serveur impossible.");
    }
  });

  async function doSaveDraft() {
    const spec = buildSpecPayload();
    const url = draftState.id ? `/api/tables/drafts/${draftState.id}` : "/api/tables/drafts";
    const method = draftState.id ? "PUT" : "POST";
    const res = await fetch(url, {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ spec }),
    });
    const data = await res.json();
    if (!res.ok || data.error) {
      throw new Error(data.error || "Enregistrement du brouillon impossible.");
    }
    draftState.id = data.id;
    return data;
  }

  saveBtn.addEventListener("click", async () => {
    resetMessages();
    saveBtn.disabled = true;
    try {
      await doSaveDraft();
      publishBtn.disabled = false;
      successEl.textContent = "Brouillon enregistré.";
      successEl.classList.remove("hidden");
    } catch (err) {
      showError(err.message);
    } finally {
      saveBtn.disabled = false;
    }
  });

  publishBtn.addEventListener("click", async () => {
    resetMessages();
    publishBtn.disabled = true;
    saveBtn.disabled = true;
    try {
      // Toujours republier depuis un enregistrement frais : la publication ne doit jamais
      // porter sur une édition non sauvegardée (voir doc du module).
      await doSaveDraft();
      const res = await fetch("/api/tables/publish", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ draft_id: draftState.id }),
      });
      const data = await res.json();
      if (!res.ok || data.error) {
        showError(data.error || "Publication impossible.");
        publishBtn.disabled = false;
        saveBtn.disabled = false;
        return;
      }
      successEl.textContent = `Table « ${data.table_name} » publiée (${data.chunks_added} chunk(s)).`;
      successEl.classList.remove("hidden");

      const position = cy.getElementById(draftNodeId).position();
      const tableName = data.table_name;
      removeDraftNodeAndEdges();

      setTimeout(() => {
        close();
        if (onPublished) onPublished({ tableName, position: { x: position.x, y: position.y } });
      }, 1400);
    } catch (err) {
      showError("Connexion au serveur impossible.");
      publishBtn.disabled = false;
      saveBtn.disabled = false;
    }
  });

  // ---------- Ouverture / fermeture ----------

  function renderAll() {
    nameInput.value = draftState.name;
    datasetSelect.value = draftState.dataset || (datasets[0] && datasets[0].dataset) || "";
    grainInput.value = draftState.grain;

    columnsWrap.innerHTML = "";
    columnRowEls.clear();
    draftState.columns.forEach(renderColumnRow);

    rulesWrap.innerHTML = "";
    ruleRowEls.clear();
    draftState.rules.forEach(renderRuleRow);

    previewOutput.classList.add("hidden");
    resetMessages();
    publishBtn.disabled = !draftState.id;
  }

  function open(draft) {
    if (mode === "linking") exitLinkingMode();

    if (draft) {
      draftState = {
        id: draft.id,
        name: draft.payload.name || "",
        dataset: draft.payload.dataset || "",
        grain: draft.payload.grain || "",
        columns: (draft.payload.columns || []).map((c) => ({
          localId: _tbUuid(),
          name: c.name || "", type: c.type || "", description: c.description || "",
          examples: c.examples || [], format: c.format || "", valid_range: c.valid_range || "",
          warning: c.warning || "", fk: c.fk || null,
        })),
        rules: (draft.payload.rules || []).map((r) => ({ localId: _tbUuid(), title: r.title || "", text: r.text || "" })),
      };
      draftNodeId = `draft-${draft.id}`;
    } else {
      draftState = freshState();
      draftNodeId = `draft-${_tbUuid()}`;
    }

    addDraftNode();
    draftState.columns.forEach((c) => { if (c.fk) drawFkEdge(c); });

    nameInput.oninput = () => { draftState.name = nameInput.value; updateDraftNodeLabel(); };
    datasetSelect.onchange = () => { draftState.dataset = datasetSelect.value; };
    grainInput.oninput = () => { draftState.grain = grainInput.value; };
    // Charge la liste puis fixe la valeur retenue dans l'état : sans cette dernière ligne,
    // un brouillon jamais touché partirait avec `dataset: ""` et se ferait refuser par
    // validate_table_spec alors que le champ affiche bien un dataset.
    loadDatasets().then(() => { draftState.dataset = datasetSelect.value; });

    renderAll();
    rootEl.classList.remove("hidden");
    nameInput.focus();
  }

  function close() {
    if (mode === "linking") exitLinkingMode();
    removeDraftNodeAndEdges();
    rootEl.classList.add("hidden");
    draftState = null;
    draftNodeId = null;
    if (onClose) onClose();
  }

  closeBtn.addEventListener("click", close);
  closeBtn2.addEventListener("click", close);

  return {
    open,
    close,
    isOpen: () => !!draftState,
  };
}
