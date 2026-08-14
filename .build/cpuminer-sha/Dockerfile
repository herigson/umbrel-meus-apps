# cpuminer-opt compilado COM as instrucoes SHA (SHA-NI) do processador.
#
# Motivo: as imagens publicas de cpuminer-opt sao compiladas com
# "-march=native" na maquina de build do autor. Como esses runners nao tem
# SHA-NI, o binario sai sem ela e o sha256d fica na velocidade do AVX2 puro
# ("SW features: AVX2 AES" / "Enabled optimizations: AVX2" no log).
#
# Flags: "-march=alderlake" e a receita do proprio JayDDee para o ISA
# AVX2 + SHA + VAES (build-allarch.sh, alvo "cpuminer-alderlake") - que e
# exatamente o do Intel N100 (Alder Lake-N / Gracemont) do Umbrel.
# Exige gcc-12+, que e o padrao no debian bookworm.
#
# Tentativa anterior que NAO compila (exit 2): "-march=x86-64-v3 -msha -mvaes".
# O nivel x86-64-v3 nao inclui AES nem PCLMUL, que o codigo assume presentes
# quando ha AVX2.
#
# Consequencia: a imagem so roda em CPUs Alder Lake ou mais novas. Isso e
# proposital - ela existe para esta maquina.

# CUIDADO ao trocar de versao: a tag "v27.4" do upstream esta mal versionada -
# ela contem o codigo 24.7 (AC_INIT diz 24.7 e o binario se identifica como
# 24.7). A ultima versao real e a v26.1, que e o que o master tambem tem.
# Sempre conferir o AC_INIT do configure.ac na tag antes de fixar aqui.
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
RUN set -eu; \
    gcc --version | head -1; \
    ./autogen.sh; \
    CFLAGS="-O3 -march=alderlake -Wall" ./configure --with-curl; \
    make -j"$(nproc)"; \
    strip -s cpuminer
# NAO rodar o binario aqui (--version / --cputest): o runner do GitHub nao e
# Alder Lake e o processo morreria com "Illegal instruction" durante o build.
# A validacao e feita na maquina de destino.

FROM debian:bookworm-slim
LABEL org.opencontainers.image.title="cpuminer-opt SHA-NI (alderlake)" \
      org.opencontainers.image.description="cpuminer-opt compilado com -march=alderlake para sha256d acelerado por SHA-NI" \
      org.opencontainers.image.source="https://github.com/JayDDee/cpuminer-opt" \
      org.opencontainers.image.licenses="GPL-2.0"

RUN set -eu; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates libcurl4 libgmp10 libjansson4 libssl3 zlib1g; \
    rm -rf /var/lib/apt/lists/*; \
    useradd -u 1000 -m -s /usr/sbin/nologin cpuminer

COPY --from=builder /src/cpuminer /usr/local/bin/cpuminer

USER cpuminer
WORKDIR /home/cpuminer
ENTRYPOINT ["cpuminer"]
