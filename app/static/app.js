// Petites améliorations progressives ; l'app fonctionne sans JavaScript.
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}

document.addEventListener("click", (e) => {
  // Confirmation des actions destructrices : <button data-confirm="Sûr ?">
  const el = e.target.closest("[data-confirm]");
  if (el && !confirm(el.dataset.confirm)) e.preventDefault();
  // Tout cocher / décocher : <input type=checkbox data-check-all="name">
  const all = e.target.closest("[data-check-all]");
  if (all) document.querySelectorAll(`input[name="${all.dataset.checkAll}"]`).forEach((c) => (c.checked = all.checked));
});

// Affiche le champ « statut Jems » uniquement quand le périmètre Jems est choisi.
function syncScope(form) {
  const scope = form.querySelector('[name="scope"]:checked, select[name="scope"]');
  const box = form.querySelector("[data-jems]");
  if (scope && box) box.hidden = scope.value !== "jems";
}
document.querySelectorAll("form").forEach((f) => {
  syncScope(f);
  f.addEventListener("change", () => syncScope(f));
});

// Les filtres se soumettent dès qu'on change une valeur.
document.querySelectorAll("form[data-autosubmit]").forEach((f) =>
  f.addEventListener("change", () => f.submit())
);

// Désactive le bouton pendant l'envoi (évite les doubles soumissions sur mobile).
document.addEventListener("submit", (e) => {
  const b = e.target.querySelector("button[type=submit]:not([name]), button:not([type]):not([name])");
  if (b) setTimeout(() => { b.disabled = true; b.dataset.label = b.textContent; b.textContent = "…"; }, 0);
});
