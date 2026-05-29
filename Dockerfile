FROM python:3.11-slim

WORKDIR /app

# Python deps first so layer cache survives code changes
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY main.py .
COPY samsung_dryer/ ./samsung_dryer/

# /config holds the mega client cert + key. Mount from the host so
# secrets aren't baked into the image.
RUN mkdir -p /config

# Unbuffered stdout so docker logs is live
ENV PYTHONUNBUFFERED=1

# Defaults — override in docker-compose.yml or `docker run -e ...`
ENV CERT_PATH=/config/mega_chain.pem \
    KEY_PATH=/config/mega.key \
    MQTT_TOPIC_PREFIX=samsung_dryer \
    HA_DISCOVERY_PREFIX=homeassistant \
    HEALTH_INTERVAL_S=60 \
    HEARTBEAT_INTERVAL_S=600

# No port — bridge is outbound-only (TLS to dryer, MQTT to broker).

CMD ["python", "main.py"]
