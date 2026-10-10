#!/usr/bin/env python3
"""Cliente NTP cru, em pacote montado na mao.

Sobreviveu da versao anterior do painel porque continua servindo a duas
coisas que o chronyc NAO responde:

* o botao "Testar" da tela de adicionar fonte - precisamos falar com um
  servidor que ainda nao esta no chrony.conf, logo nao da pra perguntar ao
  chronyd sobre ele;
* o campo "precisao" do painel, que vem do cabecalho do pacote NTP (expoente
  de 2) e nao aparece em nenhum relatorio do chronyc.
"""

import socket
import struct
import time

NTP_EPOCH_DELTA = 2208988800  # segundos entre 1900 e 1970
DEFAULT_PORT = 123


def format_ref_id(value, stratum):
    """Stratum 1 traz 4 caracteres (GPS, PPS...); acima disso, um IPv4."""
    raw = struct.pack("!I", value)
    if stratum <= 1:
        text = raw.decode("ascii", "ignore").strip("\x00").strip()
        return text or "—"
    return ".".join(str(b) for b in raw)


def query(host, port=DEFAULT_PORT, timeout=5):
    """Consulta um servidor NTP e devolve os campos do pacote + offset/delay.

    Levanta OSError/ValueError quando nao da - quem chama traduz pra tela.
    """
    packet = bytearray(48)
    packet[0] = 0x1B  # LI=0, VN=3, Mode=3 (cliente)

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
        # Expoente de 2, em segundos: -25 significa 2^-25 s, ~30 ns.
        "precision": precision,
        "rootDelay": f[4] / 65536.0,
        "rootDispersion": f[5] / 65536.0,
        "refId": format_ref_id(f[6], stratum),
        "refTime": (ref_ts - NTP_EPOCH_DELTA) if ref_ts else 0,
        # Formulas classicas do NTP.
        "offset": ((t2 - t1) + (t3 - t4)) / 2,
        "delay": (t4 - t1) - (t3 - t2),
    }
