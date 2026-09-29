#!/usr/bin/env python3
"""NerdMiner - painel web + supervisor do cpuminer, num container so.

O miner e o painel vivem juntos porque configurar pela UI exige reiniciar o
cpuminer com argumentos novos, e um container nao reinicia outro sem o socket
do Docker (que o Umbrel proibe, com razao).

So biblioteca padrao.
"""

import json
import mimetypes
import os
import signal
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import appconfig
import history
import miner
import nodeinfo

LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8080"))
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "10"))
PROBE_SECONDS = int(os.environ.get("PROBE_SECONDS", "30"))
MAX_BODY = 64 * 1024          # teto do corpo do POST; config e minuscula

# Resgate do miner - ver rescue_miner().
API_DEAD_AFTER = 4            # leituras seguidas sem API (4 x POLL = 40 s)
MINER_GRACE = 60              # carencia pro cpuminer abrir a porta da API
LOG_QUIET_SECONDS = 300       # calado mais que isso nao e mais silencio normal
RESCUE_COOLDOWN = 600         # no maximo um resgate a cada 10 min

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, "web")

_stop = threading.Event()
_lock = threading.Lock()
_current = {}
_miner_error = "aguardando a primeira leitura"
_api_fails = 0                # leituras seguidas em que a API nao respondeu
_last_rescue = 0.0
_rescues = 0
_target = {"reachable": None, "detail": "sonda ainda nao rodou",
           "checkedAt": None, "lastOkAt": None, "label": None}

# Os arquivos do front-end sao poucos e fixos: lidos uma vez na subida.
_assets = {}


def load_assets():
    for name in os.listdir(WEB_DIR):
        path = os.path.join(WEB_DIR, name)
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as handle:
            body = handle.read()
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith(("javascript", "json")):
            ctype += "; charset=utf-8"
        _assets["/" + name] = (body, ctype)
    print("assets carregados: %s" % ", ".join(sorted(_assets)), flush=True)


def target_label(cfg):
    if cfg["mode"] == "solo":
        return "%s:%s" % (nodeinfo.NODE_IP or "?", nodeinfo.RPC_PORT)
    rest = cfg["pool_url"].split("://", 1)[-1]
    return rest


def probe_target(cfg):
    """TCP simples no alvo. Nao fala o protocolo - so 'tem alguem ai'."""
    if cfg["mode"] == "solo":
        host, port = nodeinfo.NODE_IP, nodeinfo.RPC_PORT
    else:
        rest = cfg["pool_url"].split("://", 1)[-1].split("/", 1)[0]
        host, _, port = rest.rpartition(":")
    if not host or not port:
        return None, "alvo nao configurado"
    try:
        with socket.create_connection((host, int(port)), timeout=8):
            return True, "conexao aceita"
    except (OSError, ValueError) as exc:
        return False, str(exc)


def poll_target():
    while not _stop.is_set():
        cfg = appconfig.load()
        reachable, detail = probe_target(cfg)
        now = int(time.time())
        with _lock:
            _target.update(reachable=reachable, detail=detail, checkedAt=now,
                           label=target_label(cfg))
            if reachable:
                _target["lastOkAt"] = now
        history.mark_outage(reachable is False)
        _stop.wait(PROBE_SECONDS)


def rescue_miner(mstate, fails):
    """Sobe outro cpuminer quando este parou de dar sinal de vida.

    Em 29/09/2026 o OOM killer levou o cpuminer as 04:49 - ele tinha chegado
    ao teto de 1 GiB do compose - e o painel passou nove horas dizendo
    "rodando", com o hashrate congelado na ultima leitura. O supervisor nao
    percebeu a morte, e a API do cpuminer, unica fonte de numeros do painel,
    morreu junto com ele. Este e o cinto de seguranca por cima do supervisor.

    Exigimos que DOIS sinais concordem, porque cada um sozinho mente:

    * API muda pode ser susto - ela demora um instante pra abrir depois que
      o processo sobe, e por isso tambem ha carencia;
    * stdout calado pode ser o governador termico, que pausa as threads sem
      imprimir nada quando passa de --max-temp.

    Os dois juntos, passada a carencia, nao tem leitura inocente: nao esta
    produzindo. Se esta morto ou so travado nao importa - o remedio e o
    mesmo, entao nem tentamos distinguir.

    O cooldown faz um defeito permanente virar um reinicio a cada 10 min em
    vez de um laco de reinicios que impede a mineracao de acontecer.
    """
    global _last_rescue, _rescues
    if fails < API_DEAD_AFTER:
        return
    now = time.time()
    if now - (mstate.get("started_at") or 0) < MINER_GRACE:
        return
    last_log = mstate.get("last_log_at") or 0
    quiet = now - last_log
    if not last_log or quiet < LOG_QUIET_SECONDS:
        return                   # ainda fala: minerando, so sem telemetria
    with _lock:
        if now - _last_rescue < RESCUE_COOLDOWN:
            return
        _last_rescue = now
        _rescues += 1
        numero = _rescues
    print("resgate #%d: API muda ha %d leituras e stdout calado ha %d s; "
          "subindo outro cpuminer" % (numero, fails, quiet), flush=True)
    miner.apply_config()


def poll_miner():
    global _current, _miner_error, _api_fails
    while not _stop.is_set():
        mstate = miner.state()
        if not mstate["running"]:
            with _lock:
                _current = {}
                _miner_error = None
                _api_fails = 0
            history.add(None)            # parado: nao estavamos medindo
        else:
            try:
                fields = miner.parse(miner.ask())
                if not fields:
                    raise ValueError("resposta vazia da API do miner")
                with _lock:
                    _current = fields
                    _miner_error = None
                    _api_fails = 0
                rate = float(fields.get("HS", 0) or 0)
                with _lock:
                    unreachable = _target["reachable"] is False
                history.add(0.0 if unreachable else rate)
            except Exception as exc:
                with _lock:
                    _miner_error = "%s: %s" % (type(exc).__name__, exc)
                    _api_fails += 1
                    fails = _api_fails
                # Vivo mas sem responder a API: ainda subindo, ou travado.
                history.add(None)
                rescue_miner(mstate, fails)
        _stop.wait(POLL_SECONDS)


def build_status():
    """Deriva o estado real. Devolve (chave, rotulo)."""
    cfg = appconfig.load()
    mstate = miner.state()
    with _lock:
        unreachable = _target["reachable"] is False
        err = _miner_error
    if not cfg["btc_address"]:
        return "needs_setup", "configure o endereco Bitcoin"
    if cfg["paused"]:
        return "paused", "pausado"
    if not mstate["running"]:
        return "stopped", mstate["reason"] or "parado"
    # Alvo fora vem ANTES da API muda: com o node caido o cpuminer continua
    # falando (reclamando), e o stdout vivo faria "blind" parecer mineracao.
    if unreachable:
        alvo = "node" if cfg["mode"] == "solo" else "pool"
        return "target_down", "%s fora - nao esta minerando" % alvo
    if err:
        # A API pode morrer com o processo minerando muito bem: o stdout
        # continua saindo. Chamar isso de "producao parada" e mentira - e foi
        # exatamente o que o painel fez por nove horas em 29/09/2026, com o
        # miner a 10,5 MH/s do outro lado. Sem medicao nao e sem producao.
        last_log = mstate.get("last_log_at") or 0
        if last_log and (time.time() - last_log) < LOG_QUIET_SECONDS:
            return "blind", "minerando - sem telemetria"
        return "miner_down", "miner sem responder"
    return "mining", "minerando (solo)" if cfg["mode"] == "solo" else "minerando"


def stats_payload():
    cfg = appconfig.load()
    mstate = miner.state()
    status, label = build_status()
    with _lock:
        current = dict(_current)
        target = dict(_target)
        err = _miner_error
        rescues = _rescues
    payload = {
        "status": status,
        "statusLabel": label,
        "solo": cfg["mode"] == "solo",
        "paused": cfg["paused"],
        "current": current,
        "error": err,
        "target": target,
        "miner": {
            "running": mstate["running"],
            "reason": mstate["reason"],
            "startedAt": mstate["started_at"],
            "restarts": mstate["restarts"],
            "exitCode": mstate["exit_code"],
            "lastLogAt": mstate["last_log_at"],
            "rescues": rescues,
        },
        "log": mstate["log"],
        "serverTime": int(time.time()),
        "pollSeconds": POLL_SECONDS,
    }
    payload.update(nodeinfo.snapshot())
    return payload


class Handler(BaseHTTPRequestHandler):
    server_version = "nerdminer"
    protocol_version = "HTTP/1.1"
    # Com keep-alive, uma conexao ociosa segura a thread que a atende. Sem
    # timeout, um navegador esquecido aberto (ou varios) prenderia threads
    # pra sempre. 30s fecha a conexao ociosa e libera a thread; o navegador
    # reabre transparentemente no proximo poll.
    timeout = 30

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code, obj):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("Content-Length invalido")
        if length > MAX_BODY:
            raise ValueError("corpo grande demais")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8", "replace"))

    def do_GET(self):
        path = self.path.split("?")[0]
        query = self.path.split("?")[1] if "?" in self.path else ""
        if path == "/api/stats":
            return self._json(200, stats_payload())
        if path == "/api/history":
            rng = "1h"
            for part in query.split("&"):
                if part.startswith("range="):
                    rng = part[6:]
            snap = history.snapshot(rng)
            if snap is None:
                return self._json(400, {"error": "range invalido"})
            return self._json(200, snap)
        if path == "/api/config":
            return self._json(200, appconfig.public())
        if path == "/healthz":
            return self._send(200, b"ok", "text/plain; charset=utf-8")
        if path in ("/", "/index.html"):
            path = "/index.html"
        asset = _assets.get(path)
        if asset:
            return self._send(200, asset[0], asset[1])
        self._json(404, {"error": "nao encontrado"})

    def do_POST(self):
        path = self.path.split("?")[0]
        # Exigir JSON barra POST de formulario cross-site.
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if ctype != "application/json":
            return self._json(415, {"error": "use Content-Type: application/json"})
        try:
            body = self._body()
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})

        if path == "/api/config":
            try:
                appconfig.save(body)
            except appconfig.ConfigError as exc:
                return self._json(400, {"error": str(exc)})
            except OSError as exc:
                return self._json(500, {"error": "nao consegui gravar: %s" % exc})
            threading.Thread(target=miner.apply_config, daemon=True).start()
            return self._json(200, {"ok": True, "config": appconfig.public()})

        if path == "/api/mining/pause" or path == "/api/mining/resume":
            paused = path.endswith("pause")
            try:
                appconfig.save({"paused": paused})
            except appconfig.ConfigError as exc:
                return self._json(400, {"error": str(exc)})
            threading.Thread(target=miner.apply_config, daemon=True).start()
            return self._json(200, {"ok": True, "paused": paused})

        if path == "/api/restart":
            threading.Thread(target=miner.apply_config, daemon=True).start()
            return self._json(200, {"ok": True})

        self._json(404, {"error": "nao encontrado"})

    def log_message(self, *args):
        pass


def drop_privileges():
    """Ajusta o dono de /data e abaixa privilegios.

    O Umbrel monta ${APP_DATA_DIR}/data em /data. Quando esse diretorio ainda
    nao existe no host, quem o cria e o Docker - como root:root. O chown feito
    no Dockerfile nao ajuda: o bind-mount cobre o diretorio da imagem, e a
    dona que vale e a do host. Sem isto, gravar o config.json da
    "Permission denied".

    Entao subimos como root so pra corrigir o dono e caimos pra uid 1000
    imediatamente - antes de abrir socket, thread ou processo filho, pra que
    nada (nem o cpuminer) rode com privilegio.
    """
    if os.geteuid() != 0:
        return
    uid = int(os.environ.get("APP_UID", "1000"))
    gid = int(os.environ.get("APP_GID", "1000"))
    try:
        os.makedirs(appconfig.CONFIG_DIR, exist_ok=True)
        os.chown(appconfig.CONFIG_DIR, uid, gid)
        for name in os.listdir(appconfig.CONFIG_DIR):
            try:
                os.chown(os.path.join(appconfig.CONFIG_DIR, name), uid, gid)
            except OSError:
                pass
    except OSError as exc:
        print("aviso: nao consegui ajustar o dono de %s (%s)"
              % (appconfig.CONFIG_DIR, exc), flush=True)
    try:
        os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)
        print("privilegios reduzidos para uid=%d gid=%d" % (uid, gid), flush=True)
    except OSError as exc:
        print("aviso: segui como root, nao consegui abaixar privilegios (%s)"
              % exc, flush=True)


def main():
    drop_privileges()
    load_assets()
    appconfig.load()

    def handle_signal(signum, frame):
        print("sinal %s: encerrando" % signum, flush=True)
        _stop.set()
        miner.shutdown()
        os._exit(0)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    miner.apply_config()
    for target in (miner.supervise, miner.throttle_forever, poll_miner, poll_target):
        threading.Thread(target=target, daemon=True).start()
    threading.Thread(target=nodeinfo.poll_forever, args=(_stop,),
                     daemon=True).start()

    server = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    server.daemon_threads = True     # nenhuma thread de request sobrevive
    print("nerdminer na porta %d" % LISTEN_PORT, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
