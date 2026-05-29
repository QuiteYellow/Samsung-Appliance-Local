"""CoAP-over-TCP codec (RFC 8323).

Just enough of the protocol to talk to TizenRT-iotivity on the dryer:
GET (0x01), Observe (option 6), Uri-Path (11), CSM (0xE1). Token-byte
framing per RFC 8323 §4.1.
"""
import socket
import struct
from contextlib import contextmanager

# Option numbers
URI_PATH    = 11
URI_QUERY   = 15
OBSERVE     =  6
CF          = 12
ACCEPT      = 17


def _vlen(v):
    """Variable-length integer encoder used in option deltas + lengths."""
    if v < 13: return v, b''
    if v < 269: return 13, bytes([v - 13])
    if v < 65805: return 14, struct.pack('>H', v - 269)
    return 15, struct.pack('>I', v - 65805)


def enc_opts(opts):
    """Encode a list of (option_number, value_bytes) tuples."""
    out = b''
    prev = 0
    for n, v in sorted(opts, key=lambda x: x[0]):
        d, dx = _vlen(n - prev)
        l, lx = _vlen(len(v))
        out += bytes([(d << 4) | l]) + dx + lx + v
        prev = n
    return out


def parse_opts(opts_b):
    """Decode CoAP option bytes into a list of (number, value) tuples."""
    out = []
    pos = 0
    prev = 0
    while pos < len(opts_b):
        b = opts_b[pos]; pos += 1
        d, l = b >> 4, b & 0xF
        if d == 13:
            d = opts_b[pos] + 13; pos += 1
        elif d == 14:
            d = struct.unpack('>H', opts_b[pos:pos + 2])[0] + 269; pos += 2
        if l == 13:
            l = opts_b[pos] + 13; pos += 1
        elif l == 14:
            l = struct.unpack('>H', opts_b[pos:pos + 2])[0] + 269; pos += 2
        num = prev + d
        out.append((num, opts_b[pos:pos + l]))
        pos += l; prev = num
    return out


def enc_tcp(code, token=b'', opts_b=b'', payload=b''):
    """Wrap a code + options + payload into an RFC 8323 TCP frame."""
    body = opts_b + (b'\xff' + payload if payload else b'')
    n = len(body)
    ln, lx = _vlen(n)
    tkl = len(token)
    return bytes([(ln << 4) | tkl]) + lx + bytes([code]) + token + body


def _recv_n(s, n):
    b = b''
    while len(b) < n:
        c = s.recv(n - len(b))
        if not c:
            raise ConnectionError(f"closed @ {len(b)}/{n}")
        b += c
    return b


def read_tcp(s):
    """Read one RFC 8323 frame off socket s. Returns (code, token,
    options_bytes, payload_bytes)."""
    first = _recv_n(s, 1)[0]
    ln = first >> 4
    tkl = first & 0xF
    if ln < 13:
        bl = ln
    elif ln == 13:
        bl = _recv_n(s, 1)[0] + 13
    elif ln == 14:
        bl = struct.unpack('>H', _recv_n(s, 2))[0] + 269
    else:
        bl = struct.unpack('>I', _recv_n(s, 4))[0] + 65805
    code = _recv_n(s, 1)[0]
    tok = _recv_n(s, tkl)
    body = _recv_n(s, bl)
    if b'\xff' in body:
        i = body.index(b'\xff')
        return code, tok, body[:i], body[i + 1:]
    return code, tok, body, b''


def fmt_code(c):
    """0x45 → '2.05', 0x84 → '4.04', etc."""
    return f"{c >> 5}.{c & 0x1F:02d}"


CSM  = enc_tcp(0xE1)  # Capabilities & Settings Message — first frame on a new connection
PING = enc_tcp(0xE2)  # Keepalive ping (RFC 8323 §5.4); peer answers with Pong (0xE3)
