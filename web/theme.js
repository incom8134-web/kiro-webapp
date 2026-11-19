"use strict";
/* Dark or light theme. Loaded in <head> so the page never flashes the wrong colours.
   Follows the computer's setting until the user picks one with the sun / moon button. */
(function () {
  let t = null;
  try { t = localStorage.getItem("kiro-theme"); } catch (e) { /* private window */ }
  if (t !== "light" && t !== "dark") t = matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  document.documentElement.dataset.theme = t;
})();

function toggleTheme() {
  const t = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem("kiro-theme", t); } catch (e) { /* ignore */ }
  window.dispatchEvent(new Event("kiro-theme"));
}

/* The theme button shows where a press takes you: a sun in dark mode, a moon in light mode. */
function themeIcon(btn) {
  const i = btn && btn.querySelector("i");
  if (i) i.className = document.documentElement.dataset.theme === "light" ? "ri-moon-clear-line" : "ri-sun-line";
}
