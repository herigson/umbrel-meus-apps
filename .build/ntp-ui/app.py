#!/usr/bin/env python3
"""Painel do servidor NTP.

Fala o proprio protocolo NTP (UDP 123) - o chrony da imagem cturra/ntp nao
expoe a porta de comando (323) fora do localhost, entao chronyc remoto esta
fora de questao.

Duas medidas, com significados diferentes:

* SERVIDOR LOCAL -> stratum, referencia, root delay/dispersion, ultimo sync.
  Diz se o chrony esta sincronizado e o quanto ele acha que erra.
* FONTES EXTERNAS -> offset do NOSSO relogio em relacao a elas. Esta e a
  medida que responde "a hora daqui esta certa?". Comparar o painel com o
  servidor local nao serviria: os dois containers usam o mesmo relogio do
  host, entao o offset daria sempre zero.

So biblioteca padrao.
"""

import datetime
import json
import os
import socket
import struct
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Fuso do relogio exibido no painel. O NTP em si e agnostico (trafega UTC);
# isto e so apresentacao.
if os.environ.get("TZ"):
    time.tzset()

# Na rede do Umbrel vale o nome completo do container; o nome curto do
# servico nao resolve. A lista e testada em ordem.
LOCAL_HOSTS = [h.strip() for h in
               os.environ.get("NTP_HOST", "server").split(",") if h.strip()]
REFERENCE_SERVERS = [s.strip() for s in
                     os.environ.get("REFERENCE_SERVERS",
                                    "a.st1.ntp.br,b.st1.ntp.br").split(",")
                     if s.strip()]
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8080"))
LOCAL_POLL = int(os.environ.get("LOCAL_POLL_SECONDS", "20"))
# Consulta as fontes publicas com parcimonia (o minimo educado e ~64s).
REFERENCE_POLL = int(os.environ.get("REFERENCE_POLL_SECONDS", "120"))
HISTORY_POINTS = int(os.environ.get("HISTORY_POINTS", "360"))

HERE = os.path.dirname(os.path.abspath(__file__))
NTP_EPOCH_DELTA = 2208988800  # segundos entre 1900 e 1970

_lock = threading.Lock()
_local = {"error": "aguardando a primeira leitura"}
_references = []
_history = deque(maxlen=HISTORY_POINTS)
_host_ok = None


def ntp_query(host, port=123, timeout=5):
    """Consulta um servidor NTP e devolve os campos do pacote + offset/delay."""
    packet = bytearray(48)
    packet[0] = 0x1B  # LI=0, VN=3, Mode=3 (client)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        t1 = time.time()
        sock.sendto(bytes(packet), (host, port))
        data, _ = sock.recvfrom(512)
        t4 = time.time()
    finally:
        sock.close()

    if len(data) < 48:
        raise ValueError("resposta NTP menor que 48 bytes")

    f = struct.unpack("!BBBb11I", data[:48])
    li_vn_mode, stratum, poll, precision = f[0], f[1], f[2], f[3]
    root_delay = f[4] / 65536.0
    root_disp = f[5] / 65536.0
    ref_id = f[6]
    ref_ts = f[7] + f[8] / 2 ** 32
    recv_ts = f[11] + f[12] / 2 ** 32
    xmit_ts = f[13] + f[14] / 2 ** 32

    t2 = recv_ts - NTP_EPOCH_DELTA
    t3 = xmit_ts - NTP_EPOCH_DELTA

    return {
        "host": host,
        "leap": li_vn_mode >> 6,
        "version": (li_vn_mode >> 3) & 0x7,
        "stratum": stratum,
        "poll": poll,
        "precision": precision,
        "rootDelay": root_delay,
        "rootDispersion": root_disp,
        "refId": format_ref_id(ref_id, stratum),
        "refTime": (ref_ts - NTP_EPOCH_DELTA) if ref_ts else 0,
        # Formulas classicas do NTP
        "offset": ((t2 - t1) + (t3 - t4)) / 2,
        "delay": (t4 - t1) - (t3 - t2),
    }


def clock_info():
    """Hora servida e fuso configurado, pro relogio do painel."""
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


def format_ref_id(value, stratum):
    """Stratum 1 traz 4 caracteres (GPS, PPS...); acima disso, um IPv4."""
    raw = struct.pack("!I", value)
    if stratum <= 1:
        text = raw.decode("ascii", "ignore").strip("\x00").strip()
        return text or "—"
    return ".".join(str(b) for b in raw)


def read_local():
    """Consulta o servidor local, testando cada nome ate um responder."""
    global _host_ok
    candidates = [_host_ok] if _host_ok else LOCAL_HOSTS
    failures = []
    for host in candidates:
        try:
            result = ntp_query(host)
            _host_ok = host
            return result
        except (OSError, ValueError) as exc:
            failures.append("%s (%s)" % (host, exc))
    _host_ok = None
    raise OSError("nenhum nome respondeu: " + "; ".join(failures))


def poll_local():
    global _local
    while True:
        try:
            data = read_local()
            data["error"] = None
        except Exception as exc:
            data = {"error": "%s: %s" % (type(exc).__name__, exc)}
        with _lock:
            _local = data
        time.sleep(LOCAL_POLL)


def poll_references():
    global _references
    while True:
        results = []
        for server in REFERENCE_SERVERS:
            try:
                results.append(ntp_query(server))
            except Exception as exc:
                results.append({"host": server,
                                "error": "%s: %s" % (type(exc).__name__, exc)})
        good = [r for r in results if not r.get("error")]
        with _lock:
            _references = results
            if good:
                # Mediana dos offsets: uma fonte lenta nao distorce o grafico.
                offsets = sorted(r["offset"] for r in good)
                median = offsets[len(offsets) // 2]
                _history.append([int(time.time()), median])
        time.sleep(REFERENCE_POLL)


class Handler(BaseHTTPRequestHandler):
    server_version = "ntp-ui"

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
                    "local": dict(_local),
                    "references": list(_references),
                    "history": list(_history),
                    "clock": clock_info(),
                    "referencePoll": REFERENCE_POLL,
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
        pass


def main():
    threading.Thread(target=poll_local, daemon=True).start()
    threading.Thread(target=poll_references, daemon=True).start()
    print("ntp-ui na porta %d | local: %s | referencias: %s"
          % (LISTEN_PORT, "/".join(LOCAL_HOSTS), ", ".join(REFERENCE_SERVERS)),
          flush=True)
    ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
