// ---------- Beleg scannen (phone): find the sheet, let the person move the corners, flatten, upload ----------
// Everything happens on the phone: no photo leaves it before «Hochladen», and only the flattened pages are sent.
(function () {
  "use strict";
  const root = document.getElementById("scanner");
  if (!root) return;
  const $ = (s) => root.querySelector(s);
  const csrf = JSON.parse(document.body.getAttribute("hx-headers") || "{}")["X-CSRF"];
  const MAX_SRC = 3000;      // longest side of the photo we work on
  const MAX_OUT = 2200;      // longest side of a flattened page (≈ 260 dpi on A4)
  const DETECT = 480;        // longest side for finding the sheet

  const pages = [];          // {src: canvas, quad: [[x,y]×4], rot: 0..3, filter, out: canvas}
  let current = -1;          // index of the page in the editor / preview
  let editing = null;        // {src, quad, auto} while cropping

  const show = (id) => ["#scStart", "#scCrop", "#scPages"].forEach(s => { $(s).hidden = s !== id; });
  const busy = (text) => { $("#scBusy").hidden = !text; if (text) $("#scBusyText").textContent = text; };
  const error = (text) => { $("#scError").innerHTML = text ? '<div class="alert error">' + text.replace(/</g, "&lt;") + "</div>" : ""; };
  const art = () => (root.querySelector("input[name=sc-art]:checked") || {}).value || "";
  const tick = () => new Promise(r => setTimeout(r, 30));

  // ---------- load a photo (orientation from EXIF is applied by the browser) ----------
  async function load(file) {
    const url = URL.createObjectURL(file);
    try {
      const img = await new Promise((ok, fail) => { const i = new Image(); i.onload = () => ok(i); i.onerror = fail; i.src = url; });
      const s = Math.min(1, MAX_SRC / Math.max(img.naturalWidth, img.naturalHeight));
      const c = document.createElement("canvas");
      c.width = Math.round(img.naturalWidth * s); c.height = Math.round(img.naturalHeight * s);
      c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
      return c;
    } finally { URL.revokeObjectURL(url); }
  }

  // ---------- find the sheet: the largest even area (bright or dark) whose outline is a quadrilateral ----------
  function detect(src) {
    const s = Math.min(1, DETECT / Math.max(src.width, src.height));
    const w = Math.max(8, Math.round(src.width * s)), h = Math.max(8, Math.round(src.height * s));
    const c = document.createElement("canvas"); c.width = w; c.height = h;
    const ctx = c.getContext("2d", { willReadFrequently: true });
    ctx.drawImage(src, 0, 0, w, h);
    const px = ctx.getImageData(0, 0, w, h).data;
    let g = new Float32Array(w * h);
    for (let i = 0; i < w * h; i++) g[i] = 0.299 * px[i * 4] + 0.587 * px[i * 4 + 1] + 0.114 * px[i * 4 + 2];
    g = blur(blur(g, w, h, 2), w, h, 2);
    const t = otsu(g);
    let best = null;
    for (const bright of [true, false]) {
      const mask = new Uint8Array(w * h);
      for (let i = 0; i < w * h; i++) mask[i] = (g[i] > t) === bright ? 1 : 0;
      const comp = largest(mask, w, h);
      if (!comp || comp.area < w * h * 0.08 || comp.area > w * h * 0.985) continue;
      const hull = convexHull(comp.edge);
      if (hull.length < 4) continue;
      const quad = reduce(hull, 4);
      const qa = area(quad);
      const fill = comp.area / Math.max(qa, 1);
      const touches = quad.filter(p => p[0] <= 1 || p[1] <= 1 || p[0] >= w - 2 || p[1] >= h - 2).length;
      const score = fill - touches * 0.04 + (qa / (w * h) > 0.2 ? 0.05 : 0);
      if (fill > 0.82 && (!best || score > best.score)) best = { score, quad };
    }
    if (!best) return null;
    // the blur pulls the outline a little inwards: push each corner out by two pixels of the small image
    const cx = best.quad.reduce((a, p) => a + p[0], 0) / 4, cy = best.quad.reduce((a, p) => a + p[1], 0) / 4;
    const out = best.quad.map(([x, y]) => {
      const d = Math.hypot(x - cx, y - cy) || 1;
      return [Math.max(0, Math.min(w, x + (x - cx) / d * 2)), Math.max(0, Math.min(h, y + (y - cy) / d * 2))];
    });
    return order(out.map(p => [p[0] / s, p[1] / s]));
  }
  function blur(a, w, h, r) {          // box blur, horizontal then vertical
    const tmp = new Float32Array(w * h), out = new Float32Array(w * h);
    for (let y = 0; y < h; y++) {
      let acc = 0, n = 0;
      for (let x = -r; x < w; x++) {
        if (x + r < w) { acc += a[y * w + x + r]; n++; }
        if (x - r - 1 >= 0) { acc -= a[y * w + x - r - 1]; n--; }
        if (x >= 0) tmp[y * w + x] = acc / n;
      }
    }
    for (let x = 0; x < w; x++) {
      let acc = 0, n = 0;
      for (let y = -r; y < h; y++) {
        if (y + r < h) { acc += tmp[(y + r) * w + x]; n++; }
        if (y - r - 1 >= 0) { acc -= tmp[(y - r - 1) * w + x]; n--; }
        if (y >= 0) out[y * w + x] = acc / n;
      }
    }
    return out;
  }
  function otsu(g) {
    const hist = new Float64Array(256);
    for (const v of g) hist[Math.max(0, Math.min(255, v | 0))]++;
    let sum = 0; for (let i = 0; i < 256; i++) sum += i * hist[i];
    let wB = 0, sumB = 0, best = 0, t = 128;
    for (let i = 0; i < 256; i++) {
      wB += hist[i]; if (!wB) continue;
      const wF = g.length - wB; if (!wF) break;
      sumB += i * hist[i];
      const mB = sumB / wB, mF = (sum - sumB) / wF, between = wB * wF * (mB - mF) * (mB - mF);
      if (between > best) { best = between; t = i; }
    }
    return t;
  }
  function largest(mask, w, h) {       // 4-connected components; the biggest with its outline (row ends)
    const label = new Int32Array(w * h), stack = new Int32Array(w * h);
    let bestArea = 0, bestId = 0, id = 0;
    for (let i = 0; i < w * h; i++) {
      if (!mask[i] || label[i]) continue;
      id++; let n = 0, top = 0; stack[top++] = i; label[i] = id;
      while (top) {
        const p = stack[--top]; n++;
        const x = p % w, y = (p - x) / w;
        if (x > 0 && mask[p - 1] && !label[p - 1]) { label[p - 1] = id; stack[top++] = p - 1; }
        if (x < w - 1 && mask[p + 1] && !label[p + 1]) { label[p + 1] = id; stack[top++] = p + 1; }
        if (y > 0 && mask[p - w] && !label[p - w]) { label[p - w] = id; stack[top++] = p - w; }
        if (y < h - 1 && mask[p + w] && !label[p + w]) { label[p + w] = id; stack[top++] = p + w; }
      }
      if (n > bestArea) { bestArea = n; bestId = id; }
    }
    if (!bestId) return null;
    const edge = [];
    for (let y = 0; y < h; y++) {
      let lo = -1, hi = -1;
      for (let x = 0; x < w; x++) if (label[y * w + x] === bestId) { if (lo < 0) lo = x; hi = x; }
      if (lo >= 0) { edge.push([lo, y]); if (hi !== lo) edge.push([hi, y]); }
    }
    return { area: bestArea, edge };
  }
  function convexHull(points) {        // monotone chain
    const p = points.slice().sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    const cross = (o, a, b) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
    const lower = [], upper = [];
    for (const q of p) { while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], q) <= 0) lower.pop(); lower.push(q); }
    for (let i = p.length - 1; i >= 0; i--) { const q = p[i]; while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], q) <= 0) upper.pop(); upper.push(q); }
    return lower.slice(0, -1).concat(upper.slice(0, -1));
  }
  function area(poly) {
    let a = 0;
    for (let i = 0; i < poly.length; i++) { const p = poly[i], q = poly[(i + 1) % poly.length]; a += p[0] * q[1] - q[0] * p[1]; }
    return Math.abs(a) / 2;
  }
  function reduce(hull, k) {           // drop the vertex that costs the least area until k are left
    const poly = hull.slice();
    while (poly.length > k) {
      let best = 0, cost = Infinity;
      for (let i = 0; i < poly.length; i++) {
        const a = poly[(i - 1 + poly.length) % poly.length], b = poly[i], c = poly[(i + 1) % poly.length];
        const t = Math.abs((b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1])) / 2;
        if (t < cost) { cost = t; best = i; }
      }
      poly.splice(best, 1);
    }
    return poly;
  }
  function order(q) {                  // top-left, top-right, bottom-right, bottom-left
    const cx = q.reduce((s, p) => s + p[0], 0) / 4, cy = q.reduce((s, p) => s + p[1], 0) / 4;
    const sorted = q.slice().sort((a, b) => Math.atan2(a[1] - cy, a[0] - cx) - Math.atan2(b[1] - cy, b[0] - cx));
    let start = 0;
    sorted.forEach((p, i) => { if (p[0] + p[1] < sorted[start][0] + sorted[start][1]) start = i; });
    return sorted.slice(start).concat(sorted.slice(0, start));
  }
  const inset = (src, f) => [[src.width * f, src.height * f], [src.width * (1 - f), src.height * f],
                              [src.width * (1 - f), src.height * (1 - f)], [src.width * f, src.height * (1 - f)]];

  // ---------- crop editor ----------
  const photo = $("#scPhoto"), stage = $("#scStage"), poly = $("#scPoly"), frame = $("#scFrame"), loupe = $("#scLoupe");
  let view = 1;                        // displayed px per source px
  function openEditor(src, quad, auto) {
    editing = { src, quad: quad.map(p => p.slice()), auto };
    show("#scCrop");
    const maxW = stage.clientWidth || (innerWidth - 32), maxH = Math.max(260, innerHeight - 290);
    view = Math.min(maxW / src.width, maxH / src.height);
    photo.width = Math.round(src.width * view); photo.height = Math.round(src.height * view);
    photo.getContext("2d").drawImage(src, 0, 0, photo.width, photo.height);
    stage.style.width = photo.width + "px"; stage.style.height = photo.height + "px";
    frame.setAttribute("viewBox", `0 0 ${photo.width} ${photo.height}`);
    $("#scFound").textContent = auto ? "Ränder gefunden — Ecken bei Bedarf verschieben." : "Keine Ränder gefunden — Ecken auf die Belegecken ziehen.";
    draw();
  }
  function draw() {
    const q = editing.quad;
    poly.setAttribute("points", q.map(p => (p[0] * view).toFixed(1) + "," + (p[1] * view).toFixed(1)).join(" "));
    root.querySelectorAll(".sc-h").forEach(h => {
      const p = q[+h.dataset.i];
      h.style.left = (p[0] * view) + "px"; h.style.top = (p[1] * view) + "px";
    });
  }
  function showLoupe(p) {
    const z = 2.5, r = loupe.width / 2;
    const ctx = loupe.getContext("2d");
    ctx.clearRect(0, 0, loupe.width, loupe.height);
    ctx.save(); ctx.beginPath(); ctx.arc(r, r, r, 0, Math.PI * 2); ctx.clip();
    const win = loupe.width / (z * view);          // source pixels shown in the loupe
    ctx.fillStyle = "#000"; ctx.fillRect(0, 0, loupe.width, loupe.height);
    ctx.drawImage(editing.src, p[0] - win / 2, p[1] - win / 2, win, win, 0, 0, loupe.width, loupe.height);
    ctx.restore();
    ctx.strokeStyle = "#ff5c4d"; ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(r - 10, r); ctx.lineTo(r + 10, r); ctx.moveTo(r, r - 10); ctx.lineTo(r, r + 10); ctx.stroke();
    const left = p[0] * view < photo.width / 2;
    loupe.style.left = left ? "" : "6px"; loupe.style.right = left ? "6px" : "";
    loupe.hidden = false;
  }
  root.querySelectorAll(".sc-h").forEach(h => {
    h.addEventListener("pointerdown", (e) => {
      e.preventDefault(); h.setPointerCapture(e.pointerId);
      const i = +h.dataset.i, box = photo.getBoundingClientRect();
      const move = (ev) => {
        const x = Math.max(0, Math.min(photo.width, ev.clientX - box.left)) / view;
        const y = Math.max(0, Math.min(photo.height, ev.clientY - box.top)) / view;
        editing.quad[i] = [x, y]; draw(); showLoupe(editing.quad[i]);
      };
      const up = () => { h.removeEventListener("pointermove", move); h.removeEventListener("pointerup", up); h.removeEventListener("pointercancel", up); loupe.hidden = true; };
      h.addEventListener("pointermove", move); h.addEventListener("pointerup", up); h.addEventListener("pointercancel", up);
    });
  });
  $("#scAuto").onclick = () => { const q = detect(editing.src); editing.quad = q || inset(editing.src, 0.04); editing.auto = !!q; openEditor(editing.src, editing.quad, !!q); };
  $("#scFull").onclick = () => { editing.quad = inset(editing.src, 0); draw(); };
  $("#scDiscard").onclick = () => { editing = null; show(pages.length ? "#scPages" : "#scStart"); if (pages.length) preview(); };

  // ---------- flatten: perspective transform (homography from the output rectangle to the quad) ----------
  function homography(from, to) {     // solves the 8 unknowns of H with to = H·from
    const A = [], b = [];
    for (let i = 0; i < 4; i++) {
      const [x, y] = from[i], [u, v] = to[i];
      A.push([x, y, 1, 0, 0, 0, -u * x, -u * y]); b.push(u);
      A.push([0, 0, 0, x, y, 1, -v * x, -v * y]); b.push(v);
    }
    for (let c = 0; c < 8; c++) {      // Gauss with partial pivoting
      let p = c; for (let r = c + 1; r < 8; r++) if (Math.abs(A[r][c]) > Math.abs(A[p][c])) p = r;
      [A[c], A[p]] = [A[p], A[c]]; [b[c], b[p]] = [b[p], b[c]];
      for (let r = 0; r < 8; r++) {
        if (r === c) continue;
        const f = A[r][c] / A[c][c];
        for (let k = c; k < 8; k++) A[r][k] -= f * A[c][k];
        b[r] -= f * b[c];
      }
    }
    return b.map((v, i) => v / A[i][i]).concat(1);
  }
  const dist = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1]);
  function flatten(src, quad) {
    let w = Math.max(dist(quad[0], quad[1]), dist(quad[3], quad[2]));
    let h = Math.max(dist(quad[0], quad[3]), dist(quad[1], quad[2]));
    const s = Math.min(1, MAX_OUT / Math.max(w, h));
    w = Math.max(1, Math.round(w * s)); h = Math.max(1, Math.round(h * s));
    const H = homography([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], quad);
    const sctx = src.getContext("2d", { willReadFrequently: true });
    const sd = sctx.getImageData(0, 0, src.width, src.height).data, sw = src.width, sh = src.height;
    const out = document.createElement("canvas"); out.width = w; out.height = h;
    const octx = out.getContext("2d");
    const od = octx.createImageData(w, h), d = od.data;
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        const z = H[6] * x + H[7] * y + H[8];
        let u = (H[0] * x + H[1] * y + H[2]) / z, v = (H[3] * x + H[4] * y + H[5]) / z;
        u = Math.max(0, Math.min(sw - 1.001, u)); v = Math.max(0, Math.min(sh - 1.001, v));
        const x0 = u | 0, y0 = v | 0, fx = u - x0, fy = v - y0;
        const i00 = (y0 * sw + x0) * 4, i10 = i00 + 4, i01 = i00 + sw * 4, i11 = i01 + 4;
        const o = (y * w + x) * 4;
        for (let k = 0; k < 3; k++) {
          d[o + k] = (sd[i00 + k] * (1 - fx) + sd[i10 + k] * fx) * (1 - fy) + (sd[i01 + k] * (1 - fx) + sd[i11 + k] * fx) * fy;
        }
        d[o + 3] = 255;
      }
    }
    octx.putImageData(od, 0, 0);
    return out;
  }
  function documentLook(c) {           // even out the light (divide by the blurred background), then contrast
    const w = c.width, h = c.height, ctx = c.getContext("2d", { willReadFrequently: true });
    const img = ctx.getImageData(0, 0, w, h), d = img.data;
    const f = 8, bw = Math.max(1, Math.ceil(w / f)), bh = Math.max(1, Math.ceil(h / f));
    let small = new Float32Array(bw * bh);
    for (let y = 0; y < bh; y++) for (let x = 0; x < bw; x++) {
      let m = 0;                        // brightest pixel of the block ≈ paper
      for (let yy = y * f; yy < Math.min(h, y * f + f); yy += 2) for (let xx = x * f; xx < Math.min(w, x * f + f); xx += 2) {
        const i = (yy * w + xx) * 4; const g = 0.299 * d[i] + 0.587 * d[i + 1] + 0.114 * d[i + 2]; if (g > m) m = g;
      }
      small[y * bw + x] = m;
    }
    small = blur(blur(small, bw, bh, 3), bw, bh, 3);
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
      const i = (y * w + x) * 4;
      const g = 0.299 * d[i] + 0.587 * d[i + 1] + 0.114 * d[i + 2];
      const bg = Math.max(40, small[Math.min(bh - 1, (y / f) | 0) * bw + Math.min(bw - 1, (x / f) | 0)]);
      let v = g / bg * 255;
      v = (v - 40) * 255 / 200;         // a little more contrast: paper white, ink dark
      v = v < 0 ? 0 : v > 255 ? 255 : v;
      d[i] = d[i + 1] = d[i + 2] = v;
    }
    ctx.putImageData(img, 0, 0);
    return c;
  }
  function rotate(c, quarter) {
    if (!quarter) return c;
    const out = document.createElement("canvas");
    out.width = quarter % 2 ? c.height : c.width; out.height = quarter % 2 ? c.width : c.height;
    const ctx = out.getContext("2d");
    ctx.translate(out.width / 2, out.height / 2); ctx.rotate(quarter * Math.PI / 2);
    ctx.drawImage(c, -c.width / 2, -c.height / 2);
    return out;
  }
  async function render(page) {
    busy("Bereite die Seite vor …"); await tick();
    try {
      let c = flatten(page.src, page.quad);
      if (page.filter === "dokument") c = documentLook(c);
      page.out = rotate(c, page.rot);
    } finally { busy(""); }
  }

  // ---------- pages ----------
  function preview() {
    const page = pages[current];
    if (!page) { show("#scStart"); return; }
    const out = $("#scOut");
    out.width = page.out.width; out.height = page.out.height;
    out.getContext("2d").drawImage(page.out, 0, 0);
    root.querySelectorAll("input[name=sc-filter]").forEach(r => { r.checked = r.value === page.filter; });
    const strip = $("#scStrip");
    strip.innerHTML = "";
    pages.forEach((p, i) => {
      const b = document.createElement("button"); b.type = "button"; b.className = "sc-thumb" + (i === current ? " on" : "");
      b.setAttribute("aria-label", "Seite " + (i + 1));
      const t = document.createElement("canvas"); const s = 90 / Math.max(p.out.width, p.out.height);
      t.width = Math.round(p.out.width * s); t.height = Math.round(p.out.height * s);
      t.getContext("2d").drawImage(p.out, 0, 0, t.width, t.height);
      const n = document.createElement("span"); n.textContent = i + 1;
      b.append(t, n); b.onclick = () => { current = i; preview(); };
      strip.appendChild(b);
    });
    $("#scUpload").textContent = pages.length > 1 ? `Hochladen (${pages.length} Seiten)` : "Hochladen";
    show("#scPages");
  }
  $("#scApply").onclick = async () => {
    const e = editing; if (!e) return;
    if (current >= 0 && pages[current] && pages[current].src === e.src) {
      pages[current].quad = e.quad;
    } else {
      pages.push({ src: e.src, quad: e.quad, rot: 0, filter: "dokument", out: null });
      current = pages.length - 1;
    }
    editing = null;
    await render(pages[current]);
    preview();
  };
  root.querySelectorAll("input[name=sc-filter]").forEach(r => r.addEventListener("change", async () => {
    const p = pages[current]; if (!p) return; p.filter = r.value; await render(p); preview();
  }));
  $("#scRotate").onclick = async () => { const p = pages[current]; if (!p) return; p.rot = (p.rot + 1) % 4; await render(p); preview(); };
  $("#scRecrop").onclick = () => { const p = pages[current]; if (p) openEditor(p.src, p.quad, true); };
  $("#scDelete").onclick = () => { pages.splice(current, 1); current = Math.min(current, pages.length - 1); preview(); };

  async function takePhoto(file) {
    if (!file) return;
    error("");
    busy("Suche die Ränder …"); await tick();
    try {
      const src = await load(file);
      const quad = detect(src);
      current = -1;
      openEditor(src, quad || inset(src, 0.04), !!quad);
    } catch (e) { error("Das Bild lässt sich nicht öffnen."); show(pages.length ? "#scPages" : "#scStart"); }
    finally { busy(""); }
  }
  $("#scCamera").addEventListener("change", (e) => { takePhoto(e.target.files[0]); e.target.value = ""; });
  $("#scMore").addEventListener("change", (e) => { takePhoto(e.target.files[0]); e.target.value = ""; });
  $("#scFile").addEventListener("change", async (e) => {
    const file = e.target.files[0]; e.target.value = "";
    if (!file) return;
    if (file.type === "application/pdf" || /\.pdf$/i.test(file.name)) {      // a PDF goes up as it is
      const body = new FormData(); body.append("datei", file, file.name); body.append("art", art());
      return upload(body);
    }
    takePhoto(file);
  });

  // ---------- upload ----------
  let leaving = false;
  const jpeg = (c) => new Promise(ok => c.toBlob(ok, "image/jpeg", 0.82));
  async function upload(body) {
    error(""); busy("Lade hoch und lese den Beleg …");
    try {
      const r = await fetch("/m/scannen", { method: "POST", body, headers: { "X-CSRF": csrf } });
      let d = {};
      try { d = await r.json(); } catch (e) { d = { ok: false, fehler: (await r.text()).slice(0, 200) || ("Fehler " + r.status) }; }
      if (!d.ok) { error(d.fehler || "Hochladen fehlgeschlagen"); return; }
      leaving = true;
      location.href = d.url;
    } catch (e) { error("Keine Verbindung zu aeradex — bitte nochmals versuchen."); }
    finally { busy(""); }
  }
  $("#scUpload").onclick = async () => {
    if (!pages.length) return;
    busy("Bereite die Seiten vor …"); await tick();
    const body = new FormData();
    for (let i = 0; i < pages.length; i++) body.append("seite", await jpeg(pages[i].out), `seite-${i + 1}.jpg`);
    body.append("art", art());
    upload(body);
  };
  window.addEventListener("beforeunload", (e) => { if (pages.length && !leaving) { e.preventDefault(); e.returnValue = ""; } });
})();
