#!/usr/bin/env python3
"""Servidor NTP + painel: processo principal.

Este processo e o PID 1 do container. Ele supervisiona o chronyd, le o
chronyc, guarda o historico e serve a interface.

Por que ele roda como root, diferente do painel do NerdMiner: a porta 123 e
privilegiada e precisa ser reaberta toda vez que o usuario aperta "Aplicar",
entao quem sobe o chronyd nao pode ter largado o privilegio antes. O processo
exposto a rede continua sem privilegio - o chronyd cai pro usuario _chrony
sozinho, pela diretiva "user" do conf gerado - e o painel so e alcancavel
pelo app_proxy autenticado do Umbrel. De quebra, 'chronyc clients' exige o
socket unix do chronyd, que so root e o grupo _chrony abrem.

So biblioteca padrao.
"""

import datetime
import json
import mimetypes
import os
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import appconfig
import chronyd
import history
import ntpclient

LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8080"))
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "15"))
CLIENTS_TTL = int(os.environ.get("CLIENTS_TTL", "30"))
MAX_BODY = 64 * 1024

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, "web")

_stop = threading.Event()
_lock = threading.Lock()
_tracking = {}
_sources = []
_stats = {}
_error = "aguardando a primeira leitura"
_precision = None

_assets = {}


def load_assets():
    """Os arquivos do front-end sao poucos e fixos: lidos uma vez na subida."""
    for name in sorted(os.listdir(WEB_DIR)):
        path = os.path.join(WEB_DIR, name)
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as handle:
            body = handle.read()
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith(("javascript", "json")):
            ctype += "; charset=utf-8"
        _assets["/" + name] = (body, ctype)
    chronyd.log("assets carregados: %s" % ", ".join(sorted(_assets)))


def apply_timezone(name):
    """O fuso e so apresentacao - o NTP sempre trafega UTC -, mas precisa
    valer pro processo inteiro pra que strftime e astimezone concordem."""
    os.environ["TZ"] = name
    try:
        time.tzset()
    except AttributeError:
        pass        # Windows nao tem tzset; aqui sempre e Linux.


def clock_info():
    now = datetime.datetime.now().astimezone()
    offset = now.utcoffset() or datetime.timedelta(0)
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    hours, minutes = divmod(abs(total) // 60, 60)
    return {
        "epoch": time.time(),
        "timezone": os.environ.get("TZ") or "UTC",
        "abbreviation": now.tzname() or "UTC",
        "utcOffset": "%s%02d:%02d" % (sign, hours, minutes),
    }


# --------------------------------------------------------------------------
# coleta
# --------------------------------------------------------------------------

def poll_chrony():
    global _tracking, _sources, _error
    while not _stop.is_set():
        if not chronyd.state()["running"]:
            with _lock:
                _tracking, _sources = {}, []
                _error = "chronyd parado"
            history.add(None, None)
        else:
            try:
                track = chronyd.tracking()
                srcs = chronyd.sources()
                with _lock:
                    _tracking, _sources, _error = track, srcs, None
                # Guardamos o offset do SISTEMA (o quanto o relogio local
                # esta adiantado/atrasado em relacao as fontes) e o root
                # delay, que e o caminho de ida e volta ate o stratum 1.
                # Sem referencia valida, o chrony reporta stratum 0 - e ai
                # os numeros existem mas nao significam nada, entao entra
                # None e a linha do grafico quebra.
                if track.get("stratum"):
                    history.add(track.get("systemOffset"),
                                track.get("rootDelay"))
                else:
                    history.add(None, None)
            except Exception as exc:
                with _lock:
                    _error = "%s: %s" % (type(exc).__name__, exc)
                history.add(None, None)
        _stop.wait(POLL_SECONDS)


def poll_serverstats():
    """serverstats e precisao mudam devagar; ler junto com o resto so gastaria
    processo a toa."""
    global _stats, _precision
    while not _stop.is_set():
        try:
            stats = chronyd.serverstats()
        except Exception:
            stats = {}
        precision = None
        try:
            # Perguntamos a hora ao nosso proprio servidor. Alem da precisao,
            # e a unica prova de que a 123/UDP esta mesmo respondendo - o
            # chronyc fala por outra porta e nao provaria isso.
            precision = ntpclient.query("127.0.0.1", timeout=3)["precision"]
        except Exception:
            pass
        with _lock:
            _stats = stats
            if precision is not None:
                _precision = precision
        _stop.wait(CLIENTS_TTL)


def build_status(cfg, track, srcs, err):
    """Deriva o estado geral. Devolve (chave, rotulo)."""
    dstate = chronyd.state()
    if not dstate["running"]:
        return "stopped", dstate["reason"] or "parado"
    if err:
        return "unknown", "sem resposta do chronyd"
    if not track.get("stratum"):
        return "syncing", "procurando uma fonte confiavel"
    if not [s for s in srcs if s["state"] == "sync"]:
        return "syncing", "sincronizando"
    if not cfg["serve"]:
        return "not_serving", "sincronizado, mas nao servindo a rede"
    return "synced", "sincronizado"


def stats_payload():
    cfg = appconfig.load()
    with _lock:
        track = dict(_tracking)
        srcs = [dict(s) for s in _sources]
        stats = dict(_stats)
        err = _error
        precision = _precision
    status, label = build_status(cfg, track, srcs, err)
    dstate = chronyd.state()
    active = [s for s in cfg["servers"] if s["enabled"]]
    return {
        "status": status,
        "statusLabel": label,
        "error": err,
        "clock": clock_info(),
        "tracking": track,
        "sources": srcs,
        "serverStats": stats,
        "precision": precision,
        "serving": cfg["serve"],
        "allowNetworks": cfg["allow_networks"],
        "pollExponent": cfg["poll_exponent"],
        "pollSeconds": 2 ** cfg["poll_exponent"],
        "configuredSources": len(cfg["servers"]),
        "enabledSources": len(active),
        "daemon": {
            "running": dstate["running"],
            "reason": dstate["reason"],
            "startedAt": dstate["started_at"],
            "restarts": dstate["restarts"],
            "exitCode": dstate["exit_code"],
        },
        "autoRefresh": cfg["auto_refresh"],
        "serverTime": int(time.time()),
        "refreshSeconds": POLL_SECONDS,
    }


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "ntp-panel"
    protocol_version = "HTTP/1.1"
    # Com keep-alive, uma conexao ociosa segura a thread que a atende. Sem
    # timeout, uma aba esquecida aberta prenderia a thread pra sempre.
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
        return json.loads(self.rfile.read(length).decode("utf-8", "replace"))

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/api/stats":
            return self._json(200, stats_payload())
        if path == "/api/history":
            rng = "12h"
            for part in query.split("&"):
                if part.startswith("range="):
                    rng = part[6:]
            snap = history.snapshot(rng)
            if snap is None:
                return self._json(400, {"error": "periodo invalido"})
            return self._json(200, snap)
        if path == "/api/clients":
            try:
                return self._json(200, {"clients": chronyd.clients()})
            except RuntimeError as exc:
                return self._json(200, {"clients": [], "error": str(exc)})
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
                cfg = appconfig.save(body)
            except appconfig.ConfigError as exc:
                return self._json(400, {"error": str(exc)})
            except OSError as exc:
                return self._json(500, {"error": "nao consegui gravar: %s" % exc})
            apply_timezone(cfg["timezone"])
            # O reinicio vai pra uma thread: a resposta sai na hora e a
            # interface mostra o progresso, em vez de segurar a conexao por
            # alguns segundos e parecer travada.
            threading.Thread(target=chronyd.apply_config, daemon=True).start()
            return self._json(200, {"ok": True, "config": appconfig.public()})

        if path == "/api/restart":
            threading.Thread(target=chronyd.apply_config, daemon=True).start()
            return self._json(200, {"ok": True})

        if path == "/api/test":
            host = str(body.get("host", "")).strip()
            if not host:
                return self._json(400, {"error": "informe um endereco"})
            try:
                host = appconfig.clean_host(host)
            except appconfig.ConfigError as exc:
                return self._json(400, {"error": str(exc)})
            try:
                result = ntpclient.query(host, timeout=5)
            except OSError as exc:
                return self._json(200, {"ok": False,
                                        "error": "sem resposta: %s" % exc})
            except ValueError as exc:
                return self._json(200, {"ok": False, "error": str(exc)})
            return self._json(200, {"ok": True, "result": result})

        self._json(404, {"error": "nao encontrado"})

    def log_message(self, *args):
        pass


def main():
    cfg = appconfig.load()
    apply_timezone(cfg["timezone"])
    load_assets()

    def handle_signal(signum, frame):
        chronyd.log("sinal %s: encerrando" % signum)
        _stop.set()
        chronyd.shutdown()
        os._exit(0)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    chronyd.apply_config()
    for target in (chronyd.supervise, poll_chrony, poll_serverstats):
        threading.Thread(target=target, daemon=True).start()

    server = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    server.daemon_threads = True
    chronyd.log("painel na porta %d" % LISTEN_PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
