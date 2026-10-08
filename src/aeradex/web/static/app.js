(function () {
  "use strict";
  const $ = (s, el) => (el || document).querySelector(s);
  const $$ = (s, el) => Array.from((el || document).querySelectorAll(s));
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } },
  };

  // ---- theme ----
  function bindTheme() {
    const btn = $("#themeBtn");
    if (!btn) return;
    btn.onclick = () => {
      const root = document.documentElement;
      const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
      root.dataset.theme = dark ? "light" : "dark";
      store.set("aeradex-theme", root.dataset.theme);
    };
  }

  // ---- toast ----
  function toast(msg, error) {
    let t = $("#toast");
    if (!t) { t = document.createElement("div"); t.id = "toast"; t.className = "toast"; t.setAttribute("role", "status"); document.body.appendChild(t); }
    if (msg) { t.textContent = msg; t.className = "toast" + (error ? " error" : ""); }
    t.hidden = false;
    clearTimeout(t._timer);
    t._timer = setTimeout(() => { t.hidden = true; }, 3200);
  }
  window.aeradexToast = toast;

  // ---- expandable Beleg groups and generic toggles ----
  function bindPage(root) {
    $$("tr.group", root).forEach(tr => {
      tr.addEventListener("click", e => {
        if (e.target.closest("a, button, form, input")) return;
        const open = tr.classList.toggle("open");
        $$('tr[data-of="' + tr.dataset.group + '"]').forEach(r => { r.hidden = !open; });
      });
    });
    // Edit a manual Beleg in place: ✎ or double-click opens the form under the row, Esc closes it.
    const openEdit = id => {
      const row = document.getElementById(id); if (!row) return;
      row.hidden = false;
      const first = $("input[name=text]", row); if (first) { first.focus(); first.select(); }
    };
    $$("[data-editopen]", root).forEach(b => b.addEventListener("click", () => openEdit(b.dataset.editopen)));
    $$("tr[data-edit]", root).forEach(tr => tr.addEventListener("dblclick", e => {
      if (e.target.closest("a, button, form, input, details")) return;
      openEdit(tr.dataset.edit);
    }));
    $$("[data-editclose]", root).forEach(b => b.addEventListener("click", () => { document.getElementById(b.dataset.editclose).hidden = true; }));
    $$("tr.editrow", root).forEach(row => row.addEventListener("keydown", e => {
      if (e.key === "Escape") { row.hidden = true; e.stopPropagation(); }
    }));
    $$("[data-addrow]", root).forEach(btn => btn.addEventListener("click", () => {
      const list = $(btn.dataset.addrow);
      const tpl = $("template", list.parentElement) || $(btn.dataset.template);
      list.appendChild(tpl.content.cloneNode(true));
      bindRemove(list);
      const inputs = $$("input", list); if (inputs.length) inputs[inputs.length - 5 >= 0 ? inputs.length - 5 : 0].focus();
    }));
    $$(".splitrows, .posrows", root).forEach(bindRemove);
    $$("[data-mode]", root).forEach(radio => radio.addEventListener("change", () => {
      const form = radio.closest("form");
      $$("[data-show]", form).forEach(el => { el.hidden = el.dataset.show !== radio.value; });
    }));
    $$("form", root).forEach(f => f.addEventListener("input", () => { f.dataset.dirty = "1"; }));
  }
  function bindRemove(list) {
    $$("[data-removerow]", list).forEach(b => { b.onclick = () => { b.closest(".splitrow, .posrow").remove(); list.dispatchEvent(new Event("input", { bubbles: true })); }; });
  }

  // ---- journal: select Belege, recode them in bulk (konto and/or MWST code) ----
  // The journal page is swapped in and out, so the handlers sit on the document once and look up the bar.
  function recodeUpdate() {
    const bar = $("#umbuchen"); if (!bar) return;
    const boxes = $$("input[data-sel]"), all = $("[data-selall]"), preview = $("#umbuchenVorschau");
    const n = boxes.filter(b => b.checked).length;
    $("[data-selcount]", bar).textContent = n;
    bar.hidden = n === 0;
    if (all) { all.checked = n > 0 && n === boxes.length; all.indeterminate = n > 0 && n < boxes.length; }
    if (!n && preview) preview.innerHTML = "";
  }
  document.addEventListener("change", e => {
    if (e.target.matches("[data-selall]")) $$("input[data-sel]").forEach(b => { b.checked = e.target.checked; });
    if (e.target.matches("input[data-sel], [data-selall]")) recodeUpdate();
  });
  document.addEventListener("click", e => {
    if (e.target.closest("[data-selnone]")) { $$("input[data-sel]").forEach(b => { b.checked = false; }); recodeUpdate(); }
    if (e.target.closest("[data-previewclose]")) { const p = $("#umbuchenVorschau"); if (p) p.innerHTML = ""; }
  });

  // ---- booking grid (journal): a spreadsheet for simple bookings ----
  function bindGrid(root) {
    const form = $("#raster", root);
    if (!form || form.dataset.bound) return;
    form.dataset.bound = "1";
    const body = $("tbody", form), tpl = $("template", form);
    const accounts = JSON.parse(form.dataset.accounts || "{}");
    const key = "aeradex-raster:" + (form.dataset.key || "");
    const rows = () => $$("tr", body);
    const cells = tr => $$("input", tr);
    const empty = tr => cells(tr).every(c => !c.value.trim());
    const amount = v => { let t = (v || "").replace(/chf|fr\.|['’\s]/gi, ""); if (t.includes(",") && t.includes(".")) t = t.lastIndexOf(".") > t.lastIndexOf(",") ? t.replace(/,/g, "") : t.replace(/\./g, "").replace(",", "."); else t = t.replace(",", "."); return parseFloat(t); };
    const fmt = n => n.toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, "'");
    function addRow() { body.appendChild(tpl.content.cloneNode(true)); return rows()[rows().length - 1]; }
    function hint(input) {
      const small = input.nextElementSibling; if (!small) return;
      const nr = (input.value || "").trim().split(" ")[0];
      small.textContent = nr ? (accounts[nr] || "unbekanntes Konto") : "";
      small.classList.toggle("bad", !!nr && !accounts[nr]);
    }
    function update(save = true) {
      let sum = 0, n = 0, prev = "";
      rows().forEach((tr, i) => {
        $(".nr", tr).textContent = i + 1;
        const c = cells(tr);
        if (!empty(tr)) { n++; const v = amount(c[4].value); if (!isNaN(v)) sum += v; }
        c[0].placeholder = prev ? "wie oben" : "TT.MM.";
        if (c[0].value.trim()) prev = c[0].value;
        hint(c[2]); hint(c[3]);
      });
      if (!rows().length) addRow(), update(false);
      if (n) form.setAttribute("data-dirty", ""); else form.removeAttribute("data-dirty");
      $("#rasterAnzahl").textContent = n; $("#rasterSumme").textContent = fmt(sum);
      if (save) store.set(key, JSON.stringify(rows().filter(tr => !empty(tr)).map(tr => cells(tr).map(c => c.value))));
    }
    // Like Excel: Enter after tabbing across a row goes back to the column the row was started in.
    let startCol = 0, startRow = null;
    form.addEventListener("focusin", e => {
      if (!e.target.matches("input[data-col]")) return;
      const tr = e.target.closest("tr");
      if (tr !== startRow) { startRow = tr; startCol = +e.target.dataset.col; }
    });
    function move(input, dr) {
      const tr = input.closest("tr"), col = dr > 0 && startRow === tr ? startCol : +input.dataset.col;
      let target = dr > 0 ? tr.nextElementSibling : tr.previousElementSibling;
      if (!target && dr > 0) target = addRow();
      if (target) { const c = cells(target).find(x => +x.dataset.col === col); if (c) { c.focus(); c.select && c.select(); } }
    }
    form.addEventListener("input", e => { e.target.closest("tr").classList.remove("err"); update(); });
    form.addEventListener("keydown", e => {
      if (!e.target.matches("input[data-col]") || e.metaKey || e.ctrlKey || e.altKey) return;
      const listCell = e.target.hasAttribute("list");
      if (e.key === "Enter") { e.preventDefault(); move(e.target, e.shiftKey ? -1 : 1); }
      else if (!listCell && e.key === "ArrowDown") { e.preventDefault(); move(e.target, 1); }
      else if (!listCell && e.key === "ArrowUp") { e.preventDefault(); move(e.target, -1); }
    });
    form.addEventListener("paste", e => {
      const text = (e.clipboardData || window.clipboardData).getData("text");
      if (!e.target.matches("input[data-col]") || !/[\t\n]/.test(text.trim())) return;
      e.preventDefault();
      const lines = text.replace(/\r/g, "").replace(/\n+$/, "").split("\n");
      let tr = e.target.closest("tr");
      const width = cells(tr).length;
      // whole rows copied from a sheet start in the first column, wherever the cursor is
      const col = lines.every(l => l.split("\t").length >= width - 1) ? 0 : +e.target.dataset.col;
      lines.forEach((line, i) => {
        if (i > 0) tr = tr.nextElementSibling || addRow();
        const c = cells(tr);
        line.split("\t").forEach((v, j) => { const cell = c.find(x => +x.dataset.col === col + j); if (cell) cell.value = v.trim(); });
      });
      update();
    });
    form.addEventListener("click", e => {
      if (e.target.closest("[data-gridremove]")) { e.target.closest("tr").remove(); update(); }
      if (e.target.closest("[data-gridadd]")) { const tr = addRow(); update(); cells(tr)[0].focus(); }
      if (e.target.closest("[data-gridclear]") && confirm("Alle Zeilen leeren?")) { rows().forEach(tr => tr.remove()); addRow(); update(); }
    });
    const picker = $("[data-gridimport]", form.closest(".panel") || root);
    if (picker) picker.addEventListener("change", async () => {
      const file = picker.files[0]; if (!file) return;
      const body = new FormData(); body.append("datei", file); picker.value = "";
      const csrf = JSON.parse(document.body.getAttribute("hx-headers") || "{}")["X-CSRF"];
      let res;
      try { res = await (await fetch("/journal/import", { method: "POST", body, headers: { "X-CSRF": csrf } })).json(); }
      catch (x) { toast("Import fehlgeschlagen", true); return; }
      if (res.fehler) { toast(res.fehler, true); return; }
      rows().filter(empty).forEach(tr => tr.remove());
      res.zeilen.forEach(vals => { const tr = addRow(); cells(tr).forEach(c => { c.value = vals[+c.dataset.col] || ""; }); });
      update(); toast(res.meldung);
    });
    form.addEventListener("rasterFehler", e => {
      const marks = (e.detail && (e.detail.value || e.detail)) || {};
      rows().forEach(tr => { tr.classList.remove("err"); tr.title = ""; });
      Object.keys(marks).filter(k => /^\d+$/.test(k)).forEach(k => { const tr = rows()[+k - 1]; if (tr) { tr.classList.add("err"); tr.title = marks[k]; } });
    });
    form.addEventListener("htmx:afterRequest", e => { if (e.detail.xhr && e.detail.xhr.status === 204) { try { localStorage.removeItem(key); } catch (x) { /* */ } form.removeAttribute("data-dirty"); } });
    let saved = [];
    try { saved = JSON.parse(store.get(key) || "[]"); } catch (x) { saved = []; }
    if (saved.length) {
      saved.forEach((vals, i) => { const tr = rows()[i] || addRow(); cells(tr).forEach((c, j) => { c.value = vals[j] || ""; }); });
      toast("Nicht gebuchte Zeilen von vorhin wiederhergestellt");
    }
    update(false);
  }

  // ---- agent drawer ----
  function bindChat() {
    const app = $("#app"), chat = $("#chat"), open = $("#chatOpen"), close = $("#chatClose");
    if (!chat) return;
    const set = (on, remember) => {
      app.classList.toggle("chat-open", on); chat.hidden = !on; open.hidden = on;
      if (remember) store.set("aeradex-chat", on ? "1" : "0");   // only a click is a preference
    };
    set(store.get("aeradex-chat") !== "0" && innerWidth > 1120, false);
    open.onclick = () => set(true, true);
    if (close) close.onclick = () => set(false, true);
  }

  // ---- live refresh: the book changed on disk (agent, CLI, git, editor) ----
  let pendingReload = false;
  function dirty() { return $$("form[data-dirty]").length > 0 || (document.activeElement && document.activeElement.matches("input, textarea, select")); }
  function refresh() {
    if (dirty()) { $("#livebar").hidden = false; pendingReload = true; return; }
    htmx.ajax("GET", location.pathname + location.search, { target: "#page", select: "#page", swap: "outerHTML" });
  }
  function bindLive() {
    if (!window.EventSource) return;
    let first = true;
    const es = new EventSource("/events");
    es.addEventListener("changed", () => { if (first) { first = false; } refresh(); });
    $("#reloadBtn").onclick = () => location.reload();
  }

  // ---- keyboard ----
  let gPending = false;
  // the header menus (Mehr, Firma) close on a click elsewhere
  document.addEventListener("click", e => {
    document.querySelectorAll(".top details[open]").forEach(d => { if (!d.contains(e.target)) d.open = false; });
  });
  document.addEventListener("keydown", e => {
    if (e.target.matches("input, textarea, select") || e.metaKey || e.ctrlKey || e.altKey) {
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
        const form = e.target.closest("form"); if (form) { e.preventDefault(); form.requestSubmit(); }
      }
      return;
    }
    if (gPending) {                       // g + letter: go to a section (the letters are in the menu's tooltips)
      gPending = false;
      const target = document.querySelector(`.top [data-key="${CSS.escape(e.key)}"]`);
      if (target) { e.preventDefault(); location.href = target.getAttribute("href"); }
      return;
    }
    if (e.key === "g") { gPending = true; setTimeout(() => { gPending = false; }, 1200); return; }
    if (e.key === "Escape") { document.querySelectorAll(".top details[open]").forEach(d => { d.open = false; }); }
    if (e.key === "n") { location.href = "/journal?neu=1"; }
    if (e.key === "/") { const s = $("#q"); if (s) { e.preventDefault(); s.focus(); } else location.href = "/journal"; }
    if (e.key === "a") { const o = $("#chatOpen"); if (o && !o.hidden) o.click(); const i = $("#chatInput"); if (i) { e.preventDefault(); i.focus(); } }
  });

  // htmx: refused posts swap an error box into the form; network errors become a toast
  document.addEventListener("htmx:responseError", e => toast("Fehler " + e.detail.xhr.status + ": " + (e.detail.xhr.responseText || "").slice(0, 200), true));
  document.addEventListener("htmx:sendError", () => toast("aeradex ist nicht erreichbar. Läuft `aeradex ui` noch?", true));
  document.addEventListener("htmx:afterSwap", e => { if (e.detail.target.id === "page") { bindPage(e.detail.target); bindTheme(); bindGrid(e.detail.target); recodeUpdate(); $("#livebar").hidden = true; } });
  document.addEventListener("htmx:beforeRequest", e => { const b = e.detail.elt.querySelector && e.detail.elt.querySelector("button[type=submit]"); if (b) b.disabled = true; });
  document.addEventListener("htmx:afterRequest", e => { const b = e.detail.elt.querySelector && e.detail.elt.querySelector("button[type=submit]"); if (b) b.disabled = false; });

  // forms that answer with a file (POST needs the CSRF header, so no plain form post)
  document.addEventListener("submit", async (e) => {
    const form = e.target;
    if (!form.matches || !form.matches("form[data-download]")) return;
    e.preventDefault();
    const button = form.querySelector("button[type=submit]");
    if (button) button.disabled = true;
    try {
      const csrf = JSON.parse(document.body.getAttribute("hx-headers") || "{}")["X-CSRF"];
      const r = await fetch(form.action, { method: "POST", body: new FormData(form), headers: { "X-CSRF": csrf } });
      if (!r.ok) { toast(await r.text(), true); return; }
      const cd = r.headers.get("Content-Disposition") || "";
      const m = cd.match(/filename\*=UTF-8''([^;]+)/) || cd.match(/filename="([^"]+)"/);
      const a = document.createElement("a");
      a.href = URL.createObjectURL(await r.blob());
      a.download = m ? decodeURIComponent(m[1]) : "download";
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 10000);
      form.reset();
      toast("Datei erstellt");
    } catch (err) {
      toast("aeradex ist nicht erreichbar. Läuft `aeradex ui` noch?", true);
    } finally {
      if (button) button.disabled = false;
    }
  });

  // narrow screens scroll the navigation: keep the current section and tab in view
  const showCurrent = () => document.querySelectorAll(".sections a.on, .tabsrow a.on").forEach(a => {
    const bar = a.parentElement;
    if (bar.scrollWidth > bar.clientWidth) bar.scrollLeft = a.offsetLeft - bar.offsetLeft - (bar.clientWidth - a.offsetWidth) / 2;
  });
  document.addEventListener("htmx:afterSwap", e => { if (e.detail.target.id === "page") showCurrent(); });
  showCurrent();
  bindTheme(); bindPage(document); bindChat(); bindLive(); bindGrid(document); recodeUpdate();
  if ($("#toast")) toast();
  if (new URLSearchParams(location.search).get("neu")) { const d = $("#neueBuchung"); if (d) { d.open = true; const i = $("input[data-col]", d) || $("input", d); if (i) i.focus(); } }
})();

// ---------- agent drawer: send, stream, restore ----------
(function () {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const csrf = JSON.parse(document.body.getAttribute("hx-headers") || "{}")["X-CSRF"];
  const msgs = $("#msgs");
  if (!msgs) return;
  const pageLabel = () => {
    const sec = document.querySelector(".sections a.on, .top .more.on summary"), tab = document.querySelector(".tabsrow a.on");
    const name = (el) => el ? el.childNodes[0].textContent.trim() : "";
    return [name(sec) || "Übersicht", name(tab)].filter(Boolean).join(" · ");
  };
  const setCtx = () => { const el = $("#chatPage"); if (el) el.textContent = "Seite " + pageLabel(); };
  setCtx();
  document.addEventListener("htmx:afterSwap", setCtx);

  function add(kind, content, isHtml) {
    const el = document.createElement("div");
    el.className = kind === "me" ? "msg me" : kind === "text" ? "msg ai" : kind === "tool" ? "tool" : kind === "tool_error" ? "tool" : kind === "thinking" ? "thinking" : "msg err";
    if (isHtml) el.innerHTML = content; else el.textContent = content;
    if (kind === "tool_error") el.style.color = "var(--red)";
    msgs.appendChild(el);
    msgs.scrollTop = msgs.scrollHeight;
    return el;
  }
  function hideIntro() { const s = $("#chatSuggest"); if (s) s.hidden = true; }

  function follow(turn) {
    const wait = add("thinking", "Agent arbeitet");
    $("#chatSend").disabled = true;
    const es = new EventSource("/chat/stream/" + turn);
    es.onmessage = (ev) => {
      const e = JSON.parse(ev.data);
      if (e.type === "done") { es.close(); wait.remove(); $("#chatSend").disabled = false; return; }
      const el = add(e.type, e.type === "text" ? e.html : e.text, e.type === "text");
      msgs.insertBefore(el, wait);
    };
    es.onerror = () => { es.close(); wait.remove(); $("#chatSend").disabled = false; };
  }

  async function send(text) {
    text = (text || "").trim();
    if (!text) return;
    hideIntro();
    add("me", text);
    $("#chatInput").value = "";
    const body = new URLSearchParams({ message: text, page: location.pathname });
    try {
      const r = await fetch("/chat/send", { method: "POST", body, headers: { "X-CSRF": csrf } });
      const data = await r.json();
      if (!data.ok) { add("err", data.fehler || "Fehler"); return; }
      follow(data.turn);
    } catch (e) { add("err", "aeradex ist nicht erreichbar."); }
  }

  $("#composer").addEventListener("submit", (e) => { e.preventDefault(); send($("#chatInput").value); });
  $("#chatInput").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send($("#chatInput").value); } });
  document.querySelectorAll("#chatSuggest button").forEach(b => b.addEventListener("click", () => send(b.textContent)));
  $("#chatReset").addEventListener("click", async () => {
    await fetch("/chat/neu", { method: "POST", headers: { "X-CSRF": csrf } });
    msgs.querySelectorAll(".msg:not(#chatHello), .tool, .thinking").forEach(el => el.remove());
    const s = $("#chatSuggest"); if (s) s.hidden = false;
  });

  // restore the running conversation after a page load
  fetch("/chat/verlauf").then(r => r.json()).then(d => {
    if (!d.items.length) return;
    hideIntro();
    d.items.forEach(i => add(i.type, i.type === "text" ? i.html : i.text, i.type === "text"));
    if (d.busy) add("thinking", "Agent arbeitet noch");
  }).catch(() => {});
})();
