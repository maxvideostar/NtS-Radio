/* NtS-Radio - utilitare comune */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

async function api(method, url, body, raw) {
  const opt = {method, headers: {}};
  if (body !== undefined) {
    if (raw) opt.body = body;
    else { opt.body = JSON.stringify(body); opt.headers["Content-Type"] = "application/json"; }
  }
  const r = await fetch(url, opt);
  if (r.status === 401 && !url.includes("/api/login")) { location.href = "/login?next=" + encodeURIComponent(location.pathname); throw new Error("neautentificat"); }
  let data = null;
  try { data = await r.json(); } catch (e) {}
  if (!r.ok) throw new Error((data && data.error) || ("Eroare " + r.status));
  return data;
}

function toast(msg, err) {
  let t = $("#toast");
  if (!t) { t = document.createElement("div"); t.id = "toast"; document.body.appendChild(t); }
  const d = document.createElement("div");
  if (err) d.className = "err";
  d.textContent = msg;
  t.appendChild(d);
  setTimeout(() => d.remove(), err ? 6000 : 3000);
}
const fail = e => toast(e.message || String(e), true);

function modal(html, onMount) {
  const bg = document.createElement("div");
  bg.className = "modal-bg";
  bg.innerHTML = `<div class="modal">${html}</div>`;
  document.body.appendChild(bg);
  const close = () => bg.remove();
  bg.addEventListener("mousedown", e => { if (e.target === bg) close(); });
  $$("[data-close]", bg).forEach(b => b.onclick = close);
  if (onMount) onMount(bg.firstElementChild, close);
  return close;
}

function confirmBox(text) {
  return new Promise(res => {
    modal(`<h2>Confirmare</h2><p>${esc(text)}</p><div class="actions"><button class="btn" data-close>Anulează</button><button class="btn danger" id="cOk">Da</button></div>`,
      (m, close) => { $("#cOk", m).onclick = () => { close(); res(true); }; $$("[data-close]", m).forEach(b => b.addEventListener("click", () => res(false))); });
  });
}

function copyText(t) {
  if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(t).then(() => toast("Copiat"));
  else { const a = document.createElement("textarea"); a.value = t; document.body.appendChild(a); a.select(); document.execCommand("copy"); a.remove(); toast("Copiat"); }
}

/* ---------- formatare ---------- */
const nf = new Intl.NumberFormat("ro-RO");
const nf1 = new Intl.NumberFormat("ro-RO", {maximumFractionDigits: 1});
const fmtN = n => nf.format(n || 0);
const fmtN1 = n => nf1.format(n || 0);
let TZ = "Europe/Bucharest";
function setTZ(tz) { if (tz) TZ = tz; }
const fmtTime = ts => new Date(ts * 1000).toLocaleTimeString("ro-RO", {hour: "2-digit", minute: "2-digit", timeZone: TZ});
const fmtDate = ts => new Date(ts * 1000).toLocaleDateString("ro-RO", {day: "2-digit", month: "2-digit", year: "numeric", timeZone: TZ});
const fmtDT = ts => fmtDate(ts) + " " + fmtTime(ts);
const fmtDay = ts => new Date(ts * 1000).toLocaleDateString("ro-RO", {weekday: "short", day: "2-digit", month: "short", timeZone: TZ});
function fmtDur(s) {
  s = Math.max(0, Math.round(s || 0));
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), ss = s % 60;
  if (h >= 24) return Math.floor(h / 24) + "z " + (h % 24) + "h";
  if (h) return h + "h " + String(m).padStart(2, "0") + "m";
  if (m) return m + "m " + String(ss).padStart(2, "0") + "s";
  return ss + "s";
}
function fmtBytes(b) {
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0; b = b || 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return nf1.format(b) + " " + u[i];
}
const regionNames = (() => { try { return new Intl.DisplayNames(["ro"], {type: "region"}); } catch (e) { return null; } })();
function flag(cc) {
  if (!cc || cc.length !== 2 || cc === "??") return "🌐";
  return String.fromCodePoint(...[...cc.toUpperCase()].map(c => 127397 + c.charCodeAt(0)));
}
function countryName(cc) {
  if (!cc || cc === "?") return "Necunoscut";
  if (cc === "LAN") return "Rețea locală";
  try { return regionNames ? regionNames.of(cc) : cc; } catch (e) { return cc; }
}
const WD = ["Lun", "Mar", "Mie", "Joi", "Vin", "Sâm", "Dum"];
const WD_FULL = ["Luni", "Marți", "Miercuri", "Joi", "Vineri", "Sâmbătă", "Duminică"];
const modeBadge = m => m === "live" ? `<span class="badge live">LIVE din studio</span>` :
  m === "autodj" ? `<span class="badge autodj">AutoDJ</span>` : `<span class="badge off">Oprit / liniște</span>`;

/* ---------- grafice (SVG, o singură serie, tooltip la hover) ---------- */
function niceMax(v) {
  if (v <= 5) return 5;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}

function chartFrame(el, height) {
  el.innerHTML = "";
  el.classList.add("chart");
  const W = Math.max(280, el.clientWidth || 600), H = height || 220;
  const m = {l: 40, r: 12, t: 10, b: 26};
  const tip = document.createElement("div");
  tip.className = "tip";
  return {W, H, m, iw: W - m.l - m.r, ih: H - m.t - m.b, tip};
}

function yAxis(f, max, fmtY) {
  let s = "";
  for (let i = 0; i <= 4; i++) {
    const v = max * i / 4, y = f.m.t + f.ih - f.ih * i / 4;
    s += `<line class="gridline" x1="${f.m.l}" x2="${f.W - f.m.r}" y1="${y}" y2="${y}"/>` +
      `<text x="${f.m.l - 6}" y="${y + 4}" text-anchor="end">${esc(fmtY(v))}</text>`;
  }
  return s;
}

/* data: [{t, v, label?}] ; opts: fmtX(t), fmtTip(t), fmtY, height, name */
function lineChart(el, data, opts = {}) {
  const f = chartFrame(el, opts.height);
  if (!data.length) { el.innerHTML = `<div class="empty">Nu există încă date pentru această perioadă.</div>`; return; }
  const fmtY = opts.fmtY || (v => fmtN1(v));
  const max = niceMax(Math.max(...data.map(d => d.v), 1));
  const n = data.length;
  const X = i => f.m.l + (n === 1 ? f.iw / 2 : f.iw * i / (n - 1));
  const Y = v => f.m.t + f.ih - f.ih * v / max;
  const pts = data.map((d, i) => `${X(i).toFixed(1)},${Y(d.v).toFixed(1)}`);
  const area = `M${X(0)},${Y(0)} L${pts.join(" L")} L${X(n - 1)},${Y(0)} Z`;
  const fx = opts.fmtX || fmtTime;
  const tw = Math.max(50, ...[0, n - 1].map(i => String(fx(data[i].t)).length * 6.5)) + 16;
  const ticks = Math.min(n, Math.max(2, Math.floor(f.iw / tw)));
  const marks = [];
  const wOf = l => String(l).length * 6.5 + 12;
  const ext = (m, idx, last) => idx === 0 ? [m.x, m.x + m.w] : (last ? [m.x - m.w, m.x] : [m.x - m.w / 2, m.x + m.w / 2]);
  for (let k = 0; k < ticks; k++) {
    const i = Math.round(k * (n - 1) / Math.max(1, ticks - 1));
    const m = {x: X(i), label: fx(data[i].t)};
    m.w = wOf(m.label);
    const isLast = k === ticks - 1;
    while (marks.length) {
      const p = marks[marks.length - 1], pi = marks.length - 1;
      const overlap = p.label === m.label || ext(p, pi, false)[1] > ext(m, marks.length, isLast)[0];
      if (!overlap) break;
      if (isLast && marks.length > 1) marks.pop(); else { m.skip = true; break; }
    }
    if (!m.skip) marks.push(m);
  }
  let xt = marks.map((m, k) => `<text x="${m.x}" y="${f.H - 6}" text-anchor="${k === 0 ? "start" : (k === marks.length - 1 && marks.length > 1 ? "end" : "middle")}">${esc(m.label)}</text>`).join("");
  el.innerHTML = `<svg viewBox="0 0 ${f.W} ${f.H}" height="${f.H}" role="img" aria-label="${esc(opts.name || "grafic")}">
    <g class="axis">${yAxis(f, max, fmtY)}${xt}</g>
    <path d="${area}" fill="var(--series-1-fill)"/>
    <polyline points="${pts.join(" ")}" fill="none" stroke="var(--series-1)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
    <line class="xh" y1="${f.m.t}" y2="${f.m.t + f.ih}" stroke="var(--text-muted)" stroke-dasharray="3 3" opacity="0"/>
    <circle class="dot" r="5" fill="var(--series-1)" stroke="var(--surface-1)" stroke-width="2" opacity="0"/>
    <rect x="${f.m.l}" y="${f.m.t}" width="${f.iw}" height="${f.ih}" fill="transparent"/>
  </svg>`;
  el.appendChild(f.tip);
  const svg = $("svg", el), xh = $(".xh", el), dot = $(".dot", el);
  svg.addEventListener("mousemove", e => {
    const r = svg.getBoundingClientRect(), sx = (e.clientX - r.left) * f.W / r.width;
    const i = Math.max(0, Math.min(n - 1, Math.round((sx - f.m.l) / (n === 1 ? 1 : f.iw / (n - 1)))));
    const d = data[i], x = X(i), y = Y(d.v);
    xh.setAttribute("x1", x); xh.setAttribute("x2", x); xh.setAttribute("opacity", 1);
    dot.setAttribute("cx", x); dot.setAttribute("cy", y); dot.setAttribute("opacity", 1);
    f.tip.innerHTML = `${esc((opts.fmtTip || opts.fmtX || fmtDT)(d.t))}<br><b>${esc(fmtY(d.v))}</b> ${esc(opts.unit || "")}` + (d.extra ? `<br><span class="muted">${esc(d.extra)}</span>` : "");
    f.tip.style.left = (x * r.width / f.W) + "px"; f.tip.style.top = (y * r.height / f.H) + "px"; f.tip.style.opacity = 1;
  });
  svg.addEventListener("mouseleave", () => { xh.setAttribute("opacity", 0); dot.setAttribute("opacity", 0); f.tip.style.opacity = 0; });
}

/* bare verticale: labels[], values[] */
function barChart(el, labels, values, opts = {}) {
  const f = chartFrame(el, opts.height);
  if (!values.length || values.every(v => !v)) { el.innerHTML = `<div class="empty">Nu există încă date.</div>`; return; }
  const fmtY = opts.fmtY || (v => fmtN1(v));
  const max = niceMax(Math.max(...values, 1));
  const n = values.length, slot = f.iw / n, bw = Math.max(2, slot - 2);
  const Y = v => f.m.t + f.ih - f.ih * v / max;
  let bars = "", xt = "";
  const every = Math.ceil(n / Math.max(1, Math.floor(f.iw / 34)));
  values.forEach((v, i) => {
    const x = f.m.l + i * slot + 1, y = Y(v), h = f.m.t + f.ih - y, r = Math.min(4, bw / 2, h);
    bars += h > 0 ? `<path data-i="${i}" d="M${x},${y + h} V${y + r} Q${x},${y} ${x + r},${y} H${x + bw - r} Q${x + bw},${y} ${x + bw},${y + r} V${y + h} Z" fill="var(--series-1)"/>` : "";
    bars += `<rect data-i="${i}" x="${x - 1}" y="${f.m.t}" width="${slot}" height="${f.ih}" fill="transparent"/>`;
    if (i % every === 0) xt += `<text x="${x + bw / 2}" y="${f.H - 6}" text-anchor="middle">${esc(labels[i])}</text>`;
  });
  el.innerHTML = `<svg viewBox="0 0 ${f.W} ${f.H}" height="${f.H}" role="img" aria-label="${esc(opts.name || "grafic")}"><g class="axis">${yAxis(f, max, fmtY)}${xt}</g>${bars}</svg>`;
  el.appendChild(f.tip);
  const svg = $("svg", el);
  svg.addEventListener("mousemove", e => {
    const t = e.target.closest("[data-i]");
    if (!t) { f.tip.style.opacity = 0; return; }
    const i = +t.dataset.i, r = svg.getBoundingClientRect();
    f.tip.innerHTML = `${esc((opts.tipLabels || labels)[i])}<br><b>${esc(fmtY(values[i]))}</b> ${esc(opts.unit || "")}`;
    f.tip.style.left = ((f.m.l + i * slot + slot / 2) * r.width / f.W) + "px";
    f.tip.style.top = (Y(values[i]) * r.height / f.H) + "px";
    f.tip.style.opacity = 1;
    $$("path[data-i]", svg).forEach(p => p.setAttribute("opacity", p.dataset.i == i ? 1 : .55));
  });
  svg.addEventListener("mouseleave", () => { f.tip.style.opacity = 0; $$("path[data-i]", svg).forEach(p => p.setAttribute("opacity", 1)); });
}

/* listă cu bare orizontale: items [{label, value, sub}] */
function barList(el, items, opts = {}) {
  if (!items.length) { el.innerHTML = `<div class="empty">Nu există date.</div>`; return; }
  const max = Math.max(...items.map(i => i.value), 1);
  const fmt = opts.fmt || fmtN;
  el.className = "bar-list";
  el.innerHTML = items.map(i => `<div class="item" title="${esc(i.label)}: ${esc(fmt(i.value))}${i.sub ? " · " + esc(i.sub) : ""}">
     <div class="nowrap" style="overflow:hidden;text-overflow:ellipsis">${i.html || esc(i.label)}</div>
     <div class="track"><div class="fill" style="width:${(100 * i.value / max).toFixed(1)}%"></div></div>
     <div class="num">${esc(fmt(i.value))}${i.sub ? ` <span class="muted small">${esc(i.sub)}</span>` : ""}</div></div>`).join("");
}

function dbToPct(db) { return Math.max(0, Math.min(100, (db + 60) / 60 * 100)); }
