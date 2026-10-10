/* Painel do servidor NTP.
 *
 * Um rascunho local (draft) guarda o que o usuario mexeu; nada vai pro
 * servidor ate ele apertar "Aplicar". E por isso que as telas de Fontes e de
 * Ajustes compartilham o MESMO rascunho: mudar o intervalo de consulta numa
 * e sair pela outra sem aplicar nao pode perder a alteracao nem aplicar
 * metade dela.
 */

const POLLS = [[6, "1 min"], [7, "2 min"], [8, "4 min"], [10, "17 min"]];
const RANGES = [["1h", "1 h"], ["12h", "12 h"], ["24h", "24 h"], ["7d", "7 dias"]];

const SUGGESTIONS = [
  ["a.st1.ntp.br", "NTP.br · stratum 1 · São Paulo", "server"],
  ["b.st1.ntp.br", "NTP.br · stratum 1 · São Paulo", "server"],
  ["c.st1.ntp.br", "NTP.br · stratum 1 · São Paulo", "server"],
  ["pool.ntp.br", "NTP.br · conjunto nacional", "pool"],
  ["time.cloudflare.com", "Cloudflare · anycast global", "server"],
  ["br.pool.ntp.org", "pool.ntp.org · servidores no Brasil", "pool"],
];

const SRC_OPTIONS = [
  ["prefer", "Preferir esta fonte", "Usada como referência sempre que estiver saudável."],
  ["iburst", "Sincronização rápida (iburst)", "Envia várias consultas ao iniciar para acertar em segundos."],
  ["nts", "Autenticação NTS", "Protege as respostas contra adulteração. O servidor precisa suportar."],
];

let stats = null;
let config = null;      // o que esta gravado no servidor
let draft = null;       // o que o usuario esta editando
let histRange = "12h";
let histData = null;
let selected = 0;       // fonte selecionada na tela de Fontes
let addForm = { host: "", kind: "server", iburst: true, nts: false, tested: null };
let timer = null;
let clockTimer = null;
let offline = false;

const $ = function (id) { return document.getElementById(id); };

function esc(text) {
  return String(text === null || text === undefined ? "" : text)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function setText(id, text) { $(id).textContent = text; }

async function api(path, options) {
  const res = await fetch(path, options);
  const data = await res.json().catch(function () { return {}; });
  if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));
  return data;
}

function post(path, body) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
}

function clone(value) { return JSON.parse(JSON.stringify(value)); }

function dirty() {
  if (!config || !draft) return false;
  const keys = ["serve", "allow_networks", "servers", "poll_exponent",
                "min_sources", "timezone", "auto_refresh"];
  for (const k of keys) {
    if (JSON.stringify(config[k]) !== JSON.stringify(draft[k])) return true;
  }
  return false;
}

/* ---------- controles reutilizaveis ---------- */

function renderSeg(host, items, current, onPick) {
  host.innerHTML = "";
  for (const [value, label] of items) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = label;
    b.setAttribute("aria-pressed", String(value === current));
    b.addEventListener("click", function () { onPick(value); });
    host.appendChild(b);
  }
}

function makeSwitch(on, label, onToggle, disabled) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "switch";
  b.setAttribute("role", "switch");
  b.setAttribute("aria-checked", String(!!on));
  b.setAttribute("aria-label", label);
  b.disabled = !!disabled;
  b.innerHTML = '<span class="knob"></span>';
  b.addEventListener("click", function () { onToggle(!on); });
  return b;
}

function setSwitch(id, on) { $(id).setAttribute("aria-checked", String(!!on)); }

function row(label, value) {
  const d = document.createElement("div");
  d.className = "row";
  d.innerHTML = '<span class="label">' + esc(label) + '</span>' +
                '<span class="value">' + esc(value) + '</span>';
  return d;
}

/* ---------- roteamento ---------- */

const VIEWS = {
  painel: "view-painel",
  fontes: "view-fontes",
  "fontes/add": "view-add",
  historico: "view-historico",
  ajustes: "view-ajustes",
};

function currentRoute() {
  const raw = (location.hash || "#/painel").replace(/^#\/?/, "");
  return VIEWS[raw] ? raw : "painel";
}

function showView() {
  const route = currentRoute();
  for (const key in VIEWS) $(VIEWS[key]).hidden = (key !== route);
  for (const a of document.querySelectorAll(".nav-item")) {
    const on = a.dataset.route === route.split("/")[0];
    a.classList.toggle("on", on);
    if (on) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  }
  if (route === "historico") loadHistory();
  if (route === "fontes") renderFontes();
  if (route === "fontes/add") renderAdd();
  if (route === "ajustes") renderAjustes();
  window.scrollTo(0, 0);
}

/* ---------- painel ---------- */

function statusClass(status) {
  if (status === "synced") return "";
  if (status === "syncing" || status === "not_serving") return "warn";
  if (status === "stopped" || status === "unknown") return "bad";
  return "idle";
}

function renderClock() {
  if (!stats || !stats.clock) return;
  const zone = stats.clock.timezone;
  // A hora corre no navegador entre um poll e outro, ancorada no instante
  // que o servidor informou. Sem isso o relogio congelaria por 15 s e
  // depois daria um pulo - num painel de servidor de HORA, feio demais.
  const base = stats.clock.epoch * 1000 + (Date.now() - stats._received);
  const d = new Date(base);
  try {
    setText("clockTime", d.toLocaleTimeString("pt-BR", { timeZone: zone, hour12: false }));
    setText("clockDate", d.toLocaleDateString("pt-BR", {
      timeZone: zone, weekday: "long", day: "numeric", month: "long", year: "numeric" }));
  } catch (e) {
    setText("clockTime", d.toLocaleTimeString("pt-BR", { hour12: false }));
    setText("clockDate", d.toLocaleDateString("pt-BR"));
  }
  setText("clockZone", zone + " · UTC" + stats.clock.utcOffset);
}

function tile(key, value, unit, note, tone) {
  return '<div class="tile ' + (tone || "") + '">' +
    '<span class="k">' + esc(key) + '</span>' +
    '<span class="v mono">' + esc(value) +
    (unit ? '<span class="u"> ' + esc(unit) + '</span>' : "") + '</span>' +
    '<span class="n">' + esc(note || "") + '</span></div>';
}

function ago(seconds) {
  if (seconds === null || seconds === undefined || !isFinite(seconds)) return "—";
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return s + " s";
  if (s < 3600) return Math.round(s / 60) + " min";
  if (s < 86400) return Math.round(s / 3600) + " h";
  return Math.round(s / 86400) + " d";
}

function renderPainel() {
  if (!stats) return;
  const t = stats.tracking || {};
  const pill = $("statusPill");
  pill.className = "pill " + statusClass(stats.status);
  setText("statusText", stats.statusLabel || "—");
  setText("updatedAt", "Atualizado às " +
    new Date(stats.serverTime * 1000).toLocaleTimeString("pt-BR", { hour12: false }));
  setText("brandSub", "umbrel · 123/UDP");

  renderClock();
  setText("clockRef", t.refName ? ("Referência " + t.refName) : "Sem referência ainda");

  const [ov, ou] = chOffset(t.lastOffset);
  setText("offsetValue", ov);
  setText("offsetUnit", ou);

  // Mosaico de numeros. "Fontes ativas" ganha cor quando menos fontes
  // respondem do que o configurado - e cor COM rotulo, nunca cor sozinha.
  const srcs = stats.sources || [];
  const alive = srcs.filter(function (s) { return s.state === "sync" || s.state === "ok"; }).length;
  let tone = "";
  if (srcs.length && alive === 0) tone = "bad";
  else if (alive < srcs.length) tone = "warn";

  const disp = chOffset(t.rootDispersion);
  const delay = chOffset(t.rootDelay);
  const prec = stats.precision;
  const precText = prec === null || prec === undefined ? "—"
    : (Math.pow(2, prec) * 1e9).toLocaleString("pt-BR", { maximumFractionDigits: 0 });

  $("tiles").innerHTML = [
    tile("Stratum", t.stratum ? String(t.stratum) : "—", "",
         t.stratum ? "via stratum " + Math.max(0, t.stratum - 1) : "sem referência"),
    tile("Último sync", ago(t.refTime ? (stats.serverTime - t.refTime) : null), "",
         t.refTime ? "às " + new Date(t.refTime * 1000).toLocaleTimeString("pt-BR", { hour12: false }) : ""),
    tile("Erro estimado", "±" + disp[0], disp[1], "root dispersion"),
    tile("Atraso à origem", delay[0], delay[1], "root delay"),
    tile("Fontes ativas", alive + " de " + srcs.length, "",
         "consulta a cada " + pollLabel(stats.pollExponent), tone),
    tile("Precisão", precText, "ns", "resolução do relógio"),
  ].join("");

  // Lista de fontes
  const list = $("sourceList");
  list.innerHTML = "";
  if (!srcs.length) {
    list.innerHTML = '<div class="row"><span class="label">Nenhuma fonte respondeu ainda.</span></div>';
  }
  for (const s of srcs) {
    const d = document.createElement("div");
    d.className = "row wrap";
    let right;
    if (s.state === "sync" || s.state === "ok") {
      const off = chOffset(s.offset), rtt = chOffset(s.error);
      right = '<span class="src-field">Stratum <b>' + esc(s.stratum) + '</b></span>' +
              '<span class="src-field">Desvio <b>' + esc(off[0] + " " + off[1]) + '</b></span>' +
              '<span class="src-field">Margem <b>' + esc(rtt[0] + " " + rtt[1]) + '</b></span>';
    } else {
      right = '<span class="src-field" style="color:var(--warn)">' + esc(s.stateLabel) +
              ' · ' + esc(s.reachPercent) + '% das últimas 8 consultas</span>';
    }
    d.innerHTML = '<span class="src-name"><span class="src-dot ' + esc(s.state) + '"></span>' +
      '<span class="host">' + esc(s.name) + '</span>' +
      (s.state === "sync" ? '<span class="tag">em uso</span>' : "") + '</span>' + right;
    list.appendChild(d);
  }
}

function pollLabel(exp) {
  for (const [value, label] of POLLS) if (value === exp) return label;
  return Math.pow(2, exp) + " s";
}

async function loadClients() {
  let data;
  try {
    data = await api("/api/clients");
  } catch (e) {
    return;
  }
  const host = $("clientList");
  const list = data.clients || [];
  setText("clientsCount", list.length ? list.length + " dispositivo(s)" : "");
  host.innerHTML = "";
  if (!list.length) {
    host.innerHTML = '<div class="row"><span class="label">' +
      esc(data.error ? "Não consegui ler a lista: " + data.error
                     : "Ninguém pediu hora a este servidor ainda.") +
      '</span></div>';
    return;
  }
  for (const c of list.slice(0, 20)) {
    const d = document.createElement("div");
    d.className = "row wrap";
    d.innerHTML = '<span class="src-name"><span class="host mono">' + esc(c.host) + '</span></span>' +
      '<span class="src-field">Pedidos <b>' + esc(c.packets.toLocaleString("pt-BR")) + '</b></span>' +
      '<span class="src-field">Último <b>' + esc(ago(c.lastSeen)) + ' atrás</b></span>' +
      (c.dropped ? '<span class="src-field" style="color:var(--warn)">Descartados <b>' + esc(c.dropped) + '</b></span>' : "");
    host.appendChild(d);
  }
}

/* ---------- fontes ---------- */

function renderFontes() {
  if (!draft) return;
  const servers = draft.servers;
  if (selected >= servers.length) selected = Math.max(0, servers.length - 1);

  const live = {};
  for (const s of (stats && stats.sources) || []) live[s.name] = s;

  const host = $("srcRows");
  host.innerHTML = "";
  servers.forEach(function (s, i) {
    const info = live[s.host];
    const d = document.createElement("div");
    d.className = "src-row";

    let dot = "idle", sub = "Aguardando primeira consulta";
    if (!s.enabled) { dot = ""; sub = "Desativada · não é consultada"; }
    else if (info) {
      dot = info.state;
      const off = chOffset(info.offset);
      sub = (info.state === "sync" || info.state === "ok")
        ? info.stateLabel + " · " + off[0] + " " + off[1]
        : info.stateLabel;
    }

    const pick = document.createElement("button");
    pick.type = "button";
    pick.className = "src-pick";
    pick.setAttribute("aria-pressed", String(i === selected));
    pick.innerHTML = '<span class="src-dot ' + esc(dot) + '"></span>' +
      '<span class="stack"><span class="t">' + esc(s.host) + '</span>' +
      '<span class="d">' + esc(sub) + '</span></span>';
    pick.addEventListener("click", function () { selected = i; renderFontes(); });
    d.appendChild(pick);

    d.appendChild(makeSwitch(s.enabled, "Usar " + s.host, function (on) {
      draft.servers[i].enabled = on;
      renderFontes();
      syncApplyButtons();
    }));
    host.appendChild(d);
  });

  const active = servers.filter(function (s) { return s.enabled; }).length;
  setText("srcActiveNote", active + " de " + servers.length + " fontes ativas.");

  renderSrcDetail(servers[selected], live);
  renderSeg($("segPollFontes"), POLLS, draft.poll_exponent, function (v) {
    draft.poll_exponent = v; renderFontes(); syncApplyButtons();
  });
  syncApplyButtons();
}

function renderSrcDetail(src, live) {
  const detail = $("srcDetail");
  const opts = $("srcOptions");
  detail.innerHTML = "";
  opts.innerHTML = "";
  if (!src) {
    detail.innerHTML = '<div class="row"><span class="label">Nenhuma fonte configurada.</span></div>';
    return;
  }
  const info = live[src.host];
  const statusText = !src.enabled ? "Desativada · não é consultada"
    : info ? info.stateLabel : "Aguardando primeira consulta";
  const off = info ? chOffset(info.offset) : ["—", ""];
  const err = info ? chOffset(info.error) : ["—", ""];

  const head = document.createElement("div");
  head.className = "detail-head";
  head.innerHTML = '<div class="icon">' +
    '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#FF8A3D" stroke-width="1.8" aria-hidden="true">' +
    '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2" stroke-linecap="round"/></svg></div>' +
    '<div class="stack" style="min-width:0"><div style="font-size:17px;font-weight:600;overflow-wrap:anywhere">' +
    esc(src.host) + '</div><div style="font-size:13px;color:var(--text-3)">' + esc(statusText) + '</div></div>';
  detail.appendChild(head);

  const grid = document.createElement("div");
  grid.className = "detail-grid";
  grid.innerHTML =
    '<div><span class="k">Stratum</span><span class="v mono">' + esc(info && info.stratum ? info.stratum : "—") + '</span></div>' +
    '<div><span class="k">Desvio</span><span class="v mono">' + esc(off[0] + (off[1] ? " " + off[1] : "")) + '</span></div>' +
    '<div><span class="k">Margem de erro</span><span class="v mono">' + esc(err[0] + (err[1] ? " " + err[1] : "")) + '</span></div>' +
    '<div><span class="k">Tipo</span><span class="v">' + (src.kind === "pool" ? "Pool" : "Servidor") + '</span></div>';
  detail.appendChild(grid);

  const remove = document.createElement("div");
  remove.className = "row";
  remove.innerHTML = '<span class="label">Remover esta fonte</span>';
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "btn ghost";
  btn.textContent = "Remover";
  btn.disabled = draft.servers.length <= 1;
  btn.addEventListener("click", function () {
    draft.servers.splice(selected, 1);
    selected = 0;
    renderFontes();
  });
  remove.appendChild(btn);
  detail.appendChild(remove);

  for (const [key, label, desc] of SRC_OPTIONS) {
    const d = document.createElement("div");
    d.className = "row";
    d.innerHTML = '<span class="stack"><span class="t">' + esc(label) + '</span>' +
                  '<span class="d">' + esc(desc) + '</span></span>';
    d.appendChild(makeSwitch(src[key], label, function (on) {
      src[key] = on; renderFontes(); syncApplyButtons();
    }));
    opts.appendChild(d);
  }
}

/* ---------- adicionar fonte ---------- */

function renderAdd() {
  $("addHost").value = addForm.host;
  renderSeg($("segKind"), [["server", "Servidor"], ["pool", "Pool"]], addForm.kind,
    function (v) { addForm.kind = v; renderAdd(); });

  const opts = $("addOptions");
  opts.innerHTML = "";
  for (const [key, label, desc] of [SRC_OPTIONS[1], SRC_OPTIONS[2]]) {
    const d = document.createElement("div");
    d.className = "row";
    d.innerHTML = '<span class="stack"><span class="t">' + esc(label) + '</span>' +
                  '<span class="d">' + esc(desc) + '</span></span>';
    d.appendChild(makeSwitch(addForm[key], label, function (on) {
      addForm[key] = on; renderAdd();
    }));
    opts.appendChild(d);
  }

  const sg = $("suggestions");
  sg.innerHTML = "";
  const already = new Set((draft ? draft.servers : []).map(function (s) { return s.host; }));
  for (const [host, desc, kind] of SUGGESTIONS) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "sugg";
    b.disabled = already.has(host);
    b.innerHTML = '<span class="stack"><span class="t">' + esc(host) + '</span>' +
      '<span class="d">' + esc(desc) + '</span></span>' +
      '<span class="tag"' + (already.has(host) ? ' style="background:rgba(255,255,255,.08);color:var(--text-3)"' : "") +
      '>' + (already.has(host) ? "já adicionada" : (kind === "pool" ? "pool" : "servidor")) + '</span>';
    b.addEventListener("click", function () {
      addForm.host = host; addForm.kind = kind; addForm.tested = null; renderAdd();
    });
    sg.appendChild(b);
  }

  const result = $("addResult");
  if (addForm.tested) {
    result.hidden = false;
    result.className = "msg " + (addForm.tested.ok ? "ok" : "bad");
    result.textContent = addForm.tested.text;
  } else {
    result.hidden = true;
  }

  const valid = /^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$/.test(addForm.host.trim());
  $("btnAddConfirm").disabled = !valid || already.has(addForm.host.trim().toLowerCase());
}

async function testHost() {
  const host = addForm.host.trim();
  if (!host) return;
  addForm.tested = { ok: false, text: "Consultando " + host + "…" };
  renderAdd();
  try {
    const data = await post("/api/test", { host: host });
    if (!data.ok) {
      addForm.tested = { ok: false, text: data.error || "sem resposta" };
    } else {
      const r = data.result;
      const off = chOffset(r.offset), rtt = chOffset(r.delay);
      addForm.tested = { ok: true, text: "Respondeu · stratum " + r.stratum +
        " · desvio " + off[0] + " " + off[1] +
        " · ida e volta " + rtt[0] + " " + rtt[1] };
    }
  } catch (e) {
    addForm.tested = { ok: false, text: e.message };
  }
  renderAdd();
}

/* ---------- historico ---------- */

async function loadHistory() {
  renderSeg($("segRange"), RANGES, histRange, function (v) {
    histRange = v; loadHistory();
  });
  try {
    histData = await api("/api/history?range=" + encodeURIComponent(histRange));
  } catch (e) {
    return;
  }
  renderHistorico();
}

function renderHistorico() {
  if (!histData) return;
  const points = histData.points || [];
  const name = { "1h": "última hora", "12h": "últimas 12 horas",
                 "24h": "últimas 24 horas", "7d": "últimos 7 dias" }[histRange];
  setText("rangeName", "Desvio · " + name);

  const last = [].concat(points).reverse()
    .find(function (p) { return p[1] !== null && p[1] !== undefined; });
  const now = chOffset(last ? last[1] : null);
  setText("histNow", now[0]);
  setText("histNowUnit", now[1] ? now[1] + " agora" : "");

  const info = chDrawOffset($("histChart"), points, { grid: true });
  const top = chOffset(info.range);
  $("histScale").innerHTML = '<span>+' + esc(top[0]) + '</span><span>0</span><span>−' + esc(top[0]) + '</span>';

  const ticks = chTicks(points, histRange, 4);
  $("histAxis").innerHTML = ticks.map(function (t) { return "<span>" + esc(t) + "</span>"; }).join("");

  const measured = points.filter(function (p) { return p[1] !== null && p[1] !== undefined; }).length;
  setText("histNote", measured + " de " + points.length + " intervalos com medição" +
    (measured < points.length ? " — as falhas aparecem como quebras na linha." : "."));

  const s = histData.stats || {};
  const mk = function (label, value) {
    const v = chOffset(value);
    return tile(label, v[0], v[1], "");
  };
  $("histStats").innerHTML = [
    mk("Mínimo", s.min), mk("Máximo", s.max),
    mk("Média", s.mean), mk("Desvio padrão", s.stdDev),
  ].join("");

  chDrawRtt($("rttChart"), points);
  const rm = chOffset(s.rttMean);
  setText("rttMean", s.rttMean === null || s.rttMean === undefined
    ? "sem dados" : "média " + rm[0] + " " + rm[1]);
  $("rttScale").innerHTML = "";
}

/* ---------- ajustes ---------- */

function renderAjustes() {
  if (!draft) return;
  setSwitch("swServe", draft.serve);
  setSwitch("swAuto", draft.auto_refresh);
  setText("allowValue", draft.allow_networks.join(", ") || "nenhuma");
  if (document.activeElement !== $("allowInput")) {
    $("allowInput").value = draft.allow_networks.join(", ");
  }
  if (document.activeElement !== $("tzInput")) {
    $("tzInput").value = draft.timezone;
  }

  renderSeg($("segPoll"), POLLS, draft.poll_exponent, function (v) {
    draft.poll_exponent = v; renderAjustes(); syncApplyButtons();
  });

  const active = draft.servers.filter(function (s) { return s.enabled; }).length;
  setText("minSourcesValue", String(draft.min_sources));
  setText("minSourcesHint", "Só ajusta o relógio quando " + draft.min_sources +
    " fonte(s) concordam · " + active + " ativa(s)");
  $("btnMinus").disabled = draft.min_sources <= 1;
  $("btnPlus").disabled = draft.min_sources >= Math.min(4, active);

  const t = (stats && stats.tracking) || {};
  const about = $("aboutRows");
  about.innerHTML = "";
  about.appendChild(row("Host", "umbrel"));
  about.appendChild(row("Software", "chrony"));
  about.appendChild(row("Porta de comando", "fechada (socket local)"));
  about.appendChild(row("Reinícios do chronyd",
    String((stats && stats.daemon && stats.daemon.restarts) || 0)));
  about.appendChild(row("Pacotes servidos",
    String(((stats && stats.serverStats && stats.serverStats.ntpPackets) || 0).toLocaleString("pt-BR"))));
  about.appendChild(row("Leap", t.leap || "—"));

  syncApplyButtons();
}

function readAllowInput() {
  const raw = $("allowInput").value.split(",").map(function (s) { return s.trim(); })
    .filter(function (s) { return s.length; });
  draft.allow_networks = raw;
}

function syncApplyButtons() {
  const on = dirty();
  $("btnApply").disabled = !on;
  $("btnDiscard").disabled = !on;
  $("btnApplyFontes").disabled = !on;
}

async function applyConfig() {
  $("saveError").hidden = true;
  $("overlay").hidden = false;
  setText("overlayText", "Aplicando e reiniciando o chrony…");
  try {
    const data = await post("/api/config", draft);
    config = clone(data.config);
    draft = clone(data.config);
    // O chronyd precisa de alguns segundos pra subir e fazer a primeira
    // consulta. Esperar antes de ler evita mostrar "parado" logo apos um
    // Aplicar bem-sucedido, que pareceria falha.
    await new Promise(function (r) { setTimeout(r, 2500); });
    await refresh();
  } catch (e) {
    $("saveError").hidden = false;
    setText("saveError", "Não consegui aplicar: " + e.message);
  } finally {
    $("overlay").hidden = true;
    render();
  }
}

/* ---------- ciclo ---------- */

function render() {
  const route = currentRoute();
  if (route === "painel") renderPainel();
  if (route === "fontes") renderFontes();
  if (route === "fontes/add") renderAdd();
  if (route === "historico") renderHistorico();
  if (route === "ajustes") renderAjustes();

  const banner = $("banner");
  if (offline) {
    banner.hidden = false;
    banner.className = "msg bad";
    banner.textContent = "Sem conexão com o painel — tentando de novo.";
  } else if (stats && stats.status === "stopped") {
    banner.hidden = false;
    banner.className = "msg bad";
    banner.textContent = "O chronyd não está rodando: " + (stats.statusLabel || "");
  } else if (stats && stats.status === "not_serving") {
    banner.hidden = false;
    banner.className = "msg warn";
    banner.textContent = "O servidor está sincronizado mas não responde a ninguém — " +
      "\"Servir hora na rede local\" está desligado em Ajustes.";
  } else {
    banner.hidden = true;
  }
}

async function refresh() {
  try {
    const data = await api("/api/stats");
    data._received = Date.now();
    stats = data;
    offline = false;
  } catch (e) {
    offline = true;
  }
  if (!config) {
    try {
      const cfg = await api("/api/config");
      config = clone(cfg);
      if (!draft) draft = clone(cfg);
    } catch (e) { /* tenta de novo no proximo ciclo */ }
  }
  render();
  if (currentRoute() === "painel") loadClients();
}

function startTimer() {
  if (timer) clearInterval(timer);
  const seconds = (stats && stats.refreshSeconds) || 15;
  // "Atualizar automaticamente" desligado para o ciclo, mas nunca a
  // primeira leitura: uma tela em branco nao seria uma escolha do usuario.
  if (draft && draft.auto_refresh === false) return;
  timer = setInterval(refresh, seconds * 1000);
}

function bind() {
  window.addEventListener("hashchange", showView);

  $("swServe").addEventListener("click", function () {
    draft.serve = !draft.serve; renderAjustes();
  });
  $("swAuto").addEventListener("click", function () {
    draft.auto_refresh = !draft.auto_refresh; renderAjustes(); startTimer();
  });
  $("allowInput").addEventListener("input", function () {
    readAllowInput(); setText("allowValue", draft.allow_networks.join(", ") || "nenhuma");
    syncApplyButtons();
  });
  $("tzInput").addEventListener("input", function () {
    draft.timezone = $("tzInput").value.trim(); syncApplyButtons();
  });
  $("btnMinus").addEventListener("click", function () {
    draft.min_sources = Math.max(1, draft.min_sources - 1); renderAjustes();
  });
  $("btnPlus").addEventListener("click", function () {
    draft.min_sources = Math.min(4, draft.min_sources + 1); renderAjustes();
  });
  $("btnDiscard").addEventListener("click", function () {
    draft = clone(config); render();
  });
  $("btnApply").addEventListener("click", applyConfig);
  $("btnApplyFontes").addEventListener("click", applyConfig);

  $("addHost").addEventListener("input", function () {
    addForm.host = $("addHost").value; addForm.tested = null; renderAdd();
  });
  $("btnTest").addEventListener("click", testHost);
  $("btnAddConfirm").addEventListener("click", function () {
    const host = addForm.host.trim().toLowerCase();
    draft.servers.push({ host: host, kind: addForm.kind, enabled: true,
      prefer: false, iburst: addForm.iburst, nts: addForm.nts });
    selected = draft.servers.length - 1;
    addForm = { host: "", kind: "server", iburst: true, nts: false, tested: null };
    location.hash = "#/fontes";
  });

  // O relogio corre sozinho entre os polls.
  clockTimer = setInterval(function () {
    if (currentRoute() === "painel") renderClock();
  }, 1000);
}

async function boot() {
  bind();
  showView();
  await refresh();
  startTimer();
}

boot();
