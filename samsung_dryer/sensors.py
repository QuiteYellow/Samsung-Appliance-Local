"""Sensor extraction — turn a Samsung /device/0 link dict into a flat
HA-friendly sensor dict.

Sources we read are ALL `/<x>/vs/0` (Samsung-prefixed) paths, because
the OCF-standard `/<x>/0` siblings (`/operational/state/0`, `/power/0`,
`/kidslock/0`, `/remotectrl/0`) accept Observe registration but their
state machines never push notifications. We derive their OCF-shaped
values (machine_state, *_binary) from the live `/vs/0` strings instead.
"""


def index_links(device0_body):
    """Turn the /device/0 CBOR list-of-{href,rep} into a dict keyed by href."""
    out = {}
    if not isinstance(device0_body, list):
        return out
    for entry in device0_body[1:]:
        if isinstance(entry, dict) and 'href' in entry:
            out[entry['href']] = entry.get('rep') or {}
    return out


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# Samsung "state" → OCF currentMachineState.
#   "Ready"  → idle      (dryer powered on, no cycle queued)
#   "Run"    → active    (cycle running)
#   "Pause"  → pause     (cycle paused)
#   "End"    → idle      (cycle just finished, before reset)
# Unmapped strings pass through verbatim so we don't silently drop a
# state we haven't catalogued yet.
_SAMSUNG_STATE_TO_OCF = {
    'Ready':   'idle',
    'Run':     'active',
    'Running': 'active',
    'Pause':   'pause',
    'Paused':  'pause',
    'End':     'idle',
}


# Course tables. Captured 2026-05-29 by dialing every course on a
# DA_WM_TP2_20_COMMON_DV5000T dryer. Other Samsung dryers may report a
# different Table_NN; capture a fresh table for them with
# tools/course_mapper.py.
COURSE_NAMES = {
    'Table_03': {
        0x16: 'Cotton',
        0x18: 'Synthetics',
        0x19: 'Delicates',
        0x1A: 'Wool',
        0x1B: 'Bedding',
        0x1C: 'Shirts',
        0x1D: 'Towels',
        0x1E: 'Outdoor',
        0x1F: 'Mixed Load',
        0x20: 'Iron Dry',
        0x23: 'Quick Dry 35',
        0x24: 'Cool Air',
        0x25: 'Warm Air',
        0x27: 'Time Dry',
    },
}


def decode_dryer_mode(s):
    """`Table_03_Course_16` → `Cotton`. Pass through verbatim if either
    the table or the course code isn't in our lookup, so a firmware
    update that adds a new course doesn't silently vanish from MQTT."""
    if not isinstance(s, str) or '_Course_' not in s:
        return s
    table_part, _, code_str = s.partition('_Course_')
    table = COURSE_NAMES.get(table_part)
    if not table:
        return s
    try:
        code = int(code_str, 16)
    except ValueError:
        return s
    return table.get(code, s)


# Reverse map for "Cotton" → "Course_16". The dryer wants the short
# `Course_HH` form (no `Table_03_` prefix); it expands the table prefix
# internally for the response. Used by the bridge to translate HA
# select payloads back to wire format.
_COURSE_CODE_BY_NAME = {
    name: code
    for table_codes in COURSE_NAMES.values()
    for code, name in table_codes.items()
}


def encode_dryer_mode(name):
    """`Cotton` → `Course_16`. Returns None if the name isn't in our
    lookup so callers can refuse rather than POST garbage."""
    code = _COURSE_CODE_BY_NAME.get(name)
    if code is None:
        return None
    return f"Course_{code:02X}"


def course_options():
    """List of human course names for the HA select dropdown. Sorted
    so the order is stable across boots."""
    return sorted(_COURSE_CODE_BY_NAME.keys())


def flatten_sensors(links):
    """Map a /device/0 link dict to the flat sensor dict that's
    published to MQTT. Every field reads from `/<x>/vs/0` paths so push
    updates immediately drive every entity."""
    g = lambda href, k, default=None: (links.get(href) or {}).get(k, default)

    inst_w = _num(g('/energy/consumption/vs/0',
                    'x.com.samsung.da.instantaneousPower'))
    cum_wh = _num(g('/energy/consumption/vs/0',
                    'x.com.samsung.da.cumulativePower'))
    if inst_w is not None and inst_w < 0:
        # The dryer reports a phantom -500 W when idle; HA energy
        # dashboard hates negatives.
        inst_w = 0.0

    sam_state = g('/operational/state/vs/0', 'x.com.samsung.da.state')
    machine_state = (_SAMSUNG_STATE_TO_OCF.get(sam_state, sam_state)
                     if sam_state is not None
                     else g('/operational/state/0', 'currentMachineState'))

    # /operational/state/vs/0 .progress mirrors currentJobState
    progress = g('/operational/state/vs/0', 'x.com.samsung.da.progress')
    job_state = progress or g('/operational/state/0', 'currentJobState')
    # HA's value_template treats the literal string "None" as null
    # (renders as "Unknown" in the UI). Map it to something HA can
    # display verbatim while keeping the semantic of "no active job".
    if job_state in (None, 'None'):
        job_state = 'Idle'
    if progress in (None, 'None'):
        progress = 'Idle'

    remaining = (g('/operational/state/vs/0',
                   'x.com.samsung.da.remainingTime')
                 or g('/operational/state/0', 'remainingTime'))
    rem_min = None
    if remaining:
        try:
            h, m, s = remaining.split(':')
            rem_min = int(h) * 60 + int(m) + (1 if int(s) > 0 else 0)
        except Exception:
            pass

    sam_power = g('/power/vs/0', 'x.com.samsung.da.power')
    sam_kids  = g('/kidslock/vs/0', 'x.com.samsung.da.kidsLock')
    sam_rc    = g('/remotectrl/vs/0',
                  'x.com.samsung.da.remoteControlEnabled')
    power_bin = (sam_power == 'On') if sam_power is not None else None
    kids_bin  = (sam_kids != 'Ready') if sam_kids is not None else None
    rc_bin    = (str(sam_rc).lower() == 'true') if sam_rc is not None else None

    return {
        'machine_state':         machine_state,
        'job_state':             job_state,
        'progress':              progress,
        'progress_percentage':   _int(g('/operational/state/vs/0',
                                        'x.com.samsung.da.progressPercentage')
                                       or g('/operational/state/0',
                                            'progressPercentage')),
        'completion_time':       remaining,
        'completion_minutes':    rem_min,
        'delay_end_time':        g('/operational/state/vs/0',
                                   'x.com.samsung.da.delayEndTime'),
        'power_state':           sam_power,
        'power_state_binary':    power_bin,
        'child_lock':            sam_kids,
        'child_lock_binary':     kids_bin,
        'remote_control':        sam_rc,
        'remote_control_binary': rc_bin,
        'power_watts':           inst_w,
        'energy_kwh':            round(cum_wh / 1000.0, 2)
                                    if cum_wh is not None else None,
        'energy_wh_cumulative':  int(cum_wh) if cum_wh is not None else None,
        'dryer_mode':            decode_dryer_mode(
                                     g('/st/dryercourse/vs/0',
                                       'x.com.samsung.da.st.dryerMode')),
        'dry_level':             _int(g('/washer/vs/0',
                                        'x.com.samsung.da.dryLevel')),
        'dry_time':              g('/washer/vs/0',
                                   'x.com.samsung.da.dryTime'),
        'dryer_type':            g('/washer/vs/0',
                                   'x.com.samsung.da.dryerType'),
        'wrinkle_prevent':       g('/washer/vs/0',
                                   'x.com.samsung.da.wrinklePrevent'),
        'diagnosis':             g('/diagnosis/vs/0',
                                   'x.com.samsung.da.diagnosisStart'),
        'country_code':          g('/configuration/vs/0',
                                   'x.com.samsung.da.countryCode'),
    }
