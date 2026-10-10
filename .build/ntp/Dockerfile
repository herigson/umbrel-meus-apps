# Servidor NTP + painel num container so.
#
# Por que junto: configurar pela interface exige reescrever o chrony.conf e
# reiniciar o chronyd. Um container nao reinicia o vizinho sem o socket do
# Docker, que o Umbrel proibe - com razao. Era essa a parede que impedia
# qualquer ajuste pela UI na versao de dois containers.
#
# O ganho de juntar: o painel passa a falar com o chronyd pela porta de
# comando em 127.0.0.1 (chronyc), e ai tem os numeros que o proprio chrony
# calcula - tracking, sources, sourcestats, clients, serverstats - em vez de
# estimar tudo por fora com NTP cru.
#
# So biblioteca padrao do Python: sem pip, sem dependencia externa.
FROM python:3.12-slim

LABEL org.opencontainers.image.title="ntp" \
      org.opencontainers.image.description="Servidor NTP (chrony) com painel e configuracao pela interface" \
      org.opencontainers.image.source="https://github.com/herigson/umbrel-meus-apps"

# tzdata: o painel formata a hora no fuso escolhido pela UI.
# iproute2 nao entra - nada aqui precisa mexer em rede.
RUN set -eu; \
    apt-get update; \
    apt-get install -y --no-install-recommends chrony tzdata; \
    rm -rf /var/lib/apt/lists/*; \
    # O pacote do Debian cria o usuario _chrony. Confirmar aqui faz o build
    # FALHAR cedo se uma versao futura mudar o nome, em vez de so nao servir
    # hora depois de instalado.
    getent passwd _chrony > /dev/null; \
    # O chrony.conf do pacote nunca e lido: o painel gera o nosso a cada
    # arranque, a partir do config.json. Apagar evita que alguem depurando
    # dentro do container leia o arquivo errado.
    rm -f /etc/chrony/chrony.conf; \
    mkdir -p /run/chrony /var/lib/chrony; \
    chown _chrony:_chrony /run/chrony /var/lib/chrony; \
    chmod 750 /run/chrony

ENV TZ=UTC \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY app.py appconfig.py chronyd.py history.py ntpclient.py ./
COPY web/ ./web/

# 123/UDP e porta privilegiada: o chronyd abre o socket como root e ele
# mesmo cai pro usuario _chrony (diretiva "user" no conf gerado). O painel
# fica como root porque e ele quem precisa subir o chronyd de novo a cada
# "Aplicar" - e reabrir a 123 exige privilegio. Diferente do NerdMiner, onde
# o processo exposto a rede era o binario grande em C: aqui quem fala com a
# rede e o chronyd, que se rebaixa sozinho, e o painel so e alcancavel pelo
# app_proxy autenticado do Umbrel.
EXPOSE 8080 123/udp

ENTRYPOINT ["python3", "-u", "/app/app.py"]
