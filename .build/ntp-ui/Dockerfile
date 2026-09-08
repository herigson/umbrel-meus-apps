# Painel do servidor NTP: consulta via protocolo NTP e serve um dashboard.
# So biblioteca padrao do Python - sem pip, sem dependencia externa.
FROM python:3.12-slim

LABEL org.opencontainers.image.title="ntp-ui" \
      org.opencontainers.image.description="Painel web do servidor NTP (meuapps-ntp)" \
      org.opencontainers.image.source="https://github.com/herigson/umbrel-meus-apps"

# tzdata: o painel formata a hora no fuso configurado em TZ.
RUN set -eu; \
    apt-get update; \
    apt-get install -y --no-install-recommends tzdata; \
    rm -rf /var/lib/apt/lists/*; \
    useradd -u 1000 -m -s /usr/sbin/nologin painel

ENV TZ=UTC

WORKDIR /app
COPY app.py index.html ./
RUN chown -R painel:painel /app

USER painel
EXPOSE 8080
ENV PYTHONUNBUFFERED=1
CMD ["python3", "app.py"]
