const chatLog = document.getElementById("chat-log");
const emptyState = document.getElementById("empty-state");
const form = document.getElementById("ask-form");
const input = document.getElementById("question-input");
const submitBtn = document.getElementById("submit-btn");
const newChatBtn = document.getElementById("new-chat-btn");
const conversationList = document.getElementById("conversation-list");
const sidebar = document.getElementById("sidebar");
const sidebarToggle = document.getElementById("sidebar-toggle");

const sharedConversationsSection = document.getElementById("shared-conversations-section");
const sharedConversationList = document.getElementById("shared-conversation-list");
const shareModal = new boosted.Modal(document.getElementById("share-modal"));
const shareForm = document.getElementById("share-form");
const shareUsernameInput = document.getElementById("share-username-input");
const shareAddBtn = document.getElementById("share-add-btn");
const shareError = document.getElementById("share-error");
const shareList = document.getElementById("share-list");
const hintText = document.querySelector(".pit-lane .hint");

const userBadge = document.getElementById("user-badge");
const myCorrectionsBtn = document.getElementById("my-corrections-btn");
const myCorrectionsModal = new boosted.Modal(document.getElementById("my-corrections-modal"));
const myCorrectionsList = document.getElementById("my-corrections-list");
const loginOpenBtn = document.getElementById("login-open-btn");
const logoutBtn = document.getElementById("logout-btn");
const loginOverlay = document.getElementById("login-overlay");
const loginModal = new boosted.Modal(loginOverlay);
const loginForm = document.getElementById("login-form");
const loginUsernameInput = document.getElementById("login-username");
const loginPasswordInput = document.getElementById("login-password");
const loginError = document.getElementById("login-error");
const loginCancelBtn = document.getElementById("login-cancel-btn");

const chatDetails = document.getElementById("chat-details");
const chatDetailsPlaceholder = document.getElementById("chat-details-placeholder");
const chatDetailsContent = document.getElementById("chat-details-content");
const chatDetailsTitle = document.getElementById("chat-details-title");
const chatDetailsMeta = document.getElementById("chat-details-meta");
const chatDetailsBody = document.getElementById("chat-details-body");
const chatDetailsEditBtn = document.getElementById("chat-details-edit-btn");
const chatCorrectionMount = document.getElementById("chat-correction-mount");
const correctionTemplate = document.getElementById("correction-inline-template");

let currentConversationId = null;
let currentConversationReadOnly = false;
let shareModalConvId = null;
let currentUsername = null;
let currentRole = null;
const DEFAULT_HINT_TEXT = hintText ? hintText.textContent : "";

// ---------- Utilitaires ----------

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

function scrollToBottom() {
  chatLog.scrollTop = chatLog.scrollHeight;
}

function clearChatLog() {
  chatLog.innerHTML = `
    <div class="empty-state" id="empty-state">
      <div class="grid-number">P1</div>
      <p>Posez une question sur la structure des datasets Orange — colonnes, règles métier, relations entre tables.</p>
      <div class="suggestions">
        <button class="btn btn-outline-secondary rounded-pill suggestion-pill" type="button">c'est quoi IdCanalDistribution ?</button>
        <button class="btn btn-outline-secondary rounded-pill suggestion-pill" type="button">comment joindre vente et la géographie IRIS ?</button>
        <button class="btn btn-outline-secondary rounded-pill suggestion-pill" type="button">le grain de dwh_fact_Vente_t ?</button>
      </div>
    </div>`;
  attachSuggestionPillListeners();
}

function removeEmptyState() {
  const empty = document.getElementById("empty-state");
  if (empty && empty.parentNode) empty.remove();
}

// ---------- Authentification ----------

async function checkSession() {
  try {
    const res = await fetch("/api/session");
    const data = await res.json();
    currentUsername = data.username;
    currentRole = data.role;
  } catch (err) {
    currentUsername = null;
    currentRole = null;
  }
  updateAuthUI();
}

// N'affecte plus la visibilité de #my-corrections-btn / #telemetry-link / #new-table-btn /
// #my-drafts-btn : ces éléments sont désormais omis du HTML lui-même côté serveur pour un
// rôle non-tech-lead (voir templates/index.html, templates/schema.html), pas seulement
// masqués en CSS — ils n'apparaissent donc jamais dans le DOM pour les autres. Un
// changement de statut tech lead (login/logout, voir plus bas) recharge la page plutôt que
// de mettre à jour ces boutons en direct, précisément pour ne jamais avoir à les
// faire apparaître/disparaître dynamiquement dans le DOM.
function updateAuthUI() {
  const isTechLead = currentRole === "tech_lead";
  if (currentUsername) {
    const roleLabel = isTechLead ? "Tech Lead" : "Utilisateur";
    userBadge.textContent = `${isTechLead ? "✎ " : ""}${currentUsername} · ${roleLabel}`;
    userBadge.classList.toggle("user-badge-techlead", isTechLead);
    userBadge.classList.remove("hidden");
    loginOpenBtn.classList.add("hidden");
    logoutBtn.classList.remove("hidden");
    loginModal.hide();
  } else {
    userBadge.classList.add("hidden");
    userBadge.classList.remove("user-badge-techlead");
    loginOpenBtn.classList.remove("hidden");
    logoutBtn.classList.add("hidden");
  }
}

function openLoginOverlay() {
  loginError.classList.add("hidden");
  loginForm.reset();
  loginModal.show();
}

loginOverlay.addEventListener("shown.bs.modal", () => {
  loginUsernameInput.focus();
});

loginOpenBtn.addEventListener("click", openLoginOverlay);

loginCancelBtn.addEventListener("click", () => {
  loginModal.hide();
});

loginForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  loginError.classList.add("hidden");
  try {
    const res = await fetch("/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: loginUsernameInput.value.trim(),
        password: loginPasswordInput.value
      })
    });
    const data = await res.json();
    if (!res.ok || data.error) {
      loginError.textContent = data.error || "Connexion impossible.";
      loginError.classList.remove("hidden");
      return;
    }
    const wasTechLead = currentRole === "tech_lead";
    currentUsername = data.username;
    currentRole = data.role;
    // Un changement de statut tech lead recharge la page plutôt qu'une mise à jour AJAX :
    // #my-corrections-btn/#telemetry-link n'existent dans le DOM que si le serveur les a
    // rendus au chargement (voir updateAuthUI) — un rechargement est le seul moyen de les
    // faire apparaître/disparaître sans dupliquer leur logique de rendu ici en JS.
    if ((data.role === "tech_lead") !== wasTechLead) {
      window.location.reload();
      return;
    }
    updateAuthUI();
    await switchIdentity();
  } catch (err) {
    loginError.textContent = "Connexion au serveur impossible.";
    loginError.classList.remove("hidden");
  }
});

logoutBtn.addEventListener("click", async () => {
  const wasTechLead = currentRole === "tech_lead";
  try {
    await fetch("/logout", { method: "POST" });
  } catch (err) {
    console.error("Déconnexion impossible.", err);
  }
  currentUsername = null;
  currentRole = null;
  // Voir le commentaire équivalent dans le handler de login : un tech lead qui se
  // déconnecte doit voir #my-corrections-btn/#telemetry-link disparaître du DOM, ce
  // qu'un rechargement garantit directement via le rendu serveur.
  if (wasTechLead) {
    window.location.reload();
    return;
  }
  updateAuthUI();
  await switchIdentity();
  openLoginOverlay();
});

// Une conversation appartient à une identité (username ou session invité) — après un
// login ou un logout, l'identité change, donc la conversation affichée et la liste dans
// la sidebar ne sont plus valides et doivent être réinitialisées.
async function switchIdentity() {
  currentConversationId = null;
  setReadOnlyMode(false);
  clearChatLog();
  await loadConversationList();
  await loadSharedConversationList();
}

// ---------- Panneau latéral : chunk de source + correction (Verdict 4) ----------
//
// Une seule instance de panneau de correction (static/correction.js, partagée avec
// schema.js) montée en permanence dans #chat-details : click sur une puce de source =
// affiche le chunk (lecture seule) ; ✎ Corriger, réservé au tech lead sur une source
// correctible, bascule ce même panneau en mode édition (voir showEditView/showReadView).

let currentChunkEntry = null;        // dernier chunk affiché en lecture (identité structurée)
let currentChunkCorrectable = null;  // { table, colonne } si ce chunk est correctible par le rôle courant

const correctionMountNode = correctionTemplate.content.firstElementChild.cloneNode(true);
chatCorrectionMount.appendChild(correctionMountNode);

function showReadView() {
  correctionMountNode.classList.add("hidden");
  const hasEntry = !!currentChunkEntry;
  chatDetailsContent.classList.toggle("hidden", !hasEntry);
  chatDetailsPlaceholder.classList.toggle("hidden", hasEntry);
}

const correctionPanel = createCorrectionPanel(correctionMountNode, {
  onClose: showReadView,
  onApplied: () => {
    if (currentChunkEntry) openChunkPanel(currentChunkEntry, currentChunkCorrectable);
  }
});

function showEditView(table, colonne) {
  chatDetailsContent.classList.add("hidden");
  chatDetailsPlaceholder.classList.add("hidden");
  correctionPanel.open(table, colonne);
}

chatDetailsEditBtn.addEventListener("click", () => {
  if (!currentChunkCorrectable) return;
  showEditView(currentChunkCorrectable.table, currentChunkCorrectable.colonne);
});

// Affiche le chunk d'origine d'une source dans le panneau latéral. `entry` : dict
// structuré renvoyé par extract_sources() côté serveur (label/table/table_raw/section/
// colonne/source) — les conversations antérieures à ce changement stockent encore des
// chaînes simples, non cliquables (voir renderSourceFooter). `correctable` : { table,
// colonne } si cette source est éditable par le rôle courant, sinon null.
async function openChunkPanel(entry, correctable) {
  if (!entry || typeof entry === "string") return;

  correctionPanel.close();
  currentChunkEntry = entry;
  currentChunkCorrectable = correctable || null;

  chatDetailsPlaceholder.classList.add("hidden");
  chatDetailsContent.classList.remove("hidden");
  chatDetailsTitle.textContent = entry.label;
  chatDetailsMeta.textContent = entry.source || "";
  chatDetailsBody.textContent = "Chargement...";
  chatDetailsEditBtn.classList.toggle("hidden", !currentChunkCorrectable);

  const params = new URLSearchParams({
    source: entry.source || "",
    table_raw: entry.table_raw || "",
    section: entry.section || "",
    colonne: entry.colonne || "",
  });

  try {
    const res = await fetch(`/api/chunk?${params.toString()}`);
    const data = await res.json();
    if (!res.ok || data.error) {
      chatDetailsBody.textContent = data.error || "Chunk introuvable.";
      return;
    }
    chatDetailsBody.textContent = data.content;
    chatDetailsMeta.textContent = data.corrected
      ? `${entry.source} · corrigé par ${data.corrected_by}`
      : (entry.source || "");
  } catch (err) {
    chatDetailsBody.textContent = "Connexion au serveur impossible.";
  }
}

// ---------- Mes corrections (historique + retour arrière) ----------

async function loadMyCorrections() {
  myCorrectionsList.innerHTML = '<p class="text-secondary small mb-0">Chargement...</p>';
  try {
    const res = await fetch("/api/corrections/mine");
    const corrections = await res.json();
    renderMyCorrections(corrections);
  } catch (err) {
    myCorrectionsList.innerHTML = '<p class="text-danger small mb-0">Connexion au serveur impossible.</p>';
  }
}

function renderMyCorrections(corrections) {
  myCorrectionsList.innerHTML = "";

  if (!corrections.length) {
    myCorrectionsList.innerHTML = '<p class="text-secondary small mb-0">Aucune correction enregistrée pour l\'instant.</p>';
    return;
  }

  const list = document.createElement("div");
  list.className = "list-group";

  // Une correction écrit directement dans la Knowledge Base (pas d'annulation en un clic) :
  // ce panneau reste un journal de ce qui a été corrigé, pas un panneau d'édition en place.
  // "Corriger à nouveau" rouvre simplement le même flux prepare/apply que depuis une puce
  // de source, pré-rempli sur la table/colonne de la ligne — utile pour revenir sur une
  // correction faite il y a longtemps sans avoir à retrouver la conversation d'origine.
  corrections.forEach(c => {
    const item = document.createElement("div");
    item.className = "list-group-item d-flex align-items-start justify-content-between gap-2";
    item.innerHTML = `
      <div class="min-width-0">
        <code>${escapeHtml(c.table_name)} › ${escapeHtml(c.colonne)}</code>
        <p class="mb-1 mt-1">${escapeHtml(c.texte_valide)}</p>
        <span class="text-secondary small">${formatConvDate(c.date)}</span>
      </div>
      <button type="button" class="btn btn-outline-secondary btn-sm flex-shrink-0 my-correction-fix-btn">✎ Corriger à nouveau</button>`;
    item.querySelector(".my-correction-fix-btn").addEventListener("click", () => {
      myCorrectionsModal.hide();
      showEditView(c.table_name, c.colonne);
    });
    list.appendChild(item);
  });

  myCorrectionsList.appendChild(list);
}

// myCorrectionsBtn n'existe dans le DOM que pour un tech lead (voir templates/index.html) —
// absent (null) pour tout autre rôle.
if (myCorrectionsBtn) {
  myCorrectionsBtn.addEventListener("click", () => {
    myCorrectionsModal.show();
    loadMyCorrections();
  });
}

// ---------- Rendu des messages ----------

function addQuestion(text) {
  removeEmptyState();
  const row = document.createElement("div");
  row.className = "question-row";
  row.innerHTML = `
    <div class="question-tag">
      <span class="label">Question</span>
      ${escapeHtml(text)}
    </div>`;
  chatLog.appendChild(row);
}

function renderSuggestions(container, suggestions) {
  if (!suggestions || !suggestions.length) return;
  const wrap = document.createElement("div");
  wrap.className = "answer-suggestions";

  const label = document.createElement("span");
  label.className = "suggestions-label";
  label.textContent = "Essayer :";
  wrap.appendChild(label);

  suggestions.forEach(s => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn btn-outline-secondary rounded-pill inline-suggestion-pill";
    btn.textContent = s;
    btn.addEventListener("click", () => askQuestion(s));
    wrap.appendChild(btn);
  });
  container.appendChild(wrap);
}

function enhanceTables(container) {
  container.querySelectorAll("table").forEach(table => {
    table.classList.add("table", "table-striped", "table-sm");
    const wrapper = document.createElement("div");
    wrapper.className = "table-responsive";
    table.parentNode.insertBefore(wrapper, table);
    wrapper.appendChild(table);
  });
}

function addCopyButton(panel) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "btn btn-link copy-btn";
  btn.textContent = "Copier";
  btn.addEventListener("click", () => {
    const body = panel.querySelector(".answer-body");
    const text = body ? body.innerText : "";
    navigator.clipboard.writeText(text).then(() => {
      btn.textContent = "Copié ✓";
      btn.classList.add("copied");
      setTimeout(() => {
        btn.textContent = "Copier";
        btn.classList.remove("copied");
      }, 1500);
    }).catch(() => {
      btn.textContent = "Erreur";
      setTimeout(() => { btn.textContent = "Copier"; }, 1500);
    });
  });
  panel.appendChild(btn);
}

// Pied de réponse : chaque source devient sa propre puce plutôt qu'un texte joint par
// des "·". Cliquer une puce ouvre le panneau latéral qui affiche le chunk d'origine
// (voir openChunkPanel) — pour tout le monde, pas seulement le tech lead : voir la
// provenance d'une information est utile à tous, corriger reste réservé au tech lead.
// Une puce correctible (présente dans sourcesCorrigibles) affiche un repère ✎ ; le
// panneau lui-même expose alors un bouton "Corriger" pour un tech lead. Une puce déjà
// corrigée (présente dans correctionsAppliquees) porte un repère ✓ visible de tous.
// Les conversations enregistrées avant ce changement stockent `sources` comme de
// simples chaînes (pas d'identité structurée) : ces puces restent non cliquables.
function renderSourceFooter(panel, sources = [], sourcesCorrigibles = [], correctionsAppliquees = []) {
  if (!sources.length) return;

  // Clé (table, colonne) uniquement — jamais le label affiché : celui-ci inclut le
  // dataset (nécessaire pour désambiguïser les colonnes homonymes entre bio_vente et
  // bio_refgeographie), que sourcesCorrigibles/correctionsAppliquees ne portent pas.
  // Un rapprochement sur le label complet ne matche donc jamais, quel que soit le rôle.
  // .trim() : extract_sources() (app.py) ne strippe pas `colonne`, contrairement à
  // db.extract_entity() qu'utilisent extract_correctable_sources()/corrected_sources() —
  // sans ça, un espace superflu dans les métadonnées ferait échouer le rapprochement.
  const entityKey = (table, colonne) => `${(table || "").trim()}::${(colonne || "").trim()}`;
  const correctableByLabel = new Map(sourcesCorrigibles.map(s => [entityKey(s.table, s.colonne), s]));
  const appliedByLabel = new Map(correctionsAppliquees.map(c => [entityKey(c.table, c.colonne), c]));

  const footer = document.createElement("div");
  footer.className = "source-footer";

  const label = document.createElement("span");
  label.className = "source-label";
  label.textContent = "Informations fournies par";
  footer.appendChild(label);

  const chipList = document.createElement("span");
  chipList.className = "source-chip-list";

  sources.forEach((entry, i) => {
    if (i > 0) {
      const sep = document.createElement("span");
      sep.className = "source-sep";
      sep.setAttribute("aria-hidden", "true");
      sep.textContent = "·";
      chipList.appendChild(sep);
    }

    const isLegacy = typeof entry === "string";
    const sourceLabel = isLegacy ? entry : entry.label;
    const key = isLegacy ? null : entityKey(entry.table, entry.colonne);
    const applied = key ? appliedByLabel.get(key) : undefined;
    const correctable = key ? correctableByLabel.get(key) : undefined;
    const icon = applied
      ? '<span class="source-chip-icon" aria-hidden="true">✓</span>'
      : (currentRole === "tech_lead" && correctable ? '<span class="source-chip-icon" aria-hidden="true">✎</span>' : "");

    if (!isLegacy) {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "source-chip source-chip-clickable" + (correctable ? " source-chip-correctable" : "") + (applied ? " source-chip-corrected" : "");
      chip.innerHTML = `${escapeHtml(sourceLabel)}${icon}`;
      chip.title = applied ? `Corrigé par ${applied.auteur}` : "Cliquer pour voir le chunk d'origine";
      chip.addEventListener("click", () => openChunkPanel(entry, correctable));
      chipList.appendChild(chip);
    } else {
      const chip = document.createElement("span");
      chip.className = "source-chip" + (applied ? " source-chip-corrected" : "");
      if (applied) chip.title = `Corrigé par ${applied.auteur}`;
      chip.innerHTML = `${escapeHtml(sourceLabel)}${icon}`;
      chipList.appendChild(chip);
    }
  });

  footer.appendChild(chipList);
  panel.appendChild(footer);
}

// Plus de bandeau en tête de réponse : la provenance d'une correction reste visible,
// mais de façon discrète, via le seul repère ✓ sur la puce de source concernée en pied
// de réponse (renderSourceFooter). Le contenu corrigé fait désormais partie intégrante
// de la Knowledge Base — il n'y a plus d'« injection » à signaler ostensiblement.

function addAnswerPanel(html, sources = [], isError = false, suggestions = [], sourcesCorrigibles = [], correctionsAppliquees = []) {
  const panel = document.createElement("div");
  panel.className = "answer-panel" + (isError ? " error" : "");
  panel.innerHTML = `<div class="answer-body">${html}</div>`;
  enhanceTables(panel);
  if (!isError) {
    renderSourceFooter(panel, sources, sourcesCorrigibles, correctionsAppliquees);
    renderSuggestions(panel, suggestions);
    addCopyButton(panel);
  }
  chatLog.appendChild(panel);
  return panel;
}

// Libellés affichés en boucle sous la barre de progression tant que la
// requête /ask est en attente — purement décoratif, aucune progression réelle.
const LOADING_STEPS = [
  "Recherche dans la base...",
  "Analyse du contexte...",
  "Génération de la réponse..."
];
let loadingInterval = null;

function addLoadingPanel() {
  const panel = document.createElement("div");
  panel.className = "answer-panel";
  panel.id = "loading-panel";
  panel.innerHTML = `
    <div class="sector-progress">
      <div class="progress" role="progressbar" aria-label="Chargement de la réponse" aria-valuenow="100" aria-valuemin="0" aria-valuemax="100">
        <div class="progress-bar progress-bar-striped progress-bar-animated" style="width: 100%"></div>
      </div>
      <p class="sector-progress-label" id="loading-label">${LOADING_STEPS[0]}</p>
    </div>`;
  chatLog.appendChild(panel);
  scrollToBottom();
  runLoadingSequence();
}

function runLoadingSequence() {
  let i = 0;
  loadingInterval = setInterval(() => {
    const label = document.getElementById("loading-label");
    if (!label) { clearInterval(loadingInterval); loadingInterval = null; return; }
    i = (i + 1) % LOADING_STEPS.length;
    label.textContent = LOADING_STEPS[i];
  }, 800);
}

function replaceLoadingWith(html, isError = false, sources = [], suggestions = [], sourcesCorrigibles = [], correctionsAppliquees = []) {
  const panel = document.getElementById("loading-panel");
  if (!panel) return;
  if (loadingInterval) { clearInterval(loadingInterval); loadingInterval = null; }
  panel.id = "";
  panel.classList.toggle("error", isError);

  panel.innerHTML = `<div class="answer-body">${html}</div>`;
  enhanceTables(panel);
  if (!isError) {
    renderSourceFooter(panel, sources, sourcesCorrigibles, correctionsAppliquees);
    renderSuggestions(panel, suggestions);
    addCopyButton(panel);
  }
  scrollToBottom();
}

// ---------- Sidebar : liste des conversations ----------

function formatConvDate(isoString) {
  const d = new Date(isoString);
  return d.toLocaleDateString("fr-FR", { day: "2-digit", month: "2-digit" }) +
    " · " + d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
}

async function loadConversationList() {
  try {
    const res = await fetch("/api/conversations");
    const conversations = await res.json();
    renderConversationList(conversations);
  } catch (err) {
    console.error("Impossible de charger l'historique des conversations.", err);
  }
}

// ---------- Partage (lecture seule) ----------
// Une conversation possédée peut être partagée avec un autre utilisateur enregistré, qui
// la voit alors dans sa propre sidebar sous "Partagées avec moi", en lecture seule
// uniquement (voir setReadOnlyMode) — /ask, le renommage et la suppression restent
// strictement réservés au propriétaire côté serveur (app.py), la désactivation du
// formulaire ici n'est qu'une aide visuelle, pas le contrôle d'accès lui-même.

async function loadSharedConversationList() {
  if (!currentUsername) {
    sharedConversationsSection.classList.add("hidden");
    sharedConversationList.innerHTML = "";
    return;
  }
  try {
    const res = await fetch("/api/conversations/shared");
    const conversations = await res.json();
    renderSharedConversationList(conversations);
  } catch (err) {
    console.error("Impossible de charger les conversations partagées.", err);
  }
}

function renderSharedConversationList(conversations) {
  sharedConversationList.innerHTML = "";
  sharedConversationsSection.classList.toggle("hidden", !conversations.length);

  conversations.forEach(conv => {
    const item = document.createElement("div");
    item.className = "list-group-item list-group-item-action conversation-item conversation-item-shared d-flex align-items-center justify-content-between gap-2" +
      (conv.id === currentConversationId ? " active" : "");
    item.dataset.id = conv.id;

    const titleSpan = document.createElement("span");
    titleSpan.className = "conv-title";
    titleSpan.textContent = conv.title || "Nouvelle session";

    const metaSpan = document.createElement("span");
    metaSpan.className = "conv-date";
    metaSpan.textContent = `Partagée par ${conv.owner}`;

    const textWrap = document.createElement("div");
    textWrap.className = "conv-text-wrap";
    textWrap.appendChild(titleSpan);
    textWrap.appendChild(metaSpan);

    item.appendChild(textWrap);
    item.addEventListener("click", () => selectConversation(conv.id, { readOnly: true, owner: conv.owner }));

    sharedConversationList.appendChild(item);
  });
}

function setReadOnlyMode(readOnly, owner) {
  currentConversationReadOnly = readOnly;
  input.disabled = readOnly;
  submitBtn.disabled = readOnly;
  if (hintText) {
    hintText.textContent = readOnly
      ? `Conversation partagée par ${owner} — lecture seule, vous ne pouvez pas y poser de question.`
      : DEFAULT_HINT_TEXT;
  }
}

async function openShareModal(convId) {
  shareModalConvId = convId;
  shareError.classList.add("hidden");
  shareUsernameInput.value = "";
  shareList.innerHTML = '<p class="text-secondary small mb-0">Chargement...</p>';
  shareModal.show();
  await loadShareList();
}

async function loadShareList() {
  try {
    const res = await fetch(`/api/conversations/${shareModalConvId}/shares`);
    const shares = await res.json();
    renderShareList(shares);
  } catch (err) {
    shareList.innerHTML = '<p class="text-danger small mb-0">Connexion au serveur impossible.</p>';
  }
}

function renderShareList(shares) {
  shareList.innerHTML = "";
  if (!shares.length) {
    shareList.innerHTML = '<p class="text-secondary small mb-0">Pas encore partagée.</p>';
    return;
  }

  const list = document.createElement("div");
  list.className = "list-group";

  shares.forEach(s => {
    const item = document.createElement("div");
    item.className = "list-group-item d-flex align-items-center justify-content-between gap-2";

    const codeEl = document.createElement("code");
    codeEl.textContent = s.shared_with;

    const revokeBtn = document.createElement("button");
    revokeBtn.type = "button";
    revokeBtn.className = "btn btn-outline-secondary btn-sm";
    revokeBtn.textContent = "Retirer";
    revokeBtn.addEventListener("click", async () => {
      await fetch(`/api/conversations/${shareModalConvId}/share/${encodeURIComponent(s.shared_with)}`, { method: "DELETE" });
      loadShareList();
    });

    item.appendChild(codeEl);
    item.appendChild(revokeBtn);
    list.appendChild(item);
  });

  shareList.appendChild(list);
}

shareForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const username = shareUsernameInput.value.trim();
  if (!username || !shareModalConvId) return;

  shareError.classList.add("hidden");
  shareAddBtn.disabled = true;
  try {
    const res = await fetch(`/api/conversations/${shareModalConvId}/share`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username })
    });
    const data = await res.json();
    if (!res.ok || data.error) {
      shareError.textContent = data.error || "Partage impossible.";
      shareError.classList.remove("hidden");
      return;
    }
    shareUsernameInput.value = "";
    renderShareList(data);
  } catch (err) {
    shareError.textContent = "Connexion au serveur impossible.";
    shareError.classList.remove("hidden");
  } finally {
    shareAddBtn.disabled = false;
  }
});

function renderConversationList(conversations) {
  conversationList.innerHTML = "";
  conversations.forEach(conv => {
    const item = document.createElement("div");
    item.className = "list-group-item list-group-item-action conversation-item d-flex align-items-center justify-content-between gap-2" +
      (conv.id === currentConversationId ? " active" : "");
    item.dataset.id = conv.id;

    const titleSpan = document.createElement("span");
    titleSpan.className = "conv-title";
    titleSpan.textContent = conv.title || "Nouvelle session";

    const dateSpan = document.createElement("span");
    dateSpan.className = "conv-date";
    dateSpan.textContent = formatConvDate(conv.created_at);

    const shareBtn = document.createElement("button");
    shareBtn.type = "button";
    shareBtn.className = "conv-share-btn";
    shareBtn.textContent = "⇄";
    shareBtn.setAttribute("aria-label", "Partager cette conversation");
    shareBtn.title = "Partager cette conversation";
    shareBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      openShareModal(conv.id);
    });

    const deleteBtn = document.createElement("button");
    deleteBtn.type = "button";
    deleteBtn.className = "conv-delete-btn";
    deleteBtn.textContent = "✕";
    deleteBtn.setAttribute("aria-label", "Supprimer cette conversation");
    deleteBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      deleteConversation(conv.id);
    });

    const textWrap = document.createElement("div");
    textWrap.className = "conv-text-wrap";
    textWrap.appendChild(titleSpan);
    textWrap.appendChild(dateSpan);

    const actionsWrap = document.createElement("div");
    actionsWrap.className = "conv-actions-wrap";
    // Le partage requiert un compte nommé (voir POST /api/conversations/<id>/share côté
    // serveur) — pas de bouton pour une session invitée, qui n'a personne à qui partager.
    if (currentUsername) actionsWrap.appendChild(shareBtn);
    actionsWrap.appendChild(deleteBtn);

    item.appendChild(textWrap);
    item.appendChild(actionsWrap);

    item.addEventListener("click", () => selectConversation(conv.id, { readOnly: false }));
    titleSpan.addEventListener("dblclick", (e) => {
      e.stopPropagation();
      startRenaming(titleSpan, conv.id);
    });

    conversationList.appendChild(item);
  });
}

function startRenaming(titleSpan, convId) {
  const currentText = titleSpan.textContent;
  const inputEl = document.createElement("input");
  inputEl.type = "text";
  inputEl.className = "conv-rename-input";
  inputEl.value = currentText;
  inputEl.maxLength = 60;
  titleSpan.replaceWith(inputEl);
  inputEl.focus();
  inputEl.select();

  const commit = async () => {
    const newTitle = inputEl.value.trim();
    if (newTitle && newTitle !== currentText) {
      try {
        await fetch(`/api/conversations/${convId}/title`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title: newTitle })
        });
      } catch (err) {
        console.error("Renommage impossible.", err);
      }
    }
    await loadConversationList();
    setActiveConversationItem(currentConversationId);
  };

  inputEl.addEventListener("blur", commit);
  inputEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter") inputEl.blur();
    if (e.key === "Escape") { inputEl.value = currentText; inputEl.blur(); }
  });
}

async function deleteConversation(convId) {
  const confirmed = window.confirm("Supprimer définitivement cette conversation ?");
  if (!confirmed) return;

  try {
    await fetch(`/api/conversations/${convId}`, { method: "DELETE" });
    if (convId === currentConversationId) {
      currentConversationId = null;
      clearChatLog();
    }
    await loadConversationList();
  } catch (err) {
    console.error("Suppression impossible.", err);
  }
}

function setActiveConversationItem(id) {
  document.querySelectorAll(".conversation-item").forEach(item => {
    item.classList.toggle("active", item.dataset.id === id);
  });
}

async function selectConversation(id, { readOnly = false, owner = null } = {}) {
  currentConversationId = id;
  setReadOnlyMode(readOnly, owner);
  setActiveConversationItem(id);
  clearChatLog();

  try {
    const res = await fetch(`/api/conversations/${id}/messages`);
    const messages = await res.json();

    if (messages.length) {
      removeEmptyState();
      messages.forEach(m => {
        if (m.role === "user") {
          addQuestion(m.content);
        } else {
          addAnswerPanel(
            m.content_html, m.sources || [], false, m.suggestions || [],
            m.sources_corrigibles || [], m.corrections_appliquees || []
          );
        }
      });
      scrollToBottom();
    }
  } catch (err) {
    console.error("Impossible de charger cette conversation.", err);
  }
}

async function createNewConversation() {
  const res = await fetch("/api/conversations", { method: "POST" });
  const conv = await res.json();
  currentConversationId = conv.id;
  setReadOnlyMode(false);
  clearChatLog();
  await loadConversationList();
  setActiveConversationItem(conv.id);
  return conv.id;
}

// ---------- Envoi d'une question ----------

async function askQuestion(question) {
  if (currentConversationReadOnly) return; // filet de sécurité — le formulaire est déjà désactivé (setReadOnlyMode)
  if (!currentConversationId) {
    await createNewConversation();
  }

  addQuestion(question);
  addLoadingPanel();
  submitBtn.disabled = true;

  try {
    const res = await fetch("/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, conversation_id: currentConversationId })
    });
    const data = await res.json();

    if (!res.ok || data.error) {
      replaceLoadingWith(`<p>${escapeHtml(data.error || "Une erreur est survenue.")}</p>`, true);
    } else {
      replaceLoadingWith(
        data.answer_html, false, data.sources || [], data.suggestions || [],
        data.sources_corrigibles || [], data.corrections_appliquees || []
      );
      if (data.new_title) {
        await loadConversationList();
        setActiveConversationItem(currentConversationId);
      }
    }
  } catch (err) {
    replaceLoadingWith(`<p>Connexion au serveur impossible. Vérifiez que app.py tourne bien.</p>`, true);
  } finally {
    submitBtn.disabled = false;
  }
}

// ---------- Écouteurs ----------

function attachSuggestionPillListeners() {
  document.querySelectorAll(".suggestion-pill").forEach(pill => {
    pill.addEventListener("click", () => askQuestion(pill.textContent));
  });
}

form.addEventListener("submit", (e) => {
  e.preventDefault();
  const question = input.value.trim();
  if (!question) return;
  input.value = "";
  askQuestion(question);
});

newChatBtn.addEventListener("click", () => createNewConversation());

// Le panneau latéral se comporte différemment selon la largeur : en dessous
// de 1024px c'est un offcanvas Boosted plein écran (piloté par sa propre API
// JS, avec fond assombri) ; à partir de 1024px il est intégré en permanence
// dans la mise en page (cf. custom.css) et se replie simplement via la classe
// "lg-collapsed" — le composant Offcanvas de Boosted n'a pas connaissance de
// ce seuil responsive et ajouterait un fond assombri même en desktop.
let mobileSidebar = null;
const isDesktopSidebar = () => window.matchMedia("(min-width: 1024px)").matches;

sidebarToggle.addEventListener("click", () => {
  if (isDesktopSidebar()) {
    const collapsed = sidebar.classList.toggle("lg-collapsed");
    sidebarToggle.setAttribute("aria-expanded", String(!collapsed));
  } else {
    if (!mobileSidebar) mobileSidebar = new boosted.Offcanvas(sidebar);
    mobileSidebar.toggle();
  }
});

sidebar.addEventListener("shown.bs.offcanvas", () => sidebarToggle.setAttribute("aria-expanded", "true"));
sidebar.addEventListener("hidden.bs.offcanvas", () => sidebarToggle.setAttribute("aria-expanded", "false"));

// ---------- Écran de démarrage ----------

const STARTUP_SEEN_KEY = "f1de_startup_seen";

// Séquence de feux façon grille de départ F1, jouée une fois par session
// (sessionStorage) avant que le chat ne devienne interactif. La modale de
// connexion (checkSession) n'est déclenchée qu'une fois cet écran retiré,
// pour éviter tout chevauchement avec son fond assombri.
function runStartupSequence() {
  const screen = document.getElementById("startup-screen");
  if (!screen) return Promise.resolve();

  const skipAnimation = sessionStorage.getItem(STARTUP_SEEN_KEY) ||
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  sessionStorage.setItem(STARTUP_SEEN_KEY, "1");

  if (skipAnimation) {
    screen.remove();
    return Promise.resolve();
  }

  const lights = screen.querySelectorAll(".startup-light");
  return new Promise((resolve) => {
    let i = 0;
    const lightUp = () => {
      if (i < lights.length) {
        lights[i].classList.add("on");
        i++;
        setTimeout(lightUp, 180);
        return;
      }
      setTimeout(() => {
        lights.forEach(l => { l.classList.remove("on"); l.classList.add("go"); });
        setTimeout(() => {
          screen.classList.add("startup-hide");
          setTimeout(() => { screen.remove(); resolve(); }, 400);
        }, 350);
      }, 250);
    };
    setTimeout(lightUp, 300);
  });
}

attachSuggestionPillListeners();
loadConversationList();

runStartupSequence().then(() => {
  checkSession().then(() => {
    if (!currentUsername) openLoginOverlay();
    // Re-rendu nécessaire : le premier appel à loadConversationList() (ligne précédente,
    // lancé avant que checkSession() ne résolve) a rendu la liste avec currentUsername
    // encore à null, donc sans bouton de partage même pour un utilisateur déjà connecté
    // (cookie de session existant) — on corrige une fois l'identité confirmée.
    loadConversationList();
    loadSharedConversationList();
  });
});