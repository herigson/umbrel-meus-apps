# Painel web do NerdMiner: le a API do cpuminer e serve um dashboard.
# So biblioteca padrao do Python - sem pip, sem dependencia externa.
FROM python:3.12-slim

LABEL org.opencontainers.image.title="nerdminer-ui" \
      org.opencontainers.image.description="Dashboard web para o cpuminer (meuapps-nerdminer)" \
      org.opencontainers.image.source="https://github.com/herigson/umbrel-meus-apps"

RUN useradd -u 1000 -m -s /usr/sbin/nologin painel

WORKDIR /app
COPY app.py index.html ./
RUN chown -R painel:painel /app

USER painel
EXPOSE 8080
ENV PYTHONUNBUFFERED=1
CMD ["python3", "app.py"]
