/* NtS-Radio – egalizator live peste sigla netstreaming.eu (Web Audio API)
   Barele sunt desenate exact peste locul lor din jingle-logo-bg.png (coordonate pe 800x800). */
const NtsViz = (() => {
  const NB = 17, BW = 12, PITCH = 21.4, X0 = 234, BOTTOM = 764, HMAX = 74, HMIN = 6;
  const IOS = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  let ctx = null, an = null, data = null, canvas = null, raf = 0, visible = false;
  let ok = false, failed = IOS || !(window.AudioContext || window.webkitAudioContext);
  let silentSince = 0, onState = () => {}, el = null;
  const levels = new Float32Array(NB), peaks = new Float32Array(NB);
  let bands = null;

  function makeBands(sr, n) {      // benzi logaritmice 50 Hz – 14 kHz
    const out = [], lo = Math.log(50), hi = Math.log(14000);
    for (let i = 0; i <= NB; i++) out.push(Math.min(n - 1, Math.max(1, Math.round(Math.exp(lo + (hi - lo) * i / NB) / (sr / 2) * n))));
    return out;
  }

  function attach(audio) {
    if (failed || ctx) { if (ctx && ctx.state === "suspended") ctx.resume(); return; }
    el = audio;
    try {
      ctx = new (window.AudioContext || window.webkitAudioContext)();
      const src = ctx.createMediaElementSource(audio);
      an = ctx.createAnalyser();
      an.fftSize = 2048; an.smoothingTimeConstant = 0.72; an.minDecibels = -85; an.maxDecibels = -20;
      src.connect(an); an.connect(ctx.destination);
      data = new Uint8Array(an.frequencyBinCount);
      bands = makeBands(ctx.sampleRate, an.frequencyBinCount);
      ctx.resume();
      silentSince = performance.now();
      loop();
    } catch (e) { failed = true; onState(); }
  }

  function setCanvas(c) { canvas = c; }
  function show(v) { visible = v; if (canvas) canvas.style.opacity = v ? "1" : "0"; }

  function draw() {
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1, w = canvas.clientWidth, h = canvas.clientHeight;
    if (!w) return;
    if (canvas.width !== Math.round(w * dpr)) { canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr); }
    const g = canvas.getContext("2d"), k = canvas.width / 800;
    g.clearRect(0, 0, canvas.width, canvas.height);
    const grad = g.createLinearGradient(0, (BOTTOM - HMAX) * k, 0, BOTTOM * k);
    grad.addColorStop(0, "rgb(130,185,255)"); grad.addColorStop(1, "rgb(45,110,205)");
    for (let i = 0; i < NB; i++) {
      const bh = (HMIN + (HMAX - HMIN) * levels[i]) * k, x = (X0 + i * PITCH - BW / 2) * k, bw = BW * k, y = BOTTOM * k - bh;
      g.shadowColor = "rgba(70,140,255,.75)"; g.shadowBlur = 10 * k;
      g.fillStyle = grad;
      g.beginPath();
      if (g.roundRect) g.roundRect(x, y, bw, bh, 5 * k); else g.rect(x, y, bw, bh);
      g.fill();
      // vârf care cade lent
      const py = BOTTOM * k - (HMIN + (HMAX - HMIN) * peaks[i]) * k - 5 * k;
      g.shadowBlur = 0; g.fillStyle = "rgba(190,220,255,.85)";
      g.fillRect(x, py, bw, 2.5 * k);
    }
  }

  function loop() {
    raf = requestAnimationFrame(loop);
    an.getByteFrequencyData(data);
    let total = 0;
    for (let i = 0; i < NB; i++) {
      let m = 0;
      for (let j = bands[i]; j <= Math.max(bands[i], bands[i + 1] - 1); j++) m = Math.max(m, data[j]);
      total += m;
      const v = Math.pow(m / 255, 1.4) * (0.85 + 0.3 * i / NB);      // puțin mai mult pe înalte
      levels[i] = v > levels[i] ? levels[i] + (v - levels[i]) * 0.6 : levels[i] * 0.88;
      peaks[i] = Math.max(levels[i], peaks[i] - 0.012);
    }
    // dacă analiza dă doar zero deși se aude (stream fără CORS) -> renunțăm, rămâne PNG-ul animat
    const now = performance.now();
    const flowing = el && !el.paused && el.readyState >= 3;
    if (total > 0 || !flowing) { silentSince = now; if (total > 0 && !ok) { ok = true; onState(); } }
    else if (!ok && now - silentSince > 4000) { failed = true; cancelAnimationFrame(raf); onState(); return; }
    if (visible) draw();
  }

  return {
    attach, setCanvas, show,
    get active() { return ok && !failed; },
    set onchange(f) { onState = f; }
  };
})();
