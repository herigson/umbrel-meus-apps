#!/usr/bin/env python3
"""Gera o chrony.conf, supervisiona o chronyd e le o chronyc.

Tres responsabilidades que andam juntas porque dependem do mesmo processo:

1. build_conf()  - transforma o config.json num chrony.conf;
2. supervise()   - mantem um chronyd vivo, e so um;
3. query()       - fala com esse chronyd pela porta de comando local.

Sobre (3): o chronyc aceita "-c", que devolve CSV em vez do texto alinhado
feito pra humano. Parsear o CSV e menos fragil, mas a ORDEM e a QUANTIDADE de
colunas mudam entre versoes do chrony - por isso toda leitura aqui passa por
_num/_txt, que devolvem um padrao quando a coluna nao existe, em vez de
estourar IndexError e derrubar o painel inteiro por causa de um campo novo.

As licoes de processo filho vieram do NerdMiner: um Popen vivo por vez, todo
processo terminado e reapeado com wait(), o stdout e drenado por uma thread
(sem isso o pipe enche e o filho TRAVA na primeira escrita), e o historico de
log e um deque com maxlen.
"""

import os
import signal
import subprocess
import threading
import time
from collections import deque

import appconfig

CONF_PATH = "/etc/chrony/chrony.conf"
CHRONYD = os.environ.get("CHRONYD_BIN", "/usr/sbin/chronyd")
CHRONYC = os.environ.get("CHRONYC_BIN", "/usr/bin/chronyc")

# O driftfile fica em /var/lib/chrony, nao em /data: o chronyd roda como
# _chrony e o /data e um bind-mount do host cujo dono e root. Perder o drift
# ao recriar o container custa alguns minutos de reconvergencia - e com "-x"
# ligado (sem ajuste de relogio) esse arquivo e quase decorativo de todo
# jeito. Nao vale o chown de um diretorio inteiro.
DRIFT_PATH = "/var/lib/chrony/drift"

LOG_LINES = 200
RESTART_DELAY = 5
QUERY_TIMEOUT = 5

_lock = threading.RLock()
_proc = None
_log = deque(maxlen=LOG_LINES)
_state = {"running": False, "reason": "ainda nao iniciado", "started_at": None,
          "restarts": 0, "exit_code": None}
_stop = threading.Event()
_wanted = threading.Event()


def log(msg):
    """Imprime com hora, no mesmo formato do chronyd, pra que as linhas do
    supervisor e as do daemon possam ser lidas na mesma linha do tempo."""
    print("[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


# --------------------------------------------------------------------------
# 1. chrony.conf
# --------------------------------------------------------------------------

def build_conf(cfg):
    """Monta o texto do chrony.conf a partir da config do painel."""
    poll = int(cfg["poll_exponent"])
    lines = [
        "# Gerado pelo painel a cada arranque, a partir de /data/config.json.",
        "# Editar este arquivo nao adianta: ele e reescrito no proximo",
        "# 'Aplicar' e o /etc/chrony nao sobrevive ao container.",
        "",
    ]

    for src in cfg["servers"]:
        if not src["enabled"]:
            continue
        parts = [src["kind"], src["host"]]
        if src["iburst"]:
            parts.append("iburst")
        if src["prefer"]:
            parts.append("prefer")
        if src["nts"]:
            parts.append("nts")
        # minpoll == maxpoll fixa o intervalo no valor que a interface
        # promete. O padrao do chrony (6..10) deixa ele espacar as consultas
        # sozinho, o que e otimo pra precisao mas faria o rotulo "consulta a
        # cada 2 min" virar mentira. Entre obedecer a UI e otimizar sozinho,
        # obedecer a UI.
        parts += ["minpoll", str(poll), "maxpoll", str(poll)]
        lines.append(" ".join(parts))

    lines += [
        "",
        "driftfile %s" % DRIFT_PATH,
        "minsources %d" % int(cfg["min_sources"]),
        "",
        "# O relogio e do host do Umbrel, e quem o corrige e o timesyncd",
        "# dele. Este chronyd roda com -x: mede o desvio e serve a hora, mas",
        "# nunca mexe no relogio. Dois servicos disciplinando o mesmo relogio",
        "# brigariam entre si. Por isso nao ha makestep aqui.",
        "",
    ]

    lines += [
        "# O painel pergunta a hora ao PROPRIO servidor em 127.0.0.1 pra ler",
        "# o campo de precisao do pacote NTP - que nenhum relatorio do",
        "# chronyc expoe - e, de quebra, provar que a 123/UDP esta mesmo",
        "# respondendo. Sem este allow o chrony recusa ate a si mesmo, e o",
        "# cartao 'Precisao' fica vazio. Vale sempre, inclusive com o",
        "# atendimento a rede desligado: localhost nao e a rede.",
        "allow 127.0.0.1",
        "",
    ]

    if cfg["serve"]:
        for net in cfg["allow_networks"]:
            lines.append("allow %s" % net)
    else:
        lines.append("# Servico desligado na interface: sem 'allow', o chrony")
        lines.append("# sincroniza mas nao responde a ninguem na rede.")

    lines += [
        "",
        "# cmdport 0 FECHA a porta de comando UDP. O painel roda no mesmo",
        "# container e fala pelo socket unix do chronyd, que da acesso",
        "# completo (inclusive a comandos restritos como 'clients') sem",
        "# deixar nada escutando. Porta aberta que ninguem usa e so",
        "# superficie de ataque.",
        "cmdport 0",
        "user _chrony",
        "pidfile /run/chrony/chronyd.pid",
        "",
    ]
    return "\n".join(lines)


def write_conf(cfg):
    os.makedirs(os.path.dirname(CONF_PATH), exist_ok=True)
    tmp = CONF_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(build_conf(cfg))
    os.replace(tmp, CONF_PATH)


# --------------------------------------------------------------------------
# 2. supervisor
# --------------------------------------------------------------------------

def _drain(stream):
    """Le o stdout do chronyd ate o EOF. Nunca pode parar de ler."""
    try:
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip("\n")
            _log.append(line)
            print(line, flush=True)
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _spawn():
    """Sobe o chronyd. Chamar com _lock adquirido."""
    global _proc
    # -n: primeiro plano, pra que o Popen acompanhe o processo de verdade em
    #     vez de ver o pai sair logo apos o fork.
    # -x: nunca ajusta o relogio do sistema (ver build_conf).
    # -d: manda o log pro stderr, que a thread de drain reimprime.
    args = [CHRONYD, "-n", "-x", "-d", "-f", CONF_PATH]
    log("iniciando: %s" % " ".join(args))
    try:
        _proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, bufsize=0,
                                 close_fds=True)
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
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        # TimeoutExpired nao e OSError. Se escapasse daqui, _proc ficaria
        # None com running=True e o supervisor nunca mais agiria - foi
        # exatamente esse o bug de nove horas do NerdMiner.
        pass
    finally:
        _state.update(running=False, started_at=None)


def apply_config():
    """Reescreve o conf e reinicia o chronyd. Idempotente."""
    with _lock:
        cfg = appconfig.load()
        _kill()
        try:
            write_conf(cfg)
        except OSError as exc:
            _state.update(running=False,
                          reason="nao consegui gravar o chrony.conf: %s" % exc)
            _wanted.clear()
            return
        _wanted.set()
        _spawn()


def supervise():
    """Se era pra estar rodando e morreu, sobe de novo.

    Mesma forma do supervisor do NerdMiner: o laco inteiro num try/except
    (supervisor que morre por excecao nao avisa, so para de supervisionar) e
    a decisao de subir por VERIFICACAO - e pra rodar e nao ha processo - e
    nao por um campo de estado que pode dessincronizar.
    """
    global _proc

    while not _stop.is_set():
        try:
            with _lock:
                proc = _proc
                should_run = _wanted.is_set()
                if should_run and proc is not None:
                    code = proc.poll()
                    if code is not None:
                        proc.wait()
                        _proc = None
                        _state["exit_code"] = code
                        _state["restarts"] += 1
                        _state.update(running=False,
                                      reason="saiu com codigo %s; reiniciando" % code)
                        log("chronyd saiu com codigo %s; subindo outro" % code)
                elif should_run and proc is None:
                    _spawn()
        except Exception as exc:
            log("supervisor: erro no ciclo (%s: %s); seguindo em frente"
                % (type(exc).__name__, exc))
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


# --------------------------------------------------------------------------
# 3. chronyc
# --------------------------------------------------------------------------

def query(command, flags=("-N",)):
    """Roda 'chronyc -c <command>' e devolve a lista de linhas ja separadas.

    Levanta RuntimeError quando o chronyc falha - quem chama decide se isso
    vira erro na tela ou um campo vazio.

    O -N pede os NOMES originais das fontes em vez dos enderecos resolvidos.
    Sem ele o chronyc devolve "200.160.7.186" onde a config diz
    "a.st1.ntp.br", e a tela de Fontes - que casa as duas listas pelo nome -
    nunca encontra par, mostrando "aguardando primeira consulta" para sempre
    com a fonte funcionando.

    Em 'clients' o -N nao se aplica e o padrao faria DNS reverso em cada
    endereco da LAN; como a maioria nao tem PTR, seriam varios timeouts
    dentro do nosso limite de 5s. La vai -n, que e tambem o que interessa
    ver: o IP do aparelho.
    """
    # Sem -h/-p: assim o chronyc usa o socket unix (/run/chrony/chronyd.sock),
    # que e o caminho privilegiado. Pela porta UDP, comandos como 'clients'
    # seriam recusados - e ela esta fechada de todo jeito (cmdport 0).
    args = [CHRONYC, "-c"] + list(flags) + command.split()
    try:
        done = subprocess.run(args, capture_output=True, timeout=QUERY_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("chronyc %s: %s" % (command, exc))
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or b"").decode("utf-8", "replace")
        raise RuntimeError("chronyc %s: %s" % (command, detail.strip()[:200]))
    text = done.stdout.decode("utf-8", "replace")
    return [line.split(",") for line in text.splitlines() if line.strip()]


def _num(row, index, default=None):
    try:
        return float(row[index])
    except (IndexError, TypeError, ValueError):
        return default


def _int(row, index, default=None):
    value = _num(row, index, None)
    return default if value is None else int(value)


def _txt(row, index, default=""):
    try:
        return row[index].strip()
    except (IndexError, AttributeError):
        return default


# O chrony nomeia o estado de cada fonte com um caractere. Traduzir aqui, uma
# vez, evita espalhar esse vocabulario pelo front-end.
SOURCE_STATE = {
    "*": ("sync", "Em uso como referencia"),
    "+": ("ok", "Combinada com a referencia"),
    "-": ("excluded", "Descartada pelo algoritmo"),
    "?": ("unreachable", "Sem resposta"),
    "x": ("falseticker", "Hora divergente das outras"),
    "~": ("unstable", "Variacao alta demais"),
}


def tracking():
    rows = query("tracking")
    if not rows:
        raise RuntimeError("tracking vazio")
    r = rows[0]
    return {
        "refId": _txt(r, 0),
        "refName": _txt(r, 1),
        "stratum": _int(r, 2, 0),
        "refTime": _num(r, 3),
        "systemOffset": _num(r, 4),
        "lastOffset": _num(r, 5),
        "rmsOffset": _num(r, 6),
        "frequency": _num(r, 7),
        "residualFreq": _num(r, 8),
        "skew": _num(r, 9),
        "rootDelay": _num(r, 10),
        "rootDispersion": _num(r, 11),
        "updateInterval": _num(r, 12),
        "leap": _txt(r, 13),
    }


def sources():
    """Junta 'sources' e 'sourcestats': a primeira traz estado e alcance, a
    segunda traz o desvio estatistico e o desvio padrao. O painel mostra as
    duas coisas lado a lado, entao vale casar aqui e nao no navegador."""
    try:
        stats = {_txt(r, 0): r for r in query("sourcestats")}
    except RuntimeError:
        stats = {}

    out = []
    for r in query("sources"):
        name = _txt(r, 2)
        code = _txt(r, 1)
        key, label = SOURCE_STATE.get(code, ("unknown", "Estado desconhecido"))
        reach = _int(r, 5, 0) or 0
        st = stats.get(name, [])
        out.append({
            "mode": _txt(r, 0),
            "state": key,
            "stateLabel": label,
            "name": name,
            "stratum": _int(r, 3, 0),
            "poll": _int(r, 4, 0),
            # reach e um registrador de 8 bits: cada bit e uma consulta, o
            # mais recente a direita. 255 = as oito ultimas responderam.
            "reach": reach,
            "reachPercent": round(bin(reach).count("1") / 8.0 * 100),
            "lastRx": _int(r, 6, None),
            "offset": _num(r, 7),
            "originalOffset": _num(r, 8),
            "error": _num(r, 9),
            "span": _num(st, 3),
            "stdDev": _num(st, 7),
        })
    return out


def clients():
    """Quem na rede esta pedindo hora.

    E a unica forma de saber se o roteador, as cameras e os PCs realmente
    trocaram de servidor - nenhum outro comando mostra isso. Exige o socket
    unix do chronyd (so root ou o grupo _chrony), e e por isso que o painel
    roda como root.
    """
    out = []
    for r in query("clients", flags=("-n",)):
        name = _txt(r, 0)
        if not name or name.lower() == "hostname":
            continue
        out.append({
            "host": name,
            "packets": _int(r, 1, 0),
            "dropped": _int(r, 2, 0),
            "interval": _num(r, 3),
            "lastSeen": _int(r, 4, None),
        })
    out.sort(key=lambda c: c["packets"], reverse=True)
    return out


def serverstats():
    """Contadores do servidor. O numero e a ORDEM das colunas mudaram entre
    versoes do chrony (as mais novas acrescentaram campos de NTS), entao
    lemos so as primeiras, que sao estaveis desde a 3.x."""
    rows = query("serverstats")
    if not rows:
        return {}
    r = rows[0]
    return {
        "ntpPackets": _int(r, 0, 0),
        "ntpDropped": _int(r, 1, 0),
        "cmdPackets": _int(r, 2, 0),
        "cmdDropped": _int(r, 3, 0),
        "logDropped": _int(r, 4, 0),
    }
