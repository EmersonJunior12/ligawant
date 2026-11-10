"use strict";

// "Buscar em coleção": lê uma coleção pública da LigaMagic página a página e cruza com as listas.
// Fase 1 (/api/collection/page): só as linhas cujo nome está em alguma lista voltam do servidor.
// Fase 2 (/api/collection/compare): mesma edição ou não + preços da Liga, em lotes pequenos para a
// tela ir se preenchendo (cada nome fora do cache é uma página da Liga no servidor).
// Depois: o usuário marca o que quer pegar da coleção, vê os totais e gera uma mensagem para o dono.
(() => {
  const COMPARE_BATCH_NAMES = 5;
  const COMPARE_BATCH_ITEMS = 60;
  const READ_SHARE = 0.6;  // fatia da barra de progresso para a leitura; o resto é a comparação
  const PICKS_STORAGE_PREFIX = "collection-picks:";
  const BRL = new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL" });
  const LANG_SHORT = { "Português": "PT", "Inglês": "EN", "Japonês": "JP", "Espanhol": "ES", "Alemão": "DE",
    "Francês": "FR", "Italiano": "IT", "Coreano": "KO", "Russo": "RU", "Chinês Simplificado": "ZHS",
    "Chinês Tradicional": "ZHT" };
  const OTHER_EDITION_HINT = "A edição da sua lista não foi encontrada na LigaMagic: menor preço entre as outras edições.";

  const $ = (id) => document.getElementById(id);
  const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
  const els = {
    form: $("collection-form"),
    input: $("collection-url"),
    submit: $("collection-submit"),
    progress: $("collection-progress"),
    progressBar: $("collection-progress-bar"),
    progressText: $("collection-progress-text"),
    results: $("collection-results"),
    title: $("collection-title"),
    summary: $("collection-summary"),
    link: $("collection-link"),
    groups: $("collection-groups"),
    empty: $("collection-empty"),
    fab: $("picks-fab"),
    fabCount: $("picks-fab-count"),
    fabTotal: $("picks-fab-total"),
    drawer: $("picks-drawer"),
    picksCount: $("picks-count"),
    picksList: $("picks-list"),
    picksClear: $("picks-clear"),
    picksMessage: $("picks-message"),
    picksDiff: $("picks-diff"),
    messageDialog: $("message-dialog"),
    messageText: $("message-text"),
    messageCopy: $("message-copy"),
    toast: $("toast"),
  };

  // run: incrementa a cada busca; respostas de uma busca antiga são descartadas.
  // picks: chaves das comparações marcadas ("pegar da coleção"), por coleção.
  // pause: mensagem da pausa pedida pela LigaMagic (503), mostrada no grupo "Sem comparação".
  const state = { run: 0, info: null, items: [], rowsRead: 0, partial: false, pause: null, picks: new Set() };

  // ---------- helpers (mesmo padrão do list-editor.js) ----------

  function el(tag, props = {}, children = []) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (value === true) node.setAttribute(key, "");
      else if (value !== false && value !== null && value !== undefined) node.setAttribute(key, value);
    }
    for (const child of [].concat(children)) if (child) node.append(child);
    return node;
  }

  function icon(name) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "icon");
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", `#i-${name}`);
    svg.append(use);
    return svg;
  }

  let toastTimer;
  function toast(message, kind = "") {
    els.toast.textContent = message;
    els.toast.className = `toast ${kind ? "is-" + kind : ""}`;
    els.toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { els.toast.hidden = true; }, 4500);
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
      error.status = response.status;  // 503 = a LigaMagic pediu pausa: quem chama para de insistir
      throw error;
    }
    return data;
  }

  const isLigaPause = (error) => error && error.status === 503;

  const postJson = (url, body) => api(url, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });

  function setProgress(fraction, text) {
    const percent = Math.round(Math.max(0, Math.min(1, fraction)) * 100);
    els.progress.hidden = false;
    els.progressBar.style.width = `${percent}%`;
    els.progress.querySelector('[role="progressbar"]').setAttribute("aria-valuenow", String(percent));
    els.progressText.textContent = text;
  }

  function setBusy(busy) {
    els.submit.disabled = busy;
    els.input.readOnly = busy;
  }

  const plural = (n, one, many) => `${n.toLocaleString("pt-BR")} ${n === 1 ? one : many}`;

  // ---------- marcações (localStorage: conveniência deste navegador, nunca obrigatório) ----------

  // Identifica a comparação pela cópia da coleção + a carta da lista, estável entre buscas.
  const pickKey = (row, want) =>
    [row.ed, row.number, row.foil ? "f" : "", row.language, row.quality, want.list_id, want.scryfall_id].join("|");

  function loadPicks(collectionId) {
    try {
      const saved = JSON.parse(localStorage.getItem(PICKS_STORAGE_PREFIX + collectionId) || "[]");
      return new Set(Array.isArray(saved) ? saved.filter((k) => typeof k === "string") : []);
    } catch {
      return new Set();
    }
  }

  function savePicks() {
    if (!state.info) return;
    try {
      const key = PICKS_STORAGE_PREFIX + state.info.collection_id;
      if (state.picks.size) localStorage.setItem(key, JSON.stringify([...state.picks]));
      else localStorage.removeItem(key);
    } catch { /* sem storage: as marcações valem só até recarregar */ }
  }

  // Quantidade de cada comparação marcada: o que a lista quer, limitado às cópias da linha da coleção
  // que outra lista ainda não pegou. Comparação não marcada sem cópia sobrando fica desabilitada.
  function allocate() {
    for (const item of state.items) {
      let left = item.row.quantity;
      for (const want of item.wants) {
        want.picked = state.picks.has(pickKey(item.row, want));
        want.pickQty = want.picked ? Math.min(want.quantity, left) : 0;
        left -= want.pickQty;
      }
      for (const want of item.wants) want.soldOut = !want.picked && left <= 0;
    }
  }

  const pickedEntries = () => state.items.flatMap((item) =>
    item.wants.filter((want) => want.picked && want.pickQty > 0).map((want) => ({ item, want })));

  // ---------- leitura da coleção ----------

  async function run(text) {
    const runId = ++state.run;
    state.info = null;
    state.items = [];
    state.rowsRead = 0;
    state.partial = false;
    state.pause = null;
    state.picks = new Set();
    els.results.hidden = true;
    els.fab.hidden = true;
    document.body.classList.remove("has-picks-fab");
    if (els.drawer.open) els.drawer.close();
    els.groups.replaceChildren();
    setBusy(true);
    setProgress(0, "Abrindo a coleção na LigaMagic…");

    let first;
    try {
      first = await api(`/api/collection/page?${new URLSearchParams({ collection: text, page: "1" })}`);
    } catch (error) {
      if (runId !== state.run) return;
      els.progress.hidden = true;
      setBusy(false);
      toast(error.message, "error");
      els.input.focus();
      return;
    }
    if (runId !== state.run) return;

    state.info = first;
    state.picks = loadPicks(first.collection_id);
    history.replaceState(null, "", `/collection?collection=${first.collection_id}`);
    els.link.href = `https://www.ligamagic.com.br/?view=colecao/colecao&id=${first.collection_id}`;
    els.title.textContent = first.title || `Coleção ${first.collection_id}`;
    addMatches(first);
    els.results.hidden = false;
    render();

    for (let page = 2; page <= first.pages; page++) {
      setProgress(READ_SHARE * (page - 1) / first.pages,
        `Lendo a coleção: página ${page} de ${first.pages} · ${plural(state.items.length, "carta", "cartas")} das suas listas até agora`);
      try {
        const data = await api(`/api/collection/page?${new URLSearchParams({ collection: String(first.collection_id), page: String(page) })}`);
        if (runId !== state.run) return;
        addMatches(data);
        render();
      } catch (error) {
        if (runId !== state.run) return;
        state.partial = true;
        if (isLigaPause(error)) state.pause = error.message;
        toast(`Leitura parou na página ${page} de ${first.pages}: ${error.message}`, "error");
        break;
      }
    }

    // Com a Liga em pausa, nem tenta comparar: tudo vai direto para "Sem comparação".
    if (state.pause) {
      for (const item of state.items) item.failed = true;
      await loadThumbnails(runId);
    } else {
      await Promise.all([compareAll(runId), loadThumbnails(runId)]);
    }
    if (runId !== state.run) return;
    els.progress.hidden = true;
    setBusy(false);
    render();
  }

  function addMatches(data) {
    state.rowsRead += data.rows_in_page;
    for (const match of data.matches) {
      state.items.push({
        row: match.row,
        wants: match.wants.map((want) => ({ ...want, result: undefined, picked: false, pickQty: 0, soldOut: false })),
        copy: undefined,     // preço da cópia da coleção (undefined = comparando)
        failed: false,
        image: null,
        preview: null,
      });
    }
  }

  // ---------- comparação ----------

  function batches(items) {
    const out = [];
    let current = [];
    let names = new Set();
    for (const item of items) {
      const itemNames = new Set(item.wants.map((w) => w.name_en.toLowerCase()));
      const merged = new Set([...names, ...itemNames]);
      if (current.length && (merged.size > COMPARE_BATCH_NAMES || current.length >= COMPARE_BATCH_ITEMS)) {
        out.push(current);
        current = [];
        names = itemNames;
      } else {
        names = merged;
      }
      current.push(item);
    }
    if (current.length) out.push(current);
    return out;
  }

  async function compareAll(runId, items = state.items) {
    const total = items.length;
    let done = 0;
    const progress = () => setProgress(READ_SHARE + (1 - READ_SHARE) * (total ? done / total : 1),
      `Comparando edições e preços: ${done} de ${plural(total, "carta", "cartas")}`);
    progress();
    for (const batch of batches(items)) {
      // A Liga pediu pausa num lote anterior: os lotes restantes nem saem.
      if (state.pause) {
        for (const item of batch) item.failed = true;
        done += batch.length;
        continue;
      }
      try {
        const data = await postJson("/api/collection/compare", {
          items: batch.map((item) => ({
            ed: item.row.ed, number: item.row.number, foil: item.row.foil, price_buy: item.row.price_buy,
            wants: item.wants.map((w) => ({
              name_en: w.name_en, set_code: w.set_code, collector_number: w.collector_number, set_name: w.set_name,
            })),
          })),
        });
        if (runId !== state.run) return;
        batch.forEach((item, i) => {
          const result = data.results[i];
          item.copy = result.copy;
          item.wants.forEach((want, j) => { want.result = result.wants[j]; });
        });
      } catch (error) {
        if (runId !== state.run) return;
        for (const item of batch) item.failed = true;
        if (isLigaPause(error)) state.pause = error.message;
        toast(`Comparação: ${error.message}`, "error");
      }
      done += batch.length;
      progress();
      render();
    }
  }

  // Miniatura e carta ampliada da impressão da lista (a mesma do editor).
  async function loadThumbnails(runId) {
    const ids = [...new Set(state.items.map((item) => item.wants[0].scryfall_id))].slice(0, 200);
    if (!ids.length) return;
    try {
      const data = await postJson("/api/cards/details", { ids });
      if (runId !== state.run) return;
      for (const item of state.items) {
        const detail = data.details[item.wants[0].scryfall_id];
        if (detail) { item.image = detail.image; item.preview = detail.preview; }
      }
      render();
    } catch { /* sem miniatura: a linha mostra o código da edição */ }
  }

  // ---------- render: resultados ----------

  const isPending = (item) => !item.failed && item.copy === undefined;
  const isSame = (item) => item.wants.some((w) => w.result && w.result.same_edition);
  const copyName = (item) => item.row.name_pt || item.wants[0].name_pt || item.row.name_en;

  // Placeholder animado no lugar de um valor que ainda está chegando (preço, selo, total).
  function skeleton(size = "") {
    return el("span", { class: `skeleton ${size}` }, el("span", { class: "sr-only", text: "carregando" }));
  }

  function priceText(price) {
    if (!price || price.value === null || price.value === undefined) return "—";
    return BRL.format(price.value) + (price.foil ? " foil" : "");
  }

  // undefined = ainda comparando: skeleton no lugar do valor.
  const priceNode = (price) => price === undefined
    ? skeleton()
    : el("b", { class: "match-price", text: priceText(price) });

  function diffBadge(diff, title) {
    if (diff === null || diff === undefined) return null;
    if (Math.abs(diff) < 0.005) return el("span", { class: "diff-badge is-even", text: "mesmo preço" });
    const up = diff > 0;
    return el("span", {
      class: `diff-badge ${up ? "is-up" : "is-down"}`,
      text: `${up ? "+" : "−"}${BRL.format(Math.abs(diff))}`,
      title: title || `A cópia da coleção custa ${BRL.format(Math.abs(diff))} ${up ? "a mais" : "a menos"} que a edição da sua lista`,
    });
  }

  function thumb(item) {
    const want = item.wants[0];
    const image = item.image
      ? el("img", { class: "card-thumb", src: item.image, alt: "", loading: "lazy" })
      : el("div", { class: "card-thumb", text: want.set_code });
    const preview = item.preview || {};
    if (!preview.front) return image;
    const attrs = { "data-preview-front": preview.front, "data-preview-name": want.name_en };
    if (preview.back) attrs["data-preview-back"] = preview.back;
    if (item.image) attrs["data-preview-small"] = item.image;
    return el("button", {
      type: "button", class: "card-thumb-btn", "data-preview-open": "", "aria-label": `Ampliar ${want.name_en}`, ...attrs,
    }, image);
  }

  // Descrição curta da impressão: só código da edição + número (o nome por extenso vai no title).
  const printCode = (code, number) => `${code.toUpperCase()} #${number}`;

  function copyDetails(row) {
    const ed = row.ed.toUpperCase();
    // A Liga às vezes junta dois idiomas ("Português / Inglês"): abrevia cada um.
    const language = row.language
      ? row.language.split("/").map((part) => LANG_SHORT[part.trim()] || part.trim()).join("/")
      : "";
    return [
      el("span", { text: `${row.quantity}x · ${printCode(row.ed, row.number)}`, title: row.ed_name || ed }),
      language ? el("span", { class: `lang-badge ${language.startsWith("PT") ? "pt" : ""}`, text: language, title: row.language }) : null,
      row.quality ? el("em", { class: "quality-badge", text: row.quality, title: "Qualidade da cópia na coleção" }) : null,
      row.foil ? el("span", { class: "foil-badge", text: "Foil" }) : null,
    ];
  }

  // Cada carta: primeiro o que é seu (bloco latão, um por lista que quer a carta), depois a cópia
  // da coleção (bloco neutro, vem da Liga). A seleção fica na cópia da coleção, que é o que você pega.
  function renderItem(item) {
    const { row } = item;
    const namePt = row.name_pt || item.wants[0].name_pt;
    const copyPrice = item.failed
      ? (row.price_buy === null ? null : { value: row.price_buy, foil: row.foil })
      : item.copy;
    const multi = item.wants.length > 1;

    const mineBlocks = item.wants.map((want) => {
      const result = want.result;
      const same = result && result.same_edition;
      const status = result
        ? el("span", { class: `edition-badge ${same ? "is-same" : "is-other"}`, text: same ? "mesma edição" : "outra edição" })
        : item.failed ? null : skeleton("is-chip");
      const wantPrice = item.failed ? null : result ? result.price : undefined;

      return el("div", { class: "match-source is-mine" }, [
        el("div", { class: "source-head" }, [
          el("span", { class: "source-tag is-mine", text: "Lista Want" }),
          el("a", { class: "source-list", href: `/lists/${want.list_id}`, text: want.list_name, title: want.list_name }),
          status,
        ]),
        el("div", { class: "source-body" }, [
          el("span", { class: "match-detail" },
            el("span", { text: `${want.quantity}x · ${printCode(want.set_code, want.collector_number)}`, title: want.set_name })),
          el("span", { class: "match-price-group" }, [
            priceNode(wantPrice),
            result && result.price.other_edition ? el("small", { class: "price-flag", text: "outra ed.", title: OTHER_EDITION_HINT }) : null,
            result && !same ? diffBadge(result.diff) : null,
          ]),
        ]),
      ]);
    });

    // Uma seleção por lista: com duas listas querendo a carta, cada linha diz para qual lista vai.
    const picks = item.wants.map((want) => {
      const same = want.result && want.result.same_edition;
      const pickNote = want.picked
        ? el("span", { class: "pick-qty", text: `pegar ${want.pickQty}x` })
        : want.soldOut ? el("span", { class: "pick-qty is-off", text: "sem cópias sobrando" }) : null;
      const classes = ["source-pick", want.picked && "is-picked", want.soldOut && "is-off"].filter(Boolean);
      return el("label", { class: classes.join(" ") }, [
        el("input", {
          type: "checkbox", class: "check-box", "data-pick": pickKey(row, want), checked: want.picked, disabled: want.soldOut,
          "aria-label": `Selecionar a versão ${row.ed_name || row.ed.toUpperCase()} de ${copyName(item)} para a lista ${want.list_name}`,
          title: want.soldOut ? "Todas as cópias desta linha da coleção já foram marcadas para outra lista" : "",
        }),
        el("span", { class: "source-pick-text", text: same ? "Selecionar essa cópia" : "Selecionar essa versão" }),
        multi ? el("span", { class: "source-tag is-mine", text: want.list_name, title: `Lista ${want.list_name}` }) : null,
        pickNote,
      ]);
    });

    const anyPicked = item.wants.some((want) => want.picked);
    const ligaBlock = el("div", { class: `match-source is-liga${anyPicked ? " is-picked" : ""}` }, [
      el("div", { class: "source-head" }, el("span", { class: "source-tag is-liga", text: "Coleção da Liga" })),
      el("div", { class: "source-body" }, [
        el("span", { class: "match-detail" }, copyDetails(row)),
        el("span", { class: "match-price-group" }, priceNode(copyPrice)),
      ]),
      ...picks,
    ]);

    return el("li", { class: "match-row" }, [
      thumb(item),
      el("div", { class: "match-info" }, [
        el("div", { class: "match-names" }, [
          el("strong", { text: namePt || row.name_en }),
          namePt ? el("span", { text: row.name_en }) : null,
        ]),
        ...mineBlocks,
        ligaBlock,
      ]),
    ]);
  }

  // "Tentar agora": libera a pausa no servidor e compara de novo só as que ficaram sem comparação.
  async function retryFailed() {
    try {
      await postJson("/api/liga/resume", {});
    } catch (error) {
      toast(error.message, "error");
      return;
    }
    const runId = state.run;
    const items = state.items.filter((item) => item.failed);
    for (const item of items) {
      item.failed = false;
      item.copy = undefined;
      for (const want of item.wants) want.result = undefined;
    }
    state.pause = null;
    setBusy(true);
    render();
    await compareAll(runId, items);
    if (runId !== state.run) return;
    els.progress.hidden = true;
    setBusy(false);
    render();
  }

  function group(title, hint, items, action = null) {
    if (!items.length) return null;
    return el("section", { class: "match-group" }, [
      el("h3", { class: "match-group-title" }, [title, el("span", { class: "match-group-count", text: String(items.length) })]),
      hint ? el("p", { class: "muted small match-group-hint" }, [hint, action ? " " : null, action]) : null,
      el("ul", { class: "match-rows" }, items.map(renderItem)),
    ]);
  }

  function render() {
    const { info, items } = state;
    if (!info) return;
    allocate();
    const same = items.filter((item) => !isPending(item) && !item.failed && isSame(item));
    const other = items.filter((item) => !isPending(item) && !item.failed && !isSame(item));
    const pending = items.filter(isPending);
    const failed = items.filter((item) => item.failed);

    // O total da Liga (pgA) às vezes conta linha que a página não mostra: só aparece se a leitura parou no meio.
    const read = state.partial && info.total_rows
      ? `${plural(state.rowsRead, "linha lida", "linhas lidas")} de ${info.total_rows.toLocaleString("pt-BR")}`
      : plural(state.rowsRead, "linha lida", "linhas lidas");
    const cards = info.total_cards ? ` (${plural(info.total_cards, "carta", "cartas")} na coleção)` : "";
    const found = items.length
      ? ` · ${items.length.toLocaleString("pt-BR")} nas suas listas: ${same.length} na mesma edição, ${other.length} em outra`
      : "";
    // Coleção maior que o teto do servidor: só as primeiras páginas foram lidas.
    const capped = info.pages_total > info.pages
      ? ` · coleção grande: lidas as primeiras ${info.pages} de ${info.pages_total} páginas`
      : "";
    els.summary.textContent = `${read}${cards}${found}${state.partial ? " · leitura incompleta" : ""}${capped}`;

    els.groups.replaceChildren(...[
      group("Mesma edição", "A coleção tem a edição exata que está na sua lista.", same),
      group("Outra edição", "A carta está na coleção, mas em edição diferente: compare os preços (Menor da LigaMagic).", other),
      group("Comparando…", null, pending),
      group("Sem comparação", state.pause || "A LigaMagic não respondeu para estas cartas. Busque de novo daqui a pouco.", failed,
        state.pause && !els.submit.disabled
          ? el("button", { type: "button", class: "btn btn-text btn-sm", "data-retry-failed": "" }, [icon("refresh"), "Tentar agora"])
          : null),
    ].filter(Boolean));
    const finished = !els.submit.disabled;
    els.empty.hidden = Boolean(items.length) || !finished;
    renderPicks();
  }

  // ---------- render: resumo das marcadas ----------

  // Soma valor × quantidade; "…" enquanto alguma marcada ainda compara; conta as sem preço.
  function total(entries, valueOf) {
    let sum = 0;
    let missing = 0;
    let loading = false;
    for (const entry of entries) {
      const value = valueOf(entry);
      if (value === undefined) { loading = true; continue; }
      if (value === null) missing += entry.want.pickQty;
      else sum += value * entry.want.pickQty;
    }
    return { sum, missing, loading };
  }

  const wantValue = ({ item, want }) => item.failed ? null
    : want.result === undefined ? undefined : want.result.price.value ?? null;
  const editionValue = ({ item }) => item.failed ? null
    : item.copy === undefined ? undefined : item.copy.value ?? null;
  const collectionValue = ({ item }) => item.row.price_buy ?? null;

  function setTotal(name, { sum, missing, loading }) {
    const node = $(`picks-total-${name}`);
    if (loading) {
      if (!node.querySelector(".skeleton")) node.replaceChildren(skeleton("is-lg"));
    } else {
      node.textContent = BRL.format(sum);
    }
    $(`picks-missing-${name}`).textContent = missing ? `${plural(missing, "carta", "cartas")} sem preço` : "";
  }

  // Uma seleção: o que você pega da coleção e a carta do want que ela cobre, lado a lado.
  function renderPickEntry({ item, want }) {
    const { row } = item;
    const same = want.result && want.result.same_edition;
    const editionBadge = want.result
      ? el("span", { class: `edition-badge ${same ? "is-same" : "is-other"}`, text: same ? "mesma edição" : "outra edição" })
      : null;
    return el("li", { class: "pick-item" }, [
      el("div", { class: "pick-item-text" }, [
        el("strong", { text: `${want.pickQty}x ${copyName(item)}` }),
        el("span", { class: "pick-item-detail" }, [
          el("span", { class: "pick-item-key is-liga", text: "Coleção da Liga", title: "A cópia que você pega da coleção" }),
          el("span", { class: "pick-item-val" }, [
            el("span", { text: printCode(row.ed, row.number), title: row.ed_name || row.ed.toUpperCase() }),
            ...copyDetails({ ...row, quantity: want.pickQty }).slice(1),
          ]),
        ]),
        el("span", { class: "pick-item-detail" }, [
          el("span", { class: "pick-item-key is-mine", text: "Lista Want", title: "A versão que está na sua lista" }),
          el("span", { class: "pick-item-val" }, [
            el("span", { text: printCode(want.set_code, want.collector_number), title: want.set_name }),
            editionBadge,
          ]),
        ]),
      ]),
      el("button", {
        type: "button", class: "icon-btn is-sm", "data-unpick": pickKey(row, want),
        "aria-label": `Desmarcar ${copyName(item)} (lista ${want.list_name})`, title: "Desmarcar",
      }, icon("close")),
      pickPrices({ item, want }),
    ]);
  }

  // Os mesmos dois valores do resumo geral, por carta (× quantidade pega): a soma das cartas fecha
  // com os totais. A ordem e os nomes são os dos totais, para dar para conferir linha a linha.
  const DIVERGENCE_RATIO = 0.25;
  function pickPrices(entry) {
    const qty = entry.want.pickQty;
    const cell = (label, value, extra = []) => el("div", { class: "pick-price" }, [
      el("dt", { text: label }),
      el("dd", {}, [
        value === undefined ? skeleton() : el("b", { text: value === null ? "—" : BRL.format(value * qty) }),
        ...extra,
      ]),
    ]);
    const want = wantValue(entry);
    const owner = collectionValue(entry);
    const diff = want != null && owner != null ? Math.round((owner - want) * qty * 100) / 100 : null;
    // O "Menor P. Compra" da página da coleção usa outro critério (idioma/qualidade da cópia):
    // quando foge muito do menor preço da edição na Liga, avisa (a edição da Liga fica só como referência).
    const edition = editionValue(entry);
    const diverges = owner != null && edition != null && Math.abs(owner - edition) / edition > DIVERGENCE_RATIO;
    return el("dl", { class: "pick-prices" }, [
      cell("Seu want exato", want),
      cell("Valor na coleção", owner, [
        // Diferença zero não ganha selo: os dois números lado a lado já mostram a igualdade.
        diff === null || Math.abs(diff) < 0.005 ? null
          : diffBadge(diff, `O valor na coleção é ${BRL.format(Math.abs(diff))} ${diff > 0 ? "maior" : "menor"} que o seu want exato`),
        diverges ? el("span", {
          class: "price-warn", text: "≠ Liga",
          title: `A página da coleção mostra ${BRL.format(owner)} para esta cópia, mas o menor preço desta edição na LigaMagic é ${BRL.format(edition)}. A coluna da coleção considera idioma/qualidade da cópia; vale conferir antes de negociar.`,
        }) : null,
      ]),
    ]);
  }

  // Um grupo por want (lista): cabeçalho com a lista de destino, quantas cartas e o subtotal.
  function renderPickGroup({ id, name, entries }) {
    const copies = entries.reduce((n, { want }) => n + want.pickQty, 0);
    const sub = total(entries, collectionValue);
    return el("section", { class: "pick-group" }, [
      el("header", { class: "pick-group-head" }, [
        el("span", { class: "source-tag is-mine", text: "Want" }),
        el("a", { class: "source-list", href: `/lists/${id}`, text: name, title: `Abrir a lista ${name}` }),
        el("span", { class: "pick-group-meta" }, [
          plural(copies, "carta", "cartas"),
          " · ",
          el("b", { text: `${BRL.format(sub.sum)} na coleção` }),
        ]),
      ]),
      el("ul", { class: "picks-list" }, entries.map(renderPickEntry)),
    ]);
  }

  function renderPicks() {
    const entries = pickedEntries();
    const copies = entries.reduce((n, { want }) => n + want.pickQty, 0);
    els.fab.hidden = entries.length === 0;
    document.body.classList.toggle("has-picks-fab", entries.length > 0);
    if (!entries.length) {
      els.fabCount.textContent = "0";
      if (els.drawer.open) els.drawer.close();
      return;
    }

    const groups = new Map();
    for (const entry of entries) {
      const { list_id: id, list_name: name } = entry.want;
      if (!groups.has(id)) groups.set(id, { id, name, entries: [] });
      groups.get(id).entries.push(entry);
    }
    const summary = `${plural(copies, "carta", "cartas")} para ${plural(groups.size, "want", "wants")}`;
    els.picksCount.textContent = summary;

    // O contador pulsa quando muda, para a seleção nova "cair" no botão.
    if (els.fabCount.textContent !== String(copies)) {
      els.fab.classList.remove("is-bump");
      void els.fab.offsetWidth;
      els.fab.classList.add("is-bump");
    }
    els.fabCount.textContent = String(copies);
    els.fab.setAttribute("aria-label", `Ver resumo das selecionadas: ${summary}`);

    els.picksList.replaceChildren(...[...groups.values()].map(renderPickGroup));

    const wantTotal = total(entries, wantValue);
    const collectionTotal = total(entries, collectionValue);
    setTotal("want", wantTotal);
    setTotal("collection", collectionTotal);
    els.fabTotal.textContent = BRL.format(collectionTotal.sum);

    // Diferença só entre as marcadas que têm os dois preços, para não comparar somas de conjuntos diferentes.
    const both = entries.filter((entry) => {
      const a = wantValue(entry);
      const b = collectionValue(entry);
      return a !== undefined && a !== null && b !== null;
    });
    els.picksDiff.replaceChildren();
    if (wantTotal.loading || !both.length) return;
    const diff = both.reduce((n, entry) => n + (collectionValue(entry) - wantValue(entry)) * entry.want.pickQty, 0);
    const left = entries.length - both.length;
    els.picksDiff.append(
      "Valor na coleção em relação ao seu want exato: ",
      diffBadge(Math.round(diff * 100) / 100, "Diferença entre o valor que a coleção mostra e o preço das edições das suas listas, nas cartas marcadas"),
      left ? ` (${plural(left, "marcada sem preço ficou", "marcadas sem preço ficaram")} de fora)` : "",
    );
  }

  // ---------- mensagem para o dono da coleção ----------

  function buildMessage() {
    // A mesma linha da coleção marcada para duas listas vira um item só, com as quantidades somadas.
    const lines = new Map();
    for (const { item, want } of pickedEntries()) {
      const line = lines.get(item) || { item, quantity: 0 };
      line.quantity += want.pickQty;
      lines.set(item, line);
    }
    const cards = [...lines.values()].map(({ item, quantity }) => {
      const { row } = item;
      const name = row.name_pt && row.name_pt !== row.name_en ? `${row.name_pt} (${row.name_en})` : row.name_en;
      const details = [`${row.ed_name || row.ed.toUpperCase()} #${row.number}`, row.language, row.quality, row.foil ? "foil" : ""]
        .filter(Boolean).join(", ");
      return `- ${quantity}x ${name} — ${details}`;
    });
    const title = state.info.title ? ` "${state.info.title}"` : "";
    return [
      `Olá! Tenho interesse em algumas cartas da coleção${title} que você enviou:`,
      "",
      ...cards,
      "",
      "Elas ainda estão disponíveis? Como poderíamos fazer a troca?",
    ].join("\n");
  }

  // O <dialog> modal fica acima do toast: o retorno da cópia aparece no próprio botão.
  let copyTimer;
  function copyFeedback(label, iconName) {
    els.messageCopy.replaceChildren(icon(iconName), label);
    clearTimeout(copyTimer);
    copyTimer = setTimeout(() => els.messageCopy.replaceChildren(icon("copy"), "Copiar"), 2500);
  }

  async function copyMessage() {
    try {
      await navigator.clipboard.writeText(els.messageText.value);
      copyFeedback("Copiado", "check");
    } catch {
      // Sem permissão de área de transferência: deixa selecionado para o Ctrl+C.
      els.messageText.focus();
      els.messageText.select();
      copyFeedback("Use Ctrl+C", "info");
    }
  }

  // ---------- eventos ----------

  function togglePick(key, on) {
    if (on) state.picks.add(key);
    else state.picks.delete(key);
    savePicks();
    render();
  }

  els.groups.addEventListener("click", (event) => {
    if (event.target.closest("button[data-retry-failed]")) retryFailed();
  });

  els.groups.addEventListener("change", (event) => {
    const box = event.target.closest("input[data-pick]");
    if (!box) return;
    const key = box.dataset.pick;
    togglePick(key, box.checked);
    // O render recria as linhas: devolve o foco ao mesmo checkbox para o teclado não se perder.
    const again = [...els.groups.querySelectorAll("input[data-pick]")].find((node) => node.dataset.pick === key);
    if (again) again.focus({ preventScroll: true });
  });

  els.picksList.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-unpick]");
    if (button) togglePick(button.dataset.unpick, false);
  });

  els.picksClear.addEventListener("click", () => {
    state.picks.clear();
    savePicks();
    render();
  });

  els.fab.addEventListener("click", () => els.drawer.showModal());
  // Fecha no X ou no clique no fundo (o alvo é o próprio <dialog> só fora do conteúdo).
  els.drawer.addEventListener("click", (event) => {
    if (event.target.closest("[data-drawer-close]") || event.target === els.drawer) els.drawer.close();
  });

  els.picksMessage.addEventListener("click", () => {
    els.messageText.value = buildMessage();
    els.messageDialog.showModal();
    els.messageCopy.focus();
  });
  els.messageCopy.addEventListener("click", copyMessage);
  els.messageDialog.addEventListener("click", (event) => {
    if (event.target.closest("button[value='close']") || event.target === els.messageDialog) els.messageDialog.close();
  });

  els.form.addEventListener("submit", (event) => {
    event.preventDefault();
    const text = els.input.value.trim();
    if (!text) {
      toast("Cole o link da coleção.", "error");
      els.input.focus();
      return;
    }
    run(text);
  });

  // Link com ?collection=N só preenche o campo: a leitura da Liga acontece apenas no clique
  // (recarregar a página não dispara outra leitura da coleção).
  const initial = new URLSearchParams(window.location.search).get("collection");
  if (initial) {
    els.input.value = /^\d+$/.test(initial)
      ? `https://www.ligamagic.com.br/?view=colecao/colecao&id=${initial}`
      : initial;
    els.submit.focus();
  }
})();
