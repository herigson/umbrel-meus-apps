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

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, "web")

_stop = threading.Event()
_lock = threading.Lock()
_current = {}
_miner_error = "aguardando a primeira leitura"
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


def poll_miner():
    global _current, _miner_error
    while not _stop.is_set():
        running = miner.state()["running"]
        if not running:
            with _lock:
                _current = {}
                _miner_error = None
            history.add(None)            # parado: nao estavamos medindo
        else:
            try:
                fields = miner.parse(miner.ask())
                if not fields:
                    raise ValueError("resposta vazia da API do miner")
                with _lock:
                    _current = fields
                    _miner_error = None
                rate = float(fields.get("HS", 0) or 0)
                with _lock:
                    unreachable = _target["reachable"] is False
                history.add(0.0 if unreachable else rate)
            except Exception as exc:
                with _lock:
                    _miner_error = "%s: %s" % (type(exc).__name__, exc)
                # Vivo mas sem responder a API: ainda subindo, ou travado.
                history.add(None)
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
    if err:
        return "miner_down", "miner sem responder"
    if unreachable:
        alvo = "node" if cfg["mode"] == "solo" else "pool"
        return "target_down", "%s fora - nao esta minerando" % alvo
    return "mining", "minerando (solo)" if cfg["mode"] == "solo" else "minerando"


def stats_payload():
    cfg = appconfig.load()
    mstate = miner.state()
    status, label = build_status()
    with _lock:
        current = dict(_current)
        target = dict(_target)
        err = _miner_error
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


def main():
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
    for target in (miner.supervise, poll_miner, poll_target):
        threading.Thread(target=target, daemon=True).start()
    threading.Thread(target=nodeinfo.poll_forever, args=(_stop,),
                     daemon=True).start()

    server = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    server.daemon_threads = True     # nenhuma thread de request sobrevive
    print("nerdminer na porta %d" % LISTEN_PORT, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
