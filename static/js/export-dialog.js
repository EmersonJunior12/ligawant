"use strict";

// Diálogo "Gerar imagem", compartilhado pelo editor e pela tela de listas.
// Qualquer elemento com data-export-url abre o diálogo. Escolhe sem/com preços e, com preços, de quais
// sites (começa com os da barra "Preços de"; mudar aqui vale só para esta imagem). O JPG é baixado via
// fetch para mostrar progresso e erro aqui mesmo, sem sair da página.
(() => {
  const dialog = document.getElementById("export-dialog");
  if (!dialog) return;

  const form = document.getElementById("export-form");
  const status = document.getElementById("export-status");
  const go = document.getElementById("export-go");
  const modes = [...dialog.querySelectorAll("[data-export-mode]")];
  const marketsBox = document.getElementById("export-markets");
  const boxes = [...marketsBox.querySelectorAll('input[type="checkbox"]')];
  let exportUrl = null;
  let busy = false;

  const withPrices = () => modes.find((m) => m.getAttribute("aria-checked") === "true")?.dataset.exportMode === "prices";
  const chosen = () => boxes.filter((b) => b.checked).map((b) => b.value);
  const accountMarkets = () => (dialog.dataset.markets || "").split(",").filter(Boolean);

  function sync() {
    marketsBox.hidden = !withPrices();
    const empty = withPrices() && chosen().length === 0;
    go.disabled = busy || empty;
    if (!busy) setStatus(empty ? "Marque pelo menos um site, ou gere sem preços." : "");
  }

  function setMode(mode) {
    for (const m of modes) m.setAttribute("aria-checked", String(m.dataset.exportMode === mode));
    sync();
  }

  function setBusy(value) {
    busy = value;
    for (const control of [...modes, ...boxes]) control.disabled = value;
    sync();
  }

  function setStatus(message, isError = false) {
    status.textContent = message;
    status.classList.toggle("is-error", isError);
  }

  function saveBlob(blob, filename) {
    const objectUrl = URL.createObjectURL(blob);
    const link = Object.assign(document.createElement("a"), { href: objectUrl, download: filename });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(objectUrl), 10000);
  }

  async function download() {
    if (busy || !exportUrl) return;
    const markets = withPrices() ? chosen() : [];
    if (withPrices() && !markets.length) return;
    const url = markets.length ? `${exportUrl}?prices=${markets.join(",")}` : exportUrl;
    setBusy(true);
    setStatus(markets.includes("liga")
      ? "Consultando os preços e montando a imagem… (LigaMagic pode levar alguns segundos)"
      : "Montando a imagem…");
    try {
      const response = await fetch(url, { credentials: "same-origin" });
      const type = response.headers.get("Content-Type") || "";
      if (response.ok && !type.startsWith("image/jpeg")) {
        // Sessão expirada: o fetch segue o redirect e recebe a página de login.
        throw new Error("Sua sessão expirou. Recarregue a página e entre de novo.");
      }
      if (!response.ok) {
        throw new Error(response.status === 502
          ? "Scryfall indisponível agora. Tente de novo em instantes."
          : `Não foi possível gerar a imagem (erro ${response.status}).`);
      }
      const filename = /filename="([^"]+)"/.exec(response.headers.get("Content-Disposition") || "")?.[1] || "want.jpg";
      saveBlob(await response.blob(), filename);
      setBusy(false);
      dialog.close();
    } catch (error) {
      setBusy(false);
      setStatus(error.message, true);
    }
  }

  document.addEventListener("click", (event) => {
    const trigger = event.target.closest("[data-export-url]");
    if (!trigger || trigger.disabled || trigger.getAttribute("aria-disabled") === "true") return;
    exportUrl = trigger.dataset.exportUrl;
    // Cada abertura recomeça da preferência da conta.
    const markets = accountMarkets();
    for (const box of boxes) box.checked = markets.includes(box.value);
    setBusy(false);
    setMode(markets.length ? "prices" : "none");
    dialog.showModal();
  });

  // A barra "Preços de" mudou: o diálogo passa a começar com a nova escolha.
  document.addEventListener("markets-change", (event) => {
    dialog.dataset.markets = event.detail.markets.join(",");
  });

  for (const mode of modes) mode.addEventListener("click", () => setMode(mode.dataset.exportMode));
  for (const box of boxes) box.addEventListener("change", sync);
  form.addEventListener("submit", (event) => { event.preventDefault(); download(); });
  dialog.querySelector("[data-export-cancel]").addEventListener("click", () => { if (!busy) dialog.close(); });
  dialog.addEventListener("cancel", (event) => { if (busy) event.preventDefault(); });
  // Clique no scrim fecha (o alvo é o próprio <dialog> só fora da caixa).
  dialog.addEventListener("click", (event) => { if (event.target === dialog && !busy) dialog.close(); });
})();
