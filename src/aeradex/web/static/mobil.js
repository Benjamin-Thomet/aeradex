// ---------- phone view: theme, toast, live refresh, list search, installable app ----------
(function () {
  "use strict";
  const $ = (s, el) => (el || document).querySelector(s);
  const $$ = (s, el) => Array.from((el || document).querySelectorAll(s));
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } },
  };

  const theme = $("#themeBtn");
  if (theme) theme.onclick = () => {
    const root = document.documentElement;
    const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    store.set("aeradex-theme", root.dataset.theme);
  };

  function toast(msg, error) {
    let t = $("#toast");
    if (!t) { t = document.createElement("div"); t.id = "toast"; t.className = "toast"; t.setAttribute("role", "status"); document.body.appendChild(t); }
    if (msg) { t.textContent = msg; t.className = "toast" + (error ? " error" : ""); }
    t.hidden = false;
    clearTimeout(t._timer);
    t._timer = setTimeout(() => { t.hidden = true; }, 3600);
  }
  window.aeradexToast = toast;
  if ($("#toast")) toast();

  // the menu closes on a tap elsewhere
  document.addEventListener("click", e => { $$(".m-menu[open]").forEach(d => { if (!d.contains(e.target)) d.open = false; }); });

  // ---- instant search in a card list: every word must appear in the card's data-suche ----
  const fold = v => String(v || "").toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "");
  $$("input[data-filter]").forEach(input => {
    const list = $(input.dataset.filter);
    if (!list) return;
    const items = $$("[data-suche]", list).map(el => ({ el, hay: fold(el.dataset.suche + " " + el.textContent) }));
    const empty = $("[data-filter-empty]");
    const apply = () => {
      const words = fold(input.value).split(/\s+/).filter(Boolean);
      let shown = 0;
      for (const it of items) { const hit = words.every(w => it.hay.includes(w)); it.el.hidden = !hit; if (hit) shown++; }
      if (empty) empty.hidden = !!shown;
    };
    input.addEventListener("input", apply);
    if (input.value) apply();
  });

  // ---- live refresh: a page that shows work in progress (a draft with the agent) reloads by itself ----
  let pending = false, sending = false;
  const busy = () => $$("form[data-dirty]").length > 0 || $$("textarea, input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([data-filter])")
    .some(el => el.value !== el.defaultValue);
  $$("form").forEach(f => f.addEventListener("input", () => { f.dataset.dirty = "1"; }));
  document.addEventListener("htmx:beforeRequest", e => { sending = true; const b = e.detail.elt.querySelector && e.detail.elt.querySelector("button[type=submit]"); if (b) b.disabled = true; });
  document.addEventListener("htmx:afterRequest", e => { sending = false; const b = e.detail.elt.querySelector && e.detail.elt.querySelector("button[type=submit]"); if (b) b.disabled = false; });
  document.addEventListener("htmx:responseError", e => toast("Fehler " + e.detail.xhr.status + ": " + (e.detail.xhr.responseText || "").replace(/<[^>]+>/g, "").slice(0, 200), true));
  document.addEventListener("htmx:sendError", () => toast("Keine Verbindung zu aeradex.", true));
  if (window.EventSource && document.body.dataset.live !== undefined) {
    const es = new EventSource("/events");
    es.addEventListener("changed", () => {
      if (sending) return;
      if (busy()) { $("#livebar").hidden = false; pending = true; return; }
      location.reload();
    });
    $("#reloadBtn").onclick = () => location.reload();
  }

  // ---- installable app (an empty service worker: nothing of the book is stored on the phone) ----
  if ("serviceWorker" in navigator && location.protocol === "https:") {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  }
})();
