#!/usr/bin/env python3
"""Configuracao do servidor NTP, persistida em /data/config.json.

Mora em ${APP_DATA_DIR} pra sobreviver aos updates do app - era esse o
problema da versao anterior, em que tudo vinha de variavel de ambiente no
docker-compose.yml e o /etc/chrony era um tmpfs recriado a cada arranque.

Gravacao atomica: escreve num .tmp no MESMO diretorio e faz os.replace. Um
rename dentro do mesmo sistema de arquivos e atomico, entao nao existe
instante em que o config.json esteja pela metade - se faltar energia no meio,
sobra o arquivo antigo inteiro ou o novo inteiro.
"""

import ipaddress
import json
import os
import re
import tempfile
import threading

CONFIG_DIR = os.environ.get("CONFIG_DIR", "/data")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

# O chrony conta o intervalo de consulta em potencias de 2 segundos, e e por
# isso que a interface oferece 1 / 2 / 4 / 17 min: sao 2^6=64s, 2^7=128s,
# 2^8=256s e 2^10=1024s (17,07 min). Os rotulos caem exatos na escala dele,
# sem arredondar nada.
POLL_MIN = 6
POLL_MAX = 10

MIN_SOURCES_MAX = 4
MAX_SERVERS = 16
MAX_NETWORKS = 8

DEFAULTS = {
    "serve": True,
    "allow_networks": ["192.168.0.0/16"],
    "servers": [
        {"host": "a.st1.ntp.br", "kind": "server", "enabled": True,
         "prefer": True, "iburst": True, "nts": False},
        {"host": "b.st1.ntp.br", "kind": "server", "enabled": True,
         "prefer": False, "iburst": True, "nts": False},
        {"host": "time.cloudflare.com", "kind": "server", "enabled": True,
         "prefer": False, "iburst": True, "nts": False},
    ],
    "poll_exponent": 7,          # 2^7 = 128 s ~ 2 min
    "min_sources": 1,
    "timezone": "America/Sao_Paulo",
    "auto_refresh": True,
}

# Nome de host ou IP. Deliberadamente restritivo: esse texto vai parar numa
# linha do chrony.conf, entao nada de espaco, aspas ou quebra de linha que
# pudesse virar uma segunda diretiva.
HOSTNAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")

_lock = threading.Lock()
_cache = None


class ConfigError(ValueError):
    """Entrada invalida vinda da interface. Vira HTTP 400, nao 500."""


def clean_host(value):
    """Valida e normaliza um endereco de fonte. Publica porque o /api/test
    precisa aplicar a MESMA regra antes de consultar um servidor que ainda
    nao esta na config - duas validacoes diferentes para a mesma coisa
    acabariam divergindo."""
    host = str(value or "").strip().lower()
    if not host:
        raise ConfigError("endereco vazio")
    if not HOSTNAME_RE.match(host):
        raise ConfigError("endereco invalido: %s" % host[:60])
    return host


def _clean_network(value):
    text = str(value or "").strip()
    try:
        # strict=False aceita "192.168.1.10/24" e normaliza pra rede.
        net = ipaddress.ip_network(text, strict=False)
    except ValueError as exc:
        raise ConfigError("rede invalida: %s (%s)" % (text[:40], exc))
    return str(net)


def _clean_server(raw):
    if not isinstance(raw, dict):
        raise ConfigError("fonte em formato invalido")
    kind = str(raw.get("kind", "server")).strip().lower()
    if kind not in ("server", "pool"):
        raise ConfigError("tipo de fonte invalido: %s" % kind[:20])
    return {
        "host": clean_host(raw.get("host")),
        "kind": kind,
        "enabled": bool(raw.get("enabled", True)),
        "prefer": bool(raw.get("prefer", False)),
        "iburst": bool(raw.get("iburst", True)),
        "nts": bool(raw.get("nts", False)),
    }


def _validate(cfg):
    """Devolve uma copia limpa ou levanta ConfigError. Nunca altera a entrada."""
    out = dict(DEFAULTS)
    out.update({k: v for k, v in cfg.items() if k in DEFAULTS})

    out["serve"] = bool(out["serve"])
    out["auto_refresh"] = bool(out["auto_refresh"])

    nets = out["allow_networks"]
    if not isinstance(nets, list):
        raise ConfigError("allow_networks deve ser uma lista")
    if len(nets) > MAX_NETWORKS:
        raise ConfigError("no maximo %d redes" % MAX_NETWORKS)
    out["allow_networks"] = [_clean_network(n) for n in nets]
    if out["serve"] and not out["allow_networks"]:
        raise ConfigError("para servir hora e preciso ao menos uma rede permitida")

    servers = out["servers"]
    if not isinstance(servers, list):
        raise ConfigError("servers deve ser uma lista")
    if len(servers) > MAX_SERVERS:
        raise ConfigError("no maximo %d fontes" % MAX_SERVERS)
    cleaned = [_clean_server(s) for s in servers]
    seen = set()
    for item in cleaned:
        if item["host"] in seen:
            raise ConfigError("fonte repetida: %s" % item["host"])
        seen.add(item["host"])
    out["servers"] = cleaned
    if not [s for s in cleaned if s["enabled"]]:
        raise ConfigError("ao menos uma fonte precisa estar ativa")

    try:
        poll = int(out["poll_exponent"])
    except (TypeError, ValueError):
        raise ConfigError("intervalo de consulta invalido")
    if not POLL_MIN <= poll <= POLL_MAX:
        raise ConfigError("intervalo de consulta fora da faixa")
    out["poll_exponent"] = poll

    try:
        minsrc = int(out["min_sources"])
    except (TypeError, ValueError):
        raise ConfigError("fontes minimas invalido")
    active = len([s for s in cleaned if s["enabled"]])
    if not 1 <= minsrc <= MIN_SOURCES_MAX:
        raise ConfigError("fontes minimas deve ficar entre 1 e %d" % MIN_SOURCES_MAX)
    if minsrc > active:
        # Exigir mais fontes do que existem trava o chrony sem nunca
        # sincronizar - e um jeito silencioso de desligar o servidor.
        raise ConfigError(
            "fontes minimas (%d) nao pode passar do numero de fontes ativas (%d)"
            % (minsrc, active))
    out["min_sources"] = minsrc

    tz = str(out["timezone"]).strip()
    try:
        import zoneinfo
        zoneinfo.ZoneInfo(tz)
    except Exception:
        raise ConfigError("fuso horario desconhecido: %s" % tz[:60])
    out["timezone"] = tz

    return out


def load():
    global _cache
    with _lock:
        if _cache is not None:
            return dict(_cache)
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError):
            raw = {}
        try:
            cfg = _validate(raw if isinstance(raw, dict) else {})
        except ConfigError as exc:
            # Config gravada invalida (editada na mao, ou de uma versao com
            # formato diferente): cai no padrao em vez de deixar o app morto.
            print("config invalida (%s); usando os padroes" % exc, flush=True)
            cfg = _validate({})
        _cache = cfg
        return dict(cfg)


def save(patch):
    """Mescla o patch na config atual, valida o RESULTADO e grava."""
    global _cache
    with _lock:
        current = dict(_cache) if _cache is not None else None
    if current is None:
        current = load()
    merged = dict(current)
    merged.update(patch or {})
    cfg = _validate(merged)

    os.makedirs(CONFIG_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=CONFIG_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(cfg, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, CONFIG_PATH)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    with _lock:
        _cache = cfg
    return dict(cfg)


def timezones():
    """Lista de fusos pro seletor da interface.

    Sai do tzdata instalado no container, nao de uma lista escrita a mao:
    uma lista fixa envelhece junto com as regras de horario de verao, e
    poderia oferecer um fuso que o zoneinfo daqui nao conhece - entao a
    validacao recusaria o que a propria tela ofereceu.
    """
    try:
        import zoneinfo
        return sorted(zoneinfo.available_timezones())
    except Exception:
        return [DEFAULTS["timezone"], "UTC"]


def public():
    """O que a interface recebe. Hoje nao ha segredo nenhum na config - nao
    existe senha de pool aqui -, mas o acrescimo dos limites evita que o
    front-end repita numeros que o back-end ja conhece."""
    cfg = load()
    cfg["timezones"] = timezones()
    cfg["limits"] = {
        "poll_range": [POLL_MIN, POLL_MAX],
        "min_sources_max": MIN_SOURCES_MAX,
        "max_servers": MAX_SERVERS,
        "max_networks": MAX_NETWORKS,
    }
    return cfg
