FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY funambridge ./funambridge

# La config (config.yaml) vive en un volumen montado en /data
VOLUME ["/data"]
EXPOSE 9000

# Escucha en 0.0.0.0:9000 (lo avisa al arrancar). Ponlo detrás de tu propio
# proxy HTTPS; respeta X-Forwarded-Proto / X-Forwarded-Host.
ENTRYPOINT ["python", "-m", "funambridge", "--config", "/data/config.yaml"]
CMD ["serve"]
