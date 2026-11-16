# Servidor de syslog do meuapps-syslog: a imagem oficial do syslog-ng com a
# configuracao assada dentro.
#
# POR QUE ASSAR: um Update de app no umbreld recopia APENAS o
# docker-compose.yml e o umbrel-app.yml. Arquivo auxiliar montado por bind
# so chega na INSTALACAO - entao mudar a config no repo e clicar Update nao
# propagava nada, e cada ajuste virava um curl na mao dentro do servidor.
# A tag da imagem, essa sim, mora no compose. Assando a config, a entrega
# passa a ser o proprio Update.
FROM balabit/syslog-ng:4.11.0

LABEL org.opencontainers.image.title="syslog" \
      org.opencontainers.image.description="Servidor syslog-ng do meuapps-syslog" \
      org.opencontainers.image.source="https://github.com/herigson/umbrel-meus-apps"

COPY syslog-ng.conf /etc/syslog-ng/syslog-ng.conf

# O compose sobrescreve. UTC aqui so pra imagem nao depender de quem a usa.
ENV TZ=UTC

EXPOSE 514/udp
