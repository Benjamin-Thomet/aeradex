// ---------- agent chat: send, stream, restore, dictate (desktop drawer and the phone's agent page) ----------
(function () {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const csrf = JSON.parse(document.body.getAttribute("hx-headers") || "{}")["X-CSRF"];
  const msgs = $("#msgs");
  if (!msgs) return;
  const input = $("#chatInput");
  const pageLabel = () => {
    const sec = document.querySelector(".sections a.on, .top .more.on summary"), tab = document.querySelector(".tabsrow a.on");
    const name = (el) => el ? el.childNodes[0].textContent.trim() : "";
    return [name(sec) || "Übersicht", name(tab)].filter(Boolean).join(" · ");
  };
  const setCtx = () => { const el = $("#chatPage"); if (el) el.textContent = msgs.dataset.ctx || "Seite " + pageLabel(); };
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
  const grow = () => { if (input.dataset.grow !== undefined) { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 180) + "px"; } };
  input.addEventListener("input", grow);

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
    input.value = ""; grow();
    const body = new URLSearchParams({ message: text, page: msgs.dataset.page || location.pathname });
    try {
      const r = await fetch("/chat/send", { method: "POST", body, headers: { "X-CSRF": csrf } });
      const data = await r.json();
      if (!data.ok) { add("err", data.fehler || "Fehler"); return; }
      follow(data.turn);
    } catch (e) { add("err", "aeradex ist nicht erreichbar."); }
  }

  $("#composer").addEventListener("submit", (e) => { e.preventDefault(); send(input.value); });
  input.addEventListener("keydown", (e) => {
    // on a phone Enter makes a new line; the send button sends
    if (e.key === "Enter" && !e.shiftKey && !matchMedia("(pointer: coarse)").matches) { e.preventDefault(); send(input.value); }
  });
  document.querySelectorAll("#chatSuggest button").forEach(b => b.addEventListener("click", () => {
    if (b.dataset.fill !== undefined) { input.value = b.dataset.fill || b.textContent; input.focus(); grow(); return; }
    send(b.textContent);
  }));
  $("#chatReset").addEventListener("click", async () => {
    await fetch("/chat/neu", { method: "POST", headers: { "X-CSRF": csrf } });
    msgs.querySelectorAll(".msg:not(#chatHello), .tool, .thinking").forEach(el => el.remove());
    const s = $("#chatSuggest"); if (s) s.hidden = false;
  });

  // ---- microphone: record, turn into text, put it into the field to read before sending ----
  const mic = $("#chatMic"), micState = $("#chatMicState");
  const kinds = ["audio/webm;codecs=opus", "audio/mp4", "audio/webm", "audio/ogg;codecs=opus"];
  const LIMIT = 180;
  if (mic && navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder) {
    mic.hidden = false;
    let rec = null, chunks = [], timer = null, started = 0, cancelled = false;
    const state = (text, cls) => { if (!micState) return; micState.textContent = text || ""; micState.hidden = !text; micState.className = "micstate" + (cls ? " " + cls : ""); };
    const label = (s) => Math.floor(s / 60) + ":" + String(Math.floor(s % 60)).padStart(2, "0");
    const stop = (cancel) => { cancelled = !!cancel; if (rec && rec.state !== "inactive") rec.stop(); };
    mic.addEventListener("click", async () => {
      if (rec && rec.state === "recording") { stop(false); return; }
      let stream;
      try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
      catch (e) { state("Kein Zugriff aufs Mikrofon — in den Browser-Einstellungen erlauben.", "err"); return; }
      const type = kinds.find(k => MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(k)) || "";
      rec = type ? new MediaRecorder(stream, { mimeType: type }) : new MediaRecorder(stream);
      chunks = []; cancelled = false; started = Date.now();
      rec.ondataavailable = (e) => { if (e.data && e.data.size) chunks.push(e.data); };
      rec.onstop = async () => {
        clearInterval(timer); stream.getTracks().forEach(t => t.stop());
        mic.classList.remove("rec"); mic.setAttribute("aria-pressed", "false");
        const cancelBtn = $("#chatMicCancel"); if (cancelBtn) cancelBtn.hidden = true;
        if (cancelled || !chunks.length) { state(""); return; }
        const mime = (rec.mimeType || type || "audio/webm").split(";")[0];
        const ext = mime.includes("mp4") ? "m4a" : mime.includes("ogg") ? "ogg" : "webm";
        const body = new FormData();
        body.append("audio", new Blob(chunks, { type: mime }), "sprache." + ext);
        state("Erkenne Sprache …");
        mic.disabled = true;
        try {
          const r = await fetch("/chat/diktat", { method: "POST", body, headers: { "X-CSRF": csrf } });
          const d = await r.json();
          if (!d.ok) { state(d.fehler || "Nicht verstanden", "err"); return; }
          if (!d.text) { state("Nichts verstanden — bitte nochmals.", "err"); return; }
          input.value = (input.value.trim() ? input.value.trim() + " " : "") + d.text;
          grow(); input.focus();
          state("Text prüfen, dann senden.");
          setTimeout(() => state(""), 4000);
        } catch (e) { state("aeradex ist nicht erreichbar.", "err"); }
        finally { mic.disabled = false; }
      };
      rec.start(1000);
      mic.classList.add("rec"); mic.setAttribute("aria-pressed", "true");
      const cancelBtn = $("#chatMicCancel"); if (cancelBtn) cancelBtn.hidden = false;
      state("Aufnahme 0:00 · nochmals tippen zum Beenden", "rec");
      timer = setInterval(() => {
        const s = (Date.now() - started) / 1000;
        state("Aufnahme " + label(s) + " · nochmals tippen zum Beenden", "rec");
        if (s >= LIMIT) stop(false);
      }, 250);
    });
    const cancelBtn = $("#chatMicCancel");
    if (cancelBtn) cancelBtn.addEventListener("click", () => stop(true));
  }

  // a question handed over in the link (e.g. from a customer page on the phone)
  const preset = msgs.dataset.preset;
  if (preset && !input.value) { input.value = preset; grow(); requestAnimationFrame(grow); document.fonts && document.fonts.ready.then(grow); }

  // restore the running conversation after a page load
  fetch("/chat/verlauf").then(r => r.json()).then(d => {
    if (!d.items.length) return;
    hideIntro();
    d.items.forEach(i => add(i.type, i.type === "text" ? i.html : i.text, i.type === "text"));
    if (d.busy) add("thinking", "Agent arbeitet noch");
  }).catch(() => {});
})();
