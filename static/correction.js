/**
 * correction.js — panneau de correction inline (prépare -> relecture des candidats ->
 * applique), partagé entre le panneau de source du chat (app.js) et le panneau colonne
 * du schéma (schema.js).
 *
 * Une instance = un DOM monté dans un conteneur fourni par la page hôte (cloné depuis un
 * <template id="correction-inline-template"> propre à chaque page), pas un singleton
 * déplacé en DOM d'une page à l'autre : chat et schéma sont deux documents distincts,
 * chargés séparément, chacun avec son propre gabarit.
 */

function _correctionEscapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

// Diff ligne à ligne (LCS) entre l'ancien et le nouveau texte — permet au tech lead de
// repérer d'un coup d'œil EXACTEMENT ce qui change, et donc d'attraper une réécriture
// parasite sur une ligne sans rapport avant de l'appliquer.
function _correctionLineDiff(oldStr, newStr) {
  const a = (oldStr || "").split("\n");
  const b = (newStr || "").split("\n");
  const n = a.length, m = b.length;
  const dp = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--)
    for (let j = m - 1; j >= 0; j--)
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const out = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { out.push({ t: " ", s: a[i] }); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { out.push({ t: "-", s: a[i] }); i++; }
    else { out.push({ t: "+", s: b[j] }); j++; }
  }
  while (i < n) out.push({ t: "-", s: a[i++] });
  while (j < m) out.push({ t: "+", s: b[j++] });
  return out;
}

function _correctionRenderDiffHtml(oldStr, newStr) {
  const cls = { " ": "diff-ctx", "-": "diff-del", "+": "diff-add" };
  return _correctionLineDiff(oldStr, newStr)
    .map(l => `<span class="diff-line ${cls[l.t]}">${_correctionEscapeHtml((l.t === " " ? "  " : l.t + " ") + l.s)}</span>`)
    .join("");
}

/**
 * Monte un panneau de correction dans `rootEl` (le noeud cloné depuis le <template> hôte,
 * déjà inséré dans le DOM).
 * `onClose()` est appelé à chaque fermeture (Annuler, ✕, ou fin d'application réussie) —
 * laisse la page hôte revenir à sa vue de lecture (panneau de source, liste de colonnes...).
 * `onApplied({table, colonne})` est appelé juste après, uniquement suite à une application
 * réussie, pour que la page hôte rafraîchisse le contenu affiché avec la version corrigée.
 * Renvoie { open(table, colonne), close(), isOpen() }.
 */
function createCorrectionPanel(rootEl, { onApplied, onClose } = {}) {
  const targetEl = rootEl.querySelector("[data-correction-target]");
  const closeBtn = rootEl.querySelector("[data-correction-close]");
  const bruteInput = rootEl.querySelector("[data-correction-input]");
  const prepareBtn = rootEl.querySelector("[data-correction-prepare]");
  const previewWrap = rootEl.querySelector("[data-correction-preview]");
  const primaryInput = rootEl.querySelector("[data-correction-primary]");
  const propagations = rootEl.querySelector("[data-correction-propagations]");
  const errorEl = rootEl.querySelector("[data-correction-error]");
  const successEl = rootEl.querySelector("[data-correction-success]");
  const cancelBtn = rootEl.querySelector("[data-correction-cancel]");
  const applyBtn = rootEl.querySelector("[data-correction-apply]");

  let context = null;        // { table, colonne } de la source en cours de correction
  let brut = "";              // texte brut envoyé lors de la préparation
  let primaryEdit = null;     // édition primaire proposée (dry-run)
  let candidateEdits = [];    // chunks candidats repérés (texte courant, non réécrit)

  function resetForm() {
    bruteInput.value = "";
    primaryInput.value = "";
    propagations.innerHTML = "";
    candidateEdits = [];
    primaryEdit = null;
    previewWrap.classList.add("hidden");
    errorEl.classList.add("hidden");
    successEl.classList.add("hidden");
    applyBtn.disabled = true;
    prepareBtn.disabled = false;
    prepareBtn.textContent = "Préparer la correction";
  }

  function open(table, colonne) {
    context = { table, colonne };
    targetEl.textContent = `${table} › ${colonne}`;
    resetForm();
    rootEl.classList.remove("hidden");
    bruteInput.focus();
  }

  function close() {
    context = null;
    rootEl.classList.add("hidden");
    if (onClose) onClose();
  }

  // Chunks candidats : repérés de façon déterministe (même table + mention de la colonne, ou
  // mention de la colonne dans les autres fichiers), SANS jugement automatique de conflit.
  // Ils sont donc affichés avec leur texte ACTUEL, éditable : c'est le tech lead qui décide
  // si le passage doit changer et qui rédige la modification. Tant qu'un candidat n'est pas
  // édité, un repère explicite rappelle qu'il ne sera pas modifié (no-op côté serveur), pour
  // qu'une case cochée mais intouchée ne se lise pas comme « relu et approuvé ».
  function renderCandidates(candidates) {
    propagations.innerHTML = "";
    if (!candidates.length) return;

    const heading = document.createElement("p");
    heading.className = "correction-prop-heading small mb-2";
    heading.textContent = `${candidates.length} passage(s) mentionnant cette colonne — relisez et modifiez ceux qui deviennent faux :`;
    propagations.appendChild(heading);

    candidates.forEach((edit, i) => {
      const item = document.createElement("div");
      item.className = "correction-prop-item";
      item.innerHTML = `
        <div class="form-check">
          <input class="form-check-input correction-prop-check" type="checkbox" checked id="correction-prop-${i}">
          <label class="form-check-label" for="correction-prop-${i}"><code>${_correctionEscapeHtml(edit.label)}</code></label>
          <span class="correction-prop-untouched" data-idx="${i}">aucune modification proposée — éditez si nécessaire</span>
        </div>
        <details class="correction-prop-details">
          <summary class="small">Relire et modifier ce passage</summary>
          <textarea class="form-control correction-prop-after" rows="5">${_correctionEscapeHtml(edit.nouveau_texte)}</textarea>
          <pre class="correction-diff hidden" data-idx="${i}"></pre>
        </details>`;

      // Le repère « non modifié » disparaît dès la première édition réelle, remplacé par le
      // diff ligne à ligne de ce que le tech lead vient d'écrire.
      const textarea = item.querySelector(".correction-prop-after");
      const badge = item.querySelector(".correction-prop-untouched");
      const diffBox = item.querySelector(".correction-diff");
      textarea.addEventListener("input", () => {
        const modifie = textarea.value.trim() !== (edit.ancien_texte || "").trim();
        badge.classList.toggle("hidden", modifie);
        diffBox.classList.toggle("hidden", !modifie);
        if (modifie) diffBox.innerHTML = _correctionRenderDiffHtml(edit.ancien_texte, textarea.value);
      });

      propagations.appendChild(item);
    });
  }

  closeBtn.addEventListener("click", close);
  cancelBtn.addEventListener("click", close);

  prepareBtn.addEventListener("click", async () => {
    const source = context;
    const texteBrut = bruteInput.value.trim();

    errorEl.classList.add("hidden");
    successEl.classList.add("hidden");

    if (!source || !texteBrut) {
      errorEl.textContent = "Choisissez une source et écrivez une correction.";
      errorEl.classList.remove("hidden");
      return;
    }

    prepareBtn.disabled = true;
    prepareBtn.textContent = "Analyse du KB...";

    try {
      const res = await fetch("/api/corrections/prepare", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          table_name: source.table,
          colonne: source.colonne,
          texte_brut: texteBrut
        })
      });
      const data = await res.json();
      if (!res.ok || data.error) {
        errorEl.textContent = data.error || "Préparation impossible.";
        errorEl.classList.remove("hidden");
        return;
      }

      brut = texteBrut;
      primaryEdit = data.primary;
      candidateEdits = data.candidates || [];

      primaryInput.value = data.primary.nouveau_texte;
      renderCandidates(candidateEdits);
      previewWrap.classList.remove("hidden");
      applyBtn.disabled = false;
    } catch (err) {
      errorEl.textContent = "Connexion au serveur impossible.";
      errorEl.classList.remove("hidden");
    } finally {
      prepareBtn.disabled = false;
      prepareBtn.textContent = "Préparer la correction";
    }
  });

  applyBtn.addEventListener("click", async () => {
    const source = context;
    const primaryText = primaryInput.value.trim();

    errorEl.classList.add("hidden");

    if (!source || !primaryEdit || !primaryText) {
      errorEl.textContent = "La correction principale ne peut pas être vide.";
      errorEl.classList.remove("hidden");
      return;
    }

    // Édition primaire (toujours appliquée) + candidats cochés. Un candidat coché mais laissé
    // tel quel part avec nouveau_texte == ancien_texte : le serveur l'ignore (no-op), rien
    // n'est réécrit ni journalisé pour lui.
    const edits = [{ ...primaryEdit, nouveau_texte: primaryText, is_primary: true }];
    const items = propagations.querySelectorAll(".correction-prop-item");
    items.forEach((item, i) => {
      const checked = item.querySelector(".correction-prop-check").checked;
      if (!checked) return;
      const after = item.querySelector(".correction-prop-after").value.trim();
      if (!after) return;
      edits.push({ ...candidateEdits[i], nouveau_texte: after, is_primary: false });
    });

    applyBtn.disabled = true;
    applyBtn.textContent = "Application...";

    try {
      const res = await fetch("/api/corrections/apply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          table_name: source.table,
          colonne: source.colonne,
          texte_brut: brut,
          edits
        })
      });
      const data = await res.json();
      if (!res.ok || data.error) {
        errorEl.textContent = data.error || "Application impossible.";
        errorEl.classList.remove("hidden");
        applyBtn.disabled = false;
        return;
      }
      const n = data.count || edits.length;
      successEl.textContent =
        `Correction écrite dans la Knowledge Base (${n} passage${n > 1 ? "s" : ""}). Les prochaines réponses s'appuieront dessus.`;
      successEl.classList.remove("hidden");
      const applied = { table: source.table, colonne: source.colonne };
      setTimeout(() => {
        close();
        if (onApplied) onApplied(applied);
      }, 2200);
    } catch (err) {
      errorEl.textContent = "Connexion au serveur impossible.";
      errorEl.classList.remove("hidden");
      applyBtn.disabled = false;
    } finally {
      applyBtn.textContent = "Confirmer et appliquer";
    }
  });

  return {
    open,
    close,
    isOpen: () => !!context,
  };
}
