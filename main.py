#!/usr/bin/env python3
"""Samsung Dryer Bridge — entry point.

Wires the dryer's OCF CoAP-over-TLS push notifications into MQTT with
Home Assistant discovery. Runs forever; reconnects on TLS errors;
shuts down cleanly on SIGINT / SIGTERM.

All configuration is via environment variables (see samsung_dryer.config
and .env.example). Designed to run under Docker; works fine bare-metal
with .env loaded from cwd."""
import signal
import sys
import threading

import paho.mqtt.client as mqtt

from samsung_dryer.bridge import PushBridge
from samsung_dryer.config import config
from samsung_dryer.discovery import build_discovery_payloads
from samsung_dryer.logger import logger


def main():
    # Validate required config — Docker users misconfigure these
    # routinely; fail fast with a useful error.
    missing = [k for k in ('APPLIANCE_IP', 'MQTT_BROKER')
               if not getattr(config, k)]
    if missing:
        logger.error("missing required env: %s", missing)
        return 2
    for path in (config.CERT_PATH, config.KEY_PATH):
        if not path.exists():
            logger.error("client cert/key not found: %s", path)
            return 2

    logger.info("Samsung Dryer Bridge starting")
    logger.info("  dryer  = %s:%d", config.APPLIANCE_IP,
                config.APPLIANCE_OCF_PORT)
    logger.info("  broker = %s:%d (user=%s)", config.MQTT_BROKER,
                config.MQTT_PORT, config.MQTT_USER or '<anon>')
    logger.info("  topic  = %s/* (HA discovery → %s/+/%s/*)",
                config.MQTT_TOPIC_PREFIX,
                config.HA_DISCOVERY_PREFIX,
                config.MQTT_TOPIC_PREFIX)

    # --- MQTT client + LWT ---
    cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                      client_id=f"{config.MQTT_TOPIC_PREFIX}_bridge")
    if config.MQTT_USER:
        cli.username_pw_set(config.MQTT_USER, config.MQTT_PASS)
    # Headroom on the in-flight queue — the on_connect burst is ~30
    # QoS 1 messages (discovery + state + avail + remote). paho's
    # default is 20 which would block the publish loop until acks
    # land.
    cli.max_inflight_messages_set(100)
    avail_topic = f"{config.MQTT_TOPIC_PREFIX}/availability"
    cli.will_set(avail_topic, payload='offline', qos=1, retain=True)

    discovery = build_discovery_payloads(config.MQTT_TOPIC_PREFIX,
                                         config.HA_DISCOVERY_PREFIX,
                                         config.DEVICE_NAME)

    # --- Bridge ---  (constructed early so on_connect can subscribe)
    cli_placeholder = cli
    bridge_holder = {}

    def on_connect(client, userdata, flags, rc, props=None):
        if rc != 0:
            logger.warning("MQTT connect rc=%s", rc); return
        logger.info("MQTT connected → %s:%d",
                    config.MQTT_BROKER, config.MQTT_PORT)
        # Republish discovery on every (re)connect, retained.
        for topic, payload in discovery:
            client.publish(topic, payload, qos=1, retain=True)
        # Subscribe to command topics. Needs MQTT_USER to have READ
        # permission on <prefix>/cmd/# — without it the broker closes
        # the TCP connection after SUBACK and the bridge loops.
        bridge = bridge_holder.get('bridge')
        if bridge is not None:
            cmd_wildcard = f"{config.MQTT_TOPIC_PREFIX}/cmd/#"
            client.subscribe(cmd_wildcard, qos=1)
            logger.info("subscribed to %s", cmd_wildcard)
            # Re-publish availability + remote_available — LWT may have
            # fired during the disconnect, leaving stale "offline" on
            # the broker even though the bridge is healthy.
            bridge.reassert_availability()

    def on_disconnect(client, userdata, flags, rc, props=None):
        logger.warning("MQTT disconnected rc=%s", rc)

    def on_message(client, userdata, msg):
        bridge = bridge_holder.get('bridge')
        if bridge is None:
            return
        try:
            payload = msg.payload.decode('utf-8', errors='replace').strip()
        except Exception:
            return
        bridge.handle_command(msg.topic, payload)

    cli.on_connect = on_connect
    cli.on_disconnect = on_disconnect
    cli.on_message = on_message
    # Opt-in paho-mqtt packet trace — set PAHO_DEBUG=1 in .env if the
    # broker is hanging up on us and we need to see SUBACK codes /
    # disconnect reasons. Off by default; the noise is significant.
    import os, logging, sys
    if os.getenv('PAHO_DEBUG'):
        paho_logger = logging.getLogger('paho.mqtt.client')
        paho_logger.setLevel(logging.DEBUG)
        paho_handler = logging.StreamHandler(sys.stdout)
        paho_handler.setFormatter(logging.Formatter(
            '%(asctime)s  PAHO   %(message)s', datefmt='%H:%M:%S'))
        paho_logger.addHandler(paho_handler)
        paho_logger.propagate = False
        cli.enable_logger(paho_logger)
    cli.connect_async(config.MQTT_BROKER, config.MQTT_PORT, keepalive=60)
    cli.loop_start()

    bridge = PushBridge(config, cli)
    bridge_holder['bridge'] = bridge

    def health_loop():
        while not bridge.stop.is_set():
            bridge.publish_health()
            if bridge.stop.wait(config.HEALTH_INTERVAL_S):
                break

    def heartbeat_loop():
        while not bridge.stop.is_set():
            if bridge.stop.wait(config.HEARTBEAT_INTERVAL_S):
                break
            bridge.heartbeat_device0()

    threading.Thread(target=health_loop, daemon=True,
                     name='health').start()
    if config.HEARTBEAT_INTERVAL_S > 0:
        threading.Thread(target=heartbeat_loop, daemon=True,
                         name='heartbeat').start()

    def shutdown(*_):
        if bridge.stop.is_set():
            return
        logger.info("shutting down…")
        bridge.stop.set()
        try: bridge.set_availability(False)
        except Exception: pass

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        bridge.run_forever()
    finally:
        try:
            cli.loop_stop()
            cli.disconnect()
        except Exception: pass
        logger.info("stopped")
    return 0


if __name__ == '__main__':
    sys.exit(main())
