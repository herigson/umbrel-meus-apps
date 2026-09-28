#!/usr/bin/env python3
"""Configuracao persistente do NerdMiner.

Guarda o que o usuario edita pela UI em ${APP_DATA_DIR}/config.json. As
credenciais RPC do Bitcoin NAO ficam aqui - vem do ambiente (o Umbrel as
injeta a partir do exports.sh do app bitcoin), justamente pra senha nenhuma
ser gravada em disco por nos.

Escrita atomica (tmp + rename) pra um desligamento no meio da gravacao nao
deixar um JSON truncado, que impediria o app de subir na proxima vez.
"""

import json
import os
import re
import tempfile
import threading

CONFIG_DIR = os.environ.get("CONFIG_DIR", "/data")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

# Limites de validacao. Espelhados no front-end, mas quem manda e aqui: o
# navegador pode ser contornado, o servidor nao.
MIN_SCANTIME = 5
MAX_SCANTIME = 3600
MAX_THREADS = max(1, os.cpu_count() or 1)
# Abaixo de 45 C a maquina praticamente nunca mineraria; acima de 95 nao
# protege nada. 0 desliga o governador.
MIN_TEMP = 45
MAX_TEMP = 95

DEFAULTS = {
    "mode": "solo",              # "solo" (getblocktemplate) ou "pool" (stratum)
    "btc_address": "",           # vazio = miner nao sobe; a UI pede o endereco
    "threads": 1,
    "scantime": 60,              # 5s (padrao do cpuminer) fritava o node
    # Governador termico do proprio cpuminer (--max-temp): passou do limite,
    # as threads pausam ate esfriar. 0 = desligado. E o jeito de controlar
    # temperatura pela UI - o teto de CPU do compose um container nao muda.
    "max_temp": 0,
    "pool_url": "stratum+tcp://public-pool.io:21496",
    "pool_password": "x",
    "paused": False,
}

# bech32 (bc1/tb1/bcrt1) e base58 (1.../3...). Nao valida checksum - isso o
# cpuminer faz e recusa na inicializacao. Aqui so barramos erro obvio antes
# de reiniciar o processo a toa.
_BECH32 = re.compile(r"^(bc1|tb1|bcrt1)[02-9ac-hj-np-z]{6,87}$")
_BECH32_UPPER = re.compile(r"^(BC1|TB1|BCRT1)[02-9AC-HJ-NP-Z]{6,87}$")
_BASE58 = re.compile(r"^[13][1-9A-HJ-NP-Za-km-z]{25,39}$")

_lock = threading.Lock()
_cache = None


class ConfigError(ValueError):
    """Erro de validacao, com mensagem que vai direto pro usuario."""


def validate_address(addr):
    """bech32 nao pode misturar maiusculas e minusculas (BIP-173)."""
    if not addr:
        raise ConfigError("Informe o endereco Bitcoin que recebera a recompensa.")
    if _BECH32.match(addr) or _BECH32_UPPER.match(addr) or _BASE58.match(addr):
        return addr
    if addr.lower().startswith(("bc1", "tb1", "bcrt1")):
        raise ConfigError(
            "Endereco bech32 invalido. Use tudo em minusculas, sem misturar "
            "maiusculas (o padrao nao permite os dois no mesmo endereco).")
    raise ConfigError("Endereco Bitcoin invalido. Comece com bc1, 1 ou 3.")


def _validate(cfg):
    out = dict(DEFAULTS)

    mode = cfg.get("mode", out["mode"])
    if mode not in ("solo", "pool"):
        raise ConfigError("Modo deve ser 'solo' ou 'pool'.")
    out["mode"] = mode

    addr = (cfg.get("btc_address") or "").strip()
    if addr:
        validate_address(addr)
    out["btc_address"] = addr

    try:
        threads = int(cfg.get("threads", out["threads"]))
    except (TypeError, ValueError):
        raise ConfigError("Numero de threads invalido.")
    if not 1 <= threads <= MAX_THREADS:
        raise ConfigError("Threads deve estar entre 1 e %d." % MAX_THREADS)
    out["threads"] = threads

    try:
        scantime = int(cfg.get("scantime", out["scantime"]))
    except (TypeError, ValueError):
        raise ConfigError("Scantime invalido.")
    if not MIN_SCANTIME <= scantime <= MAX_SCANTIME:
        raise ConfigError("Scantime deve estar entre %d e %d segundos."
                          % (MIN_SCANTIME, MAX_SCANTIME))
    out["scantime"] = scantime

    try:
        max_temp = int(cfg.get("max_temp", out["max_temp"]))
    except (TypeError, ValueError):
        raise ConfigError("Temperatura maxima invalida.")
    if max_temp != 0 and not MIN_TEMP <= max_temp <= MAX_TEMP:
        raise ConfigError("Temperatura maxima deve ser 0 (desligado) ou entre "
                          "%d e %d C." % (MIN_TEMP, MAX_TEMP))
    out["max_temp"] = max_temp

    pool_url = (cfg.get("pool_url") or "").strip()
    if mode == "pool":
        if not pool_url.startswith(("stratum+tcp://", "stratum+tcps://")):
            raise ConfigError("URL da pool deve comecar com stratum+tcp://")
        rest = pool_url.split("://", 1)[1]
        if ":" not in rest:
            raise ConfigError("Informe a porta da pool (ex.: ...:21496).")
        try:
            port = int(rest.rsplit(":", 1)[1])
        except ValueError:
            raise ConfigError("Porta da pool invalida.")
        if not 1 <= port <= 65535:
            raise ConfigError("Porta da pool deve estar entre 1 e 65535.")
    out["pool_url"] = pool_url or DEFAULTS["pool_url"]

    pw = cfg.get("pool_password")
    out["pool_password"] = DEFAULTS["pool_password"] if pw is None else str(pw)

    out["paused"] = bool(cfg.get("paused", out["paused"]))
    return out


def load():
    """Le do disco uma vez e memoriza. Config corrompida nao derruba o app."""
    global _cache
    with _lock:
        if _cache is not None:
            return dict(_cache)
        data = {}
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                data = {}
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            # Arquivo ilegivel: seguimos com o padrao em vez de nao subir.
            print("config.json ilegivel (%s); usando padroes" % exc, flush=True)
            data = {}
        try:
            _cache = _validate(data)
        except ConfigError as exc:
            print("config.json invalido (%s); usando padroes" % exc, flush=True)
            _cache = dict(DEFAULTS)
        return dict(_cache)


def save(patch):
    """Aplica um patch parcial, valida o resultado inteiro e grava.

    Devolve a config nova. Levanta ConfigError sem tocar no disco se algo
    nao passar - o estado em memoria tambem fica intacto nesse caso.
    """
    global _cache
    with _lock:
        current = dict(_cache) if _cache is not None else dict(DEFAULTS)
        merged = dict(current)
        for key, value in (patch or {}).items():
            if key in DEFAULTS:
                merged[key] = value
        validated = _validate(merged)

        os.makedirs(CONFIG_DIR, exist_ok=True)
        # tmp + rename: o rename e atomico no mesmo filesystem, entao nunca
        # existe um config.json pela metade.
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=CONFIG_DIR, prefix=".config-",
            suffix=".tmp", delete=False)
        tmp_path = handle.name
        try:
            with handle:
                json.dump(validated, handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, CONFIG_PATH)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        _cache = validated
        return dict(_cache)


def public(cfg=None):
    """Versao pra API: sem senha em texto claro."""
    cfg = dict(cfg or load())
    cfg.pop("pool_password", None)
    cfg["pool_password_set"] = True
    cfg["max_threads"] = MAX_THREADS
    cfg["temp_range"] = [MIN_TEMP, MAX_TEMP]
    return cfg
