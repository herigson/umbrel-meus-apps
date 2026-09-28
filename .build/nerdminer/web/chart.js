/* Gráfico de área, base zero, série única.
 *
 * Série única = sem legenda: o título do card já nomeia o que está no eixo.
 * Base zero porque a pergunta é de magnitude ("quanto estou produzindo") —
 * uma queda a zero precisa aparecer como queda até o chão, não como um
 * degrau num eixo truncado.
 *
 * null quebra a linha (não estávamos medindo) e 0 desenha até a base
 * (estávamos medindo e a produção era zero). São coisas diferentes.
 */
"use strict";

const NS = "http://www.w3.org/2000/svg";

function el(tag, attrs, parent) {
  const node = document.createElementNS(NS, tag);
  for (const k in attrs) node.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(node);
  return node;
}

function niceCeil(value) {
  if (!(value > 0)) return 1;
  const exp = Math.floor(Math.log10(value));
  const base = Math.pow(10, exp);
  const n = value / base;
  const step = n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10;
  return step * base;
}

const HASH_UNITS = [
  [1e21, "ZH/s"], [1e18, "EH/s"], [1e15, "PH/s"], [1e12, "TH/s"],
  [1e9, "GH/s"], [1e6, "MH/s"], [1e3, "kH/s"],
];

function fmtHashrate(hs) {
  const n = Number(hs) || 0;
  for (const [factor, unit] of HASH_UNITS) {
    if (n >= factor) return [(n / factor).toFixed(2), unit];
  }
  return [n.toFixed(0), "H/s"];
}

function clockOf(ts, range) {
  const d = new Date(ts * 1000);
  if (range === "7d") {
    return d.toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit" });
  }
  return d.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" });
}

/* points: [[ts, value|null], ...] */
function renderChart(svg, points, opts) {
  const o = opts || {};
  const range = o.range || "1h";
  const tip = o.tip;
  svg.textContent = "";
  if (tip) tip.style.opacity = 0;

  const withValue = points.filter((p) => p[1] !== null && p[1] !== undefined);
  if (points.length < 2 || withValue.length === 0) {
    const t = el("text", {
      x: "50%", y: "50%", "text-anchor": "middle",
      fill: "var(--faint)", "font-size": "12"
    }, svg);
    t.textContent = points.length ? "sem medições neste período" : "coletando dados…";
    return;
  }

  const w = svg.clientWidth || 640;
  const h = svg.clientHeight || 220;
  const pad = { top: 18, right: 10, bottom: 24, left: 52 };
  const plotW = Math.max(1, w - pad.left - pad.right);
  const plotH = Math.max(1, h - pad.top - pad.bottom);

  const t0 = points[0][0];
  const t1 = points[points.length - 1][0];
  const span = Math.max(1, t1 - t0);
  const peak = Math.max.apply(null, withValue.map((p) => p[1]));
  const yMax = niceCeil(peak * 1.12) || 1;

  const X = (t) => pad.left + ((t - t0) / span) * plotW;
  const Y = (v) => pad.top + plotH - (v / yMax) * plotH;

  const unit = fmtHashrate(yMax)[1];
  const div = { "TH/s": 1e12, "GH/s": 1e9, "MH/s": 1e6, "kH/s": 1e3, "H/s": 1 }[unit];

  // Unidade uma vez como legenda do eixo; os ticks ficam números puros,
  // senão o rótulo do topo estoura a margem esquerda.
  const cap = el("text", { x: 2, y: 11, fill: "var(--faint)", "font-size": "11" }, svg);
  cap.textContent = unit;

  // Grade recessiva
  for (let i = 0; i <= 3; i++) {
    const v = (yMax / 3) * i;
    const y = Y(v);
    el("line", {
      x1: pad.left, y1: y, x2: w - pad.right, y2: y,
      stroke: i === 0 ? "var(--border-strong)" : "var(--border)", "stroke-width": 1
    }, svg);
    const lab = el("text", {
      x: pad.left - 8, y: y + 4, "text-anchor": "end",
      fill: "var(--faint)", "font-size": "11"
    }, svg);
    lab.style.fontVariantNumeric = "tabular-nums";
    lab.textContent = i === 0 ? "0" : (v / div).toFixed(v / div >= 10 ? 0 : 1);
  }

  for (let i = 0; i <= 3; i++) {
    const t = t0 + (span / 3) * i;
    const lab = el("text", {
      x: X(t), y: h - 7,
      "text-anchor": i === 0 ? "start" : i === 3 ? "end" : "middle",
      fill: "var(--faint)", "font-size": "11"
    }, svg);
    lab.style.fontVariantNumeric = "tabular-nums";
    lab.textContent = clockOf(t, range);
  }

  const defs = el("defs", {}, svg);
  const grad = el("linearGradient",
    { id: "areaFill", x1: "0", y1: "0", x2: "0", y2: "1" }, defs);
  el("stop", { offset: "0", "stop-color": "var(--accent)", "stop-opacity": ".22" }, grad);
  el("stop", { offset: "1", "stop-color": "var(--accent)", "stop-opacity": "0" }, grad);

  // Segmenta em trechos contínuos: null quebra a linha.
  const segments = [];
  let run = [];
  for (const p of points) {
    if (p[1] === null || p[1] === undefined) {
      if (run.length) segments.push(run);
      run = [];
    } else {
      run.push(p);
    }
  }
  if (run.length) segments.push(run);

  for (const seg of segments) {
    const d = seg.map((p, i) => (i ? "L" : "M") + X(p[0]).toFixed(1) + " " +
                                 Y(p[1]).toFixed(1)).join(" ");
    if (seg.length > 1) {
      el("path", {
        d: d + " L" + X(seg[seg.length - 1][0]).toFixed(1) + " " + Y(0).toFixed(1) +
           " L" + X(seg[0][0]).toFixed(1) + " " + Y(0).toFixed(1) + " Z",
        fill: "url(#areaFill)"
      }, svg);
    }
    el("path", {
      d: seg.length > 1 ? d : d + " l0.01 0",
      fill: "none", stroke: "var(--accent)", "stroke-width": 2,
      "stroke-linejoin": "round", "stroke-linecap": "round"
    }, svg);
  }

  // Quedas registradas: marcador + anel na cor da superfície pra destacar.
  for (const ev of (o.events || [])) {
    if (ev.t < t0 || ev.t > t1) continue;
    el("circle", {
      cx: X(ev.t), cy: Y(0), r: 4,
      fill: "var(--warn)", stroke: "var(--surface)", "stroke-width": 2
    }, svg);
  }

  // Camada de hover: obrigatória num gráfico HTML.
  const cross = el("line", {
    y1: pad.top, y2: pad.top + plotH, stroke: "var(--border-strong)",
    "stroke-width": 1, opacity: 0
  }, svg);
  const marker = el("circle", {
    r: 4.5, fill: "var(--accent)", stroke: "var(--surface)",
    "stroke-width": 2, opacity: 0
  }, svg);

  const onMove = (evt) => {
    const box = svg.getBoundingClientRect();
    const cx = (evt.touches ? evt.touches[0].clientX : evt.clientX) - box.left;
    const t = t0 + ((cx - pad.left) / plotW) * span;
    let best = null;
    for (const p of withValue) {
      if (!best || Math.abs(p[0] - t) < Math.abs(best[0] - t)) best = p;
    }
    if (!best) return;
    const px = X(best[0]);
    const py = Y(best[1]);
    cross.setAttribute("x1", px); cross.setAttribute("x2", px);
    cross.setAttribute("opacity", 1);
    marker.setAttribute("cx", px); marker.setAttribute("cy", py);
    marker.setAttribute("opacity", 1);
    if (tip) {
      const [v, u] = fmtHashrate(best[1]);
      tip.innerHTML = "<strong>" + v + " " + u + "</strong><div class='t'>" +
                      clockOf(best[0], range) + "</div>";
      tip.style.left = px + "px";
      tip.style.top = py + "px";
      tip.style.opacity = 1;
    }
  };
  const onLeave = () => {
    cross.setAttribute("opacity", 0);
    marker.setAttribute("opacity", 0);
    if (tip) tip.style.opacity = 0;
  };

  // O gráfico é redesenhado a cada atualização do histórico. Sem remover os
  // listeners do desenho anterior, eles se acumulariam no mesmo elemento —
  // um vazamento lento, mas vazamento. Guardamos os antigos no próprio nó.
  const prev = svg._chartHandlers;
  if (prev) {
    svg.removeEventListener("mousemove", prev.onMove);
    svg.removeEventListener("mouseleave", prev.onLeave);
    svg.removeEventListener("touchmove", prev.onMove, { passive: true });
    svg.removeEventListener("touchend", prev.onLeave);
  }
  svg.addEventListener("mousemove", onMove);
  svg.addEventListener("mouseleave", onLeave);
  svg.addEventListener("touchmove", onMove, { passive: true });
  svg.addEventListener("touchend", onLeave);
  svg._chartHandlers = { onMove, onLeave };
}
