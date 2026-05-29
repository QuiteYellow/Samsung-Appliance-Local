"""Configuration — env-var driven so Docker / Unraid can set everything
via docker-compose environment block. Falls back to a .env file in the
working directory for local dev."""
import os
from pathlib import Path


def _load_env_file():
    """If a .env file is present in cwd, hydrate os.environ from it."""
    env_path = Path.cwd() / '.env'
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        if '=' not in line or line.startswith('#'):
            continue
        k, v = line.split('=', 1)
        k = k.strip(); v = v.strip()
        # Don't clobber existing env (Docker compose env wins)
        if k and k not in os.environ:
            os.environ[k] = v


_load_env_file()


class Config:
    """Runtime config. All knobs are env-var driven; CLI flags in main.py
    override any of these."""

    # --- Dryer (OCF / CoAP over TLS) ---
    APPLIANCE_IP        = os.getenv('APPLIANCE_IP')
    APPLIANCE_OCF_PORT  = int(os.getenv('APPLIANCE_OCF_PORT', '49154'))

    # Cert lookup: prefer explicit env, then the Docker mount at
    # /config/*, then a local ./certs/ directory for bare-metal dev.
    @staticmethod
    def _resolve_cert(env_key, basename):
        if os.getenv(env_key):
            return Path(os.environ[env_key])
        docker_path = Path('/config') / basename
        if docker_path.exists():
            return docker_path
        return Path.cwd() / 'certs' / basename
    CERT_PATH           = _resolve_cert.__func__('CERT_PATH', 'mega_chain.pem')
    KEY_PATH            = _resolve_cert.__func__('KEY_PATH',  'mega.key')

    # --- MQTT broker ---
    MQTT_BROKER         = os.getenv('MQTT_BROKER')
    MQTT_PORT           = int(os.getenv('MQTT_PORT', '1883'))
    MQTT_USER           = os.getenv('MQTT_USER') or None
    MQTT_PASS           = os.getenv('MQTT_PASS') or None
    MQTT_TOPIC_PREFIX   = os.getenv('MQTT_TOPIC_PREFIX', 'samsung_dryer')
    HA_DISCOVERY_PREFIX = os.getenv('HA_DISCOVERY_PREFIX', 'homeassistant')
    DEVICE_NAME         = os.getenv('DEVICE_NAME', 'Samsung Dryer')

    # --- Timers ---
    HEALTH_INTERVAL_S    = int(os.getenv('HEALTH_INTERVAL_S', '60'))
    HEARTBEAT_INTERVAL_S = int(os.getenv('HEARTBEAT_INTERVAL_S', '600'))


config = Config()
