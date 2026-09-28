/* NerdMiner - roteador, polling e render.
 *
 * Nada de framework. Os timers são recriados, nunca empilhados, e todo
 * listener é registrado uma vez só na subida — o painel fica aberto por dias
 * e não pode crescer com o tempo.
 */
"use strict";

const $ = (id) => document.getElementById(id);

const STATUS = {
  mining:      { dot: "ok",   head: "Minerando" },
  target_down: { dot: "bad",  head: "Sem trabalho" },
  miner_down:  { dot: "bad",  head: "Miner sem responder" },
  paused:      { dot: "warn", head: "Pausado" },
  stopped:     { dot: "warn", head: "Parado" },
  needs_setup: { dot: "warn", head: "Configuração pendente" },
};

let stats = null;
let config = null;
let formBaseline = null;
let range = "1h";
let statsTimer = null;
let histTimer = null;
let offline = false;

/* ---------- formatadores ---------- */

const nf = new Intl.NumberFormat("pt-BR");

/* Reaproveita o formatador do chart.js, que é carregado antes. Ter uma
   segunda cópia aqui já custou caro: as duas declaravam `const HASH_UNITS`
   no mesmo escopo global, e a redeclaração derrubava este arquivo inteiro
   com SyntaxError — a página carregava e nada buscava dados. */
const fmtHash = fmtHashrate;

function fmtBig(n) {
  const v = Number(n);
  if (!isFinite(v) || v <= 0) return "—";
  if (v >= 1e12) return (v / 1e12).toFixed(2) + " T";
  if (v >= 1e9) return (v / 1e9).toFixed(2) + " G";
  if (v >= 1e6) return (v / 1e6).toFixed(2) + " M";
  if (v >= 1e3) return (v / 1e3).toFixed(2) + " k";
  return nf.format(Math.round(v));
}

function fmtDuration(seconds) {
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d) return d + "d " + h + "h " + m + "m";
  if (h) return h + "h " + m + "m";
  if (m) return m + "m";
  return s + "s";     // "0m" logo após subir ficava sem sentido
}

function fmtClock(ts) {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleTimeString("pt-BR",
    { hour: "2-digit", minute: "2-digit" });
}

/* Chance de achar um bloco em 24h: p = hashrate * 86400 / (dif * 2^32).
   Mostrado como "1 em N" — a forma que as pessoas entendem loteria. */
function oddsPerDay(hashrate, difficulty) {
  const hs = Number(hashrate) || 0;
  const diff = Number(difficulty) || 0;
  if (hs <= 0 || diff <= 0) return "—";
  const p = (hs * 86400) / (diff * Math.pow(2, 32));
  if (p <= 0) return "—";
  if (p >= 1) return "praticamente certa";
  const one = 1 / p;
  const exp = Math.floor(Math.log10(one));
  const mant = one / Math.pow(10, exp);
  if (exp < 4) return "1 em " + nf.format(Math.round(one));
  return "1 em " + mant.toFixed(1).replace(".", ",") + " × 10" + sup(exp);
}

function sup(n) {
  const map = { "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴",
                "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹", "-": "⁻" };
  return String(n).split("").map((c) => map[c] || c).join("");
}

/* Parte dos valores vem da config que o próprio usuário digita (URL da pool,
   por exemplo) e do que o cpuminer devolve. Nada disso entra em innerHTML
   sem passar por aqui. */
function esc(value) {
  return String(value).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

function rows(target, pairs) {
  target.innerHTML = pairs
    .map(([k, v]) => "<dt>" + esc(k) + "</dt><dd>" +
         (v === null || v === undefined || v === "" ? "—" : esc(v)) + "</dd>")
    .join("");
}

/* ---------- API ---------- */

async function getJSON(url) {
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) {
    let msg = "HTTP " + res.status;
    try { msg = (await res.json()).error || msg; } catch (e) { /* corpo não-JSON */ }
    throw new Error(msg);
  }
  return res.json();
}

async function postJSON(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));
  return data;
}

/* ---------- render do painel ---------- */

function renderStats() {
  if (!stats) return;
  const st = STATUS[stats.status] || STATUS.stopped;
  const cur = stats.current || {};
  const net = stats.network || {};
  const mining = stats.status === "mining";

  $("headline").textContent = st.head;
  const upt = stats.miner && stats.miner.startedAt
    ? "Ativo há " + fmtDuration(stats.serverTime - stats.miner.startedAt)
    : (stats.statusLabel || "");
  $("subline").textContent = mining
    ? "Loteria de bloco: cada hash é um bilhete. " + upt
    : (stats.statusLabel || "");

  // Alvo (node ou pool): cor E rótulo, nunca cor sozinha.
  const tgt = stats.target || {};
  $("targetKind").textContent = stats.solo ? "Node" : "Pool";
  $("targetHost").textContent = tgt.label || "—";
  const dot = $("targetDot");
  dot.className = "dot " + (tgt.reachable === true ? "ok"
                          : tgt.reachable === false ? "bad" : "");
  $("targetState").textContent = tgt.reachable === true ? "Conectado"
    : tgt.reachable === false ? "Sem resposta" : "verificando…";

  // KPIs
  const [hv, hu] = fmtHash(mining ? cur.HS : 0);
  $("kpiHash").textContent = stats.miner && stats.miner.running ? (mining ? hv : "0") : "—";
  $("kpiHashUnit").textContent = stats.miner && stats.miner.running ? hu : "";
  const note = $("kpiHashNote");
  if (stats.miner && stats.miner.running && !mining) {
    const [lv, lu] = fmtHash(cur.HS);
    note.textContent = "produção parada · última leitura " + lv + " " + lu;
    note.className = "n bad";
  } else {
    note.textContent = cur.CPUS ? cur.CPUS + " thread(s)" : " ";
    note.className = "n";
  }

  $("kpiDiff").textContent = fmtBig(net.difficulty);
  $("kpiDiffNote").textContent = net.blockHeight
    ? "bloco " + nf.format(net.blockHeight) : " ";

  $("kpiBlocks").textContent = cur.SOL !== undefined ? cur.SOL : "0";
  $("kpiBlocksNote").textContent = stats.miner && stats.miner.restarts
    ? stats.miner.restarts + " reinício(s) do processo" : " ";

  $("kpiOdds").textContent = oddsPerDay(cur.HS, net.difficulty);

  // Pool: só existe em modo stratum. Em solo o miner não submete share
  // nenhuma — só blocos — então o card inteiro sai da tela.
  const poolCard = $("poolCard");
  poolCard.hidden = !!stats.solo;
  if (!stats.solo) {
    const acc = Number(cur.ACC || 0);
    const rej = Number(cur.REJ || 0);
    const total = acc + rej;
    const perMin = Number(cur.ACCMN || 0);
    rows($("poolRows"), [
      ["Shares aceitas", nf.format(acc)],
      ["Rejeitadas", total
        ? nf.format(rej) + " (" + ((rej / total) * 100).toFixed(1) + "%)"
        : nf.format(rej)],
      ["Share a cada", perMin > 0 ? (1 / perMin).toFixed(1) + " min" : null],
      ["Dificuldade do share", cur.DIFF ? fmtBig(cur.DIFF) : null],
    ]);
  }

  // Rede
  rows($("netRows"), [
    ["Altura do bloco", net.blockHeight ? nf.format(net.blockHeight) : null],
    ["Dificuldade", fmtBig(net.difficulty)],
    ["Hashrate global", net.networkHashrate ? fmtHash(net.networkHashrate).join(" ") : null],
    ["Próximo halving", net.halvingIn ? nf.format(net.halvingIn) + " blocos" : null],
    ["Sincronizado", net.ibd === undefined ? null : (net.ibd ? "não (IBD)" : "sim")],
  ]);

  // Dispositivo. A temperatura leva RÓTULO junto do número — uma barra
  // colorida sem legenda deixaria a cor carregando o significado sozinha.
  const temp = Number(cur.TEMP);
  const limit = config && config.max_temp ? Number(config.max_temp) : 0;
  let tempText = null;
  if (isFinite(temp) && temp > 0) {
    // Com limite configurado, o rótulo diz onde estamos em relação a ELE —
    // é isso que explica um hashrate baixo, e não a escala genérica.
    tempText = Math.round(temp) + " °C · " + (
      limit ? (temp >= limit ? "no limite, pausando" : "abaixo do limite de " + limit + " °C")
            : (temp > 80 ? "alta" : temp >= 70 ? "atenção" : "normal"));
  }
  rows($("devRows"), [
    ["Temperatura", tempText],
    ["Frequência", cur.FREQ ? (Number(cur.FREQ) / 1e6).toFixed(2) + " GHz" : null],
    ["Algoritmo", cur.ALGO && !/^\d+$/.test(cur.ALGO) ? cur.ALGO : "sha256d"],
    ["Versão", cur.NAME ? (cur.NAME + " " + (cur.VER || "")).trim() : null],
  ]);

  $("brandSub").textContent = (cur.NAME || "cpuminer") + " · " +
    (stats.solo ? "solo" : "pool");

  // Botão pausar/retomar
  const btn = $("btnToggle");
  btn.textContent = stats.paused ? "Retomar" : "Pausar";
  btn.disabled = stats.status === "needs_setup";

  // Banner
  const banner = $("banner");
  if (offline) {
    banner.textContent = "Sem conexão com o dispositivo — tentando novamente.";
    banner.className = "banner";
  } else if (stats.status === "needs_setup") {
    banner.innerHTML = "Informe o endereço Bitcoin em <a href='#/config'>Configuração</a> para começar a minerar.";
    banner.className = "banner info";
  } else if (stats.status === "target_down") {
    banner.textContent = (stats.solo ? "O node não está respondendo" :
      "A pool não está respondendo") + " — o miner segue tentando reconectar sozinho. " +
      (tgt.detail ? "Sondagem: " + tgt.detail + "." : "");
    banner.className = "banner";
  } else if (stats.networkError && !stats.network) {
    banner.textContent = "Sem dados da rede Bitcoin: " + stats.networkError;
    banner.className = "banner info";
  } else {
    banner.className = "banner hidden";
  }

  document.title = (mining ? hv + " " + hu + " · " : "") + "NerdMiner";
  document.body.classList.toggle("stale", offline);
}

function renderHistory(hist) {
  if (!hist) return;
  const label = { "1h": "última hora", "24h": "últimas 24h", "7d": "últimos 7 dias" };
  $("chartTitle").textContent = "Hashrate · " + (label[hist.range] || hist.range);
  renderChart($("chart"), hist.points, {
    range: hist.range, tip: $("tip"), events: hist.events
  });

  const last = (hist.events || [])[hist.events.length - 1];
  $("chartNote").innerHTML = last
    ? "Última queda às " + fmtClock(last.t) + " (" + fmtDuration(last.duration_s) + " sem trabalho)."
    : "&nbsp;";

  const tbody = $("dataTable").querySelector("tbody");
  tbody.innerHTML = hist.points.slice(-80).reverse().map((p) => {
    const v = p[1] === null || p[1] === undefined
      ? "<span style='color:var(--faint)'>sem medição</span>"
      : fmtHash(p[1]).join(" ");
    return "<tr><td>" + fmtClock(p[0]) + "</td><td>" + v + "</td></tr>";
  }).join("");
}

/* ---------- configuração ---------- */

function fillForm(cfg) {
  $("fMode").value = cfg.mode;
  $("fAddr").value = cfg.btc_address || "";
  $("fThreads").value = cfg.threads;
  $("fThreads").max = cfg.max_threads;
  $("fScan").value = cfg.scantime;
  $("fMaxTemp").value = cfg.max_temp;
  if (cfg.temp_range) {
    $("fMaxTemp").min = 0;
    $("fMaxTemp").max = cfg.temp_range[1];
  }
  $("fPoolUrl").value = cfg.pool_url || "";
  $("fPoolPw").value = "";
  $("threadsHelp").textContent = "1 a " + cfg.max_threads +
    ". Mais threads = mais calor e menos CPU para os outros apps.";
  onModeChange();
  formBaseline = snapshotForm();
  markDirty();
  rows($("sysRows"), [
    ["Modo atual", cfg.mode === "solo" ? "Solo (getblocktemplate)" : "Pool (stratum)"],
    ["Alvo", stats && stats.target ? stats.target.label : null],
    ["Threads disponíveis", cfg.max_threads],
  ]);
}

function snapshotForm() {
  return JSON.stringify({
    mode: $("fMode").value,
    btc_address: $("fAddr").value.trim(),
    threads: $("fThreads").value,
    scantime: $("fScan").value,
    max_temp: $("fMaxTemp").value,
    pool_url: $("fPoolUrl").value.trim(),
    pool_password: $("fPoolPw").value,
  });
}

function markDirty() {
  const dirty = formBaseline !== null && snapshotForm() !== formBaseline;
  $("btnSave").disabled = !dirty;
  $("btnDiscard").disabled = !dirty;
  return dirty;
}

function onModeChange() {
  const solo = $("fMode").value === "solo";
  $("poolBlock").hidden = solo;
  $("modeHelp").textContent = solo
    ? "Fala getblocktemplate direto com o seu Bitcoin Node. Sem pool, sem taxa."
    : "Conecta num servidor stratum externo. Útil se o node estiver fora.";
}

function showError(id, msg) {
  const node = $(id);
  node.textContent = msg || "";
  node.hidden = !msg;
}

function validateForm() {
  showError("errAddr", ""); showError("errNum", ""); showError("errPool", "");
  $("fAddr").removeAttribute("aria-invalid");

  const addr = $("fAddr").value.trim();
  if (!addr) {
    showError("errAddr", "Informe o endereço que receberá a recompensa.");
    $("fAddr").setAttribute("aria-invalid", "true");
    return null;
  }
  const lower = addr.toLowerCase();
  const bech = lower.startsWith("bc1") || lower.startsWith("tb1") || lower.startsWith("bcrt1");
  if (bech && addr !== lower && addr !== addr.toUpperCase()) {
    showError("errAddr", "Endereço bech32 não pode misturar maiúsculas e minúsculas.");
    $("fAddr").setAttribute("aria-invalid", "true");
    return null;
  }
  if (!bech && !/^[13][1-9A-HJ-NP-Za-km-z]{25,39}$/.test(addr)) {
    showError("errAddr", "Endereço inválido. Comece com bc1, 1 ou 3.");
    $("fAddr").setAttribute("aria-invalid", "true");
    return null;
  }

  const threads = parseInt($("fThreads").value, 10);
  const scan = parseInt($("fScan").value, 10);
  const maxT = parseInt($("fThreads").max, 10) || 1;
  if (!(threads >= 1 && threads <= maxT)) {
    showError("errNum", "Threads deve estar entre 1 e " + maxT + ".");
    return null;
  }
  if (!(scan >= 5 && scan <= 3600)) {
    showError("errNum", "Scantime deve estar entre 5 e 3600 segundos.");
    return null;
  }

  const maxTemp = parseInt($("fMaxTemp").value, 10);
  if (!isFinite(maxTemp) || maxTemp < 0) {
    showError("errNum", "Temperatura máxima inválida. Use 0 para desligar.");
    return null;
  }
  if (maxTemp !== 0 && !(maxTemp >= 45 && maxTemp <= 95)) {
    showError("errNum", "Temperatura máxima deve ser 0 (desligado) ou entre 45 e 95 °C.");
    return null;
  }

  const mode = $("fMode").value;
  const poolUrl = $("fPoolUrl").value.trim();
  if (mode === "pool" && !/^stratum\+tcps?:\/\/[^\s:]+:\d{1,5}$/.test(poolUrl)) {
    showError("errPool", "Use o formato stratum+tcp://host:porta");
    return null;
  }

  const body = { mode, btc_address: addr, threads, scantime: scan,
                 max_temp: maxTemp };
  if (mode === "pool") body.pool_url = poolUrl;
  const pw = $("fPoolPw").value;
  if (pw) body.pool_password = pw;   // vazio = mantém a atual
  return body;
}

async function saveConfig() {
  $("saveError").classList.add("hidden");
  const body = validateForm();
  if (!body) return;
  $("overlayText").textContent = "Salvando e reiniciando o miner…";
  $("overlay").classList.remove("hidden");
  try {
    const out = await postJSON("api/config", body);
    config = out.config;
    fillForm(config);
    await waitForBackend();
    location.hash = "#/painel";
  } catch (err) {
    // Falha de gravação (permissão, disco cheio) NÃO é erro do campo
    // endereço. Mandar isso pro erro do campo dizia a coisa errada: o
    // valor podia estar perfeito e o problema ser o /data.
    $("saveError").textContent = "Não foi possível salvar: " + err.message;
    $("saveError").classList.remove("hidden");
  } finally {
    $("overlay").classList.add("hidden");
  }
}

async function waitForBackend() {
  for (let i = 0; i < 20; i++) {
    try {
      stats = await getJSON("api/stats");
      renderStats();
      return;
    } catch (e) {
      await new Promise((r) => setTimeout(r, 500));
    }
  }
}

/* ---------- roteador ---------- */

function route() {
  const hash = location.hash || "#/painel";
  const name = hash.replace("#/", "") === "config" ? "config" : "painel";
  $("view-painel").hidden = name !== "painel";
  $("view-config").hidden = name !== "config";
  for (const a of document.querySelectorAll(".nav-item")) {
    a.classList.toggle("on", a.dataset.route === name);
  }
  if (name === "config" && config) fillForm(config);
  if (name === "painel") refreshHistory();
}

/* ---------- polling ---------- */

async function tickStats() {
  try {
    stats = await getJSON("api/stats");
    offline = false;
    document.body.classList.remove("loading");   // chegou a primeira leitura
  } catch (err) {
    offline = true;
  }
  renderStats();
}

async function refreshHistory() {
  try {
    renderHistory(await getJSON("api/history?range=" + range));
  } catch (err) { /* mantém o gráfico anterior */ }
}

function startTimers() {
  stopTimers();
  statsTimer = setInterval(tickStats, 5000);
  histTimer = setInterval(refreshHistory, 60000);
}

function stopTimers() {
  if (statsTimer) clearInterval(statsTimer);
  if (histTimer) clearInterval(histTimer);
  statsTimer = histTimer = null;
}

/* ---------- ligação ---------- */

function wire() {
  window.addEventListener("hashchange", route);

  for (const btn of document.querySelectorAll(".seg button")) {
    btn.addEventListener("click", () => {
      range = btn.dataset.range;
      for (const b of document.querySelectorAll(".seg button")) {
        b.classList.toggle("on", b === btn);
      }
      refreshHistory();
    });
  }

  $("btnToggle").addEventListener("click", async () => {
    const paused = !(stats && stats.paused);
    try {
      await postJSON("api/mining/" + (paused ? "pause" : "resume"), {});
      await tickStats();
    } catch (err) { /* estado volta no próximo poll */ }
  });

  $("btnRestart").addEventListener("click", async () => {
    if (!confirm("Reiniciar o processo do minerador?")) return;
    $("overlayText").textContent = "Reiniciando o miner…";
    $("overlay").classList.remove("hidden");
    try {
      await postJSON("api/restart", {});
      await new Promise((r) => setTimeout(r, 1500));
      await tickStats();
    } finally {
      $("overlay").classList.add("hidden");
    }
  });

  $("cfgForm").addEventListener("input", markDirty);
  $("cfgForm").addEventListener("change", markDirty);
  $("fMode").addEventListener("change", onModeChange);
  $("btnSave").addEventListener("click", saveConfig);
  $("btnDiscard").addEventListener("click", () => { if (config) fillForm(config); });
  $("cfgForm").addEventListener("submit", (e) => { e.preventDefault(); saveConfig(); });

  window.addEventListener("beforeunload", (e) => {
    if (!$("view-config").hidden && markDirty()) {
      e.preventDefault();
      e.returnValue = "";
    }
  });

  // Aba escondida não precisa de polling: economiza CPU do servidor e do
  // navegador, e é o que a especificação pede.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      stopTimers();
    } else {
      startTimers();
      tickStats();
      refreshHistory();
    }
  });

  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(refreshHistory, 200);
  });
}

async function boot() {
  wire();
  route();
  await tickStats();
  try {
    config = await getJSON("api/config");
    if (!$("view-config").hidden) fillForm(config);
  } catch (err) { /* a UI de config avisa ao abrir */ }
  await refreshHistory();
  startTimers();
}

boot();
