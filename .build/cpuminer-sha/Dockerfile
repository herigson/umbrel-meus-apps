# cpuminer-opt compilado COM as instrucoes SHA (SHA-NI) do processador.
#
# Motivo: as imagens publicas de cpuminer-opt sao compiladas com
# "-march=native" na maquina de build do autor. Como esses runners nao tem
# SHA-NI, o binario sai sem ela e o sha256d fica na velocidade do AVX2 puro
# ("SW features: AVX2 AES" / "Enabled optimizations: AVX2" no log).
#
# Aqui as flags sao explicitas, entao o resultado nao depende do hardware que
# compila:
#   -march=x86-64-v3  base AVX2/BMI2/FMA (portavel: Zen1+, Ice Lake+, Gracemont)
#   -msha             instrucoes SHA-NI  <- o que triplica o sha256d
#   -mvaes            VAES (o N100 tem; irrelevante pro sha256d, util p/ outros algos)
#
# Alvo validado: Intel N100 (Alder Lake-N / Gracemont) do Umbrel.
# Conferir depois de subir: o log deve mostrar SHA em "SW features".

ARG CPUMINER_VERSION=v27.4

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
RUN set -eu; \
    ./autogen.sh; \
    CFLAGS="-O3 -march=x86-64-v3 -msha -mvaes -Wall -fno-common" \
        ./configure --with-curl; \
    make -j"$(nproc)"; \
    make install
# NAO rodar o binario aqui (--version / --cputest): o runner do GitHub pode
# nao ter SHA-NI e o processo morreria com "Illegal instruction" durante o
# build. A validacao e feita na maquina de destino.

FROM debian:bookworm-slim
LABEL org.opencontainers.image.title="cpuminer-opt SHA-NI" \
      org.opencontainers.image.description="cpuminer-opt compilado com -msha para sha256d acelerado por hardware" \
      org.opencontainers.image.source="https://github.com/JayDDee/cpuminer-opt" \
      org.opencontainers.image.licenses="GPL-2.0"

RUN set -eu; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates libcurl4 libgmp10 libjansson4 libssl3 zlib1g; \
    rm -rf /var/lib/apt/lists/*; \
    useradd -u 1000 -m -s /usr/sbin/nologin cpuminer

COPY --from=builder /usr/local/bin/cpuminer /usr/local/bin/cpuminer

USER cpuminer
WORKDIR /home/cpuminer
ENTRYPOINT ["cpuminer"]
