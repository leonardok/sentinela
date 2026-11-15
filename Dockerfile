FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends nut-server nut-client iputils-ping \
 && rm -rf /var/lib/apt/lists/* \
 && rm -f /etc/nut/*.conf /etc/nut/upsd.users

COPY sentinela.py painel.html /app/

ENV PYTHONUNBUFFERED=1 \
    PORTA=3493 \
    UI_PORTA=8493 \
    CONFIG_ARQUIVO=/config/sentinela.json

VOLUME /config
EXPOSE 3493 8493

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD upsc "${UPS_NOME:-sentinela}@127.0.0.1:${PORTA}" ups.status >/dev/null 2>&1 || exit 1

ENTRYPOINT ["python3", "/app/sentinela.py"]
