#!/usr/bin/env python3
"""Historico de hashrate em tres resolucoes, tudo limitado por construcao.

  1h -> 360 pontos de 10s
 24h -> 288 pontos de 5min (media)
  7d -> 168 pontos de 1h  (media)

Todo buffer e um deque com maxlen, e os baldes de media usam soma+contagem em
vez de acumular listas. Nao existe caminho em que isso cresca sem limite - a
memoria fica fixa desde o primeiro minuto.

Semantica dos valores:
  None -> nao estavamos medindo (miner parado/pausado). A linha quebra.
  0.0  -> o miner estava vivo mas sem produzir (alvo fora). Queda visivel.
Sao coisas diferentes e o grafico mostra as duas de forma diferente.
"""

import threading
import time
from collections import deque

RANGES = {
    "1h":  {"interval": 10,   "points": 360},
    "24h": {"interval": 300,  "points": 288},
    "7d":  {"interval": 3600, "points": 168},
}
MAX_EVENTS = 50

_lock = threading.Lock()
_series = {name: deque(maxlen=spec["points"]) for name, spec in RANGES.items()}
# balde em aberto de cada resolucao agregada: soma, contagem e inicio
_bucket = {name: {"sum": 0.0, "count": 0, "seen": 0, "start": 0}
           for name in ("24h", "7d")}
_events = deque(maxlen=MAX_EVENTS)
_outage = {"since": None}


def _flush(name, now):
    spec = RANGES[name]
    bucket = _bucket[name]
    if bucket["start"] == 0:
        bucket["start"] = now - (now % spec["interval"])
        return
    if now - bucket["start"] < spec["interval"]:
        return
    if bucket["seen"] == 0:
        value = None
    elif bucket["count"] == 0:
        value = 0.0
    else:
        value = bucket["sum"] / bucket["count"]
    _series[name].append([bucket["start"], value])
    bucket["sum"] = 0.0
    bucket["count"] = 0
    bucket["seen"] = 0
    bucket["start"] = now - (now % spec["interval"])


def add(value, now=None):
    """value: float (produzindo), 0.0 (vivo sem produzir) ou None (parado)."""
    now = int(now or time.time())
    with _lock:
        _series["1h"].append([now, value])
        for name in ("24h", "7d"):
            _flush(name, now)
            bucket = _bucket[name]
            if value is not None:
                bucket["seen"] += 1
                if value > 0:
                    bucket["sum"] += float(value)
                    bucket["count"] += 1


def mark_outage(active, now=None):
    """Abre/fecha uma queda. So grava o evento quando ela termina."""
    now = int(now or time.time())
    with _lock:
        if active and _outage["since"] is None:
            _outage["since"] = now
        elif not active and _outage["since"] is not None:
            started = _outage["since"]
            _outage["since"] = None
            duration = max(0, now - started)
            if duration >= 30:   # ruido de um poll nao vira evento
                _events.append({"t": started, "type": "outage",
                                "duration_s": duration})


def snapshot(range_name="1h"):
    spec = RANGES.get(range_name)
    if spec is None:
        return None
    with _lock:
        points = list(_series[range_name])
        events = list(_events)
        pending = _outage["since"]
    return {
        "range": range_name,
        "interval_s": spec["interval"],
        "points": points,
        "events": events,
        "outageSince": pending,
    }
