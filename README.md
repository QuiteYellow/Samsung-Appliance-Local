#     SmartThings Local

**Local-first Home Assistant integration for newer-generation Samsung dryers.** Everything the SmartThings app shows about your dryer, plus things the official SmartThings cloud HA integration *can't* expose — straight off your LAN, with sub-second updates and no Home Assistant telemetry leaving your network.

### What you get

- **Sub-second state updates.** Cycle starts, pauses, ends, or changes course → Home Assistant reflects it within ~1 second. No polling cadence, no cloud round-trip.
- **Full course control from HA.** Change the dryer's course (14 named courses — Cotton, Synthetics, Bedding, Wool, Towels, Bedding+, …) directly from a Home Assistant dropdown. **The official SmartThings cloud integration doesn't expose this at all.**
- **Start / Pause / Stop from HA**, gated by the dryer's physical Remote Control button so it still matches the appliance's own safety UX.
- **Energy Dashboard ready.** Live watts + cumulative kWh (`total_increasing`) wired straight into HA's Energy Dashboard.
- **Smooth countdown.** Remaining-time ticks every second on the HA card (client-side extrapolated between the dryer's coarser pushes) instead of jumping in 1-minute steps.
- **~25 auto-discovered entities** — machine state, job state, dry level, dry time, child lock, wrinkle prevent, alarms, firmware revision, energy, course, remaining time, the lot. Zero YAML.
- **Your state stays on your LAN.** Every byte of dryer state flows dryer → your MQTT broker → HA. Samsung's cloud never sees a request from Home Assistant. *(The dryer itself still maintains its own TLS session to Samsung — that's the appliance's design, not ours, and our bridge can't change it.)*

### Under the hood

Push-mode bridge: one sustained TLS session to the dryer, CoAP Observe (RFC 7641) on ~11 of the dryer's `/<x>/vs/0` resources, every state change forwarded to MQTT in roughly a second. Home Assistant auto-discovers ~25 entities — no HA YAML required.

---

## Part 1 — Is your dryer compatible?

This is the first thing to check, because if you're on the older firmware, nothing below applies and you want a different project.

Run this against your appliance:

```sh
nmap -Pn -p 8888,49152-49160 "$APPLIANCE_IP"
```

Read the result:

- **`8888/tcp` open** → older firmware (roughly 2018–2022), which uses a token-based HTTPS API on `:8888`. **This project does not target that.** Stop here.
- **`8888/tcp` closed, `49154/tcp` (or any other 4915x port) open** → newer firmware (roughly 2023 onwards). Keep reading — this is what the bridge talks to.

### Has it been tested on my exact model?

Built and tested against one specific Samsung dryer:`DA_WM_TP2_20_COMMON` platform, `mnid=0AJT`, `setupid=310`. Other appliances on the same firmware family (washers, dishwashers, AC units) almost certainly speak the same protocol, but the resource paths and which writes actually persist will vary or it may not even work at all I also tried a Samsung oven on the same firmware family, it was much more locked down — none of the high-port OCF sockets the dryer exposes were reachable.

---

## Part 2 — What you'll get

### Reads

Every value the SmartThings app shows for the dryer, locally:

- Machine state (idle / active / paused), job state, progress %
- Remaining time (with smooth per-second client-side extrapolation between the dryer's coarser pushes)
- Energy: kWh cumulative and instantaneous watts — wired straight into the HA Energy Dashboard
- Course / dryer mode, as a human-readable name
- Child Lock, Power, Remote Control, alarms, dry level, dry time, country code, firmware revision, etc.

Push latency to HA is roughly 1 second for any value that changes on a state transition. `remainingTime` is only pushed by the dryer on transitions (start, pause, end, course change); the bridge extrapolates it locally so the countdown ticks smoothly.

### Writes

| Capability | Works? | Notes |
|---|---|---|
| Wrinkle Prevent toggle | ✅ | persists |
| Start / Pause / Stop cycle | ✅ | via `/operational/state/vs/0`, **needs Remote Control on** |
| Change course | ✅ | via `/st/dryercourse/vs/0`, **needs Remote Control on**. Notably, this is *not* available via the SmartThings Cloud HA integration. |
| Power on/off | ❌ | accepted (2.04) but reverts within seconds — hardware-mirrored |
| Toggle Remote Control | ❌ | same — hardware-mirrored |
| Toggle Child Lock | ❌ | same — hardware-mirrored |

"Hardware-mirrored" means a physical button on the dryer's front panel that the firmware reads directly. The OCF resource exists for state mirroring only — writes get accepted out of politeness but the firmware overwrites them from the button position.

### What this can't do

- **Cleanly work without cloud reachability.** The recommendation here is **all or nothing.** Either let the dryer reach Samsung's cloud normally (you get a rock-solid local session with sub-second push updates), or fully block it (firewall the dryer's MAC, no DNS — the local CoAPS session tears down every ~30 s and the bridge reconnects, so sensors update in the 28-second windows between reconnects). Don't try to half-block: if DNS resolves but the cloud IPs are unreachable (sinkhole, IP-level firewall), the dryer holds a stable local session but stops emitting Observe pushes entirely. Worst of both worlds. `findings.md` has the diagnostic that pinned this down.
- **Decrypt, read, or modify the cloud session.** TLS is mutually authenticated and the dryer pins Samsung's OCF Root CA. Without flash-level extraction of the dryer's cloud-side private key, you can't MITM it.
- **Replace SmartThings for first-time setup.** You still need the SmartThings app and a hub once, to get the dryer onto Wi-Fi and to extract the Hub UUID we use below.

---

## Part 3 — Understanding the cert

The bridge authenticates to the dryer with a **TLS client cert**. There is no PSK, no token, no API key — the cert *is* the credential.

The dryer's OCF stack (a Samsung fork of TizenRT iotivity) decides "who you are" by running `memmem` over the raw bytes of your cert's Subject DN, looking for the literal string `uuid:`. So a cert with `CN=urn:uuid:<HUB_UUID>` makes the dryer believe you *are* that UUID. SubjectAltName is not consulted.

The dryer's baseline ACL grants `perm=31` (CRUDN — full access) on`href=*` to **the SmartThings hub's** UUID. So once your cert tells the dryer you're the hub, you have read+write on every resource, with no ACL setup needed afterwards.

> The baseline ACL also contains wildcard `R+W` ACEs on a handful of paths like `/hass/state/vs/0` and `/hass/command/vs/0`. They look promising but they're not. The handlers behind them return `4.04` in every state observed.

So the whole task is: **get a cert whose Subject DN contains your SmartThings hub's UUID, signed by a CA the dryer trusts.** Two ingredients:

1. Your **Hub UUID** (see below).
2. The **`AC14K_M` intermediate CA cert + key.** Samsung's diagnostic intermediate. Published in [cicciovo/homebridge-samsung-airconditioner](https://github.com/cicciovo/homebridge-samsung-airconditioner) for the older port-8888 Samsung AC API. The same cert is still trusted as an authentication-eligible intermediate on the newer OCF firmware. Commonly referenced as `cert_1.pem` + `key.pem`. The dryer accepts any leaf signed by this intermediate as authentication-eligible; access is then gated by the UUID embedded in the leaf.

---

## Fast path — `bootstrap.py`

Most of Parts 4 and 5 can be automated. Once you've placed
`ac14k_m.pem` and its key under `./certs/` (from the
[cicciovo/homebridge-samsung-airconditioner](https://github.com/cicciovo/homebridge-samsung-airconditioner)
repo linked above), run:

```sh
python -m venv .venv
.venv/bin/pip install -r requirements-bootstrap.txt
.venv/bin/python bootstrap.py
```

It will:

1. Prompt for your dryer's IP + OCF port.
2. Locate `ac14k_m.pem` + key in `./certs/`.
3. Run the anonymous `/oic/sec/doxm` read described in Part 4 Option A
   to discover your hub UUID. If that fails (e.g. a different firmware
   revision closed the wildcard ACE, or the bridge is competing for the
   TLS session), it falls back to asking you for the UUID — use Option
   B or C from Part 4 to obtain it.
4. Generate the leaf cert with the right Subject DN, EKU, SAN entries,
   and Samsung role OID, signed by AC14K_M with SHA-1 via `openssl`.
   Writes `certs/mega.key` + `certs/mega_chain.pem`.
5. Optionally write `.env` from `.env.example` with the IP/port filled
   in (you still need to set `MQTT_BROKER` / `MQTT_USER` / `MQTT_PASS`).

The only external dependency beyond the runtime `requirements.txt` is
the `openssl` CLI, which is used for cert generation (so SHA-1 signing
keeps working independent of `python-cryptography`'s policy).

**Don't run `bootstrap.py` while the bridge container or `main.py` is
up** — the dryer's OCF stack allows one TLS session at a time, and a
second handshake will get the dryer to immediately close the new
socket after CSM. Stop the bridge first, run bootstrap, then start the
bridge again.

If you'd rather do this by hand (or the script fails), the next two
parts spell out exactly what it does. They're worth reading either way —
the script is just an executable version of them.

---

## Part 4 — Finding your Hub UUID

It looks like `ef43c5a5-b5db-4127-ba5d-d18c92600e22`.

### Read the hub UUID from `/oic/sec/doxm` anonymously *(the magic escalation path)*

It's what `bootstrap.py` does automatically.

The dryer's baseline ACL contains a **wildcard ACE** (`subjectuuid=*`, `perm=2` Read) granting *any* authenticated peer read access to `/oic/sec/doxm` and `/oic/sec/pstat`. So you don't need to be the hub to read doxm — you just need a TLS handshake that validates back to AC14K_M.

And `/oic/sec/doxm.devowneruuid` is literally the SmartThings hub's UUID (OCF spec §8.4 — it's the UUID of the entity that onboarded the device).

So the escalation chain is:

1. Sign a leaf with AC14K_M whose Subject DN contains **no** `uuid:` substring. The dryer's `memmem` scan finds nothing → it treats you as an anonymous-but-CA-trusted peer.
2. TLS handshake. Chain validates → connection accepted.
3. `GET /oic/sec/doxm`. Wildcard ACE 3 grants you Read → `2.05` with a CBOR payload.
4. Parse → `devowneruuid` is your hub UUID.
5. Now mint the *real* leaf with `CN=urn:uuid:<HUB_UUID>...` (Part 5) and reconnect. This time the `memmem` scan resolves you to the hub UUID, and you inherit ACE 0's CRUDN.

That's it. Two TLS handshakes (one anonymous to discover, one privileged to operate) and you're authenticated as the hub. The whole thing falls out of the firmware's combination of:

- A wildcard ACE on doxm (probably intended for OCF onboarding discovery).
- The naive `memmem(subject_dn, "uuid:")` cert→peerId mapping.
- A CA private key (`AC14K_M`) that leaked publicly years ago and was never rotated.

> **Note:** `GET /oic/sec/acl` is *not* readable anonymously — that one requires hub identity (and returns `4.01` to anonymous peers). The wildcard ACE specifically covers doxm and pstat, not acl. If you go looking, look at the right resource.

Hold onto this UUID — you need it in the next part.

---

## Part 5 — Generating your leaf cert

You'll end up with two files:

- `certs/mega.key` — leaf private key (RSA 2048 is fine)
- `certs/mega_chain.pem` — concatenation of `<leaf>.pem` + `AC14K_M.pem`

The recipe below matches the canonical cert known to authenticate (and is what `bootstrap.py` produces verbatim):

- **Subject**: `CN=urn:uuid:<HUB_UUID>, O=Samsung Electronics, C=KR`. The Subject DN must contain the literal bytes `uuid:<HUB_UUID>` somewhere — the `O`/`C` fields are defensive padding.
- **Issuer**: AC14K_M.
- **Signature**: **SHA-1 with RSA**. The dryer's TizenRT mbedtls build rejects SHA-256 leaves in some firmware revisions; SHA-1 works everywhere observed. Counterintuitive but correct, and the reason `bootstrap.py` shells out to `openssl` instead of using `python-cryptography` (which removed SHA-1 signing in v43).
- **Extensions**:
  - Basic Constraints: `CA:FALSE`.
  - Key Usage: `digitalSignature, keyEncipherment`.
  - Extended Key Usage: `clientAuth, serverAuth, 1.3.6.1.4.1.51414.0.1.2` (the Samsung iot-Identity OID).
  - SubjectAltName: four entries — `URI:urn:uuid:<HUB_UUID>`, `URI:uri:uuid:<HUB_UUID>`, `URI:uuid:<HUB_UUID>`, and `DNS:<HUB_UUID>`. Defensive — the dryer doesn't read SAN, but adjacent tooling (and possibly future firmware) might.
  - Custom extension: Samsung role OID `1.3.6.1.4.1.51414.1.3` with any role string (e.g. `samsung.role.hub`). The string is empirically irrelevant — only the OID's presence matters.
- **Validity**: at least a year ahead of the dryer's clock; the canonical leaf uses five years.

Drop the resulting `mega.key` and `mega_chain.pem` into `./certs/` (or `/config/` if you're going straight to Docker). The bridge auto-detects both paths; override with `CERT_PATH` / `KEY_PATH` in `.env` if you keep them somewhere else.

### Do I need to install any ACEs after this?

No, and you probably *shouldn't*. The hub UUID is the only privileged identity persistently in the ACL — once your leaf maps to it via the `memmem` trick, you inherit ACE 0's CRUDN-on-everything and that's it.

You *can* `POST /oic/sec/acl` to add ACEs for additional identities — the dryer returns `2.04 Changed` and the new ACE appears on reads. **But they don't persist across a power cycle.** Only ACE 0 (the hub) and ACE 4 (a second factory-baked privileged UUID) survive reboot. Don't build flows that depend on installed ACEs sticking around.

If you ever feel like you've lost access after a dryer reboot, you haven't — just reconnect with the hub-UUID leaf and you still have CRUDN through ACE 0.

---

## Part 6 — Running it

Two paths: bare metal (good for the first test, so you can read logs directly), and Docker (the real deployment).

### Bare metal — the first run

```sh
cp .env.example .env
# edit: APPLIANCE_IP, MQTT_BROKER, MQTT_USER, MQTT_PASS

python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python main.py
```

If everything's right, you'll see this in the first ~3 seconds:

```
08:12:48  INFO   samsung_dryer  Samsung Dryer Bridge starting
08:12:50  INFO   samsung_dryer  TLS connected — subscribing 11 paths
08:12:51  INFO   samsung_dryer  seeded /device/0 → 25 links; sensors live
```

Now flip to Home Assistant: **Settings → Devices & Services → MQTT**  should show a "Samsung Dryer" device with all entities populated.

If you don't see entities: verify the MQTT broker is reachable, that the MQTT user has READ permission on `samsung_dryer/cmd/#` (the broker will silently drop the TCP connection shortly after SUBSCRIBE if not — it looks like a network problem in logs), and that HA's MQTT integration discovery prefix matches `HA_DISCOVERY_PREFIX`.

### Docker — the real deployment

```sh
docker compose up -d --build
docker compose logs -f
```

The container is outbound-only. No ports exposed. It needs egress to the dryer's IP and to your MQTT broker, plus read access to `./certs/`.

### Deploying to a remote Linux host (Unraid, etc.)

```sh
# Once: upload the certs onto the remote (deploy.sh excludes them).
ssh "$SSH_HOST" mkdir -p "$REMOTE_DIR/certs"
scp certs/mega_chain.pem certs/mega.key "$SSH_HOST:$REMOTE_DIR/certs/"

# Each deploy: rsync source + .env, rebuild container on the host.
./deploy.sh
```

Set `SSH_HOST` and `REMOTE_DIR` in `.env`.

### Verifying with `mosquitto_sub`

If you want to watch the raw traffic to convince yourself it's working:

```sh
# Bridge → broker (state, availability, health)
mosquitto_sub -h $MQTT_BROKER -u $MQTT_USER -P $MQTT_PASS \
    -t 'samsung_dryer/#' -v

# HA discovery configs
mosquitto_sub -h $MQTT_BROKER -u $MQTT_USER -P $MQTT_PASS \
    -t 'homeassistant/+/samsung_dryer/#' -v
```

---

## Part 7 — Traps to avoid

These look like obvious improvements. They are not. Each one was tried and broke the iotivity stack in subtle ways.

- **Don't add Observe subscriptions on OCF-standard `/<x>/0` paths**
  (`/power/0`, `/kidslock/0`, `/operational/state/0`). They register successfully — but their state machines never push. Use the Samsung `/<x>/vs/0` siblings instead, which do.
- **Don't run `main.py` locally while the container is up.** Same MQTT`client_id` means they'll fight at ~1 Hz forever. If HA seems to flap, `pgrep -af main.py` before debugging anything else.

---

## Reference

### Configuration

Env-var driven (no CLI flags). Edit `.env`.

| Knob | Meaning |
|---|---|
| `APPLIANCE_IP` | Dryer LAN IP |
| `APPLIANCE_OCF_PORT` | Defaults to `49154` |
| `MQTT_BROKER` / `MQTT_PORT` | Broker — typically your HA Mosquitto add-on |
| `MQTT_USER` / `MQTT_PASS` | Broker creds |
| `MQTT_TOPIC_PREFIX` | Defaults to `samsung_dryer`; doubles as the HA device identifier (changing it re-keys the device) |
| `HA_DISCOVERY_PREFIX` | HA discovery topic root (default `homeassistant`) |
| `DEVICE_NAME` | Friendly name on the HA device card |
| `HEALTH_INTERVAL_S` | Seconds between `bridge/health` publishes (default 60) |
| `HEARTBEAT_INTERVAL_S` | Seconds between belt-and-braces `/device/0` reseed; `0` disables (default 600) |
| `CERT_PATH` / `KEY_PATH` | Override cert lookup (auto-detects `/config/` then `./certs/`) |

### MQTT topics — outgoing (bridge → broker)

| Topic | Retain | When |
|---|---|---|
| `<prefix>/availability` | yes | `online` after seed; `offline` on disconnect (LWT) |
| `<prefix>/remote_available` | yes | `online` iff bridge is up AND Remote Control on the dryer is on. Gates the control entities. |
| `<prefix>/state` | yes | **Only when the flat sensor view actually changes.** Idle: silent. Active: ticks every ~15 s as `completion_time` decrements. |
| `<prefix>/bridge/health` | yes | Every `HEALTH_INTERVAL_S` seconds (default 60) |
| `<ha_prefix>/{sensor,binary_sensor,switch,button,select}/<prefix>/.../config` | yes | At every MQTT connect — HA discovery configs |

### MQTT topics — incoming (bridge subscribes)

The bridge subscribes to `<prefix>/cmd/#`. **The MQTT user must have
READ permission on that subtree** — without it the broker accepts the
SUBSCRIBE then closes the TCP connection shortly after, which looks
identical to a network issue. Check broker logs first if writes never
land.

| Topic | Payloads | Result |
|---|---|---|
| `<prefix>/cmd/wrinkle_prevent` | `On`, `Off` | POST `/washer/vs/0` `{wrinklePrevent: <payload>}` — persists |
| `<prefix>/cmd/operational_state` | `Run`, `Pause`, `Ready` | POST `/operational/state/vs/0`. Requires Remote Control. |
| `<prefix>/cmd/dryer_mode` | human course name (e.g. `Cotton`) | Translated via `samsung_dryer/sensors.py::COURSE_NAMES` to `Course_HH`, then POST. Requires Remote Control. |

### Home Assistant entities

| Type | Count | Examples |
|---|---|---|
| `sensor` | 17 | Machine state, Job state, Energy kWh (`total_increasing` → Energy Dashboard), Power (W), Remaining minutes, Completion time, Dryer mode |
| `binary_sensor` | 4 | Running, Power switch, Child lock, Remote control |
| `switch` | 1 | Wrinkle prevent — always available |
| `button` | 3 | Start, Pause, Stop — disabled in HA when Remote Control is off |
| `select` | 1 | Course (drop-down of all 14 named courses) — disabled when Remote Control is off |

Gated control entities use HA's `availability_mode: all` against`<prefix>/availability` and `<prefix>/remote_available`. Flip RemoteControl on the dryer's front panel and the buttons light up in HA.

### Repo layout

```
main.py                Bridge entry point
samsung_dryer/         Bridge package (coap codec, sensors, MQTT, HA discovery)
Dockerfile             Build container
docker-compose.yml     One-service compose (Unraid-flavored mounts)
deploy.sh              rsync + ssh docker compose up
.env.example           Template — copy to .env and fill in
findings.md            Research writeup — how the local API was
                       reverse-engineered, including what *doesn't* work
```

`certs/` is gitignored. Drop the privileged client cert + key there; the container mounts that directory read-only at `/config`.

---

## Contributing

Patches and corrections welcome — especially:

- Confirmations or refutations on other Samsung appliance models or firmware versions. An `nmap` fingerprint + a `GET /oic/d` dump is enough to know if you're on the same firmware family.
- More course-code → name mappings for other dryer models or tables (currently only `Table_03` is captured; see samsung_dryer/sensors.py:COURSE_NAMES`).
- A proper Home Assistant custom component wrapping the bridge so there's a config flow instead of YAML / env editing.

If you submit a PR, please don't include real device UUIDs, MACs, IPs, or bearer tokens. Use the placeholders from `.env.example`.
