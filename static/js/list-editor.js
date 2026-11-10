"use strict";

(() => {
  const SEARCH_DEBOUNCE_MS = 300;
  const MIN_SEARCH_LENGTH = 2;
  const MAX_QUANTITY = 99;
  const MARKETS = [["liga", "Liga"], ["tcgplayer", "TCG"], ["cardmarket", "CM"]];
  const MONEY = {
    liga: new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL" }),
    tcgplayer: new Intl.NumberFormat("pt-BR", { style: "currency", currency: "USD" }),
    cardmarket: new Intl.NumberFormat("pt-BR", { style: "currency", currency: "EUR" }),
  };
  const EMPTY_PRICES = { tcgplayer: { value: null, foil: false }, cardmarket: { value: null, foil: false } };
  const EMPTY_LIGA = { value: null, foil: false, quality: null, other_edition: false };
  // Cada carta distinta é uma página da Liga no servidor; lotes pequenos preenchem a lista aos poucos.
  const LIGA_BATCH_NAMES = 5;
  const OTHER_EDITION_HINT = "Esta edição não foi encontrada na LigaMagic: menor preço entre as outras edições.";
  // Autosave: espera a pessoa parar de mexer por esse tempo antes de mandar a lista inteira.
  const AUTOSAVE_DELAY_MS = 800;

  const $ = (id) => document.getElementById(id);
  const csrfToken = document.querySelector('meta[name="csrf-token"]').content;

  const initial = JSON.parse($("list-data").textContent);
  const state = {
    id: initial.id,
    name: initial.name,
    cards: initial.cards,
    dirty: false,
    saving: false,
    saveError: false,
    // Sobe a cada alteração: um save que termina com a revisão já mudada não limpa o "dirty".
    revision: 0,
    // Sites de preço ligados na barra "Preços de" (preferência da conta).
    markets: initial.markets || MARKETS.map(([market]) => market),
  };

  const isOn = (market) => state.markets.includes(market);
  const activeMarkets = () => MARKETS.filter(([market]) => isOn(market));
  const scryfallOn = () => isOn("tcgplayer") || isOn("cardmarket");

  const els = {
    name: $("list-name"),
    search: $("card-search"),
    spinner: $("search-spinner"),
    results: $("search-results"),
    rows: $("card-rows"),
    empty: $("empty-cards"),
    count: $("card-count"),
    jpg: $("btn-jpg"),
    saveState: $("save-state"),
    modal: $("prints-modal"),
    modalSubtitle: $("prints-subtitle"),
    printsGrid: $("prints-grid"),
    printsFilter: $("prints-filter"),
    toast: $("toast"),
    totals: $("totals"),
    refresh: $("btn-refresh"),
    pricesAge: $("prices-age"),
  };

  // ---------- helpers ----------

  function el(tag, props = {}, children = []) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value);
    }
    for (const child of [].concat(children)) if (child) node.append(child);
    return node;
  }

  // Ícone do Material UI pelo sprite da página (partials/icons.svg).
  function icon(name) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "icon");
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", `#i-${name}`);
    svg.append(use);
    return svg;
  }

  // Atributos lidos pelo card-preview.js: a imagem grande é sempre da impressão em inglês.
  function previewAttrs(item) {
    const preview = item.preview || {};
    if (!preview.front) return {};
    const attrs = { "data-preview-front": preview.front, "data-preview-name": item.name_en };
    if (preview.back) attrs["data-preview-back"] = preview.back;
    if (item.image) attrs["data-preview-small"] = item.image;
    return attrs;
  }

  let toastTimer;
  function toast(message, kind = "") {
    els.toast.textContent = message;
    els.toast.className = `toast ${kind ? "is-" + kind : ""}`;
    els.toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { els.toast.hidden = true; }, 3500);
  }

  async function api(url, options = {}) {
    const response = await fetch(url, {
      ...options,
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrfToken, ...(options.headers || {}) },
    });
    let data = {};
    try { data = await response.json(); } catch { /* corpo vazio */ }
    if (response.status === 401) {
      window.location.href = "/login";
      throw new Error(data.error || "Sessão expirada.");
    }
    if (!response.ok) {
      const error = new Error(data.error || `Erro ${response.status}`);
      error.status = response.status;  // 503 = a LigaMagic pediu pausa ou recusou
      throw error;
    }
    return data;
  }

  // Placeholder animado no lugar de um valor que ainda está chegando (preço, total).
  function skeleton(size = "") {
    return el("span", { class: `skeleton ${size}` }, el("span", { class: "sr-only", text: "carregando" }));
  }

  function setDirty(value) {
    state.dirty = value;
    if (value) {
      state.revision += 1;
      state.saveError = false;
      scheduleSave();
    }
    updateToolbar();
  }

  // O que impede salvar agora (a API recusa lista sem nome ou sem carta): vira o texto do status.
  function saveBlocker() {
    if (!els.name.value.trim()) return "Dê um nome para salvar";
    if (!state.cards.length) return "Adicione uma carta para salvar";
    return null;
  }

  function updateToolbar() {
    // O JPG sai do que está salvo no banco, então só libera com tudo salvo.
    const canDownload = Boolean(state.id) && !state.dirty && state.cards.length > 0;
    els.jpg.disabled = !canDownload;
    els.jpg.title = canDownload ? "" : "A imagem libera quando a lista terminar de salvar";
    if (state.id) els.jpg.dataset.exportUrl = `/lists/${state.id}/want.jpg`;
    renderSaveState();
  }

  function renderSaveState() {
    const blocker = state.dirty ? saveBlocker() : null;
    let kind;
    if (state.saving || (state.dirty && !blocker && !state.saveError)) kind = "saving";
    else if (state.saveError) kind = "error";
    else if (blocker) kind = `blocked:${blocker}`;
    else if (state.id) kind = "saved";
    else kind = "";

    const node = els.saveState;
    // Só redesenha quando muda: preserva o foco no "Tentar de novo" e não reinicia a animação.
    if (node.dataset.kind === kind) return;
    node.dataset.kind = kind;
    node.hidden = !kind;
    node.className = `save-state is-${kind.split(":")[0]}`;
    if (kind === "saving") node.replaceChildren(el("span", { class: "save-spinner", "aria-hidden": "true" }), "Salvando…");
    else if (kind === "saved") node.replaceChildren(icon("check"), "Salvo");
    else if (kind === "error") {
      node.replaceChildren(icon("info"), "Não salvou", el("button", {
        type: "button", class: "save-retry", onclick: () => save(),
      }, "Tentar de novo"));
    } else if (blocker) node.replaceChildren(icon("info"), blocker);
    else node.replaceChildren();
  }

  // ---------- preços ----------

  // card.prices (Scryfall): null = ainda carregando. card.liga: undefined = ainda carregando.
  // Objeto com value null = carta sem preço naquele marketplace.
  function priceOf(item, market) {
    if (market === "liga") return item.liga;
    return item.prices ? item.prices[market] : undefined;
  }

  function isLoading(item, market) {
    return market === "liga" ? item.liga === undefined : !item.prices;
  }

  function priceText(price, market) {
    if (!price || price.value === null || price.value === undefined) return "—";
    return MONEY[market].format(price.value) + (price.foil ? " foil" : "");
  }

  function renderPriceLines(item, quantity = 1) {
    return activeMarkets().map(([market, label]) => {
      const loading = isLoading(item, market);
      const price = loading ? null : priceOf(item, market);
      const hasValue = price && price.value !== null && price.value !== undefined;
      const subtotal = hasValue && quantity > 1
        ? `${quantity}× = ${MONEY[market].format(price.value * quantity)}`
        : "";
      // Fade-in só no render em que o preço chega; os renders seguintes mostram direto.
      const shownKey = `_shown_${market}`;
      const arriving = !loading && !item[shownKey];
      if (!loading) item[shownKey] = true;
      else delete item[shownKey];
      return el("span", { class: `price-line${arriving ? " price-in" : ""}`, "data-market": market }, [
        el("b", { text: label }),
        loading ? skeleton() : el("span", { text: priceText(price, market) }),
        hasValue && price.quality
          ? el("em", { class: "quality-badge", text: price.quality, title: "Melhor qualidade em estoque nesta edição" })
          : null,
        hasValue && price.other_edition
          ? el("small", { class: "price-flag", text: "outra ed.", title: OTHER_EDITION_HINT })
          : null,
        subtotal ? el("small", { text: subtotal }) : null,
      ]);
    });
  }

  function renderTotals() {
    els.totals.hidden = state.cards.length === 0 || state.markets.length === 0;
    for (const box of els.totals.querySelectorAll(".total-box[data-market]")) box.hidden = !isOn(box.dataset.market);
    for (const [market] of activeMarkets()) {
      let total = 0;
      let missing = 0;
      let loading = false;
      for (const card of state.cards) {
        if (isLoading(card, market)) { loading = true; continue; }
        const value = priceOf(card, market)?.value;
        if (value === null || value === undefined) missing += card.quantity;
        else total += value * card.quantity;
      }
      const totalNode = $(`total-${market}`);
      if (loading) {
        if (!totalNode.querySelector(".skeleton")) totalNode.replaceChildren(skeleton("is-lg"));
      } else {
        totalNode.textContent = MONEY[market].format(total);
      }
      $(`missing-${market}`).textContent = missing ? `${missing} ${missing === 1 ? "carta" : "cartas"} sem preço` : "";
    }
  }

  let detailsRunning = false;

  async function loadDetails(refresh = false) {
    const ids = [...new Set(state.cards.filter((c) => !c.prices).map((c) => c.scryfall_id))];
    if (!ids.length) return;
    detailsRunning = true;
    updateRefreshButton();
    try {
      const data = await api("/api/cards/details", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids, refresh }),
      });
      for (const card of state.cards) {
        const detail = data.details[card.scryfall_id];
        if (detail) {
          card.prices = card.prices || detail.prices;
          card.image = card.image || detail.image;
          card.preview = card.preview || detail.preview;
        }
      }
    } catch (error) {
      toast(`Não foi possível carregar os preços: ${error.message}`, "error");
    }
    for (const card of state.cards) if (!card.prices) card.prices = EMPTY_PRICES;
    detailsRunning = false;
    renderCards();
  }

  // ---------- LigaMagic ----------

  // Mesma chave do servidor (ligamagic.cache_key): nome completo, sem diferenciar maiúsculas.
  const ligaKey = (name) => name.split(/\s+/).filter(Boolean).join(" ").toLowerCase();

  function ligaPayload(item) {
    return {
      name_en: item.name_en, set_code: item.set_code,
      collector_number: item.collector_number, set_name: item.set_name,
    };
  }

  async function fetchLiga(items, refresh = false) {
    const data = await api("/api/cards/liga", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cards: items.map(ligaPayload), refresh }),
    });
    return data.prices;
  }

  let ligaRunning = false;
  // Ligado pelo "Atualizar preços": vale para todos os lotes até a fila esvaziar.
  let ligaRefresh = false;

  // Busca em lotes até não sobrar carta pendente; carta adicionada no meio entra no próximo lote.
  async function loadLiga() {
    if (ligaRunning || !isOn("liga")) return;
    ligaRunning = true;
    try {
      for (;;) {
        if (!isOn("liga")) break;  // desligada pela barra no meio da carga: o resto fica pendente
        const pending = state.cards.filter((c) => c.liga === undefined);
        if (!pending.length) break;
        const names = new Set();
        const batch = pending.filter((card) => {
          const name = ligaKey(card.name_en);
          if (!names.has(name) && names.size >= LIGA_BATCH_NAMES) return false;
          names.add(name);
          return true;
        });
        try {
          const prices = await fetchLiga(batch, ligaRefresh);
          batch.forEach((card, i) => { card.liga = prices[i] || EMPTY_LIGA; });
        } catch (error) {
          // Pausa/recusa da Liga: as pendentes ficam marcadas para o "Tentar agora".
          const paused = error.status === 503;
          for (const card of pending) { card.liga = EMPTY_LIGA; if (paused) card.ligaPaused = true; }
          if (paused) showLigaPause(error.message);
          toast(`Não foi possível carregar os preços da LigaMagic: ${error.message}`, "error");
        }
        renderCards();
      }
    } finally {
      ligaRunning = false;
      ligaRefresh = false;
      updateRefreshButton();
    }
  }

  // "Tentar agora": libera a pausa no servidor e busca de novo só as cartas que ficaram sem a Liga por ela.
  const ligaPauseBox = el("span", { class: "liga-pause muted small", hidden: "" });
  els.pricesAge.after(ligaPauseBox);

  function showLigaPause(message) {
    ligaPauseBox.hidden = false;
    ligaPauseBox.replaceChildren(message, " ", el("button", {
      type: "button", class: "btn btn-text btn-sm", onclick: async () => {
        try {
          await api("/api/liga/resume", { method: "POST" });
        } catch (error) {
          toast(error.message, "error");
          return;
        }
        ligaPauseBox.hidden = true;
        for (const card of state.cards) {
          if (card.ligaPaused) { card.liga = undefined; delete card.ligaPaused; }
        }
        renderCards();
        loadLiga();
      },
    }, [icon("refresh"), "Tentar agora"]));
  }

  // ---------- atualizar preços ----------

  const AGE_FORMAT = new Intl.DateTimeFormat("pt-BR", {
    day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit",
  });

  function updateRefreshButton() {
    const busy = ligaRunning || detailsRunning;
    els.refresh.hidden = state.cards.length === 0 || state.markets.length === 0;
    els.refresh.disabled = busy;
    els.refresh.textContent = busy ? "Atualizando…" : "Atualizar preços";

    // A consulta mais antiga da lista é a que importa: "preços da Liga de" quando.
    const times = isOn("liga")
      ? state.cards.map((c) => c.liga?.fetched_at).filter(Boolean).map((t) => new Date(t))
      : [];
    if (!times.length) { els.pricesAge.textContent = ""; return; }
    const oldest = new Date(Math.min(...times));
    els.pricesAge.textContent = `Liga de ${AGE_FORMAT.format(oldest)}`;
    els.pricesAge.title = "Hora da consulta mais antiga entre as cartas da lista";
  }

  function refreshPrices() {
    if (ligaRunning || detailsRunning || !state.cards.length || !state.markets.length) return;
    // Só os sites ligados: com a Liga sozinha, o Scryfall nem é consultado.
    for (const card of state.cards) {
      if (scryfallOn()) card.prices = null;
      if (isOn("liga")) card.liga = undefined;
    }
    ligaRefresh = isOn("liga");
    renderCards();
    if (scryfallOn()) loadDetails(true);
    loadLiga();
  }

  els.refresh.addEventListener("click", refreshPrices);

  // Barra "Preços de" (market-bar.js): mostra/esconde na hora e busca a Liga se ela foi ligada agora.
  document.addEventListener("markets-change", (event) => {
    state.markets = event.detail.markets;
    renderCards();
    if (!els.modal.hidden && modalContext && currentPrints.length) {
      renderPrints();
      if (isOn("liga") && currentPrints.some((p) => p.liga === undefined)) loadPrintsLiga(currentPrints);
    }
    loadLiga();
  });

  // ---------- lista de cartas ----------

  function renderCards() {
    els.rows.replaceChildren(...state.cards.map(renderCardRow));
    els.empty.hidden = state.cards.length > 0;
    const total = state.cards.reduce((sum, c) => sum + c.quantity, 0);
    els.count.textContent = state.cards.length
      ? `${total} ${total === 1 ? "carta" : "cartas"}, ${state.cards.length} distintas`
      : "";
    renderTotals();
    updateToolbar();
    updateRefreshButton();
  }

  function renderCardRow(card, index) {
    const thumb = card.image
      ? el("img", { class: "card-thumb", src: card.image, alt: "", loading: "lazy" })
      : el("div", { class: "card-thumb", text: card.set_code });
    // Quantidade vira pilha: 2 cópias = uma carta atrás, 3+ = duas.
    const stackClass = card.quantity >= 3 ? "stack-3" : card.quantity === 2 ? "stack-2" : "";
    const stackNode = el("div", { class: `card-stack ${stackClass}` }, thumb);
    // Miniatura = botão: no mouse o hover mostra a carta grande; toque/clique/Enter abre em tela cheia.
    const zoom = previewAttrs(card);
    const stack = zoom["data-preview-front"]
      ? el("button", {
        type: "button", class: "card-thumb-btn", "data-preview-open": "",
        "aria-label": `Ampliar ${card.name_en}`, ...zoom,
      }, stackNode)
      : stackNode;

    const title = card.name_pt || card.name_en;
    // Linha de colecionador, como no rodapé da carta: código, número, edição.
    const sub = [card.name_pt ? card.name_en : null, `${card.set_code} #${card.collector_number}, ${card.set_name}`]
      .filter(Boolean).join(" / ");

    const qty = el("div", { class: "qty" }, [
      el("button", { type: "button", "aria-label": "Diminuir", title: "Diminuir", onclick: () => changeQuantity(index, -1) }, icon("remove")),
      el("output", { text: String(card.quantity), "aria-label": "Quantidade" }),
      el("button", { type: "button", "aria-label": "Aumentar", title: "Aumentar", onclick: () => changeQuantity(index, 1) }, icon("add")),
    ]);

    const actions = el("div", { class: "card-actions" }, [
      el("button", {
        type: "button", class: "btn btn-text btn-sm", title: "Trocar a edição",
        onclick: () => openPrints({ name_en: card.name_en, name_pt: card.name_pt }, index),
      }, [icon("swap"), "Edição"]),
      el("button", {
        type: "button", class: "icon-btn is-danger", "aria-label": `Remover ${title}`, title: "Remover",
        onclick: () => removeCard(index),
      }, icon("delete")),
    ]);

    return el("li", { class: "card-row" }, [
      stack,
      el("div", { class: "card-info" }, [el("strong", { text: title, title }), el("span", { text: sub, title: sub })]),
      el("div", { class: "card-prices" }, renderPriceLines(card, card.quantity)),
      qty,
      actions,
    ]);
  }

  function changeQuantity(index, delta) {
    const card = state.cards[index];
    const next = card.quantity + delta;
    if (next < 1) { removeCard(index); return; }
    card.quantity = Math.min(next, MAX_QUANTITY);
    setDirty(true);
    renderCards();
  }

  function removeCard(index) {
    state.cards.splice(index, 1);
    setDirty(true);
    renderCards();
  }

  function applyPrint(print, names, replaceIndex) {
    const card = {
      scryfall_id: print.id,
      name_en: print.name_en,
      name_pt: names.name_pt || null,
      set_code: print.set_code,
      set_name: print.set_name,
      collector_number: print.collector_number,
      image: print.image,
      preview: print.preview,
      prices: print.prices || EMPTY_PRICES,
      liga: print.liga,  // undefined se o modal fechou antes da Liga responder: loadLiga() busca
    };

    const duplicate = state.cards.findIndex((c, i) => c.scryfall_id === print.id && i !== replaceIndex);
    if (replaceIndex !== null) {
      const quantity = state.cards[replaceIndex].quantity;
      if (duplicate >= 0) {
        // Trocou para uma versão que já está na lista: soma as quantidades.
        state.cards[duplicate].quantity = Math.min(state.cards[duplicate].quantity + quantity, MAX_QUANTITY);
        state.cards.splice(replaceIndex, 1);
      } else {
        state.cards[replaceIndex] = { ...card, quantity };
      }
    } else if (duplicate >= 0) {
      state.cards[duplicate].quantity = Math.min(state.cards[duplicate].quantity + 1, MAX_QUANTITY);
      toast(`+1 ${card.name_en} (${card.set_code})`);
    } else {
      state.cards.push({ ...card, quantity: 1 });
      toast(`${names.name_pt || card.name_en} adicionada`, "success");
    }
    setDirty(true);
    renderCards();
    loadLiga();
  }

  // ---------- busca ----------

  let searchTimer;
  let searchController;
  let searchItems = [];
  let activeIndex = -1;

  function closeResults() {
    els.results.hidden = true;
    els.search.setAttribute("aria-expanded", "false");
    activeIndex = -1;
  }

  function renderResults(results, term) {
    searchItems = results;
    activeIndex = -1;
    if (!results.length) {
      els.results.replaceChildren(el("li", { class: "result-empty", text: `Nenhuma carta encontrada para "${term}".` }));
    } else {
      els.results.replaceChildren(...results.map((item, i) => {
        const isPt = Boolean(item.name_pt) && item.lang === "pt";
        const main = isPt ? item.name_pt : item.name_en;
        const sub = isPt ? item.name_en : item.name_pt;
        return el("li", {
          role: "option", id: `result-${i}`,
          onmousedown: (event) => { event.preventDefault(); chooseResult(i); },
        }, [
          el("div", { class: "result-names" }, [
            el("span", { text: main }),
            sub ? el("span", { class: "result-sub", text: sub }) : null,
          ]),
          el("span", { class: `lang-badge ${isPt ? "pt" : ""}`, text: isPt ? "PT" : "EN" }),
        ]);
      }));
    }
    els.results.hidden = false;
    els.search.setAttribute("aria-expanded", "true");
  }

  function highlight(index) {
    const items = els.results.querySelectorAll('li[role="option"]');
    items.forEach((li, i) => li.classList.toggle("is-active", i === index));
    if (items[index]) items[index].scrollIntoView({ block: "nearest" });
    activeIndex = index;
  }

  function chooseResult(index) {
    const item = searchItems[index];
    if (!item) return;
    closeResults();
    els.search.value = "";
    openPrints(item, null);
  }

  async function runSearch(term) {
    if (searchController) searchController.abort();
    searchController = new AbortController();
    els.spinner.hidden = false;
    try {
      const data = await api(`/api/cards/search?q=${encodeURIComponent(term)}`, { signal: searchController.signal });
      if (els.search.value.trim() === term) renderResults(data.results, term);
    } catch (error) {
      if (error.name !== "AbortError") toast(error.message, "error");
    } finally {
      els.spinner.hidden = true;
    }
  }

  els.search.addEventListener("input", () => {
    clearTimeout(searchTimer);
    const term = els.search.value.trim();
    if (term.length < MIN_SEARCH_LENGTH) {
      if (searchController) searchController.abort();
      closeResults();
      return;
    }
    searchTimer = setTimeout(() => runSearch(term), SEARCH_DEBOUNCE_MS);
  });

  els.search.addEventListener("keydown", (event) => {
    if (els.results.hidden) return;
    const count = els.results.querySelectorAll('li[role="option"]').length;
    if (event.key === "ArrowDown" && count) { event.preventDefault(); highlight((activeIndex + 1) % count); }
    else if (event.key === "ArrowUp" && count) { event.preventDefault(); highlight((activeIndex - 1 + count) % count); }
    else if (event.key === "Enter") { event.preventDefault(); chooseResult(activeIndex >= 0 ? activeIndex : 0); }
    else if (event.key === "Escape") closeResults();
  });

  els.search.addEventListener("blur", () => setTimeout(closeResults, 120));

  // ---------- modal de versões ----------

  let currentPrints = [];
  let modalContext = null;

  function renderPrints() {
    const filter = els.printsFilter.value.trim().toLowerCase();
    const selectedId = modalContext.replaceIndex !== null ? state.cards[modalContext.replaceIndex]?.scryfall_id : null;
    const visible = currentPrints.filter((p) =>
      !filter || `${p.set_name} ${p.set_code} ${p.year} ${p.collector_number}`.toLowerCase().includes(filter));

    if (!visible.length) {
      els.printsGrid.replaceChildren(el("p", { class: "prints-loading", text: "Nenhuma versão com esse filtro." }));
      return;
    }
    // Cada edição: um botão que escolhe (tocar na carta) e, ao lado, a lupa que só amplia.
    els.printsGrid.replaceChildren(...visible.map((print) => {
      const zoom = previewAttrs(print);
      const label = `${print.set_name} (${print.set_code}) #${print.collector_number}`;
      return el("div", { class: `print-option ${print.id === selectedId ? "is-selected" : ""}` }, [
        el("button", {
          type: "button", class: "print-pick", "aria-label": `Escolher ${label}`,
          onclick: () => { closeModal(); applyPrint(print, modalContext.names, modalContext.replaceIndex); },
        }, [
          print.image ? el("img", { src: print.image, alt: "", loading: "lazy", ...zoom }) : el("img", { alt: "" }),
          el("strong", { text: print.set_name }),
          el("span", { text: `${print.set_code} #${print.collector_number}, ${print.year}` }),
          el("div", { class: "print-prices" }, renderPriceLines(print)),
        ]),
        zoom["data-preview-front"]
          ? el("button", {
            type: "button", class: "icon-btn is-sm print-zoom", "data-preview-open": "",
            "aria-label": `Ampliar ${label}`, title: "Ampliar", ...zoom,
          }, icon("zoom-in"))
          : null,
      ]);
    }));
  }

  async function openPrints(names, replaceIndex) {
    modalContext = { names, replaceIndex };
    currentPrints = [];
    els.printsFilter.value = "";
    els.modalSubtitle.textContent = names.name_pt ? `${names.name_pt} — ${names.name_en}` : names.name_en;
    els.printsGrid.replaceChildren(el("p", { class: "prints-loading", text: "Carregando versões no Scryfall…" }));
    els.modal.hidden = false;
    document.body.style.overflow = "hidden";

    try {
      const data = await api(`/api/cards/prints?name=${encodeURIComponent(names.name_en)}`);
      if (modalContext?.names !== names) return;
      currentPrints = data.prints;
      if (!currentPrints.length) {
        els.printsGrid.replaceChildren(el("p", { class: "prints-loading", text: "Nenhuma versão em inglês encontrada." }));
        return;
      }
      renderPrints();
      els.printsFilter.focus();
    } catch (error) {
      els.printsGrid.replaceChildren(el("p", { class: "prints-loading", text: error.message }));
      return;
    }
    if (isOn("liga")) loadPrintsLiga(currentPrints);
  }

  // Todas as impressões são da mesma carta: uma página da Liga só, chegando depois do Scryfall.
  async function loadPrintsLiga(prints) {
    try {
      const prices = await fetchLiga(prints);
      prints.forEach((print, i) => { print.liga = prices[i] || EMPTY_LIGA; });
    } catch (error) {
      for (const print of prints) print.liga = EMPTY_LIGA;
      if (currentPrints === prints) toast(`LigaMagic: ${error.message}`, "error");
    }
    if (currentPrints === prints && !els.modal.hidden) renderPrints();
  }

  function closeModal() {
    els.modal.hidden = true;
    document.body.style.overflow = "";
  }

  els.printsFilter.addEventListener("input", renderPrints);
  els.modal.querySelectorAll("[data-close-modal]").forEach((node) => node.addEventListener("click", () => {
    closeModal();
    els.search.focus();
  }));
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    // Um <dialog> aberto por cima (carta ampliada, confirmação) fecha sozinho com Esc; o modal fica.
    if (document.querySelector("dialog[open]")) return;
    if (!els.modal.hidden) closeModal();
    // Tooltip de informação aberto pelo foco do teclado: Esc fecha.
    else if (document.activeElement?.classList.contains("info-btn")) document.activeElement.blur();
  });

  // ---------- salvar / JPG ----------

  // ---------- autosave / JPG ----------

  let saveTimer;

  function scheduleSave(delay = AUTOSAVE_DELAY_MS) {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(save, delay);
  }

  const savePayload = () => JSON.stringify({
    name: els.name.value.trim(),
    cards: state.cards.map((c) => ({ scryfall_id: c.scryfall_id, quantity: c.quantity, name_pt: c.name_pt })),
  });

  // Um save por vez: dois POST /api/lists simultâneos numa lista nova criariam duas listas.
  // Alteração feita durante o save sobe a revisão, e o finally agenda o próximo.
  async function save() {
    clearTimeout(saveTimer);
    if (state.saving || !state.dirty || saveBlocker()) { updateToolbar(); return; }

    const revision = state.revision;
    const name = els.name.value.trim();
    state.saving = true;
    state.saveError = false;
    updateToolbar();
    try {
      const data = await api(state.id ? `/api/lists/${state.id}` : "/api/lists", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: savePayload(),
      });
      state.id = data.id;
      state.name = name;
      history.replaceState(null, "", data.url);
      document.title = `${name} · LigaWant`;
      if (state.revision === revision) state.dirty = false;
    } catch (error) {
      state.saveError = true;
      // fetch rejeita com TypeError quando nem chega ao servidor (rede caiu, servidor desligado).
      const reason = error instanceof TypeError ? "sem conexão com o servidor" : error.message;
      toast(`Não foi possível salvar: ${reason}`, "error");
    } finally {
      state.saving = false;
      if (state.dirty && !state.saveError) scheduleSave();
      updateToolbar();
    }
  }

  els.name.addEventListener("input", () => setDirty(true));

  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      state.saveError = false;
      save();
    }
  });

  // Saindo com alteração no debounce: a lista já existe, então o keepalive manda sem perguntar.
  // Lista nova (o create devolve o id) ou bloqueada (sem nome/carta) ainda pede confirmação.
  window.addEventListener("beforeunload", (event) => {
    if (!state.dirty) return;
    if (state.id && !saveBlocker()) {
      clearTimeout(saveTimer);
      fetch(`/api/lists/${state.id}`, {
        method: "POST",
        keepalive: true,
        credentials: "same-origin",
        headers: { "X-CSRF-Token": csrfToken, "Content-Type": "application/json" },
        body: savePayload(),
      });
      return;
    }
    event.preventDefault();
    event.returnValue = "";
  });

  // ---------- init ----------

  els.name.value = state.name;
  renderCards();
  loadDetails();
  loadLiga();
  if (!state.id) els.name.focus();
})();
