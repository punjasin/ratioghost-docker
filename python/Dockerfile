# Ratio Ghost (Python port) — build context must be the repo root:
#   docker build -f python/Dockerfile -t ratioghost .
#   docker run --rm -p 3773:3773 -p 3774:3774 -e RG_ONLY_LOCAL=0 ratioghost
FROM python:3.12-slim

WORKDIR /app

# Application (stdlib only — no pip dependencies) and the TLS cert/key used
# for HTTPS tracker interception.
COPY python/ratioghost.py /app/ratioghost.py
COPY tls/ /app/tls/

# HTTP proxy port and the HTTPS interception port (listen_port + 1).
EXPOSE 3773 3774

ENV RG_BIND=0.0.0.0 \
    RG_LISTEN_PORT=3773 \
    RG_ONLY_LOCAL=0

CMD ["python", "-u", "ratioghost.py"]
