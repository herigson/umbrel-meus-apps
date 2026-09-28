#!/usr/bin/env python3
"""Dados da rede Bitcoin lidos do SEU proprio node, via RPC.

O painel poderia buscar isso de uma API publica (mempool.space), mas o node
esta a um salto de distancia e ja tem a resposta. Assim o painel continua
funcionando sem internet e sem contar pra ninguem de fora o que voce olha.

Guarda so um dicionario pequeno em memoria, substituido a cada leitura - nada
acumula.
"""

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.request

NODE_IP = os.environ.get("APP_BITCOIN_NODE_IP", "").strip()
RPC_PORT = os.environ.get("APP_BITCOIN_RPC_PORT", "8332").strip()
RPC_USER = os.environ.get("APP_BITCOIN_RPC_USER", "").strip()
RPC_PASS = os.environ.get("APP_BITCOIN_RPC_PASS", "")

TTL = int(os.environ.get("NODE_INFO_TTL", "60"))
TIMEOUT = 8
HALVING_INTERVAL = 210000

_lock = threading.Lock()
_cache = {"data": None, "at": 0, "error": "ainda nao consultado"}


def _rpc(method, params=None):
    if not NODE_IP:
        raise RuntimeError("Bitcoin Node nao disponivel")
    url = "http://%s:%s/" % (NODE_IP, RPC_PORT)
    body = json.dumps({"jsonrpc": "1.0", "id": "nerdminer",
                       "method": method, "params": params or []}).encode()
    token = base64.b64encode(
        ("%s:%s" % (RPC_USER, RPC_PASS)).encode()).decode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Basic %s" % token)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        # Teto explicito: uma resposta de getblockchaininfo tem alguns KB.
        payload = json.loads(resp.read(1 << 20).decode("utf-8", "replace"))
    if payload.get("error"):
        raise RuntimeError(str(payload["error"]))
    return payload.get("result")


def fetch():
    """Le do node. Chamado so pelo poller, nunca por um handler HTTP."""
    chain = _rpc("getblockchaininfo")
    height = int(chain.get("blocks") or 0)
    difficulty = float(chain.get("difficulty") or 0.0)

    net_hashrate = None
    try:
        mining = _rpc("getmininginfo")
        net_hashrate = float(mining.get("networkhashps") or 0.0) or None
    except (RuntimeError, urllib.error.URLError, OSError, ValueError):
        # getmininginfo e opcional: sem ele o painel so esconde um campo.
        pass

    remaining = HALVING_INTERVAL - (height % HALVING_INTERVAL) if height else None

    return {
        "blockHeight": height or None,
        "difficulty": difficulty or None,
        "networkHashrate": net_hashrate,
        "halvingIn": remaining,
        "chain": chain.get("chain"),
        "ibd": bool(chain.get("initialblockdownload")),
        "updatedAt": int(time.time()),
    }


def poll_forever(stop_event):
    while not stop_event.is_set():
        try:
            data = fetch()
            with _lock:
                _cache["data"] = data      # substitui, nao acumula
                _cache["at"] = int(time.time())
                _cache["error"] = None
        except Exception as exc:           # node reiniciando, RPC fora, etc.
            with _lock:
                _cache["error"] = "%s: %s" % (type(exc).__name__, exc)
        stop_event.wait(TTL)


def snapshot():
    with _lock:
        data = dict(_cache["data"]) if _cache["data"] else None
        return {"network": data, "networkError": _cache["error"]}
