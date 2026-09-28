#!/usr/bin/env python3
"""Supervisor do cpuminer: sobe, derruba e reinicia o processo.

Existe porque o cpuminer le TUDO por linha de comando - nao ha nada que ele
releia em runtime. Mudar endereco, threads ou scantime significa reiniciar o
processo com argumentos novos, e e isso que permite configurar pela UI.

Cuidados de memoria (o vazamento de 1 GiB/h do proprio cpuminer ensinou):

* um unico Popen vivo por vez; nunca guardamos os antigos;
* todo processo terminado e REAPED com wait(), senao vira zumbi;
* o stdout e drenado por uma thread - se a gente usasse PIPE sem ler, o
  buffer encheria e o cpuminer TRAVARIA na primeira escrita;
* as linhas de log vao pra um deque com maxlen, entao o historico e limitado
  por construcao, e sao reimpressas pra "docker logs" continuar funcionando.
"""

import os
import signal
import socket
import subprocess
import threading
import time
from collections import deque

import appconfig

API_HOST = "127.0.0.1"
API_PORT = 4048

NODE_IP = os.environ.get("APP_BITCOIN_NODE_IP", "").strip()
NODE_RPC_PORT = os.environ.get("APP_BITCOIN_RPC_PORT", "8332").strip()
NODE_RPC_USER = os.environ.get("APP_BITCOIN_RPC_USER", "").strip()
NODE_RPC_PASS = os.environ.get("APP_BITCOIN_RPC_PASS", "")

CPUMINER = os.environ.get("CPUMINER_BIN", "/usr/local/bin/cpuminer")
LOG_LINES = 200
RESTART_DELAY = 5
# Periodo do ciclo de trabalho do throttle de CPU (ver throttle_forever).
THROTTLE_PERIOD = 0.25

_lock = threading.RLock()
_proc = None
_log = deque(maxlen=LOG_LINES)
_state = {"running": False, "reason": "ainda nao iniciado", "started_at": None,
          "restarts": 0, "exit_code": None}
_cpu_percent = 100
_stop = threading.Event()
_wanted = threading.Event()   # setado = deve estar rodando


def build_args(cfg):
    """Monta os argumentos. Devolve (args, motivo_de_nao_subir)."""
    if cfg["paused"]:
        return None, "pausado pelo usuario"
    if not cfg["btc_address"]:
        return None, "endereco Bitcoin nao configurado"

    args = [CPUMINER, "-a", "sha256d"]

    if cfg["mode"] == "solo":
        if not NODE_IP:
            return None, "Bitcoin Node nao disponivel (APP_BITCOIN_NODE_IP vazio)"
        args += ["-o", "http://%s:%s" % (NODE_IP, NODE_RPC_PORT),
                 "-u", NODE_RPC_USER,
                 "-p", NODE_RPC_PASS,
                 "--coinbase-addr=%s" % cfg["btc_address"]]
    else:
        args += ["-o", cfg["pool_url"],
                 "-u", "%s.umbrel" % cfg["btc_address"],
                 "-p", cfg["pool_password"]]

    args += ["-t", str(cfg["threads"]),
             "-s", str(cfg["scantime"])]

    # Governador termico do proprio cpuminer: acima do limite as threads
    # pausam ("CPU temp too high: XC max Y, waiting...") e voltam ao esfriar.
    # E o unico jeito de controlar temperatura de dentro do container - o
    # teto de CPU e cgroup, e um container nao altera o proprio cgroup.
    if cfg.get("max_temp"):
        args += ["--max-temp=%d" % int(cfg["max_temp"])]

    # Mesmo container que o painel, entao loopback basta - a API nao fica
    # exposta nem pra rede interna dos apps.
    args += ["--api-bind", "%s:%d" % (API_HOST, API_PORT)]
    return args, None


def _drain(stream):
    """Le o stdout do cpuminer ate o EOF. Nunca pode parar de ler."""
    try:
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip("\n")
            _log.append(line)          # deque com maxlen: limitado
            print(line, flush=True)    # mantem o "docker logs" util
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _spawn(cfg):
    """Sobe o processo. Chamar com _lock adquirido."""
    global _proc
    args, reason = build_args(cfg)
    if args is None:
        _state.update(running=False, reason=reason, started_at=None)
        return

    redacted = list(args)
    for i, item in enumerate(redacted):
        if i and redacted[i - 1] == "-p":
            redacted[i] = "***"
    print("iniciando: %s" % " ".join(redacted), flush=True)

    try:
        _proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=0, close_fds=True)
    except OSError as exc:
        _state.update(running=False, reason="falha ao iniciar: %s" % exc,
                      started_at=None)
        _proc = None
        return

    threading.Thread(target=_drain, args=(_proc.stdout,), daemon=True).start()
    _state.update(running=True, reason="rodando", started_at=int(time.time()),
                  exit_code=None)


def _kill(timeout=10):
    """Derruba e REAPA o processo. Chamar com _lock adquirido."""
    global _proc
    proc = _proc
    _proc = None
    if proc is None:
        return
    try:
        # ARMADILHA: um processo parado por SIGSTOP NAO responde a SIGTERM -
        # ele so processa o sinal quando voltar a rodar. Se o throttle o
        # tiver pausado neste instante, terminate() nao mata nada e o
        # wait() estoura o timeout. SIGCONT primeiro, sempre.
        proc.send_signal(signal.SIGCONT)
    except OSError:
        pass
    try:
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=timeout)   # sem este wait, vira zumbi
    except OSError:
        pass
    _state.update(running=False, started_at=None)


def apply_config():
    """Reinicia o miner com a config atual. Idempotente."""
    global _cpu_percent
    with _lock:
        _kill()
        cfg = appconfig.load()
        _cpu_percent = int(cfg.get("cpu_percent", 100))
        if cfg["paused"] or not cfg["btc_address"]:
            _wanted.clear()
            args, reason = build_args(cfg)
            _state.update(running=False, reason=reason or "parado")
            return
        _wanted.set()
        _spawn(cfg)


def throttle_forever():
    """Limita o uso de CPU por ciclo de trabalho: SIGCONT -> espera ->
    SIGSTOP -> espera, num periodo curto.

    Existe porque o teto de CPU do container e cgroup, e um container nao
    altera o proprio cgroup - entao nao ha como expor isso na UI de outro
    jeito. E o mesmo principio da ferramenta "cpulimit".

    So RESTRINGE dentro do teto do compose: se la diz 0.5 de nucleo, 100%
    aqui continua sendo 0.5. Para passar disso, o compose e que muda.

    O periodo e curto (250ms) pra nao criar engasgo visivel, e longo o
    bastante pra nao virar tempestade de sinais. Durante a pausa a API do
    cpuminer tambem para de responder, mas a conexao fica na fila do kernel
    e e aceita ao retomar - o timeout do painel e de 5s, folgado.
    """
    while not _stop.is_set():
        with _lock:
            proc = _proc
            pct = _cpu_percent
        if proc is None or pct >= 100 or proc.poll() is not None:
            _stop.wait(1)
            continue
        on = THROTTLE_PERIOD * (pct / 100.0)
        off = THROTTLE_PERIOD - on
        try:
            proc.send_signal(signal.SIGCONT)
            time.sleep(on)
            proc.send_signal(signal.SIGSTOP)
            time.sleep(off)
        except (OSError, ValueError):
            # processo morreu no meio do ciclo; o supervisor cuida
            _stop.wait(1)
    # Ao sair, nunca deixar o processo parado.
    with _lock:
        proc = _proc
    if proc is not None:
        try:
            proc.send_signal(signal.SIGCONT)
        except OSError:
            pass


def supervise():
    """Loop: se era pra estar rodando e morreu, sobe de novo."""
    while not _stop.is_set():
        with _lock:
            proc = _proc
            should_run = _wanted.is_set()
            if should_run and proc is not None:
                code = proc.poll()
                if code is not None:
                    proc.wait()          # reap
                    _proc = None
                    _state["exit_code"] = code
                    _state["restarts"] += 1
                    _state.update(running=False,
                                  reason="saiu com codigo %s; reiniciando" % code)
            elif should_run and proc is None and not _state["running"]:
                _spawn(appconfig.load())
        _stop.wait(RESTART_DELAY)


def shutdown():
    _stop.set()
    with _lock:
        _wanted.clear()
        _kill()


def state():
    with _lock:
        out = dict(_state)
        out["log"] = list(_log)[-40:]
        return out


def ask(command="summary", timeout=5):
    """Fala com a API texto do cpuminer. Resposta termina em '|'."""
    with socket.create_connection((API_HOST, API_PORT), timeout=timeout) as sock:
        sock.sendall(command.encode() + b"\n")
        chunks = []
        total = 0
        while True:
            data = sock.recv(4096)
            if not data:
                break
            chunks.append(data)
            total += len(data)
            if b"|" in data or total > 65536:   # teto: resposta nao cresce
                break
    return b"".join(chunks).decode("utf-8", "replace")


def parse(raw):
    fields = {}
    for pair in raw.split("|")[0].split(";"):
        if "=" in pair:
            key, value = pair.split("=", 1)
            fields[key] = value
    return fields
