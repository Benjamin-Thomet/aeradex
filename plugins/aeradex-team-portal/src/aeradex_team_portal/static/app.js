// Team app (aeradex): record time, material and absences (offline too), see own balances
// and open encrypted payslips. Plain JavaScript, no build step. Data: GET /app/api/ich (cached in IndexedDB),
// entries: POST /app/api/eintraege (queued in IndexedDB until online).
"use strict";

const API = "/app/api";
const enc = new TextEncoder();
const $ = (sel, root = document) => root.querySelector(sel);
const view = () => $("#view");
let ich = null;          // last /api/ich answer (or cached copy)
let queue = [];          // entries not yet on the server
let flushing = false;

// ---------- small helpers ----------

function h(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
}
function iso(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
function parseIso(s) { const [y, m, d] = s.split("-").map(Number); return new Date(y, m - 1, d); }
function today() { return iso(new Date()); }
function num(v, digits = 2) {
  const n = Number(v ?? 0);
  return n.toLocaleString("de-CH", {minimumFractionDigits: digits, maximumFractionDigits: digits});
}
function days(v) {
  const n = Number(v ?? 0);
  return n.toLocaleString("de-CH", {minimumFractionDigits: 0, maximumFractionDigits: 1});
}
function signed(v) { return Number(v) > 0.001 ? `+${hours(v)}` : hours(v); }
function hours(v) {
  const n = Number(v ?? 0);
  const sign = n < 0 ? "−" : "";
  const a = Math.abs(n);
  const hh = Math.floor(a + 1e-9);
  const mm = Math.round((a - hh) * 60);
  return `${sign}${hh}:${String(mm).padStart(2, "0")}`;
}
const LOCK = `<svg class="lock" viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>`;
const DAYS = ["So", "Mo", "Di", "Mi", "Do", "Fr", "Sa"];
const MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"];
function dayLabel(s) { const d = parseIso(s); return `${DAYS[d.getDay()]} ${d.getDate()}.${d.getMonth() + 1}.`; }
function stand(ts) {
  if (!ts) return "noch nie";
  const d = new Date(ts);
  return `${d.toLocaleDateString("de-CH")} ${d.toLocaleTimeString("de-CH", {hour: "2-digit", minute: "2-digit"})}`;
}
function uuid() {
  if (crypto.randomUUID) return crypto.randomUUID();
  const b = crypto.getRandomValues(new Uint8Array(16));
  b[6] = (b[6] & 15) | 64; b[8] = (b[8] & 63) | 128;
  const x = [...b].map((v) => v.toString(16).padStart(2, "0")).join("");
  return `${x.slice(0, 8)}-${x.slice(8, 12)}-${x.slice(12, 16)}-${x.slice(16, 20)}-${x.slice(20)}`;
}
function b64(buf) { return btoa(String.fromCharCode(...new Uint8Array(buf))); }
function unb64(s) { return Uint8Array.from(atob(s), (c) => c.charCodeAt(0)); }
function hex(buf) { return [...new Uint8Array(buf)].map((v) => v.toString(16).padStart(2, "0")).join(""); }

// One word that names this installation (cookie, header, storage, key salts); the portal writes it into the page.
const KENNUNG = document.documentElement.dataset.kennung || "aeradex";
const TITEL = document.documentElement.dataset.titel || "Team";
const LOGO = document.documentElement.dataset.logo === "1";

// ---------- IndexedDB: cache, queue, unwrapped key ----------

let dbp = null;
function idb() {
  if (!dbp) {
    dbp = new Promise((resolve, reject) => {
      const r = indexedDB.open(`${KENNUNG}-team`, 1);
      r.onupgradeneeded = () => { r.result.createObjectStore("kv"); r.result.createObjectStore("queue", {keyPath: "uuid"}); };
      r.onsuccess = () => resolve(r.result);
      r.onerror = () => reject(r.error);
    });
  }
  return dbp;
}
async function tx(store, mode, fn) {
  const db = await idb();
  return new Promise((resolve, reject) => {
    const t = db.transaction(store, mode);
    const out = fn(t.objectStore(store));
    t.oncomplete = () => resolve(out && "result" in out ? out.result : out);
    t.onerror = () => reject(t.error);
  });
}
const kvGet = (k) => tx("kv", "readonly", (s) => s.get(k));
const kvSet = (k, v) => tx("kv", "readwrite", (s) => s.put(v, k));
const queueAll = () => tx("queue", "readonly", (s) => s.getAll());
const queuePut = (item) => tx("queue", "readwrite", (s) => s.put(item));
const queueDel = (id) => tx("queue", "readwrite", (s) => s.delete(id));
async function wipe() {
  await tx("kv", "readwrite", (s) => s.clear());
  await tx("queue", "readwrite", (s) => s.clear());
}

// ---------- server ----------

class Offline extends Error {}
async function call(method, path, body) {
  let resp;
  try {
    resp = await fetch(API + path, {method, credentials: "same-origin",
      headers: {"content-type": "application/json", [`x-${KENNUNG}`]: "1"}, body: body ? JSON.stringify(body) : undefined});
  } catch (e) {
    throw new Offline("Keine Verbindung");
  }
  let data = {};
  try { data = await resp.json(); } catch (e) { /* empty */ }
  if (resp.status === 401 && path !== "/login" && path !== "/aktivieren" && path !== "/passwort") {
    ich = null;
    // someone who is on the way in (login, activation with a code) stays where they are
    if (!/^#\/(login|aktivieren)/.test(location.hash)) location.hash = "#/login";
    throw new Error(data.fehler || "Bitte anmelden");
  }
  if (!resp.ok || data.ok === false) throw new Error(data.fehler || `Fehler ${resp.status}`);
  return data;
}

async function load() {
  try {
    ich = await call("GET", "/ich");
    await kvSet("ich", ich);
  } catch (e) {
    if (!(e instanceof Offline)) throw e;
    ich = ich || (await kvGet("ich")) || null;
  }
  queue = await queueAll();
  return ich;
}

async function flush() {
  if (flushing) return;
  flushing = true;
  try {
    for (const item of await queueAll()) {
      try {
        if (item.op === "delete") await call("DELETE", `/eintraege/${item.uuid}`);
        else await call("POST", "/eintraege", {uuid: item.uuid, kind: item.kind, data: item.data});
        await queueDel(item.uuid);
      } catch (e) {
        if (e instanceof Offline) break;
        // the server refused this one: keep it visible as refused, never lose it silently
        item.fehler = e.message;
        await queuePut(item);
      }
    }
    queue = await queueAll();
    if (navigator.onLine) await load().catch(() => {});
  } finally {
    flushing = false;
    netBadge();
  }
}

async function submit(kind, data, id) {
  const item = {uuid: id || uuid(), kind, data, op: "put", zeit: Date.now()};
  await queuePut(item);
  queue = await queueAll();
  netBadge();
  await flush();
  return item;
}

async function remove(id) {
  const local = queue.find((q) => q.uuid === id);
  const onServer = (ich?.eintraege || []).some((e) => e.uuid === id);
  if (local && !onServer) { await queueDel(id); queue = await queueAll(); return; }
  await queuePut({uuid: id, op: "delete", zeit: Date.now()});
  await flush();
  const refused = queue.find((q) => q.uuid === id && q.op === "delete" && q.fehler);
  if (refused) {   // the server said no (closed meanwhile): say so instead of keeping a dead request
    await queueDel(id);
    queue = await queueAll();
    throw new Error(refused.fehler);
  }
}

function netBadge() {
  const el = $("#net");
  if (!el) return;
  const waiting = queue.filter((q) => !q.fehler).length;
  if (!navigator.onLine) el.innerHTML = `<span class="net off">offline</span>`;
  else if (waiting) el.innerHTML = `<span class="net queue">${waiting} wartet</span>`;
  else el.innerHTML = "";
}

// ---------- crypto ----------

async function authKey(password, benutzer) {
  const base = await crypto.subtle.importKey("raw", enc.encode(password), "PBKDF2", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits({name: "PBKDF2", hash: "SHA-256", salt: enc.encode(`${KENNUNG}-auth:${benutzer}`),
    iterations: 100000}, base, 256);
  return hex(bits);
}
async function wrapKey(password, pkcs8) {
  const salt = crypto.getRandomValues(new Uint8Array(16));
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const iter = 600000;
  const k = await passwordKey(password, salt, iter);
  const ct = await crypto.subtle.encrypt({name: "AES-GCM", iv}, k, pkcs8);
  return {salt: b64(salt), iv: b64(iv), ct: b64(ct), iter};
}
async function passwordKey(password, salt, iter) {
  const base = await crypto.subtle.importKey("raw", enc.encode(password), "PBKDF2", false, ["deriveKey"]);
  return crypto.subtle.deriveKey({name: "PBKDF2", hash: "SHA-256", salt, iterations: iter}, base,
    {name: "AES-GCM", length: 256}, false, ["encrypt", "decrypt"]);
}
async function unwrapKey(password, wrapped) {
  const k = await passwordKey(password, unb64(wrapped.salt), wrapped.iter);
  const pkcs8 = await crypto.subtle.decrypt({name: "AES-GCM", iv: unb64(wrapped.iv)}, k, unb64(wrapped.ct));
  return crypto.subtle.importKey("pkcs8", pkcs8, {name: "ECDH", namedCurve: "P-256"}, false, ["deriveBits"]);
}
async function rememberKey(password, wrapped) {
  if (!wrapped || !wrapped.ct) return;
  await kvSet("key", await unwrapKey(password, wrapped));
}
async function decryptDoc(person, doc) {
  const priv = await kvGet("key");
  if (!priv) throw new Error("Bitte einmal ab- und wieder anmelden, um Dokumente zu öffnen");
  const env = doc.envelope;
  const epk = await crypto.subtle.importKey("spki", unb64(env.epk), {name: "ECDH", namedCurve: "P-256"}, false, []);
  const shared = await crypto.subtle.deriveBits({name: "ECDH", public: epk}, priv, 256);
  const hk = await crypto.subtle.importKey("raw", shared, "HKDF", false, ["deriveKey"]);
  const aes = await crypto.subtle.deriveKey({name: "HKDF", hash: "SHA-256", salt: enc.encode(`${KENNUNG}-doc-salt-v1`),
    info: enc.encode(`${KENNUNG}-doc-v1`)}, hk, {name: "AES-GCM", length: 256}, false, ["decrypt"]);
  return crypto.subtle.decrypt({name: "AES-GCM", iv: unb64(env.iv), additionalData: enc.encode(`${person}/${doc.doc_id}`)},
    aes, unb64(env.ct));
}

// ---------- rendering ----------

function msg(text, kind = "err") { return text ? `<div class="msg ${kind}" role="${kind === "err" ? "alert" : "status"}">${h(text)}</div>` : ""; }
function hoursInApp() { return shared().stunden !== false; }
function setTab(name) {
  $("#tabs").classList.toggle("hidden", !name);
  for (const t of ["erfassen", "woche"]) {         // hours on paper: the app keeps requests, account and documents
    const a = document.querySelector(`#tabs a[data-tab="${t}"]`);
    if (a) a.classList.toggle("hidden", !hoursInApp());
  }
  document.body.classList.toggle("entry", !name);
  for (const a of document.querySelectorAll("#tabs a")) {
    if (a.dataset.tab === name) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
  $("#who").textContent = ich ? ich.name : "";
  netBadge();
}
function options(list, selected, empty) {
  return (empty !== undefined ? `<option value="">${h(empty)}</option>` : "") +
    list.map((o) => `<option value="${h(o.value)}" ${o.value === selected ? "selected" : ""}>${h(o.label)}</option>`).join("");
}
function shared() { return ich?.shared || {}; }
function mein() { return ich?.mein || {}; }
function kundeLabel(nr) {
  const k = (shared().kunden || []).find((x) => x.nummer === nr);
  return k ? k.name : nr;
}
function projekt(nr) { return (shared().projekte || []).find((x) => x.nummer === nr); }
function zielLabel(d) {
  // what an entry is for: the customer (and project), or the internal category
  if (d.projekt) {
    const p = projekt(d.projekt);
    const kunde = d.kunde || (p && p.kunde_nr);
    return [kunde ? kundeLabel(kunde) : (p && p.kunde) || "", p ? p.name : d.projekt].filter(Boolean).join(" · ");
  }
  if (d.kunde) return kundeLabel(d.kunde);
  return `Intern: ${d.kategorie || "Intern"}`;
}
function catalogText(list, nr) { const p = (shared()[list] || []).find((x) => x.nummer === nr); return p ? p.text : nr; }

// ----- login / activation -----

function loginKopf() {
  return LOGO ? `<div class="logo"><img src="/app/static/logo.png" alt="${h(TITEL)}"></div>` : `<h2 class="muted">${h(TITEL)}</h2>`;
}

function renderLogin(error = "") {
  setTab(null);
  view().innerHTML = `<div class="login">
    ${loginKopf()}
    <h1>Anmelden</h1>${msg(error)}
    <form id="f" class="card" autocomplete="on">
      <div class="field"><label for="u">Benutzername</label><input id="u" name="benutzer" autocomplete="username" autocapitalize="none" required></div>
      <div class="field"><label for="p">Passwort</label><input id="p" name="passwort" type="password" autocomplete="current-password" required></div>
      <button class="btn primary block" type="submit">Anmelden</button>
    </form>
    <p class="muted small">Zum ersten Mal hier? <a href="#/aktivieren">Mit dem Code aus dem Büro aktivieren</a>.<br>Passwort vergessen? Im Büro einen neuen Code verlangen.</p>
  </div>`;
  $("#f").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const btn = ev.submitter; btn.disabled = true; btn.textContent = "Prüfe …";
    const benutzer = $("#u").value.trim().toLowerCase(), pw = $("#p").value;
    try {
      await call("POST", "/login", {benutzer, auth: await authKey(pw, benutzer)});
      await load();
      await rememberKey(pw, ich.wrapped);
      location.hash = "#/erfassen";
    } catch (e) { renderLogin(e.message); }
  });
}

function renderActivate(error = "") {
  setTab(null);
  view().innerHTML = `<div class="login">
    ${loginKopf()}
    <h1>Konto aktivieren</h1>${msg(error)}
    <form id="f" class="card">
      <div class="field"><label for="u">Benutzername</label><input id="u" autocomplete="username" autocapitalize="none" required></div>
      <div class="field"><label for="c">Code aus dem Büro</label><input id="c" autocapitalize="characters" placeholder="ABCD-EFGH" required></div>
      <div class="field"><label for="p">Neues Passwort (mind. 10 Zeichen)</label><input id="p" type="password" autocomplete="new-password" minlength="10" required></div>
      <div class="field"><label for="p2">Passwort wiederholen</label><input id="p2" type="password" autocomplete="new-password" minlength="10" required></div>
      <button class="btn primary block" type="submit">Aktivieren</button>
    </form>
    <p class="muted small">Mit dem Passwort werden auch deine Lohnabrechnungen verschlüsselt. Nur du kannst sie öffnen.</p>
    <p class="small"><a href="#/login">Zurück zur Anmeldung</a></p>
  </div>`;
  $("#f").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const pw = $("#p").value;
    if (pw !== $("#p2").value) return renderActivate("Die Passwörter stimmen nicht überein");
    const btn = ev.submitter; btn.disabled = true; btn.textContent = "Schlüssel wird erzeugt …";
    const benutzer = $("#u").value.trim().toLowerCase();
    try {
      const pair = await crypto.subtle.generateKey({name: "ECDH", namedCurve: "P-256"}, true, ["deriveBits"]);
      const pkcs8 = await crypto.subtle.exportKey("pkcs8", pair.privateKey);
      const spki = await crypto.subtle.exportKey("spki", pair.publicKey);
      const wrapped = await wrapKey(pw, pkcs8);
      await call("POST", "/aktivieren", {benutzer, code: $("#c").value, auth: await authKey(pw, benutzer), pubkey: b64(spki), wrapped});
      await load();
      await rememberKey(pw, wrapped);
      location.hash = "#/erfassen";
    } catch (e) { renderActivate(e.message); }
  });
}

// ----- Erfassen -----

let erfassenArt = "zeit";
function recentCombos() {
  const seen = new Map();
  const all = [...queue.filter((q) => q.kind === "zeit" && q.op === "put").map((q) => q.data),
    ...(ich?.eintraege || []).filter((e) => e.kind === "zeit").map((e) => e.data),
    ...(mein().eintraege || []).filter((e) => e.kind === "zeit").map((e) => e.data)].filter((d) => d && !d._loeschen).reverse();
  for (const d of all) {
    const key = `${d.kunde || ""}|${d.projekt || ""}|${d.leistung || ""}|${d.kategorie || ""}`;
    if (!seen.has(key)) seen.set(key, d);
  }
  return [...seen.values()].slice(0, 6);
}

function kundeOf(d) { return d.kunde || (d.projekt && projekt(d.projekt) ? projekt(d.projekt).kunde_nr : ""); }

// ----- customers: search, suggestions, addresses -----

function fold(v) { return String(v || "").toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, ""); }
function kundeDaten(nr) { return (shared().kunden || []).find((x) => x.nummer === nr); }
function adresse(k) { return [k.strasse, [k.ort, k.land].filter(Boolean).join(" ")].filter(Boolean).join(", "); }
// Apple Maps on an iPhone, Google Maps elsewhere (on Android the link opens the Maps app)
function karte(k) {
  const q = encodeURIComponent(adresse(k));
  return /iPhone|iPad|iPod/.test(navigator.userAgent) ? `https://maps.apple.com/?q=${q}` : `https://www.google.com/maps/search/?api=1&query=${q}`;
}
function kundenTreffer(q, limit = 8) {
  // every word must appear in name, contact, address or one of the customer's objects; name matches first
  const words = fold(q).split(/\s+/).filter(Boolean);
  const out = [];
  for (const k of shared().kunden || []) {
    const name = fold(`${k.name} ${k.kontakt || ""}`);
    const hay = `${name} ${fold(adresse(k))} ${objekteVon(k.nummer).map((p) => fold(p.name)).join(" ")} ${fold(k.nummer)}`;
    if (!words.every((w) => hay.includes(w))) continue;
    out.push({k, rank: words.length && name.startsWith(words[0]) ? 0 : words.every((w) => name.includes(w)) ? 1 : 2});
  }
  out.sort((a, b) => a.rank - b.rank || a.k.name.localeCompare(b.k.name, "de-CH", {numeric: true}));
  return out.slice(0, limit).map((x) => x.k);
}
function zuletztKunden() {
  // the customers of the last entries, for the empty search field
  const seen = [];
  const all = [...queue.filter((q) => q.op === "put").map((q) => q.data),
    ...(mein().eintraege || []).filter((e) => e.data).map((e) => e.data)].filter((d) => d && !d._loeschen).reverse();
  for (const d of all) { const nr = kundeOf(d); if (nr && kundeDaten(nr) && !seen.includes(nr)) seen.push(nr); }
  return seen.slice(0, 5).map(kundeDaten);
}

function zielField(d, withIntern) {
  // one search field: customers by name, place or object; internal work (Werkstatt, Büro …) below them
  const s = shared();
  const sel = kundeOf(d) ? `kunde:${kundeOf(d)}` : d.kategorie ? `intern:${d.kategorie}` : "";
  const label = sel.startsWith("kunde:") ? kundeLabel(sel.slice(6)) : sel ? `Intern: ${d.kategorie}` : "";
  return `<div class="field combo"><label for="zielSuche">${withIntern ? "Kunde oder intern" : "Kunde"}</label>
      <input id="zielSuche" type="search" autocomplete="off" autocapitalize="off" spellcheck="false" enterkeyhint="search"
        role="combobox" aria-autocomplete="list" aria-expanded="false" aria-controls="zielListe"
        placeholder="${withIntern ? "Name, Ort oder intern …" : "Name oder Ort tippen …"}" value="${h(label)}">
      <input type="hidden" id="ziel" value="${h(sel)}" data-intern="${withIntern ? "1" : ""}">
      <ul id="zielListe" class="combo-list hidden" role="listbox"></ul>
      <p class="muted small hidden" id="zielAdresse"></p></div>
    <div class="field hidden" id="projektWrap"><label for="projekt">${h(wort())}${s.projekt_pflicht ? "" : ` <span class="unit">optional</span>`}</label><select id="projekt"></select></div>`;
}

// what the book calls its projects (e.g. «Objekt») and whether a customer with some needs one chosen
function wort() { return shared().projekt_wort || "Projekt"; }
function objekteVon(kunde) { return (shared().projekte || []).filter((p) => p.kunde_nr === kunde); }
function objektFehlt(z) { return !!(shared().projekt_pflicht && z.kunde && !z.projekt && objekteVon(z.kunde).length); }

function bindSuche(onPick) {
  // the suggestions under #zielSuche; picking one sets #ziel and fires its change event
  const input = $("#zielSuche"), hidden = $("#ziel"), list = $("#zielListe");
  const withIntern = !!hidden.dataset.intern;
  let items = [], active = -1;
  const show = () => {
    const q = input.value.trim();
    const typed = q && !(hidden.value && input.value === input.dataset.chosen);
    const kunden = typed ? kundenTreffer(q) : zuletztKunden();
    const intern = !withIntern ? [] : (shared().kategorien || []).filter((k) => !typed || fold(k).includes(fold(q)) || fold("intern").startsWith(fold(q)));
    items = [...kunden.map((k) => ({value: `kunde:${k.nummer}`, label: k.name, sub: [k.kontakt, k.ort].filter(Boolean).join(" · ")})),
      ...intern.map((k) => ({value: `intern:${k}`, label: `Intern: ${k}`, sub: ""}))];
    active = items.length && typed ? 0 : -1;
    const head = !typed && kunden.length ? `<li class="combo-head" role="presentation">Zuletzt</li>` : "";
    list.innerHTML = items.length ? head + items.map((it, i) => `${!typed && i === kunden.length && kunden.length ? `<li class="combo-head" role="presentation">Intern</li>` : ""}<li role="option" id="opt${i}" data-i="${i}" aria-selected="${i === active}"><span>${h(it.label)}</span>${it.sub ? `<span class="muted small">${h(it.sub)}</span>` : ""}</li>`).join("")
      : `<li class="combo-head" role="presentation">${typed ? "Kein Kunde gefunden. Fehlt einer, sag es dem Büro." : "Tippe den Namen oder den Ort."}</li>`;
    list.classList.remove("hidden");
    input.setAttribute("aria-expanded", "true");
    if (active >= 0) input.setAttribute("aria-activedescendant", `opt${active}`); else input.removeAttribute("aria-activedescendant");
  };
  const hide = () => { list.classList.add("hidden"); input.setAttribute("aria-expanded", "false"); };
  const pick = (it) => {
    hidden.value = it.value; input.value = it.label; input.dataset.chosen = it.label; hide();
    hidden.dispatchEvent(new Event("change"));
    if (onPick) onPick(it);
  };
  input.dataset.chosen = input.value;
  input.addEventListener("focus", () => { if (hidden.value) input.select(); show(); });
  input.addEventListener("input", () => {
    if (hidden.value) { hidden.value = ""; hidden.dispatchEvent(new Event("change")); }
    show();
  });
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
      ev.preventDefault();
      if (list.classList.contains("hidden")) show();
      if (!items.length) return;
      active = (active + (ev.key === "ArrowDown" ? 1 : items.length - 1)) % items.length;
      for (const li of list.querySelectorAll("[role=option]")) li.setAttribute("aria-selected", String(Number(li.dataset.i) === active));
      input.setAttribute("aria-activedescendant", `opt${active}`);
    } else if (ev.key === "Enter" && !list.classList.contains("hidden")) {
      ev.preventDefault();                       // Enter picks, it does not send the form
      if (active >= 0) pick(items[active]);
    } else if (ev.key === "Escape") hide();
  });
  // pointerdown, not click: the field's blur would close the list first
  list.addEventListener("pointerdown", (ev) => {
    const li = ev.target.closest("[role=option]");
    if (li) { ev.preventDefault(); pick(items[Number(li.dataset.i)]); }
  });
  input.addEventListener("blur", () => {
    setTimeout(() => {
      hide();
      if (!hidden.value && input.value.trim()) {     // typed a name exactly or one match only: take it
        const hits = kundenTreffer(input.value, 2);
        if (hits.length === 1) pick({value: `kunde:${hits[0].nummer}`, label: hits[0].name});
      }
    }, 150);
  });
}

function bindZiel(d = {}) {
  // the project list follows the customer; it only appears when that customer has open projects
  bindSuche();
  const fill = () => {
    const ziel = $("#ziel").value;
    const kunde = ziel.startsWith("kunde:") ? ziel.slice(6) : "";
    const list = objekteVon(kunde);
    const pflicht = !!shared().projekt_pflicht && list.length > 0;
    $("#projektWrap").classList.toggle("hidden", !list.length);
    $("#projekt").required = pflicht;
    $("#projekt").innerHTML = `<option value="">${pflicht ? `${h(wort())} wählen …` : `ohne ${h(wort())}`}</option>` + list.map((p) => `<option value="${h(p.nummer)}" ${p.nummer === d.projekt ? "selected" : ""}>${h(p.name)}</option>`).join("");
    const lw = $("#leistungWrap");
    if (lw) lw.classList.toggle("hidden", ziel.startsWith("intern:"));
    const k = kundeDaten(kunde), where = $("#zielAdresse");
    where.classList.toggle("hidden", !(k && adresse(k)));
    where.innerHTML = k && adresse(k) ? `${h(adresse(k))} · <a href="${h(karte(k))}" target="_blank" rel="noopener">Karte</a>` : "";
  };
  $("#ziel").addEventListener("change", () => { d = {}; fill(); });
  fill();
}

function readZiel() {
  const ziel = $("#ziel").value;
  const intern = ziel.startsWith("intern:");
  return {kunde: ziel.startsWith("kunde:") ? ziel.slice(6) : "", projekt: intern ? "" : $("#projekt").value,
    kategorie: intern ? ziel.slice(7) : ""};
}

function renderErfassen(note = "", noteKind = "ok", prefill = null) {
  setTab("erfassen");
  if (!ich) return renderLogin();
  const s = shared();
  const d = prefill || {};
  const editing = !!(prefill && prefill.uuid);
  const combos = editing ? [] : recentCombos();
  let body;
  if (erfassenArt === "zeit") {
    body = `
      ${combos.length ? `<div class="chips" aria-label="Zuletzt">${combos.map((c, i) => `<button class="chip" type="button" data-combo="${i}">${h(zielLabel(c))}${c.leistung ? " · " + h(catalogText("leistungen", c.leistung)) : ""}</button>`).join("")}</div>` : ""}
      <form id="f" class="card">
        <div class="field"><label for="datum">Datum</label><input id="datum" type="date" value="${h(d.datum || today())}" required></div>
        <div class="field"><label for="std">Stunden</label><input id="std" inputmode="decimal" placeholder="z.B. 4:30" value="${h(d.stunden ? hours(d.stunden) : "")}"></div>
        <div class="row"><div class="field"><label for="von">oder von</label><input id="von" type="time"></div><div class="field"><label for="bis">bis</label><input id="bis" type="time"></div></div>
        ${zielField(d, true)}
        ${(s.leistungen || []).length ? `<div class="field" id="leistungWrap"><label for="leistung">Leistungsart</label><select id="leistung">${options((s.leistungen || []).map((l) => ({value: l.nummer, label: l.text})), d.leistung || "", "—")}</select></div>` : ""}
        <div class="field"><label for="text">Was wurde gemacht?</label><input id="text" value="${h(d.text || "")}" placeholder="${shared().text_beispiel ? `z.B. ${h(shared().text_beispiel)}` : ""}">${s.rechnung_tage ? `<p class="muted small">Arbeitet ihr zu zweit am selben ${h(wort())}, genügt eine Beschreibung pro Tag.</p>` : ""}</div>
        <button class="btn primary block" type="submit">${editing ? "Änderung speichern" : "Speichern"}</button>
      </form>`;
  } else {
    body = `<form id="f" class="card">
        <div class="field"><label for="datum">Datum</label><input id="datum" type="date" value="${h(d.datum || today())}" required></div>
        ${zielField(d, false)}
        <div class="field"><label for="produkt">Material</label><select id="produkt" required>${options([...(s.material || []).map((m) => ({value: m.nummer, label: `${m.text} (${m.einheit})`})), {value: "TEXT", label: "Anderes (eigener Text) …"}], d.produkt || "", "Material wählen …")}</select></div>
        <div class="field"><label for="menge">Menge <span id="einheit" class="unit"></span></label><input id="menge" inputmode="decimal" value="${h(d.menge || "")}" required></div>
        <div class="field"><label for="text" id="textLabel">Bemerkung</label><input id="text" value="${h(d.text || "")}" placeholder="optional"></div>
        <button class="btn primary block" type="submit">${editing ? "Änderung speichern" : "Speichern"}</button>
      </form>`;
  }
  view().innerHTML = `<h1>Erfassen</h1>${msg(note, noteKind)}
    ${editing ? "" : `<div class="seg" role="group" aria-label="Art"><button type="button" data-art="zeit" aria-pressed="${erfassenArt === "zeit"}">Arbeitszeit</button><button type="button" data-art="material" aria-pressed="${erfassenArt === "material"}">Material</button></div>`}
    ${body}
    ${!(s.kunden || []).length ? `<p class="muted small">Noch keine Kunden vom Büro übertragen (letzter Abgleich: ${h(stand(ich.letzter_abgleich))}).</p>` : ""}`;
  for (const b of document.querySelectorAll("[data-art]")) b.addEventListener("click", () => { erfassenArt = b.dataset.art; renderErfassen(); });
  bindZiel(d);

  if (erfassenArt === "zeit") {
    for (const b of document.querySelectorAll("[data-combo]")) {
      b.addEventListener("click", () => {
        const c = combos[Number(b.dataset.combo)];
        renderErfassen("", "ok", {datum: $("#datum").value, kunde: kundeOf(c), projekt: c.projekt, kategorie: c.kategorie, leistung: c.leistung, text: c.text});
      });
    }
    $("#f").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      let std = $("#std").value.trim();
      if ($("#von").value && $("#bis").value) {
        const [a, b] = [$("#von").value, $("#bis").value].map((t) => { const [x, y] = t.split(":").map(Number); return x + y / 60; });
        std = String(b > a ? (b - a).toFixed(2) : "");
      } else if (std.includes(":")) {
        const [x, y] = std.split(":").map(Number); std = (x + (y || 0) / 60).toFixed(2);
      }
      std = std.replace(",", ".");
      if (!(Number(std) > 0 && Number(std) <= 24)) return renderErfassen("Stunden angeben (z.B. 4:30) oder von/bis", "err", {...d, ...readZiel(), text: $("#text").value});
      const z = readZiel();
      if (!z.kunde && !z.kategorie) return renderErfassen("Kunde oder intern wählen", "err", {...d, datum: $("#datum").value, stunden: std});
      if (objektFehlt(z)) return renderErfassen(`Zuerst das ${wort()} wählen`, "err", {...d, ...z, datum: $("#datum").value, stunden: std, text: $("#text").value});
      const leistung = z.kategorie || !$("#leistung") ? "" : $("#leistung").value;   // no field: fixed in the book
      const text = $("#text").value.trim();
      const einerGenuegt = !!(s.rechnung_tage && z.projekt);   // one description per day on the invoice
      if (z.kunde && !text && !leistung && !einerGenuegt) return renderErfassen($("#leistung") ? "Was wurde gemacht? Text oder Leistungsart" : "Was wurde gemacht?", "err", {...d, ...z, datum: $("#datum").value, stunden: std});
      await save("zeit", {datum: $("#datum").value, stunden: std, ...z, leistung, text}, prefill?.uuid);
    });
  } else {
    const unit = () => {
      const m = (s.material || []).find((x) => x.nummer === $("#produkt").value);
      const own = $("#produkt").value === "TEXT";          // «Anderes»: the text is the description, required
      $("#einheit").textContent = m ? `in ${m.einheit}` : own ? "(Stück)" : "";
      $("#textLabel").textContent = own ? "Was? (z.B. Silikon weiss, Kartusche)" : "Bemerkung";
      $("#text").placeholder = own ? "Beschreibung" : "optional";
      $("#text").required = own;
    };
    $("#produkt").addEventListener("change", unit);
    unit();
    $("#f").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const menge = $("#menge").value.replace(",", ".");
      if (!(Number(menge) > 0)) return renderErfassen("Menge angeben", "err");
      const z = readZiel();
      if (!z.kunde) return renderErfassen("Kunde wählen", "err");
      if (objektFehlt(z)) return renderErfassen(`Zuerst das ${wort()} wählen`, "err", {...d, ...z, datum: $("#datum").value, produkt: $("#produkt").value, menge, text: $("#text").value});
      if ($("#produkt").value === "TEXT" && !$("#text").value.trim()) return renderErfassen("Beschreiben, was verwendet wurde", "err");
      await save("material", {datum: $("#datum").value, kunde: z.kunde, projekt: z.projekt, produkt: $("#produkt").value, menge, text: $("#text").value.trim()}, prefill?.uuid);
    });
  }
}

async function save(kind, data, id) {
  await submit(kind, data, id);
  const failed = queue.find((q) => q.fehler);
  if (failed) { await queueDel(failed.uuid); queue = await queueAll(); return renderErfassen(failed.fehler, "err"); }
  const waiting = queue.some((q) => !q.fehler);
  renderErfassen(waiting ? "Gespeichert auf dem Handy, wird gesendet sobald Empfang da ist."
    : id ? "Änderung gesendet. Das Büro übernimmt sie beim nächsten Abgleich." : "Gespeichert. Das Büro übernimmt es beim nächsten Abgleich.", waiting ? "info" : "ok");
}

// ----- Woche -----

let weekStart = null;
function monday(d) { const x = new Date(d); x.setHours(0, 0, 0, 0); x.setDate(x.getDate() - ((x.getDay() + 6) % 7)); return x; }

function allItems() {
  // the office's booked entries, overlaid by whatever is still on its way (portal mailbox, phone queue):
  // a new entry, a change of a booked one, or its deletion
  const out = [];
  const booked = new Map((mein().eintraege || []).filter((e) => e.uuid).map((e) => [e.uuid, e]));
  const pending = new Map((ich?.eintraege || []).filter((e) => e.kind !== "abwesenheit" && e.status !== "uebernommen").map((e) => [e.uuid, e]));
  const queued = new Map(queue.filter((q) => q.kind !== "abwesenheit").map((q) => [q.uuid, q]));
  for (const e of mein().eintraege || []) {
    if (e.uuid && (pending.has(e.uuid) || queued.has(e.uuid))) continue;
    out.push(fromBooked(e));
  }
  for (const [u, e] of pending) {
    if (queued.has(u)) continue;
    if (e.status === "abgelehnt" && booked.has(u)) {   // a refused change: the booked version stays, with the reason
      out.push({...fromBooked(booked.get(u)), status: "Änderung abgelehnt", cls: "bad", reason: e.reason});
      continue;
    }
    out.push(fromData(e.kind, e.data, u, e.status, e.reason, booked.has(u)));
  }
  for (const q of queue) {
    if (q.kind === "abwesenheit" || q.op !== "put") continue;
    out.push(fromData(q.kind, q.data, q.uuid, q.fehler ? "abgelehnt" : "wartet", q.fehler, booked.has(q.uuid)));
  }
  return out;
}
function fromBooked(e) {
  return {uuid: e.uuid, datum: e.datum, menge: e.menge, art: e.art, einheit: e.einheit, was: e.was, text: e.text,
    status: "übernommen", cls: "ok", editable: !!e.aenderbar, locked: e.gesperrt || (e.uuid ? "" : "im Büro erfasst"),
    kind: e.kind, data: e.data};
}
function fromData(kind, d, id, status, reason, change) {
  const labels = {offen: [change ? "Änderung gesendet" : "gesendet", "info"], in_uebernahme: ["wird übernommen", "info"],
    uebernommen: ["übernommen", "ok"], abgelehnt: ["abgelehnt", "bad"], wartet: ["auf dem Handy", "warn"]};
  let [label, cls] = labels[status] || [status, "info"];
  if (d._loeschen) [label, cls] = ["wird gelöscht", "warn"];
  const editable = !d._loeschen && ["offen", "wartet", "abgelehnt"].includes(status);
  if (kind === "zeit") {
    return {uuid: id, datum: d.datum, menge: d.stunden, art: "Zeit", was: zielLabel(d),
      text: [d.leistung ? catalogText("leistungen", d.leistung) : "", d.text].filter(Boolean).join(" · "),
      status: label, cls, reason, editable, kind, data: d, deleting: !!d._loeschen};
  }
  const m = (shared().material || []).find((x) => x.nummer === d.produkt);
  return {uuid: id, datum: d.datum, menge: d.menge, art: "Produkt", einheit: m ? m.einheit : "", was: m ? m.text : d.produkt === "TEXT" ? "Anderes" : d.produkt,
    text: [zielLabel(d), d.text].filter(Boolean).join(" · "), status: label, cls, reason, editable, kind, data: d,
    deleting: !!d._loeschen};
}

function renderWoche() {
  setTab("woche");
  if (!ich) return renderLogin();
  weekStart = weekStart || monday(new Date());
  const days = [...Array(7)].map((_, i) => { const d = new Date(weekStart); d.setDate(d.getDate() + i); return iso(d); });
  const items = allItems();
  const tage = mein().tage || {};
  const feiertage = shared().feiertage || {};
  const abw = mein().abwesenheiten || [];
  let sumSoll = 0, sumIst = 0;
  const rows = days.map((day) => {
    const list = items.filter((x) => x.datum === day);
    const ist = list.filter((x) => x.art === "Zeit" && x.cls !== "bad" && !x.deleting).reduce((a, x) => a + Number(x.menge), 0);
    const soll = Number(tage[day] || 0);
    const absent = abw.find((a) => a.von <= day && day <= a.bis);
    const absentHours = absent ? Number(absent.stunden_pro_tag || 0) : 0;
    // days still ahead are not missing hours: the running week counts its target only up to today
    if (day <= today()) sumSoll += soll;
    sumIst += ist + absentHours;
    if (!list.length && !soll && !absent && !feiertage[day] && parseIso(day).getDay() % 6 === 0) return "";
    const note = [feiertage[day], absent && absent.art].filter(Boolean).map(h).join(" · ");
    return `<div class="day"><div class="dayhead"><strong>${dayLabel(day)}${note ? ` <span class="note">${note}</span>` : ""}</strong><span class="soll">${hours(ist + absentHours)}${soll ? ` <span class="of">von ${hours(soll)}</span>` : ""}</span></div>
      ${list.length ? list.map((x) => `<div class="item${x.deleting ? " deleting" : ""}"><button class="link" data-i="${items.indexOf(x)}"><div class="what">${h(x.was)}</div><div class="meta">${h(x.text)}</div>${x.reason ? `<div class="reason">${h(x.reason)}</div>` : ""}</button>
        <div class="side"><div class="qty">${x.locked ? LOCK : ""}${x.art === "Zeit" ? hours(x.menge) : `${num(x.menge, 0)} ${h(x.einheit)}`}</div>${x.cls === "ok" ? "" : `<span class="pill ${x.cls}">${h(x.status)}</span>`}</div></div>`).join("") : `<div class="empty">${absent ? (absentHours ? `${h(absent.art)}: zählt ${hours(absentHours)}` : h(absent.art)) : "nichts erfasst"}</div>`}</div>`;
  }).join("");
  const kw = weekNumber(weekStart);
  view().innerHTML = `<h1>Meine Woche</h1>
    <div class="weeknav"><button class="btn small" id="prev" aria-label="Vorherige Woche">‹</button><strong>KW ${kw} · ${dayLabel(days[0])} – ${dayLabel(days[6])}</strong><button class="btn small" id="next" aria-label="Nächste Woche">›</button></div>
    <div class="stats three"><div class="stat"><div class="k">Ist</div><div class="v">${hours(sumIst)}</div></div><div class="stat"><div class="k">${days[6] > today() && days[0] <= today() ? "Soll bis heute" : "Soll"}</div><div class="v">${hours(sumSoll)}</div></div><div class="stat"><div class="k">Differenz</div><div class="v ${sumIst - sumSoll > 0.001 ? "pos" : sumIst - sumSoll < -0.001 ? "neg" : ""}">${signed(sumIst - sumSoll)}</div></div></div>
    <div class="card">${rows || `<div class="empty">Keine Einträge</div>`}</div>
    <p class="muted small">Ist inklusive Ferien, Krankheit und anderer Abwesenheiten. Eigene Einträge kannst du antippen und ändern, bis das Büro den Zeitraum abschliesst${mein().abgeschlossen_bis ? ` (abgeschlossen bis ${dayLabel(mein().abgeschlossen_bis)})` : ""}. Stand Büro: ${h(stand(mein().stand || shared().stand))}</p>`;
  $("#prev").addEventListener("click", () => { weekStart.setDate(weekStart.getDate() - 7); renderWoche(); });
  $("#next").addEventListener("click", () => { weekStart.setDate(weekStart.getDate() + 7); renderWoche(); });
  for (const b of document.querySelectorAll("[data-i]")) b.addEventListener("click", () => editItem(items[Number(b.dataset.i)]));
}

function weekNumber(d) {
  const t = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
  t.setUTCDate(t.getUTCDate() + 4 - (t.getUTCDay() || 7));
  return Math.ceil(((t - Date.UTC(t.getUTCFullYear(), 0, 1)) / 86400000 + 1) / 7);
}

function lockText(why) {
  return {abgeschlossen: "Das Büro hat diesen Zeitraum abgeschlossen.", verrechnet: "Dieser Eintrag ist schon verrechnet.",
    "im Büro erfasst": "Diesen Eintrag hat das Büro erfasst."}[why] || why;
}

function editItem(x) {
  const dlg = $("#dlg");
  const head = `<h2>${h(x.was)}</h2><p class="muted">${dayLabel(x.datum)} · ${x.art === "Zeit" ? hours(x.menge) + " h" : num(x.menge, 0) + " " + h(x.einheit)}${x.text ? " · " + h(x.text) : ""}</p>`;
  if (!x.editable) {
    dlg.innerHTML = `${head}${msg(x.deleting ? "Wird beim nächsten Abgleich gelöscht." : x.locked ? `${lockText(x.locked)} Änderungen bitte im Büro melden.` : "Wird gerade übernommen. Bitte später nochmals.", "info")}
      <div class="actions fill"><button class="btn" id="close">Schliessen</button></div>`;
    dlg.showModal();
    $("#close").addEventListener("click", () => dlg.close());
    return;
  }
  dlg.innerHTML = `${head}${x.reason ? msg(x.status === "Änderung abgelehnt" ? "Änderung abgelehnt: " + x.reason : "Abgelehnt: " + x.reason) : ""}
    <div class="actions fill"><button class="btn primary" id="bearb">Bearbeiten</button><button class="btn danger" id="del">Löschen</button></div>
    <div class="actions fill dlgfoot"><button class="btn" id="close">Schliessen</button></div>`;
  dlg.showModal();
  $("#close").addEventListener("click", () => dlg.close());
  $("#del").addEventListener("click", async () => {
    if (!confirm("Diesen Eintrag löschen?")) return;
    dlg.close();
    try { await remove(x.uuid); } catch (e) { alert(e.message); }
    await load().catch(() => {}); renderWoche();
  });
  $("#bearb").addEventListener("click", () => {
    dlg.close();
    erfassenArt = x.kind;
    location.hash = "#/erfassen";
    setTimeout(() => renderErfassen("Eintrag bearbeiten und speichern", "info", {...x.data, uuid: x.uuid}), 0);
  });
}

// ----- Abwesenheiten -----

function absenceItems() {
  // booked absences and requests, overlaid by changes still on their way (like the week)
  const label = {offen: ["beantragt", "warn"], bewilligt: ["bewilligt", "ok"], abgelehnt: ["abgelehnt", "bad"],
    eingetragen: ["eingetragen", "ok"], wartet: ["auf dem Handy", "warn"], in_uebernahme: ["wird übernommen", "info"],
    gesendet: ["gesendet", "info"]};
  const since = iso(new Date(Date.now() - 60 * 86400000));
  const booked = [
    ...(mein().abwesenheiten || []).filter((a) => a.bis >= since).map((a) => ({...a, halb: Number(a.anteil) === 0.5, st: ["Krank", "Unfall"].includes(a.art) ? "eingetragen" : "bewilligt"})),
    ...(mein().antraege || []).filter((a) => a.status !== "bewilligt").map((a) => ({...a, st: a.status}))];
  const byUuid = new Map(booked.filter((b) => b.uuid).map((b) => [b.uuid, b]));
  const pending = new Map((ich?.eintraege || []).filter((e) => e.kind === "abwesenheit" && e.status !== "uebernommen").map((e) => [e.uuid, e]));
  const queued = new Map(queue.filter((q) => q.kind === "abwesenheit").map((q) => [q.uuid, q]));
  const out = [];
  const make = (d, st, extra) => {
    const [text, cls] = label[st] || [st, "info"];
    return {art: d.art, von: d.von, bis: d.bis || d.von, halb: !!d.halb, status: text, cls, ...extra};
  };
  for (const b of booked) {
    if (b.uuid && (pending.has(b.uuid) || queued.has(b.uuid))) continue;
    out.push(make(b, b.st, {uuid: b.uuid, grund: b.grund, editable: !!b.aenderbar, locked: b.gesperrt || (b.uuid ? "" : "im Büro erfasst"),
      data: b.data, request: (b.id || "").startsWith("ANT-")}));
  }
  for (const [u, e] of pending) {
    if (queued.has(u)) continue;
    if (e.status === "abgelehnt" && byUuid.has(u)) {
      const b = byUuid.get(u);
      out.push(make(b, b.st, {uuid: u, grund: "Änderung abgelehnt: " + e.reason, editable: true, data: b.data, request: (b.id || "").startsWith("ANT-")}));
      continue;
    }
    const st = e.data._loeschen ? "wird gelöscht" : e.status === "offen" ? (byUuid.has(u) ? "Änderung gesendet" : "gesendet") : e.status;
    out.push(make(e.data, st, {uuid: u, grund: e.reason, editable: !e.data._loeschen && ["offen", "abgelehnt"].includes(e.status), data: e.data,
      deleting: !!e.data._loeschen}));
  }
  for (const q of queue) {
    if (q.kind !== "abwesenheit" || q.op !== "put") continue;
    out.push(make(q.data, q.fehler ? "abgelehnt" : "wartet", {uuid: q.uuid, grund: q.fehler, editable: true, data: q.data}));
  }
  return out.sort((a, b) => b.von.localeCompare(a.von));
}

function renderAbwesend(note = "", kind = "ok", edit = null) {
  setTab("abwesend");
  if (!ich) return renderLogin();
  const arten = ["Ferien", "Krank", "Unfall", "Kompensation", "Weiterbildung", "Unbezahlt"];
  const d = edit ? edit.data : {art: "Ferien", von: today(), bis: today(), halb: false, notiz: ""};
  const items = absenceItems();
  const range = (a) => a.von === a.bis ? dayLabel(a.von) : `${dayLabel(a.von)} – ${dayLabel(a.bis)}`;
  view().innerHTML = `<h1>Abwesenheiten</h1>${msg(note, kind)}
    <form id="f" class="card">
      ${edit ? `<h2>Ändern</h2>` : ""}
      <div class="field"><label for="art">Art</label><select id="art">${options(arten.map((a) => ({value: a, label: a})), d.art)}</select></div>
      <div class="field"><label for="von">Von</label><input id="von" type="date" value="${h(d.von)}" required></div>
      <div class="field"><label for="bis">Bis</label><input id="bis" type="date" value="${h(d.bis || d.von)}" required></div>
      <label class="check"><input type="checkbox" id="halb" ${d.halb ? "checked" : ""}> nur halbe Tage</label>
      <div class="field"><label for="notiz">Bemerkung</label><input id="notiz" value="${h(d.notiz || "")}" placeholder="optional"></div>
      <p class="small muted" id="hint"></p>
      <div class="actions fill"><button class="btn primary" type="submit" id="go">Senden</button>${edit ? `<button class="btn" type="button" id="cancel">Abbrechen</button>` : ""}</div>
    </form>
    <div class="card"><h2>Meine Abwesenheiten</h2>
      ${items.map((a, i) => `<div class="item${a.deleting ? " deleting" : ""}"><button class="link" data-a="${i}"><div class="what">${h(a.art)}${a.halb ? " (halbe Tage)" : ""}</div><div class="meta">${range(a)}</div>${a.grund ? `<div class="${a.cls === "bad" || String(a.grund).startsWith("Änderung") ? "reason" : "meta"}">${h(a.grund)}</div>` : ""}</button>
        <div class="side"><span class="pill ${a.cls}">${a.locked ? LOCK + " " : ""}${h(a.status)}</span></div></div>`).join("") || `<div class="empty">Keine</div>`}
    </div>`;
  const hint = () => {
    const a = $("#art").value;
    $("#hint").textContent = ["Krank", "Unfall"].includes(a) ? "Wird direkt eingetragen." + (shared().arztzeugnis_ab_tag ? ` Ab dem ${shared().arztzeugnis_ab_tag}. Tag braucht es ein Arztzeugnis.` : "") : "Wird als Antrag gesendet und im Büro bewilligt.";
    $("#go").textContent = edit ? "Änderung senden" : ["Krank", "Unfall"].includes(a) ? "Melden" : "Antrag senden";
  };
  $("#art").addEventListener("change", hint); hint();
  $("#von").addEventListener("change", () => { if ($("#bis").value < $("#von").value) $("#bis").value = $("#von").value; });
  if (edit) $("#cancel").addEventListener("click", () => renderAbwesend());
  for (const b of document.querySelectorAll("[data-a]")) b.addEventListener("click", () => editAbsence(items[Number(b.dataset.a)]));
  $("#f").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if ($("#bis").value < $("#von").value) return renderAbwesend("«Bis» liegt vor «Von»", "err", edit);
    await submit("abwesenheit", {art: $("#art").value, von: $("#von").value, bis: $("#bis").value, halb: $("#halb").checked, notiz: $("#notiz").value.trim()}, edit && edit.uuid);
    const failed = queue.find((q) => q.fehler);
    if (failed) { await queueDel(failed.uuid); queue = await queueAll(); return renderAbwesend(failed.fehler, "err", edit); }
    renderAbwesend(edit ? "Änderung gesendet." : "Gesendet.", "ok");
  });
  if (edit) window.scrollTo(0, 0);
}

function editAbsence(a) {
  const dlg = $("#dlg");
  const head = `<h2>${h(a.art)}</h2><p class="muted">${a.von === a.bis ? dayLabel(a.von) : `${dayLabel(a.von)} – ${dayLabel(a.bis)}`} · ${h(a.status)}</p>`;
  if (!a.editable || !a.uuid) {
    const why = a.deleting ? "Wird beim nächsten Abgleich gelöscht." : a.locked === "bewilligt" || a.locked === "abgelehnt"
      ? `Der Antrag ist schon ${a.locked}.` : a.locked ? lockText(a.locked) : "Wird gerade übernommen.";
    dlg.innerHTML = `${head}${msg(why + (a.deleting ? "" : " Änderungen bitte im Büro melden."), "info")}<div class="actions fill"><button class="btn" id="close">Schliessen</button></div>`;
    dlg.showModal();
    $("#close").addEventListener("click", () => dlg.close());
    return;
  }
  dlg.innerHTML = `${head}<div class="actions fill"><button class="btn primary" id="bearb">Ändern</button><button class="btn danger" id="del">${a.request ? "Zurückziehen" : "Löschen"}</button></div>
    <div class="actions fill dlgfoot"><button class="btn" id="close">Schliessen</button></div>`;
  dlg.showModal();
  $("#close").addEventListener("click", () => dlg.close());
  $("#bearb").addEventListener("click", () => { dlg.close(); renderAbwesend("", "ok", {uuid: a.uuid, data: a.data}); });
  $("#del").addEventListener("click", async () => {
    if (!confirm(a.request ? "Antrag zurückziehen?" : "Diese Abwesenheit löschen?")) return;
    dlg.close();
    try { await remove(a.uuid); } catch (e) { alert(e.message); }
    await load().catch(() => {}); renderAbwesend(a.request ? "Antrag wird zurückgezogen." : "Wird gelöscht.", "ok");
  });
}

// ----- Konto -----

function renderKonto(note = "", kind = "ok") {
  setTab("konto");
  if (!ich) return renderLogin();
  const k = mein().konto;
  const sign = (v) => (Number(v) > 0 ? "pos" : Number(v) < 0 ? "neg" : "");
  const body = !k ? `<div class="card"><p class="muted">Noch keine Daten vom Büro. Sie erscheinen nach dem nächsten Abgleich.</p></div>` : `
    ${k.stundenlohn ? "" : `<div class="card"><h2>Überstunden ${h(k.jahr)}</h2><div class="stats">
      <div class="stat"><div class="k">Saldo Jahr</div><div class="v ${sign(k.saldo_jahr)}">${signed(k.saldo_jahr)}</div></div>
      <div class="stat"><div class="k">${h(MONTHS[k.monat.monat - 1])}</div><div class="v ${sign(k.monat.saldo)}">${signed(k.monat.saldo)}</div></div></div>
      <table class="list small"><thead><tr><th>Monat</th><th class="n">Ist</th><th class="n">Soll</th><th class="n">Saldo</th></tr></thead><tbody>${(k.monate || []).filter((m) => m.soll !== null && (Number(m.soll) || Number(m.ist))).map((m) => `<tr><td>${h(MONTHS[m.monat - 1])}</td><td class="n">${hours(m.ist)}</td><td class="n">${hours(m.soll)}</td><td class="n ${sign(m.saldo)}">${signed(m.saldo)}</td></tr>`).join("")}</tbody></table></div>`}
    ${k.stundenlohn ? `<div class="card"><h2>Stunden ${h(MONTHS[k.monat.monat - 1])}</h2><div class="stat"><div class="k">erfasst</div><div class="v">${hours(k.monat.ist)}</div></div></div>` : ""}
    <div class="card"><h2>Ferien ${h(k.jahr)}</h2><div class="stats">
      <div class="stat"><div class="k">Noch frei</div><div class="v">${days(k.ferien.rest)} <span class="u">Tage</span></div></div>
      <div class="stat"><div class="k">Total ${h(k.jahr)}</div><div class="v">${days(Number(k.ferien.anspruch) + Number(k.ferien.vortrag))} <span class="u">Tage</span></div></div></div>
      <table class="list small"><tbody><tr><td>Anspruch ${h(k.jahr)}</td><td class="n">${days(k.ferien.anspruch)}</td></tr><tr><td>Vortrag Vorjahr</td><td class="n">${days(k.ferien.vortrag)}</td></tr>
      <tr><td>Bezogen</td><td class="n">${days(k.ferien.bezogen)}</td></tr><tr><td>Geplant</td><td class="n">${days(k.ferien.geplant)}</td></tr></tbody></table></div>`;
  view().innerHTML = `<h1>Mein Konto</h1><p class="sub">Stand Büro: ${h(stand(mein().stand || shared().stand))}</p>${msg(note, kind)}${body}
    <div class="card"><h2>Passwort ändern</h2><form id="pw">
      <div class="field"><label for="alt">Bisheriges Passwort</label><input id="alt" type="password" autocomplete="current-password" required></div>
      <div class="field"><label for="neu">Neues Passwort (mind. 10 Zeichen)</label><input id="neu" type="password" autocomplete="new-password" minlength="10" required></div>
      <button class="btn block" type="submit">Passwort ändern</button></form></div>
    <button class="btn block" id="logout">Abmelden</button>`;
  $("#logout").addEventListener("click", async () => {
    if (queue.some((q) => !q.fehler) && !confirm("Es sind noch Einträge auf dem Handy, die nicht gesendet wurden. Trotzdem abmelden?")) return;
    try { await call("POST", "/logout"); } catch (e) { /* offline: still forget locally */ }
    await wipe(); ich = null; location.hash = "#/login";
  });
  $("#pw").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const alt = $("#alt").value, neu = $("#neu").value;
    try {
      const k2 = await passwordKey(alt, unb64(ich.wrapped.salt), ich.wrapped.iter);
      const pkcs8 = await crypto.subtle.decrypt({name: "AES-GCM", iv: unb64(ich.wrapped.iv)}, k2, unb64(ich.wrapped.ct));
      const wrapped = await wrapKey(neu, pkcs8);
      await call("POST", "/passwort", {alt: await authKey(alt, ich.benutzer), neu: await authKey(neu, ich.benutzer), wrapped});
      await load(); await rememberKey(neu, wrapped);
      renderKonto("Passwort geändert.");
    } catch (e) { renderKonto(e.message === "The operation failed for an operation-specific reason" ? "Bisheriges Passwort falsch" : e.message, "err"); }
  });
}

// ----- Dokumente -----

async function renderDokumente(note = "") {
  setTab("dokumente");
  if (!ich) return renderLogin();
  view().innerHTML = `<h1>Lohn &amp; Dokumente</h1><p class="muted">Lädt …</p>`;
  let docs = [];
  try { docs = (await call("GET", "/dokumente")).docs; }
  catch (e) { view().innerHTML = `<h1>Lohn &amp; Dokumente</h1>${msg(e instanceof Offline ? "Dokumente brauchen eine Verbindung." : e.message, "info")}`; return; }
  const kinds = {lohn: "Lohnabrechnungen", lohnausweis: "Lohnausweise", zeit: "Arbeitszeitnachweise"};
  const groups = Object.entries(kinds).map(([k, label]) => {
    const list = docs.filter((d) => d.kind === k);
    const name = (d) => {
      const [y, m] = String(d.period || "").split("-");
      return k === "lohnausweis" ? y : m ? `${MONTHS[Number(m) - 1]} ${y}` : d.title;
    };
    return list.length ? `<div class="card"><h2>${label}</h2>${list.map((d) => `<button class="docrow" data-doc="${h(d.doc_id)}" aria-label="${h(d.title)} öffnen"><span>${h(name(d))}</span><span class="open">PDF öffnen <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg></span></button>`).join("")}</div>` : "";
  }).join("");
  view().innerHTML = `<h1>Lohn &amp; Dokumente</h1>${msg(note)}<p class="sub">Verschlüsselt. Nur du kannst sie öffnen.</p>${groups || `<div class="card"><p class="muted">Noch keine Dokumente.</p></div>`}`;
  for (const b of document.querySelectorAll("[data-doc]")) {
    b.addEventListener("click", async () => {
      try {
        const d = (await call("GET", `/dokumente/${encodeURIComponent(b.dataset.doc)}`)).doc;
        const plain = await decryptDoc(ich.person, d);
        const url = URL.createObjectURL(new Blob([plain], {type: "application/pdf"}));
        const a = document.createElement("a");
        a.href = url; a.download = `${d.title}.pdf`; a.target = "_blank"; a.rel = "noopener";
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 60000);
      } catch (e) { renderDokumente(e.name === "OperationError" ? "Dokument lässt sich nicht entschlüsseln. Bitte ab- und wieder anmelden." : e.message); }
    });
  }
}

// ----- Kunden -----

let kundenSuche = "";
let erfassenVorlage = null;     // from a customer card: the form opens with that customer
function renderKunden(offen = "") {
  setTab("kunden");
  if (!ich) return renderLogin();
  const alle = shared().kunden || [];
  view().innerHTML = `<h1>Kunden</h1>
    <div class="field"><label for="kSuche">Suchen</label><input id="kSuche" type="search" autocomplete="off" spellcheck="false"
      enterkeyhint="search" placeholder="Name, Strasse, Ort oder ${h(wort())}" value="${h(kundenSuche)}"></div>
    <div id="kListe"></div>
    ${!alle.length ? `<p class="muted small">Noch keine Kunden vom Büro übertragen (letzter Abgleich: ${h(stand(ich.letzter_abgleich))}).</p>` : ""}`;
  const draw = () => {
    const q = kundenSuche.trim();
    const shown = q ? kundenTreffer(q, 50) : (zuletztKunden().length ? zuletztKunden() : alle.slice(0, 20));
    const more = q ? kundenTreffer(q, 1000).length - shown.length : 0;
    $("#kListe").innerHTML = (!q && alle.length ? `<p class="muted small">${zuletztKunden().length ? "Zuletzt bei dir. " : ""}${alle.length} Kunden: Namen oder Ort tippen.</p>` : "") +
      (shown.length ? shown.map((k) => kundeKarte(k, k.nummer === offen || shown.length === 1)).join("")
        : q ? `<p class="empty">Kein Kunde gefunden. Fehlt einer, sag es dem Büro.</p>` : "") +
      (more > 0 ? `<p class="muted small">… und ${more} weitere: genauer suchen.</p>` : "");
    for (const b of document.querySelectorAll("[data-erfassen]")) {
      b.addEventListener("click", () => {
        const [art, nr, obj] = b.dataset.erfassen.split("|");
        erfassenArt = art;
        erfassenVorlage = {kunde: nr, projekt: obj || ""};
        location.hash = "#/erfassen";
      });
    }
  };
  $("#kSuche").addEventListener("input", (ev) => { kundenSuche = ev.target.value; draw(); });
  draw();
}

function kundeKarte(k, auf) {
  const objekte = objekteVon(k.nummer);
  return `<details class="card kunde"${auf ? " open" : ""}><summary><span class="kname">${h(k.name)}</span>
      <span class="muted small">${h([k.kontakt, k.ort].filter(Boolean).join(" · "))}</span></summary>
    ${adresse(k) ? `<p class="addr">${k.kontakt ? `${h(k.kontakt)}<br>` : ""}${h(k.strasse)}${k.strasse ? "<br>" : ""}${h([k.ort, k.land].filter(Boolean).join(" "))}</p>
      <p><a class="btn small" href="${h(karte(k))}" target="_blank" rel="noopener">Karte öffnen</a></p>` : `<p class="muted small">Keine Adresse im Büro erfasst.</p>`}
    ${objekte.length ? `<h2>${h(wort())}e</h2>${objekte.map((p) => `<div class="item"><div class="what">${h(p.name)}</div><div class="side"><button class="btn small" type="button" data-erfassen="zeit|${h(k.nummer)}|${h(p.nummer)}">Zeit</button></div></div>`).join("")}` : ""}
    <div class="actions fill"><button class="btn small" type="button" data-erfassen="zeit|${h(k.nummer)}|">Zeit erfassen</button>
      <button class="btn small" type="button" data-erfassen="material|${h(k.nummer)}|">Material</button></div></details>`;
}

// ---------- router ----------

const ROUTES = {login: renderLogin, aktivieren: renderActivate, erfassen: () => { const v = erfassenVorlage; erfassenVorlage = null; renderErfassen("", "ok", v); }, woche: renderWoche,
  kunden: () => renderKunden(), abwesend: () => renderAbwesend(), konto: () => renderKonto(), dokumente: () => renderDokumente()};

async function route() {
  const name = (location.hash.replace(/^#\/?/, "") || "erfassen").split("?")[0];
  if (!ich && !["login", "aktivieren"].includes(name)) {
    try { await load(); } catch (e) { /* 401 → login */ }
    if (!ich) { if (location.hash !== "#/login") location.hash = "#/login"; else renderLogin(); return; }
  }
  const start = hoursInApp() ? "erfassen" : "abwesend";
  const allowed = hoursInApp() || !["erfassen", "woche"].includes(name);
  (allowed && ROUTES[name] || ROUTES[start])();
  window.scrollTo(0, 0);
}

window.addEventListener("hashchange", route);
window.addEventListener("online", () => flush().then(route));
window.addEventListener("offline", netBadge);
document.addEventListener("visibilitychange", () => { if (!document.hidden && navigator.onLine) flush(); });
setInterval(() => { if (navigator.onLine && queue.length) flush(); }, 30000);
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/app/sw.js", {scope: "/app/"}).catch(() => {});
queueAll().then((q) => { queue = q; }).catch(() => {}).finally(() => { route(); if (navigator.onLine) flush(); });
