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
      store.set("batzen-theme", root.dataset.theme);
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
  window.batzenToast = toast;

  // ---- expandable Beleg groups and generic toggles ----
  function bindPage(root) {
    $$("tr.group", root).forEach(tr => {
      tr.addEventListener("click", e => {
        if (e.target.closest("a, button, form, input")) return;
        const open = tr.classList.toggle("open");
        $$('tr[data-of="' + tr.dataset.group + '"]').forEach(r => { r.hidden = !open; });
      });
    });
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

  // ---- agent drawer ----
  function bindChat() {
    const app = $("#app"), chat = $("#chat"), open = $("#chatOpen"), close = $("#chatClose");
    if (!chat) return;
    const set = (on, remember) => {
      app.classList.toggle("chat-open", on); chat.hidden = !on; open.hidden = on;
      if (remember) store.set("batzen-chat", on ? "1" : "0");   // only a click is a preference
    };
    set(store.get("batzen-chat") !== "0" && innerWidth > 1120, false);
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
  document.addEventListener("keydown", e => {
    if (e.target.matches("input, textarea, select") || e.metaKey || e.ctrlKey || e.altKey) {
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
        const form = e.target.closest("form"); if (form) { e.preventDefault(); form.requestSubmit(); }
      }
      return;
    }
    if (e.key === "n") { location.href = "/journal?neu=1"; }
    if (e.key === "/") { const s = $("#q"); if (s) { e.preventDefault(); s.focus(); } else location.href = "/journal"; }
    if (e.key === "a") { const o = $("#chatOpen"); if (o && !o.hidden) o.click(); const i = $("#chatInput"); if (i) { e.preventDefault(); i.focus(); } }
  });

  // htmx: refused posts swap an error box into the form; network errors become a toast
  document.addEventListener("htmx:responseError", e => toast("Fehler " + e.detail.xhr.status + ": " + (e.detail.xhr.responseText || "").slice(0, 200), true));
  document.addEventListener("htmx:sendError", () => toast("batzen ist nicht erreichbar. Läuft `batzen ui` noch?", true));
  document.addEventListener("htmx:afterSwap", e => { if (e.detail.target.id === "page") { bindPage(e.detail.target); bindTheme(); $("#livebar").hidden = true; } });
  document.addEventListener("htmx:beforeRequest", e => { const b = e.detail.elt.querySelector && e.detail.elt.querySelector("button[type=submit]"); if (b) b.disabled = true; });
  document.addEventListener("htmx:afterRequest", e => { const b = e.detail.elt.querySelector && e.detail.elt.querySelector("button[type=submit]"); if (b) b.disabled = false; });

  bindTheme(); bindPage(document); bindChat(); bindLive();
  if ($("#toast")) toast();
  if (new URLSearchParams(location.search).get("neu")) { const d = $("#neueBuchung"); if (d) { d.open = true; const i = $("input", d); if (i) i.focus(); } }
})();

// ---------- agent drawer: send, stream, restore ----------
(function () {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const csrf = JSON.parse(document.body.getAttribute("hx-headers") || "{}")["X-CSRF"];
  const msgs = $("#msgs");
  if (!msgs) return;
  const pageLabel = () => { const a = document.querySelector(".navlist a.active"); return (a ? a.childNodes[0].textContent.trim() : "Übersicht"); };
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
    } catch (e) { add("err", "batzen ist nicht erreichbar."); }
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
