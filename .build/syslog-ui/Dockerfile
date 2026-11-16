# Tela de leitura dos logs do meuapps-syslog: nginx com a config e a pagina
# assadas dentro.
#
# POR QUE ASSAR: ver o cabecalho de .build/syslog/Dockerfile. Resumo: o
# Update do umbreld so recopia o compose e o umbrel-app.yml, entao enquanto
# a pagina era um bind mount cada ajuste de uma linha de CSS exigia SSH e
# curl no servidor.
FROM nginx:1.27-alpine

LABEL org.opencontainers.image.title="syslog-ui" \
      org.opencontainers.image.description="Tela de leitura dos logs do meuapps-syslog" \
      org.opencontainers.image.source="https://github.com/herigson/umbrel-meus-apps"

# O alpine nao traz tzdata, e sem ele a variavel TZ e ignorada em silencio -
# a pagina e os indices voltariam a sair em UTC.
RUN apk add --no-cache tzdata

# Config COMPLETA, em /etc/nginx/nginx.conf e nao em conf.d: a diretiva
# "user" que o botao de apagar exige so existe no contexto principal.
COPY nginx.conf /etc/nginx/nginx.conf
COPY index.html /app/index.html

# O compose sobrescreve.
ENV TZ=UTC

EXPOSE 80
