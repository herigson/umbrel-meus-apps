#!/usr/bin/env python3
"""Historico do desvio do relogio e do atraso de ida e volta.

Quatro resolucoes, uma por botao da tela de Historico:

   1h -> 120 pontos de 30s
  12h -> 144 pontos de 5min  (media)
  24h -> 144 pontos de 10min (media)
   7d -> 168 pontos de 1h    (media)

Todo buffer e um deque com maxlen e os baldes de media guardam soma+contagem
em vez de acumular listas - sao 576 pontos no total e a memoria fica fixa
desde o primeiro minuto, sem caminho que cresca sem limite.

Semantica dos valores, a mesma dos outros paineis desta loja:
  None -> nao estavamos medindo (chronyd parado, ou sem fonte valida).
          A linha QUEBRA no grafico.
  0.0  -> medimos e deu zero.
Sao fatos diferentes e precisam de desenhos diferentes. Desenhar zero no
lugar de "nao sei" e inventar um numero que ninguem mediu.
"""

import threading
import time
from collections import deque

RANGES = {
    "1h":  {"interval": 30,   "points": 120},
    "12h": {"interval": 300,  "points": 144},
    "24h": {"interval": 600,  "points": 144},
    "7d":  {"interval": 3600, "points": 168},
}

_lock = threading.Lock()
_series = {name: deque(maxlen=spec["points"]) for name, spec in RANGES.items()}
_bucket = {name: {"off_sum": 0.0, "off_n": 0, "rtt_sum": 0.0, "rtt_n": 0,
                  "seen": 0, "start": 0}
           for name in RANGES}


def _flush(name, now):
    spec = RANGES[name]
    b = _bucket[name]
    if b["start"] == 0:
        b["start"] = now - (now % spec["interval"])
        return
    if now - b["start"] < spec["interval"]:
        return
    if b["seen"] == 0:
        offset = None
        rtt = None
    else:
        offset = b["off_sum"] / b["off_n"] if b["off_n"] else None
        rtt = b["rtt_sum"] / b["rtt_n"] if b["rtt_n"] else None
    _series[name].append([b["start"], offset, rtt])
    b.update(off_sum=0.0, off_n=0, rtt_sum=0.0, rtt_n=0, seen=0,
             start=now - (now % spec["interval"]))


def add(offset, rtt, now=None):
    """offset e rtt em SEGUNDOS, ou None quando nao houve medicao."""
    now = int(now or time.time())
    with _lock:
        for name in RANGES:
            _flush(name, now)
            b = _bucket[name]
            if offset is None and rtt is None:
                continue
            b["seen"] += 1
            if offset is not None:
                b["off_sum"] += float(offset)
                b["off_n"] += 1
            if rtt is not None:
                b["rtt_sum"] += float(rtt)
                b["rtt_n"] += 1


def snapshot(name):
    """Pontos do periodo + as estatisticas que a tela de Historico mostra.

    As estatisticas saem dos pontos JA agregados, nao das amostras cruas:
    e o mesmo conjunto que o grafico desenha, entao o numero no card sempre
    bate com o que se ve na linha. Estatistica calculada sobre um conjunto
    diferente do desenhado e uma armadilha classica.
    """
    if name not in RANGES:
        return None
    with _lock:
        points = list(_series[name])

    offsets = [p[1] for p in points if p[1] is not None]
    rtts = [p[2] for p in points if p[2] is not None]
    stats = {
        "count": len(offsets),
        "min": min(offsets) if offsets else None,
        "max": max(offsets) if offsets else None,
        "mean": (sum(offsets) / len(offsets)) if offsets else None,
        "rttMean": (sum(rtts) / len(rtts)) if rtts else None,
    }
    if len(offsets) > 1:
        mean = stats["mean"]
        var = sum((v - mean) ** 2 for v in offsets) / (len(offsets) - 1)
        stats["stdDev"] = var ** 0.5
    else:
        stats["stdDev"] = None

    return {
        "range": name,
        "interval": RANGES[name]["interval"],
        "points": points,
        "stats": stats,
    }
