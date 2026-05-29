"""Push-mode bridge: OCF CoAP Observe → MQTT.

  Dryer  ──CoAP Observe (RFC 7641, ~11 /vs/0 paths)──►  PushBridge
                                                            │
                                                            ▼
                                                          MQTT broker
                                                            │
                                                            ▼
                                                       Home Assistant

State changes push from the dryer over a sustained TLS session. The
bridge updates an in-memory link dict, recomputes flat sensors, and
publishes to MQTT ONLY when the flat-sensor dict actually changes."""
import json
import signal
import socket
import ssl
import threading
import time

import cbor2

from .coap import (
    URI_PATH, OBSERVE, CF, CSM, enc_opts, enc_tcp, read_tcp,
    parse_opts, fmt_code,
)
from .discovery import command_handlers
from .sensors import flatten_sensors, index_links
from .logger import logger


# Paths we subscribe Observe on. Only Samsung's /vs/0 siblings actually
# push notifications; the OCF-standard /<x>/0 paths accept registration
# silently but never fire.
OBSERVE_PATHS = [
    ['operational', 'state', 'vs', '0'],     # state, remainingTime, progress
    ['power',       'vs', '0'],              # power on/off
    ['kidslock',    'vs', '0'],              # child lock
    ['remotectrl',  'vs', '0'],              # remote control enabled
    ['energy',      'consumption', 'vs', '0'],  # W + kWh
    ['course',      'vs', '0'],              # course/mode
    ['washer',      'vs', '0'],              # dryLevel, dryTime, type
    ['diagnosis',   'vs', '0'],
    ['alarms',      'vs', '0'],
    ['st',          'dryercourse', 'vs', '0'],
    ['wm',          'jobbeginingstatus', 'vs', '0'],
]


class PushBridge:
    """Single sustained TLS session to the dryer; reader thread demuxes
    incoming notifications by token; MQTT publishes on real change only.

    Reconnects with exponential backoff on TLS errors. Publishes
    availability=offline (LWT and explicit) when the dryer side is
    unreachable so HA marks entities unavailable instead of trusting
    stale state."""

    def __init__(self, config, mqtt_client):
        self.config = config
        self.mqtt = mqtt_client
        self.sock = None
        self.send_lock = threading.Lock()
        self.tok_counter = 0
        self.links = {}              # href → rep
        self.last_state_pub = None
        self.last_remote_pub = None  # last samsung_dryer/remote_available value
        self.observe_tokens = {}     # token bytes → href
        self.pending = {}            # token bytes → (Event, container)
        self.stop = threading.Event()
        self.started_ts = time.time()
        self.session_started_ts = None
        self.last_change_ts = None
        self.last_seed_ts = None
        self.notif_count = 0
        self.connect_count = 0
        self.error_count = 0
        self._publish_gate = False   # opens after first /device/0 seed
        # Dryer pushes /operational/state/vs/0 on state transitions but
        # not on remainingTime ticks. Anchor = (ts, total_seconds) at last
        # push; we extrapolate downward while machine_state == 'active'.
        self._remaining_anchor = None

        p = config.MQTT_TOPIC_PREFIX
        self.state_topic    = f"{p}/state"
        self.avail_topic    = f"{p}/availability"
        self.remote_topic   = f"{p}/remote_available"
        self.health_topic   = f"{p}/bridge/health"
        self.cmd_handlers   = command_handlers()
        self.cmd_topic_prefix = f"{p}/cmd/"

    # ---- token / TLS plumbing ----------------------------------------

    def _next_tok(self):
        self.tok_counter = (self.tok_counter + 1) & 0xFFFFFFFF
        return self.tok_counter.to_bytes(4, 'big')

    def _open_tls(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        # Mega client cert is SHA-1 signed by Samsung's AC14K_M CA.
        # OpenSSL 3.x's default SECLEVEL=2 rejects SHA-1 in cert chains
        # (CA_MD_TOO_WEAK), so on Docker (OpenSSL 3) we drop the
        # security level. Older OpenSSL (1.x, common on macOS Python
        # 3.9) doesn't recognize the @SECLEVEL token and would refuse
        # the whole cipher string — fall through silently.
        try:
            ctx.set_ciphers('DEFAULT:@SECLEVEL=0')
        except ssl.SSLError:
            pass
        ctx.load_cert_chain(certfile=str(self.config.CERT_PATH),
                            keyfile=str(self.config.KEY_PATH))
        s = ctx.wrap_socket(socket.create_connection(
            (self.config.APPLIANCE_IP, self.config.APPLIANCE_OCF_PORT),
            timeout=10))
        s.send(CSM)
        s.settimeout(2)
        try: read_tcp(s)
        except (socket.timeout, ConnectionError): pass
        s.settimeout(None)
        self.sock = s

    def _close_sock(self):
        if self.sock:
            try: self.sock.close()
            except Exception: pass
            self.sock = None
        for tok, (ev, container) in list(self.pending.items()):
            container['code'] = None
            container['payload'] = b''
            container['err'] = 'socket closed'
            ev.set()
        self.pending.clear()
        self.observe_tokens.clear()

    # ---- request primitives ------------------------------------------

    def _send(self, path_segs, observe=False, token=None,
              method=0x01, body=None):
        """Send a CoAP request. method: 0x01 GET, 0x02 POST. body: dict
        (CBOR-encoded) or None."""
        if token is None:
            token = self._next_tok()
        opts = [(URI_PATH, s.encode()) for s in path_segs]
        if observe:
            opts.append((OBSERVE, b''))
        payload = b''
        if body is not None:
            payload = cbor2.dumps(body)
            opts.append((CF, bytes([60])))  # application/cbor
        with self.send_lock:
            self.sock.send(enc_tcp(method, token=token,
                                   opts_b=enc_opts(opts),
                                   payload=payload))
        return token

    def _oneshot(self, method, path_segs, body=None, timeout=8):
        tok = self._next_tok()
        ev = threading.Event()
        container = {}
        self.pending[tok] = (ev, container)
        try:
            self._send(path_segs, observe=False, token=tok,
                       method=method, body=body)
            if not ev.wait(timeout):
                raise TimeoutError(
                    f"timeout waiting for /{'/'.join(path_segs)}")
            if 'err' in container:
                raise ConnectionError(container['err'])
            return container['code'], container['payload']
        finally:
            self.pending.pop(tok, None)

    def oneshot_get(self, path_segs, timeout=8):
        """Send GET, block for matching token, return (code, payload)."""
        return self._oneshot(0x01, path_segs, timeout=timeout)

    def post(self, path_segs, body, timeout=8):
        """POST with CBOR body. Returns (code, payload)."""
        return self._oneshot(0x02, path_segs, body=body, timeout=timeout)

    def subscribe(self, path_segs):
        tok = self._send(path_segs, observe=True)
        href = '/' + '/'.join(path_segs)
        self.observe_tokens[tok] = href

    # ---- response handling -------------------------------------------

    def _on_response(self, code, tok, opts_b, pl):
        # CoAP-TCP signaling frames (RFC 8323 §5) — CSM=0xE1, Ping=0xE2,
        # Pong=0xE3, Release=0xE4, Abort=0xE5. The dryer doesn't reply
        # to Pings, and we don't send any, so we just drop them if they
        # ever show up.
        if 0xE1 <= code <= 0xE5:
            return
        # Pending one-shot GET first
        rec = self.pending.get(tok)
        if rec is not None:
            ev, container = rec
            container['code'] = code
            container['payload'] = pl
            ev.set()
            return
        # Observation notification
        href = self.observe_tokens.get(tok)
        if href is not None:
            if code != 0x45:
                logger.warning("observe %s: non-2.05 %s",
                               href, fmt_code(code))
                return
            try:
                rep = cbor2.loads(pl) if pl else {}
            except Exception as e:
                logger.warning("observe %s: cbor decode err %s", href, e)
                return
            if not isinstance(rep, dict):
                return
            self.links[href] = rep
            if href == '/operational/state/vs/0':
                self._capture_remaining_anchor(rep)
            self.notif_count += 1
            self.last_change_ts = time.time()
            self.maybe_publish_state()
            return
        # Stale token after reconnect — drop quietly.

    def reader_loop(self):
        try:
            while not self.stop.is_set():
                self.sock.settimeout(60)
                try:
                    code, tok, opts_b, pl = read_tcp(self.sock)
                except socket.timeout:
                    continue
                self._on_response(code, tok, opts_b, pl)
        except (ssl.SSLError, ConnectionError, OSError) as e:
            logger.warning("reader: %s", e)
            self.error_count += 1

    # ---- session lifecycle -------------------------------------------

    def session(self):
        self._open_tls()
        self.session_started_ts = time.time()
        self.connect_count += 1
        logger.info("TLS connected — subscribing %d paths", len(OBSERVE_PATHS))

        self._publish_gate = False
        rt = threading.Thread(target=self.reader_loop, daemon=True,
                              name='reader')
        rt.start()

        # Register observations first — initial Observe responses carry
        # current rep, so they pre-populate self.links.
        for path in OBSERVE_PATHS:
            self.subscribe(path)
            time.sleep(0.05)

        # Seed via /device/0 — covers resources we don't observe.
        code, pl = self.oneshot_get(['device', '0'], timeout=10)
        if code != 0x45:
            raise RuntimeError(f"/device/0 -> {fmt_code(code)}")
        body = cbor2.loads(pl)
        for href, rep in index_links(body).items():
            # Don't clobber reps already received via observation —
            # those are at least as fresh.
            self.links.setdefault(href, rep)
        self.last_seed_ts = time.time()

        self._publish_gate = True
        self.maybe_publish_state(force=True)
        self.set_availability(True)
        logger.info("seeded /device/0 → %d links; sensors live",
                    len(self.links))

        rt.join()

    def heartbeat_device0(self):
        """Belt-and-braces re-seed of resources we don't observe."""
        if self.sock is None:
            return
        try:
            code, pl = self.oneshot_get(['device', '0'], timeout=10)
        except Exception as e:
            logger.warning("heartbeat /device/0: %s", e)
            return
        if code != 0x45:
            logger.warning("heartbeat /device/0: %s", fmt_code(code))
            return
        body = cbor2.loads(pl)
        for href, rep in index_links(body).items():
            if href not in self.observe_tokens.values():
                # Authoritative for non-observed resources
                self.links[href] = rep
        self.last_seed_ts = time.time()
        self.maybe_publish_state()

    # ---- MQTT publishing ---------------------------------------------

    def _capture_remaining_anchor(self, rep):
        rem = rep.get('x.com.samsung.da.remainingTime')
        if not isinstance(rem, str):
            return
        try:
            h, m, s = rem.split(':')
            self._remaining_anchor = (time.time(),
                                      int(h) * 3600 + int(m) * 60 + int(s))
        except (ValueError, AttributeError):
            pass

    def _extrapolate_remaining(self, sensors):
        if sensors.get('machine_state') != 'active' or self._remaining_anchor is None:
            return sensors
        ts, total = self._remaining_anchor
        remaining = max(0, int(total - (time.time() - ts)))
        h, rest = divmod(remaining, 3600)
        m, s = divmod(rest, 60)
        sensors = dict(sensors)
        sensors['completion_time'] = f"{h}:{m:02d}:{s:02d}"
        sensors['completion_minutes'] = h * 60 + m + (1 if s > 0 else 0)
        return sensors

    def maybe_publish_state(self, force=False):
        if not force and not self._publish_gate:
            return
        sensors = self._extrapolate_remaining(flatten_sensors(self.links))
        if not force and sensors == self.last_state_pub:
            return
        self.last_state_pub = sensors
        self.mqtt.publish(self.state_topic,
                          json.dumps(sensors).encode(),
                          qos=1, retain=True)
        # Drive the remote-control availability gate from the just-
        # published state. HA disables Start/Pause/Stop/Course when
        # remote_available is "offline".
        self.publish_remote_available(sensors.get('remote_control_binary'))
        if not force:
            logger.info(
                "state changed (machine=%s power=%sW energy=%skWh notif#%d)",
                sensors.get('machine_state'),
                sensors.get('power_watts'),
                sensors.get('energy_kwh'),
                self.notif_count,
            )

    def publish_remote_available(self, remote_on, force=False):
        """Publish <prefix>/remote_available — online iff bridge is up
        AND the dryer's Remote Control physical switch is on. The
        gated entities use this with availability_mode=all. Pass
        force=True to re-publish even if the value hasn't changed
        (used on MQTT reconnect)."""
        value = 'online' if remote_on else 'offline'
        if not force and value == self.last_remote_pub:
            return
        self.last_remote_pub = value
        try:
            self.mqtt.publish(self.remote_topic, value,
                              qos=1, retain=True)
            logger.info("remote_available → %s", value)
        except Exception as e:
            logger.warning("remote_available publish: %s", e)

    def reassert_availability(self):
        """Called on MQTT (re)connect. If the bridge has a healthy TLS
        session and a published state, re-publish availability + remote
        availability so HA doesn't see them stuck offline from a stale
        LWT firing."""
        if self.sock is None or self.last_state_pub is None:
            return
        self.set_availability(True)
        self.publish_remote_available(
            self.last_state_pub.get('remote_control_binary'),
            force=True)

    def set_availability(self, online):
        try:
            self.mqtt.publish(self.avail_topic,
                              'online' if online else 'offline',
                              qos=1, retain=True)
        except Exception as e:
            logger.warning("avail publish: %s", e)
        if not online:
            # Bridge offline → remote_available offline too.
            self.last_remote_pub = None  # force re-publish on next state
            try:
                self.mqtt.publish(self.remote_topic, 'offline',
                                  qos=1, retain=True)
            except Exception: pass

    # ---- MQTT command handling ---------------------------------------

    def handle_command(self, topic, payload):
        """Called from main.py's on_message for any <prefix>/cmd/* topic."""
        if not topic.startswith(self.cmd_topic_prefix):
            return
        suffix = topic[len(self.cmd_topic_prefix) - len('cmd/'):]
        handler = self.cmd_handlers.get(suffix)
        if handler is None:
            logger.warning("unknown command topic: %s", topic); return
        result = handler(payload)
        if result is None:
            logger.warning("rejected command %s payload=%r", topic, payload)
            return
        path_segs, body = result
        if self.sock is None:
            logger.warning("command %s: no TLS session", topic); return
        try:
            code, pl = self.post(path_segs, body, timeout=8)
        except Exception as e:
            logger.warning("command %s POST failed: %s", topic, e)
            return
        logger.info("command %s payload=%r → %s",
                    suffix, payload, fmt_code(code))
        # The dryer typically pushes an Observe notification with the
        # new state within a second — no explicit re-read needed.

    def publish_health(self):
        now = time.time()
        h = {
            'mode':              'push',
            'connect_count':     self.connect_count,
            'error_count':       self.error_count,
            'notif_count':       self.notif_count,
            'last_change_age_s': (round(now - self.last_change_ts, 1)
                                  if self.last_change_ts else None),
            'last_seed_age_s':   (round(now - self.last_seed_ts, 1)
                                  if self.last_seed_ts else None),
            'session_age_s':     (round(now - self.session_started_ts, 1)
                                  if self.session_started_ts else None),
            'uptime_seconds':    round(now - self.started_ts, 0),
        }
        try:
            self.mqtt.publish(self.health_topic, json.dumps(h).encode(),
                              qos=0, retain=True)
        except Exception as e:
            logger.warning("health publish: %s", e)

    # ---- top-level loop ----------------------------------------------

    def _publish_tick_loop(self):
        # Keeps extrapolated completion_time fresh while a cycle runs.
        # maybe_publish_state self-guards via _publish_gate, so this is
        # safe during reconnects.
        while not self.stop.wait(15.0):
            try:
                self.maybe_publish_state()
            except Exception as e:
                logger.warning("publish tick: %s", e)

    def run_forever(self):
        threading.Thread(target=self._publish_tick_loop, daemon=True,
                         name='publish-tick').start()
        backoff = 1.0
        while not self.stop.is_set():
            try:
                self.session()
                backoff = 1.0
            except Exception as e:
                self.error_count += 1
                logger.warning("session error: %s", e)
            self._close_sock()
            self.set_availability(False)
            self.session_started_ts = None
            if self.stop.is_set():
                break
            wait = min(backoff, 30.0)
            logger.info("reconnect in %.0fs", wait)
            if self.stop.wait(wait):
                break
            backoff = min(backoff * 2, 30.0)
