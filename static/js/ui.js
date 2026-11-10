"use strict";

// Comportamentos Material compartilhados por todas as páginas:
// - ripple no clique (como o TouchRipple do MUI) em botões, icon buttons e chips;
// - diálogo de confirmação Material para qualquer <form data-confirm="..."> (no lugar do window.confirm).
(() => {
  const RIPPLE_TARGETS = ".btn, .icon-btn, .market-chip, .seg-btn, .qty button";
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  document.addEventListener("pointerdown", (event) => {
    if (reducedMotion.matches || event.button !== 0) return;
    const host = event.target.closest(RIPPLE_TARGETS);
    if (!host || host.disabled) return;
    const rect = host.getBoundingClientRect();
    const size = Math.hypot(rect.width, rect.height) * 2;
    const ripple = document.createElement("span");
    ripple.className = "ripple";
    ripple.style.width = ripple.style.height = `${size}px`;
    ripple.style.left = `${event.clientX - rect.left - size / 2}px`;
    ripple.style.top = `${event.clientY - rect.top - size / 2}px`;
    host.classList.add("ripple-host");
    host.append(ripple);
    ripple.addEventListener("animationend", () => ripple.remove());
  });

  // ---------- confirmação ----------

  let dialog = null;

  function buildDialog() {
    dialog = document.createElement("dialog");
    dialog.className = "confirm-dialog";
    dialog.setAttribute("aria-labelledby", "confirm-title");
    dialog.innerHTML = `
      <div class="dialog-body">
        <h2 id="confirm-title"></h2>
        <p id="confirm-text"></p>
        <div class="dialog-actions">
          <button type="button" class="btn btn-text" value="cancel">Cancelar</button>
          <button type="button" class="btn btn-primary" value="ok"></button>
        </div>
      </div>`;
    document.body.append(dialog);
    dialog.addEventListener("click", (event) => {
      const button = event.target.closest("button[value]");
      if (button) dialog.close(button.value);
      else if (event.target === dialog) dialog.close("cancel");  // clique no scrim
    });
    return dialog;
  }

  function confirmAction(text, action) {
    const box = dialog || buildDialog();
    box.querySelector("#confirm-title").textContent = `${action}?`;
    box.querySelector("#confirm-text").textContent = text;
    const ok = box.querySelector('button[value="ok"]');
    ok.textContent = action;
    box.returnValue = "cancel";
    box.showModal();
    box.querySelector('button[value="cancel"]').focus();  // ação destrutiva: o foco começa no Cancelar
    return new Promise((resolve) => {
      box.addEventListener("close", () => resolve(box.returnValue === "ok"), { once: true });
    });
  }

  document.addEventListener("submit", async (event) => {
    const form = event.target;
    if (!form.matches("form[data-confirm]") || form.dataset.confirmed === "1") return;
    event.preventDefault();
    const ok = await confirmAction(form.dataset.confirm, form.dataset.confirmAction || "Confirmar");
    if (!ok) return;
    form.dataset.confirmed = "1";
    form.requestSubmit();
  });
})();
