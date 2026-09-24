#!/usr/bin/env python3
"""Painel web do NerdMiner.

Le a API do cpuminer (TCP, comando "summary" -> string chave=valor terminada
em "|"), sonda a pool por conta propria, guarda um historico em memoria e
serve um dashboard HTML.

So biblioteca padrao - sem dependencias pra instalar.
"""

import json
import os
import socket
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Lista de nomes a tentar, separados por virgula. Na rede do Umbrel os
# containers se enxergam pelo nome completo (<app-id>_<servico>_1) - o mesmo
# formato usado no APP_HOST do app_proxy; o nome curto do servico ("miner"),
# que funciona no docker compose puro, nao resolve. A lista cobre os dois, e
# tambem a nomenclatura com hifen que o Compose v2 usa.
MINER_HOSTS = [h.strip() for h in
               os.environ.get("MINER_HOST", "miner").split(",") if h.strip()]
MINER_PORT = int(os.environ.get("MINER_PORT", "4048"))
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8080"))
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "10"))
# 720 pontos a cada 10s = 2 horas de historico
HISTORY_POINTS = int(os.environ.get("HISTORY_POINTS", "720"))

# A API do cpuminer responde mesmo sem pool nenhuma do outro lado: ela diz se
# o MINER esta vivo, nao se ele esta minerando. Por isso o painel sonda a pool
# por conta propria - e a unica fonte de verdade sobre "tem alguem pra receber
# meus shares".
POOL_URL = os.environ.get("POOL_URL", "").strip()
POOL_CHECK_SECONDS = int(os.environ.get("POOL_CHECK_SECONDS", "30"))
# Conectado mas sem share aceita por muito tempo = falha silenciosa da pool,
# que a sondagem de TCP sozinha nao pega.
SHARE_STALL_SECONDS = int(os.environ.get("SHARE_STALL_SECONDS", "900"))

HERE = os.path.dirname(os.path.abspath(__file__))

_lock = threading.Lock()
_current = {}
_error = "aguardando a primeira leitura"
_history = deque(maxlen=HISTORY_POINTS)
_host_ok = None   # nome que respondeu da ultima vez
_raw = None       # ultima resposta crua, exposta em /api/stats pra diagnostico
_acc_last = None       # ultimo valor de ACC visto
_acc_changed_at = None  # quando ACC mudou pela ultima vez


def parse_pool_url(url):
    """stratum+tcp://public-pool.io:21496 -> (public-pool.io, 21496)"""
    if not url:
        return None, None
    rest = url.split("://", 1)[-1].split("/", 1)[0]
    if ":" not in rest:
        return rest or None, None
    host, _, port = rest.rpartition(":")
    try:
        return host or None, int(port)
    except ValueError:
        return rest or None, None


POOL_HOST, POOL_PORT = parse_pool_url(POOL_URL)
POOL_HOST = os.environ.get("POOL_HOST", "").strip() or POOL_HOST
_env_port = os.environ.get("POOL_PORT", "").strip()
POOL_PORT = int(_env_port) if _env_port else POOL_PORT

# Modo solo (GBT direto no proprio node) nao tem shares: o cpuminer so submete
# quando acha um BLOCO, entao ACC fica 0 pra sempre e a deteccao de share
# estagnada dispararia falso alarme todo dia. O esquema da URL distingue os
# dois modos, do mesmo jeito que o cpuminer faz internamente:
#   stratum+tcp://...  -> pool (tem shares)
#   http://...         -> getblocktemplate no node (nao tem shares)
SOLO_MODE = POOL_URL.lower().startswith(("http://", "https://"))

_pool = {"url": POOL_URL or None, "host": POOL_HOST, "port": POOL_PORT,
         "reachable": None, "checkedAt": None, "lastOkAt": None,
         "detail": "sonda ainda nao rodou"}


def _talk(host, command):
    with socket.create_connection((host, MINER_PORT), timeout=5) as sock:
        sock.sendall(command.encode() + b"\n")
        chunks = []
        while True:
            data = sock.recv(4096)
            if not data:
                break
            chunks.append(data)
            if b"|" in data:  # a resposta termina em "|"
                break
    return b"".join(chunks).decode("utf-8", "replace")


def ask(command="summary"):
    """Fala com a API do cpuminer, testando cada nome ate um responder."""
    global _host_ok
    candidates = [_host_ok] if _host_ok else MINER_HOSTS
    failures = []
    for host in candidates:
        try:
            answer = _talk(host, command)
            _host_ok = host
            return answer
        except OSError as exc:
            failures.append("%s (%s)" % (host, exc))
    # o nome que funcionava parou: na proxima volta testa a lista toda
    _host_ok = None
    raise OSError("nenhum nome respondeu: " + "; ".join(failures))


def parse(raw):
    """NAME=cpuminer-opt;VER=26.1;... -> dict"""
    fields = {}
    for pair in raw.split("|")[0].split(";"):
        if "=" in pair:
            key, value = pair.split("=", 1)
            fields[key] = value
    return fields


def probe_pool():
    """Abre um TCP na pool. Nao fala stratum - so responde se tem alguem la."""
    if not POOL_HOST or not POOL_PORT:
        return None, "pool nao configurada (defina POOL_URL)"
    try:
        with socket.create_connection((POOL_HOST, POOL_PORT), timeout=8):
            return True, "conexao aceita"
    except OSError as exc:
        return False, str(exc)


def poll_pool_forever():
    while True:
        reachable, detail = probe_pool()
        now = int(time.time())
        with _lock:
            _pool["reachable"] = reachable
            _pool["detail"] = detail
            _pool["checkedAt"] = now
            if reachable:
                _pool["lastOkAt"] = now
        time.sleep(POOL_CHECK_SECONDS)


def poll_forever():
    global _current, _error, _raw, _acc_last, _acc_changed_at
    while True:
        try:
            answer = ask()
            fields = parse(answer)
            if not fields:
                raise ValueError("resposta vazia da API do miner")
            now = int(time.time())
            with _lock:
                _current = fields
                _raw = answer.strip()
                _error = None

                acc = fields.get("ACC")
                if acc != _acc_last:
                    _acc_last = acc
                    _acc_changed_at = now
                elif _acc_changed_at is None:
                    _acc_changed_at = now

                # Sem pool nao ha trabalho, entao nao ha producao. O cpuminer
                # mantem o ultimo hashrate calculado na API, e gravar esse
                # numero desenharia uma linha reta durante a queda - exatamente
                # a informacao errada. Zero e o valor honesto.
                rate = float(fields.get("HS", 0) or 0)
                if _pool["reachable"] is False:
                    rate = 0.0
                _history.append([now, rate])
        except Exception as exc:  # miner reiniciando, API fora, etc.
            with _lock:
                _error = "%s: %s" % (type(exc).__name__, exc)
                _history.append([int(time.time()), 0.0])
        time.sleep(POLL_SECONDS)


def build_status():
    """Deriva o estado real. Chamar com _lock ja adquirido."""
    if _error:
        return "miner_down", "sem contato com o miner"
    if _pool["reachable"] is False:
        return ("pool_down", "node fora - nao esta minerando" if SOLO_MODE
                else "pool fora - nao esta minerando")
    # Em solo nao ha shares, entao a deteccao de estagnacao nao se aplica.
    # SHARE_STALL_SECONDS <= 0 tambem desliga, explicitamente.
    if not SOLO_MODE and SHARE_STALL_SECONDS > 0:
        stalled = (int(time.time()) - _acc_changed_at
                   if _acc_changed_at is not None else None)
        if stalled is not None and stalled > SHARE_STALL_SECONDS:
            return "no_shares", "conectado, mas sem share aceita"
    return "mining", "minerando (solo)" if SOLO_MODE else "minerando"


class Handler(BaseHTTPRequestHandler):
    server_version = "nerdminer-ui"

    def _send(self, code, body, content_type):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/stats":
            with _lock:
                status, label = build_status()
                stalled = (int(time.time()) - _acc_changed_at
                           if _acc_changed_at is not None else None)
                payload = {
                    "current": dict(_current),
                    "history": list(_history),
                    "error": _error,
                    "status": status,
                    "statusLabel": label,
                    "solo": SOLO_MODE,
                    "pool": dict(_pool),
                    "sharesStalledSeconds": stalled,
                    "shareStallLimit": SHARE_STALL_SECONDS,
                    "pollSeconds": POLL_SECONDS,
                    "serverTime": int(time.time()),
                    "minerHost": _host_ok,
                    "raw": _raw,
                }
            self._send(200, json.dumps(payload).encode(), "application/json")
        elif path in ("/", "/index.html"):
            try:
                with open(os.path.join(HERE, "index.html"), "rb") as handle:
                    self._send(200, handle.read(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, b"index.html nao encontrado", "text/plain")
        elif path == "/healthz":
            self._send(200, b"ok", "text/plain; charset=utf-8")
        else:
            self._send(404, b"nao encontrado", "text/plain; charset=utf-8")

    def log_message(self, *args):
        pass  # sem log de acesso


def main():
    threading.Thread(target=poll_forever, daemon=True).start()
    threading.Thread(target=poll_pool_forever, daemon=True).start()
    print("nerdminer-ui na porta %d, tentando %s:%d a cada %ds; %s %s"
          % (LISTEN_PORT, "/".join(MINER_HOSTS), MINER_PORT, POLL_SECONDS,
             "node (solo)" if SOLO_MODE else "pool",
             ("%s:%s" % (POOL_HOST, POOL_PORT)) if POOL_HOST
             else "nao configurada"),
          flush=True)
    ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
