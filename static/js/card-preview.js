"use strict";

// Carta ampliada (sempre a imagem da versão em inglês, que vem do servidor em data-preview-front/back).
//
// Marcação:
//   data-preview-front="url large"  data-preview-back="url large (dupla face)"
//   data-preview-small="url small (placeholder enquanto a grande carrega)"  data-preview-name="nome"
//   data-preview-open   -> clique/toque/Enter abre a carta em tela cheia
//
// Mouse: passar por cima de qualquer [data-preview-front] mostra a carta grande ao lado.
// Toque, clique e teclado: [data-preview-open] abre um <dialog> com a carta (Esc, X ou toque fora fecha).
(() => {
  const HOVER_DELAY_MS = 120;
  const GAP = 16;
  const EDGE = 8;
  const CARD_RATIO = 680 / 488;

  const svgIcon = (name) => {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "icon");
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", `#i-${name}`);
    svg.append(use);
    return svg;
  };

  function sourceOf(node) {
    const el = node.closest("[data-preview-front]") || node;
    return {
      front: el.dataset.previewFront,
      back: el.dataset.previewBack || null,
      small: el.dataset.previewSmall || null,
      name: el.dataset.previewName || "",
    };
  }

  // Carta com blur-up: a miniatura ampliada aparece na hora e a grande entra por cima quando carrega.
  function cardElement(url, small, alt) {
    const card = document.createElement("div");
    card.className = "preview-card";
    if (small) {
      const placeholder = new Image();
      placeholder.className = "is-placeholder";
      placeholder.alt = "";
      placeholder.src = small;
      card.append(placeholder);
    }
    const full = new Image();
    full.className = "is-full";
    full.alt = alt;
    full.decoding = "async";
    full.addEventListener("load", () => full.classList.add("is-loaded"), { once: true });
    full.src = url;
    if (full.complete) full.classList.add("is-loaded");
    card.append(full);
    return card;
  }

  // ---------- hover (mouse) ----------

  const pop = document.createElement("div");
  pop.className = "preview-pop";
  pop.setAttribute("aria-hidden", "true");
  document.body.append(pop);

  let hoverTarget = null;
  let hoverTimer = 0;

  function hidePop() {
    clearTimeout(hoverTimer);
    hoverTarget = null;
    pop.classList.remove("is-open");
  }

  function showPop(target) {
    const src = sourceOf(target);
    if (!src.front) return;
    const rect = target.getBoundingClientRect();
    const width = Math.round(Math.min(300, (window.innerHeight - 2 * EDGE) / CARD_RATIO));
    const height = Math.round(width * CARD_RATIO);
    const roomRight = window.innerWidth - rect.right - GAP - EDGE;
    const roomLeft = rect.left - GAP - EDGE;
    // Dupla face lado a lado só quando cabe; senão, só a frente.
    const pair = src.back && Math.max(roomRight, roomLeft) >= 2 * width + 12;
    const total = pair ? 2 * width + 12 : width;

    pop.replaceChildren(cardElement(src.front, src.small, src.name));
    if (pair) pop.append(cardElement(src.back, null, `${src.name} (verso)`));
    pop.style.setProperty("--preview-w", `${width}px`);

    let left;
    const fromRight = roomRight < total && roomLeft >= roomRight;
    if (!fromRight) left = Math.min(rect.right + GAP, window.innerWidth - total - EDGE);
    else left = Math.max(EDGE, rect.left - GAP - total);
    const top = Math.min(Math.max(EDGE, rect.top + rect.height / 2 - height / 2), window.innerHeight - height - EDGE);
    pop.style.left = `${Math.max(EDGE, left)}px`;
    pop.style.top = `${top}px`;
    pop.classList.toggle("from-right", fromRight);
    requestAnimationFrame(() => pop.classList.add("is-open"));
  }

  // pointerover dispara ao entrar em qualquer elemento: decide se ainda está sobre uma carta.
  // (Funciona mesmo quando a lista é redesenhada por baixo do ponteiro.)
  document.addEventListener("pointerover", (event) => {
    if (event.pointerType !== "mouse") return;
    const target = event.target.closest("[data-preview-front]");
    if (target === hoverTarget) return;
    hidePop();
    if (!target || dialogOpen()) return;
    hoverTarget = target;
    hoverTimer = setTimeout(() => { if (hoverTarget === target && target.isConnected) showPop(target); }, HOVER_DELAY_MS);
  });
  document.addEventListener("pointerout", (event) => { if (!event.relatedTarget) hidePop(); });
  window.addEventListener("scroll", hidePop, { capture: true, passive: true });
  window.addEventListener("resize", hidePop);
  document.addEventListener("pointerdown", hidePop);

  // ---------- tela cheia (toque, clique, teclado) ----------

  let dialog = null;
  const dialogOpen = () => Boolean(dialog && dialog.open);

  function buildDialog() {
    dialog = document.createElement("dialog");
    dialog.className = "preview-dialog";
    dialog.setAttribute("aria-label", "Carta ampliada");

    const close = document.createElement("button");
    close.type = "button";
    close.className = "icon-btn preview-close";
    close.setAttribute("aria-label", "Fechar");
    close.title = "Fechar";
    close.append(svgIcon("close"));
    close.addEventListener("click", () => dialog.close());

    const stage = document.createElement("div");
    stage.className = "preview-stage";
    stage.tabIndex = -1;  // o foco abre na carta (como o Dialog do MUI), não no X
    dialog.append(close, stage);

    // Toque fora da carta (no scrim) fecha; toque na carta não.
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog || event.target === stage) dialog.close();
    });
    dialog.addEventListener("close", () => { stage.replaceChildren(); });
    document.body.append(dialog);
    return dialog;
  }

  function openDialog(trigger) {
    const src = sourceOf(trigger);
    if (!src.front) return;
    hidePop();
    const box = dialog || buildDialog();
    const stage = box.querySelector(".preview-stage");
    let showingBack = false;

    const render = () => {
      const url = showingBack ? src.back : src.front;
      const parts = [cardElement(url, showingBack ? null : src.small, showingBack ? `${src.name} (verso)` : src.name)];
      const caption = document.createElement("p");
      caption.className = "preview-caption";
      caption.textContent = src.name;
      parts.push(caption);
      if (src.back) {
        const flip = document.createElement("button");
        flip.type = "button";
        flip.className = "btn btn-tonal";
        flip.append(svgIcon("flip"), showingBack ? "Ver frente" : "Virar carta");
        flip.addEventListener("click", () => { showingBack = !showingBack; render(); stage.querySelector(".btn")?.focus(); });
        parts.push(flip);
      }
      stage.replaceChildren(...parts);
    };

    render();
    box.showModal();
    stage.focus({ preventScroll: true });
  }

  document.addEventListener("click", (event) => {
    const trigger = event.target.closest("[data-preview-open]");
    if (!trigger) return;
    event.preventDefault();
    openDialog(trigger);
  });
})();
