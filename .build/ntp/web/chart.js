/* Graficos do painel NTP, em SVG montado na mao.
 *
 * Sem biblioteca: sao duas formas (linha com area e linha simples) e o
 * painel precisa abrir sem internet. Puxar 80 kB de CDN pra desenhar dois
 * caminhos seria trocar a independencia do app por conveniencia minha.
 *
 * Tudo aqui e prefixado com "ch" ou declarado dentro de funcao. Dois
 * arquivos <script> classicos compartilham o MESMO escopo global, entao um
 * "const" repetido entre eles e SyntaxError e mata o segundo arquivo
 * inteiro - um bug que ja custou caro no painel do NerdMiner.
 */

const CH_NS = "http://www.w3.org/2000/svg";

/* Segundos -> texto em ms com virgula decimal (pt-BR). */
function chMs(seconds, digits) {
  if (seconds === null || seconds === undefined || !isFinite(seconds)) return "—";
  const ms = seconds * 1000;
  const d = digits === undefined ? (Math.abs(ms) < 10 ? 1 : 0) : digits;
  return ms.toLocaleString("pt-BR", { minimumFractionDigits: d, maximumFractionDigits: d });
}

/* Escolhe a unidade pela ordem de grandeza: abaixo de 1 ms, microssegundos
   dizem mais do que "0,0 ms". */
function chOffset(seconds) {
  if (seconds === null || seconds === undefined || !isFinite(seconds)) return ["—", ""];
  const abs = Math.abs(seconds);
  if (abs >= 1) return [seconds.toLocaleString("pt-BR", { maximumFractionDigits: 2 }), "s"];
  if (abs >= 0.001) return [chMs(seconds), "ms"];
  const us = seconds * 1e6;
  return [us.toLocaleString("pt-BR", { maximumFractionDigits: 0 }), "µs"];
}

function chClock(ts, range) {
  const d = new Date(ts * 1000);
  if (range === "7d") {
    return d.toLocaleDateString("pt-BR", { weekday: "short" }).replace(".", "");
  }
  return d.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit", hour12: false });
}

/* Quebra a serie em trechos continuos. Um null significa "nao estavamos
   medindo" - a linha PRECISA interromper ali, senao o desenho liga dois
   instantes distantes como se nada tivesse acontecido no meio. */
function chSegments(points, index) {
  const out = [];
  let run = [];
  for (let i = 0; i < points.length; i++) {
    const v = points[i][index];
    if (v === null || v === undefined) {
      if (run.length) { out.push(run); run = []; }
    } else {
      run.push([i, v]);
    }
  }
  if (run.length) out.push(run);
  return out;
}

function chClear(svg) {
  while (svg.firstChild) svg.removeChild(svg.firstChild);
}

function chEl(name, attrs) {
  const node = document.createElementNS(CH_NS, name);
  for (const key in attrs) node.setAttribute(key, attrs[key]);
  return node;
}

function chPath(seg, toX, toY) {
  return seg.map(function (p, i) {
    return (i ? "L" : "M") + toX(p[0]).toFixed(1) + " " + toY(p[1]).toFixed(1);
  }).join(" ");
}

/* Gráfico do desvio: escala SIMÉTRICA em torno do zero, porque o sinal
   importa — adiantado e atrasado são coisas diferentes e a linha do zero
   tem que ficar no meio, visível. */
function chDrawOffset(svg, points, opts) {
  const W = 600;
  const H = Number(svg.getAttribute("viewBox").split(" ")[3]) || 150;
  const PAD = 4;
  chClear(svg);

  const values = points.map(function (p) { return p[1]; })
                       .filter(function (v) { return v !== null && v !== undefined; });
  if (!values.length) {
    svg.appendChild(chEl("line", { x1: 0, y1: H / 2, x2: W, y2: H / 2,
      stroke: "#444444", "stroke-width": 1, "stroke-dasharray": "4 5",
      "vector-effect": "non-scaling-stroke" }));
    return { range: 0 };
  }

  let range = Math.max.apply(null, values.map(Math.abs));
  if (!isFinite(range) || range <= 0) range = 0.001;
  range = range * 1.15;

  const n = Math.max(points.length - 1, 1);
  const toX = function (i) { return (i / n) * W; };
  const toY = function (v) { return H / 2 - (v / range) * (H / 2 - PAD); };

  // Linhas de grade: topo, zero e base. Só o zero é tracejado — ele é
  // referência, as outras são só escala.
  if (opts && opts.grid) {
    svg.appendChild(chEl("line", { x1: 0, y1: PAD, x2: W, y2: PAD,
      stroke: "#383838", "stroke-width": 1, "vector-effect": "non-scaling-stroke" }));
    svg.appendChild(chEl("line", { x1: 0, y1: H - PAD, x2: W, y2: H - PAD,
      stroke: "#383838", "stroke-width": 1, "vector-effect": "non-scaling-stroke" }));
  }
  svg.appendChild(chEl("line", { x1: 0, y1: H / 2, x2: W, y2: H / 2,
    stroke: "#444444", "stroke-width": 1, "stroke-dasharray": "4 5",
    "vector-effect": "non-scaling-stroke" }));

  const segs = chSegments(points, 1);
  for (const seg of segs) {
    const d = chPath(seg, toX, toY);
    if (seg.length > 1) {
      const first = toX(seg[0][0]).toFixed(1);
      const last = toX(seg[seg.length - 1][0]).toFixed(1);
      svg.appendChild(chEl("path", {
        d: d + " L" + last + " " + (H / 2) + " L" + first + " " + (H / 2) + " Z",
        fill: "rgba(255,138,61,0.18)" }));
      svg.appendChild(chEl("path", { d: d, fill: "none", stroke: "#FF8A3D",
        "stroke-width": 2, "stroke-linejoin": "round",
        "vector-effect": "non-scaling-stroke" }));
    } else {
      // Um ponto solto não desenha linha nenhuma. Sem este círculo, uma
      // medição isolada entre duas falhas sumiria do gráfico.
      svg.appendChild(chEl("circle", { cx: toX(seg[0][0]).toFixed(1),
        cy: toY(seg[0][1]).toFixed(1), r: 2, fill: "#FF8A3D" }));
    }
  }
  return { range: range };
}

/* Ida e volta: escala a partir do ZERO. É uma magnitude — um atraso de
   13 ms desenhado como se o eixo começasse em 12 ms pareceria enorme. */
function chDrawRtt(svg, points) {
  const W = 600;
  const H = Number(svg.getAttribute("viewBox").split(" ")[3]) || 120;
  const PAD = 6;
  chClear(svg);

  const values = points.map(function (p) { return p[2]; })
                       .filter(function (v) { return v !== null && v !== undefined; });
  if (!values.length) return { max: 0 };

  let top = Math.max.apply(null, values) * 1.2;
  if (!isFinite(top) || top <= 0) top = 0.001;

  const n = Math.max(points.length - 1, 1);
  const toX = function (i) { return (i / n) * W; };
  const toY = function (v) { return H - PAD - (v / top) * (H - 2 * PAD); };

  for (const seg of chSegments(points, 2)) {
    const d = chPath(seg, toX, toY);
    if (seg.length > 1) {
      svg.appendChild(chEl("path", { d: d, fill: "none", stroke: "#FF8A3D",
        "stroke-width": 2, "stroke-linejoin": "round",
        "vector-effect": "non-scaling-stroke" }));
    } else {
      svg.appendChild(chEl("circle", { cx: toX(seg[0][0]).toFixed(1),
        cy: toY(seg[0][1]).toFixed(1), r: 2, fill: "#FF8A3D" }));
    }
  }
  return { max: top };
}

/* Quatro marcas de tempo igualmente espaçadas, como no design. */
function chTicks(points, range, count) {
  const total = count || 4;
  if (!points.length) return [];
  const out = [];
  for (let k = 0; k < total; k++) {
    const i = Math.round((k / (total - 1)) * (points.length - 1));
    out.push(chClock(points[i][0], range));
  }
  return out;
}
