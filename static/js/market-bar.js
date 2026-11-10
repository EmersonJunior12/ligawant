"use strict";

// Barra "Preços de": liga/desliga cada site de preço. A escolha é salva na conta e vale para o
// editor, o resumo de "Minhas listas" e o JPG. Avisa as telas com o evento "markets-change".
(() => {
  const bar = document.getElementById("market-bar");
  if (!bar) return;

  const status = document.getElementById("market-bar-status");
  const chips = [...bar.querySelectorAll(".market-chip")];
  const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
  let saving = null;  // AbortController da gravação em andamento

  const current = () => chips.filter((c) => c.getAttribute("aria-pressed") === "true").map((c) => c.dataset.market);

  function apply(markets) {
    for (const chip of chips) chip.setAttribute("aria-pressed", String(markets.includes(chip.dataset.market)));
  }

  function announce(markets) {
    document.dispatchEvent(new CustomEvent("markets-change", { detail: { markets } }));
  }

  async function save(markets, previous) {
    if (saving) saving.abort();  // cliques rápidos: vale o último
    const controller = new AbortController();
    saving = controller;
    status.textContent = "";
    status.classList.remove("is-error");
    try {
      const response = await fetch("/api/settings/markets", {
        method: "POST",
        credentials: "same-origin",
        signal: controller.signal,
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
        body: JSON.stringify({ markets }),
      });
      let data = {};
      try { data = await response.json(); } catch { /* corpo vazio */ }
      if (response.status === 401) { window.location.href = "/login"; return; }
      if (!response.ok) throw new Error(data.error || `Erro ${response.status}`);
      apply(data.markets);
      if (data.markets.join() !== markets.join()) announce(data.markets);
      // Gravado no servidor: quem consulta a API com a preferência (resumo das listas) pode pedir de novo.
      document.dispatchEvent(new CustomEvent("markets-saved", { detail: { markets: data.markets } }));
    } catch (error) {
      if (error.name === "AbortError") return;
      apply(previous);
      announce(previous);
      status.textContent = `Não foi possível salvar: ${error.message}`;
      status.classList.add("is-error");
    } finally {
      if (saving === controller) saving = null;
    }
  }

  for (const chip of chips) {
    chip.addEventListener("click", () => {
      const previous = current();
      const on = chip.getAttribute("aria-pressed") !== "true";
      chip.setAttribute("aria-pressed", String(on));
      const markets = current();
      announce(markets);  // otimista: a tela reage na hora, a gravação vem atrás
      save(markets, previous);
    });
  }
})();
