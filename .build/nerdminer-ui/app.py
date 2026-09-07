#!/usr/bin/env python3
"""Painel web do NerdMiner.

Le a API do cpuminer (TCP, comando "summary" -> string chave=valor terminada
em "|"), guarda um historico em memoria e serve um dashboard HTML.

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

HERE = os.path.dirname(os.path.abspath(__file__))

_lock = threading.Lock()
_current = {}
_error = "aguardando a primeira leitura"
_history = deque(maxlen=HISTORY_POINTS)
_host_ok = None  # nome que respondeu da ultima vez


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
    """'NAME=cpuminer-opt;VER=26.1;...|' -> dict"""
    fields = {}
    for pair in raw.split("|")[0].split(";"):
        if "=" in pair:
            key, value = pair.split("=", 1)
            fields[key] = value
    return fields


def poll_forever():
    global _current, _error
    while True:
        try:
            fields = parse(ask())
            if not fields:
                raise ValueError("resposta vazia da API do miner")
            with _lock:
                _current = fields
                _error = None
                _history.append([int(time.time()), float(fields.get("HS", 0) or 0)])
        except Exception as exc:  # miner reiniciando, API fora, etc.
            with _lock:
                _error = "%s: %s" % (type(exc).__name__, exc)
        time.sleep(POLL_SECONDS)


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
                payload = {
                    "current": dict(_current),
                    "history": list(_history),
                    "error": _error,
                    "pollSeconds": POLL_SECONDS,
                    "serverTime": int(time.time()),
                    "minerHost": _host_ok,
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
    server = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    print("nerdminer-ui na porta %d, tentando %s:%d a cada %ds"
          % (LISTEN_PORT, "/".join(MINER_HOSTS), MINER_PORT, POLL_SECONDS),
          flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
