# NerdMiner: cpuminer-opt + painel web + supervisor, num container so.
#
# Por que junto: configurar pela UI exige reiniciar o cpuminer com argumentos
# novos (ele le tudo por linha de comando e nada em runtime). Um container nao
# reinicia outro sem o socket do Docker, que o Umbrel proibe - com razao.
#
# Substitui as imagens cpuminer-sha e nerdminer-ui.

# CUIDADO ao trocar de versao: a tag "v27.4" do upstream esta mal versionada -
# contem o codigo 24.7. A ultima real e a v26.1. Conferir sempre o AC_INIT do
# configure.ac na tag antes de fixar aqui.
ARG CPUMINER_VERSION=v26.1

FROM debian:bookworm-slim AS builder
ARG CPUMINER_VERSION

RUN set -eu; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        autoconf automake build-essential ca-certificates git pkg-config \
        libcurl4-openssl-dev libgmp-dev libjansson-dev libssl-dev zlib1g-dev; \
    rm -rf /var/lib/apt/lists/*

RUN set -eu; \
    git clone --depth 1 --branch "${CPUMINER_VERSION}" \
        https://github.com/JayDDee/cpuminer-opt.git /src

WORKDIR /src

# CORRECAO DE BUG DO UPSTREAM (v26.1 e master em 24/09/2026): estouro de
# buffer de pilha no comando "summary" da API. Em api.c:
#
#     char algo[64];        // linha 137
#     char diff_str[16];    // linha 142
#     sprintf( diff_str, "%.6f", diff );
#
# A dificuldade da mainnet com 6 decimais da 22 caracteres + \0 = 23 bytes num
# buffer de 16. Os 7 que sobram invadem o buffer vizinho (algo). Sintoma:
# "ALGO=218750" (final do DIFF) em vez de "ALGO=sha256d". Acontece a cada
# chamada de summary, ou seja, a cada poll do painel.
#
# Os greps sao de proposito: se o upstream mudar a linha, o build FALHA em vez
# de aplicar um patch silenciosamente vazio.
RUN set -eu; \
    grep -q 'char diff_str\[16\];' api.c; \
    sed -i 's/char diff_str\[16\];/char diff_str[48];/' api.c; \
    grep -q 'char diff_str\[48\];' api.c; \
    echo "patch do diff_str aplicado"

# "-march=alderlake" e a receita do proprio JayDDee para AVX2 + SHA + VAES
# (build-allarch.sh), que e o ISA do Intel N100. A imagem so roda em Alder
# Lake ou mais novo - proposital, ela existe para esta maquina.
RUN set -eu; \
    gcc --version | head -1; \
    ./autogen.sh; \
    CFLAGS="-O3 -march=alderlake -Wall" ./configure --with-curl; \
    make -j"$(nproc)"; \
    strip -s cpuminer
# NAO rodar o binario aqui: o runner do GitHub nao e Alder Lake e o processo
# morreria com "Illegal instruction" durante o build.

FROM debian:bookworm-slim
LABEL org.opencontainers.image.title="NerdMiner CPU" \
      org.opencontainers.image.description="cpuminer-opt com SHA-NI, painel web e configuracao pela UI" \
      org.opencontainers.image.source="https://github.com/herigson/umbrel-meus-apps" \
      org.opencontainers.image.licenses="GPL-2.0"

RUN set -eu; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates libcurl4 libgmp10 libjansson4 libssl3 zlib1g \
        python3-minimal python3; \
    rm -rf /var/lib/apt/lists/*; \
    useradd -u 1000 -m -s /usr/sbin/nologin nerdminer; \
    mkdir -p /data; \
    chown nerdminer:nerdminer /data

COPY --from=builder /src/cpuminer /usr/local/bin/cpuminer
COPY app.py appconfig.py history.py miner.py nodeinfo.py /app/
COPY web/ /app/web/

ENV PYTHONUNBUFFERED=1 \
    CONFIG_DIR=/data \
    LISTEN_PORT=8080

# Sem USER aqui de proposito: o processo sobe como root, corrige o dono de
# /data (que o Docker cria como root ao montar o volume do Umbrel) e abaixa
# para uid 1000 em drop_privileges(), no app.py - antes de qualquer socket,
# thread ou processo filho. O cpuminer tambem nasce ja sem privilegio.
WORKDIR /app
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=3).status==200 else 1)"

ENTRYPOINT ["python3", "-u", "/app/app.py"]
