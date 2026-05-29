"""Home Assistant MQTT discovery payloads.

Publishes one device with sensor + binary_sensor + switch + button +
select entities, all keyed to the same state topic via `value_template`
for reads, and to per-action MQTT command topics under
`<prefix>/cmd/...` for writes. Entities re-publish on every (re)connect;
topics are retained so HA picks them up whenever its MQTT integration
connects.

Two availability topics are in play:
  <prefix>/availability     — bridge alive (LWT)
  <prefix>/remote_available — bridge alive AND dryer's Remote Control
                              physical switch is on.

Control entities that the dryer only honours with Remote Control
enabled (Start / Pause / Stop, Course select) reference BOTH topics
with `availability_mode: all`, so HA disables them in the UI when
remote control is off. Wrinkle Prevent works any time so it only
references the base availability topic."""
import json

from .sensors import course_options


def device_block(topic_prefix, device_name):
    return {
        'identifiers':  [topic_prefix],
        'name':         device_name,
        'manufacturer': 'Samsung',
        'model':        'OCF dryer (TizenRT-iotivity)',
    }


# (key, friendly name, extra-config-dict)
SENSORS = [
    ('machine_state',       'Machine state',       {'icon': 'mdi:tumble-dryer'}),
    ('job_state',           'Job state',           {}),
    ('progress',            'Progress',            {}),
    ('progress_percentage', 'Progress percent',
        {'unit_of_measurement': '%', 'state_class': 'measurement'}),
    ('completion_time',     'Completion time',     {'icon': 'mdi:timer-sand'}),
    ('completion_minutes',  'Remaining minutes',
        {'unit_of_measurement': 'min', 'device_class': 'duration',
         'state_class': 'measurement'}),
    ('delay_end_time',      'Delay end time',      {'icon': 'mdi:timer'}),
    ('power_state',         'Power state',         {}),
    ('power_watts',         'Power',
        {'unit_of_measurement': 'W', 'device_class': 'power',
         'state_class': 'measurement'}),
    ('energy_kwh',          'Energy',
        {'unit_of_measurement': 'kWh', 'device_class': 'energy',
         'state_class': 'total_increasing'}),
    ('dryer_mode',          'Dryer mode',          {}),
    ('dry_level',           'Dry level',           {}),
    ('dry_time',            'Dry time',            {}),
    ('dryer_type',          'Dryer type',          {}),
    ('wrinkle_prevent',     'Wrinkle prevent',     {}),
    ('diagnosis',           'Diagnosis',           {}),
    ('country_code',        'Country code',        {}),
]


# (key, friendly name, value_template, device_class)
BINARY_SENSORS = [
    ('running', 'Running',
        "{{ 'ON' if value_json.machine_state == 'active' else 'OFF' }}",
        'running'),
    ('power_switch', 'Power switch',
        "{{ 'ON' if value_json.power_state_binary else 'OFF' }}",
        'power'),
    ('child_lock_active', 'Child lock',
        "{{ 'ON' if value_json.child_lock_binary else 'OFF' }}",
        'lock'),
    ('remote_control_enabled', 'Remote control',
        "{{ 'ON' if value_json.remote_control_binary else 'OFF' }}",
        'connectivity'),
]


# MQTT command-topic suffixes. The bridge subscribes to these and
# dispatches to the right OCF POST. Defined here so discovery configs
# and the bridge stay in sync.
CMD_WRINKLE_PREVENT  = 'cmd/wrinkle_prevent'
CMD_OPERATIONAL      = 'cmd/operational_state'
CMD_DRYER_MODE       = 'cmd/dryer_mode'


def _avail_base(avail_topic):
    return [{'topic': avail_topic,
             'payload_available':     'online',
             'payload_not_available': 'offline'}]


def _avail_with_remote(avail_topic, remote_topic):
    return [
        {'topic': avail_topic,
         'payload_available':     'online',
         'payload_not_available': 'offline'},
        {'topic': remote_topic,
         'payload_available':     'online',
         'payload_not_available': 'offline'},
    ]


def build_discovery_payloads(topic_prefix, ha_prefix, device_name):
    """Return list of (discovery_topic, payload_bytes) tuples ready to
    publish (retained) on MQTT connect."""
    state_topic   = f"{topic_prefix}/state"
    avail_topic   = f"{topic_prefix}/availability"
    remote_topic  = f"{topic_prefix}/remote_available"
    dev = device_block(topic_prefix, device_name)
    out = []

    # --- read-only sensors ---------------------------------------------
    for key, name, extra in SENSORS:
        cfg = {
            'name':                  name,
            'unique_id':             f"{topic_prefix}_{key}",
            'object_id':             f"{topic_prefix}_{key}",
            'state_topic':           state_topic,
            'value_template':        f"{{{{ value_json.{key} }}}}",
            'availability':          _avail_base(avail_topic),
            'device':                dev,
        }
        cfg.update(extra)
        out.append((f"{ha_prefix}/sensor/{topic_prefix}/{key}/config",
                    json.dumps(cfg).encode()))

    for key, name, template, dclass in BINARY_SENSORS:
        cfg = {
            'name':                  name,
            'unique_id':             f"{topic_prefix}_{key}",
            'object_id':             f"{topic_prefix}_{key}",
            'state_topic':           state_topic,
            'value_template':        template,
            'payload_on':            'ON',
            'payload_off':           'OFF',
            'device_class':          dclass,
            'availability':          _avail_base(avail_topic),
            'device':                dev,
        }
        out.append((f"{ha_prefix}/binary_sensor/{topic_prefix}/{key}/config",
                    json.dumps(cfg).encode()))

    # --- switch: wrinkle prevent (always available) --------------------
    cfg = {
        'name':            'Wrinkle prevent',
        'unique_id':       f"{topic_prefix}_wrinkle_prevent_switch",
        'object_id':       f"{topic_prefix}_wrinkle_prevent_switch",
        'state_topic':     state_topic,
        'value_template':  '{{ value_json.wrinkle_prevent }}',
        'state_on':        'On',
        'state_off':       'Off',
        'command_topic':   f"{topic_prefix}/{CMD_WRINKLE_PREVENT}",
        'payload_on':      'On',
        'payload_off':     'Off',
        'icon':            'mdi:iron',
        'availability':    _avail_base(avail_topic),
        'device':          dev,
    }
    out.append((f"{ha_prefix}/switch/{topic_prefix}/wrinkle_prevent/config",
                json.dumps(cfg).encode()))

    # --- buttons: Start / Pause / Stop (gated on remote control) -------
    buttons = [
        ('start', 'Start cycle', 'Run',   'mdi:play'),
        ('pause', 'Pause cycle', 'Pause', 'mdi:pause'),
        ('stop',  'Stop cycle',  'Ready', 'mdi:stop'),
    ]
    for key, name, payload_press, icon in buttons:
        cfg = {
            'name':              name,
            'unique_id':         f"{topic_prefix}_{key}",
            'object_id':         f"{topic_prefix}_{key}",
            'command_topic':     f"{topic_prefix}/{CMD_OPERATIONAL}",
            'payload_press':     payload_press,
            'icon':              icon,
            'availability':      _avail_with_remote(avail_topic, remote_topic),
            'availability_mode': 'all',
            'device':            dev,
        }
        out.append((f"{ha_prefix}/button/{topic_prefix}/{key}/config",
                    json.dumps(cfg).encode()))

    # --- select: course (gated on remote control) ----------------------
    cfg = {
        'name':              'Course',
        'unique_id':         f"{topic_prefix}_course_select",
        'object_id':         f"{topic_prefix}_course_select",
        'state_topic':       state_topic,
        'value_template':    '{{ value_json.dryer_mode }}',
        'command_topic':     f"{topic_prefix}/{CMD_DRYER_MODE}",
        'options':           course_options(),
        'icon':              'mdi:tumble-dryer',
        'availability':      _avail_with_remote(avail_topic, remote_topic),
        'availability_mode': 'all',
        'device':            dev,
    }
    out.append((f"{ha_prefix}/select/{topic_prefix}/course/config",
                json.dumps(cfg).encode()))

    return out


# Mapping from command-topic suffix to (path_segs, body_builder_fn(payload_str)).
# The bridge imports this to wire up the on_message dispatcher.
def command_handlers():
    from .sensors import encode_dryer_mode
    def _wrinkle(p):
        if p not in ('On', 'Off'): return None
        return ['washer', 'vs', '0'], {'x.com.samsung.da.wrinklePrevent': p}
    def _operational(p):
        if p not in ('Run', 'Pause', 'Ready'): return None
        return ['operational', 'state', 'vs', '0'], {'x.com.samsung.da.state': p}
    def _course(p):
        code = encode_dryer_mode(p)
        if code is None: return None
        return ['st', 'dryercourse', 'vs', '0'], {'x.com.samsung.da.st.dryerMode': code}
    return {
        CMD_WRINKLE_PREVENT: _wrinkle,
        CMD_OPERATIONAL:     _operational,
        CMD_DRYER_MODE:      _course,
    }
