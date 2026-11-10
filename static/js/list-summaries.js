"use strict";

// "Minhas listas": valor total de cada lista nos sites de preço ligados, carregado depois da página.
(() => {
  const targets = new Map(
    [...document.querySelectorAll(".list-summary[data-list-id]")]
      .map((node) => [node.dataset.listId, node.querySelector("[data-list-prices]")]),
  );
  if (!targets.size) return;

  const MONEY = {
    liga: new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL" }),
    tcgplayer: new Intl.NumberFormat("pt-BR", { style: "currency", currency: "USD" }),
    cardmarket: new Intl.NumberFormat("pt-BR", { style: "currency", currency: "EUR" }),
  };
  const NAMES = { liga: "LigaMagic", tcgplayer: "TCGplayer", cardmarket: "Cardmarket" };
  const LIGA_HINT = "LigaMagic: valores da última consulta guardada. Abra a lista para buscar os preços atuais.";
  let request = null;

  function el(tag, props = {}, text = "") {
    const node = Object.assign(document.createElement(tag), props);
    if (text) node.textContent = text;
    return node;
  }

  function setAll(text) {
    for (const target of targets.values()) target.replaceChildren(text ? el("span", { className: "list-price-muted" }, text) : "");
  }

  function render(markets, lists) {
    for (const [id, target] of targets) {
      const totals = lists[id];
      if (!totals) { target.replaceChildren(); continue; }  // lista sem cartas
      target.replaceChildren(...markets.map((market) => {
        const { value, missing } = totals[market];
        const item = el("span", { className: "list-price" });
        item.dataset.market = market;  // cor do site (CSS: [data-market])
        item.title = market === "liga" ? LIGA_HINT : `Total no ${NAMES[market]}`;
        item.append(el("b", {}, MONEY[market].format(value)));
        if (missing) {
          item.append(el("small", {}, ` (${missing} s/ preço)`));
          item.title += `\n${missing} ${missing === 1 ? "carta" : "cartas"} sem preço no ${NAMES[market]}.`;
        }
        return item;
      }));
    }
  }

  async function load() {
    if (request) request.abort();
    const controller = new AbortController();
    request = controller;
    setAll("…");
    try {
      const response = await fetch("/api/lists/summaries", { credentials: "same-origin", signal: controller.signal });
      if (response.status === 401) { window.location.href = "/login"; return; }
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `Erro ${response.status}`);
      render(data.markets, data.lists);
    } catch (error) {
      if (error.name === "AbortError") return;
      setAll("preços indisponíveis agora");
    } finally {
      if (request === controller) request = null;
    }
  }

  // O servidor calcula com a preferência gravada: só pede de novo depois que a barra salvou.
  document.addEventListener("markets-saved", load);

  load();
})();
