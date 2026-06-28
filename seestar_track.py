#!/usr/bin/env python3
"""
seestar_track.py — ADS-B plane tracker for Seestar S30 Pro (PoC)

Polls ADS-B sources (adsb.lol, adsb.one), selects the best in-sector
aircraft by lowest estimated angular rate, converts its position to RA/Dec,
and sends goto commands to the Seestar over its native JSON-over-TCP protocol.

Setup:
  1. Copy config.sample.toml to config.toml and fill in your location and Seestar details.
  2. Point the Seestar at a roughly empty patch of sky and switch to Scenery mode.
  3. python3 seestar_track.py           # live — connects to telescope
     python3 seestar_track.py --dry-run # safe — prints commands, no connection

Notes:
  • The Seestar API uses equatorial coords (RA/Dec), so az/el is converted to
    RA/Dec using Greenwich Mean Sidereal Time + observer longitude.
  • Method name "scope_goto" is based on seestar_alp source; verify against
    seestar_alp/device/seestar_device.py if the telescope ignores the command.
  • The Seestar must be on the same WiFi network (or in AP mode) before connecting.
"""

import argparse
import json
import math
import os
import socket
import sys
import time
import tomllib
import urllib.error
import urllib.request
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_config():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.toml")
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        print("Error: config.toml not found. Copy config.sample.toml and edit it.",
              file=sys.stderr)
        sys.exit(1)

_cfg               = _load_config()
_obs               = _cfg["observer"]
CENTER_LAT         = float(_obs["center_lat"])
CENTER_LON         = float(_obs["center_lon"])
RADIUS_KM          = float(_obs["radius_km"])
OBSERVER_ALT_M     = float(_obs.get("observer_alt_m", 0.0))
GEOID_OFFSET_M     = float(_obs.get("geoid_offset_m", 0.0))
OBSERVER_ALT_MSL_M = OBSERVER_ALT_M - GEOID_OFFSET_M
RADIUS_NM          = RADIUS_KM / 1.852

_see               = _cfg.get("seestar", {})
SEESTAR_HOST       = _see.get("host", "")
SEESTAR_PORT       = int(_see.get("port", 4700))
TARGET_CALLSIGN    = _see.get("target_callsign", "").strip().upper() or None
TARGET_HEX         = _see.get("target_hex", "").strip().lower() or None
POLL_INTERVAL      = float(_see.get("poll_interval_s", 2.0))
SEESTAR_PEM_PATH   = _see.get("pem", "").strip() or None
_sec_start = _see.get("sector_start")
_sec_end   = _see.get("sector_end")
SEESTAR_SECTOR = (float(_sec_start), float(_sec_end)) if _sec_start is not None else None
SLEW_TIME_S    = float(_see.get("slew_time_s",  16.0))  # observed typical slew duration
LOOKAHEAD_S    = float(_see.get("lookahead_s",  90.0))  # pre-position this many seconds ahead
AZ_OFFSET_DEG  = float(_see.get("az_offset_deg", 0.0))  # compass correction — see README

_MIN_SUN_EXCLUSION = 15.0   # hard floor — cannot be configured below this
SUN_EXCLUSION_DEG  = max(_MIN_SUN_EXCLUSION, float(_see.get("sun_exclusion_deg", 30.0)))
PHOTO_MAX_KM       = float(_see.get("photo_max_km", 20.0))
PHOTO_MIN_EL_DEG   = float(_see.get("photo_min_el_deg", 15.0))

_GREEN = "\033[32m" if sys.stdout.isatty() else ""
_RED   = "\033[31m" if sys.stdout.isatty() else ""
_RESET = "\033[0m"  if sys.stdout.isatty() else ""

USER_AGENT = "seestar-track/1.0 (personal hobby use)"

# ---------------------------------------------------------------------------
# ADS-B sources
# ---------------------------------------------------------------------------

def _http_get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _normalize_adsbx(ac):
    alt = ac.get("alt_baro")
    if alt == "ground":
        alt = 0
    return {
        "hex":      ac.get("hex"),
        "callsign": (ac.get("flight") or "").strip() or None,
        "lat":      ac.get("lat"),
        "lon":      ac.get("lon"),
        "alt_ft":   alt,
        "track":    ac.get("track"),
        "gs_kt":    ac.get("gs"),
    }


def _fetch_adsb_lol(lat, lon, radius_nm):
    url = f"https://api.adsb.lol/v2/lat/{lat}/lon/{lon}/dist/{radius_nm}"
    return [_normalize_adsbx(a) for a in _http_get_json(url).get("ac", [])]


def _fetch_adsb_one(lat, lon, radius_nm):
    url = f"https://api.adsb.one/v2/lat/{lat}/lon/{lon}/dist/{radius_nm}"
    return [_normalize_adsbx(a) for a in _http_get_json(url).get("ac", [])]


_SOURCES = [("adsb.lol", _fetch_adsb_lol), ("adsb.one", _fetch_adsb_one)]


def fetch_aircraft():
    for name, fn in _SOURCES:
        try:
            planes = fn(CENTER_LAT, CENTER_LON, RADIUS_NM)
            planes = [p for p in planes if p["lat"] is not None and p["lon"] is not None]
            return name, planes
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError):
            continue
    return None, []

# ---------------------------------------------------------------------------
# Geometry: az/el from observer to aircraft
# ---------------------------------------------------------------------------

def _haversine_m(lat2, lon2):
    R = 6_371_000.0
    phi1, phi2 = math.radians(CENTER_LAT), math.radians(lat2)
    dphi = math.radians(lat2 - CENTER_LAT)
    dlam = math.radians(lon2 - CENTER_LON)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def azimuth_deg(lat2, lon2):
    phi1 = math.radians(CENTER_LAT)
    phi2 = math.radians(lat2)
    dlam = math.radians(lon2 - CENTER_LON)
    y = math.sin(dlam) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlam)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def elevation_deg(lat2, lon2, alt_ft):
    if alt_ft is None:
        return None
    dh = alt_ft * 0.3048 - OBSERVER_ALT_MSL_M
    dist = _haversine_m(lat2, lon2)
    if dist < 1.0:
        return 90.0
    return math.degrees(math.atan2(dh, dist))


def _in_seestar_sector(az):
    """Return True if az is within the configured Seestar sector, or always
    True if no sector is defined (360° observable)."""
    if SEESTAR_SECTOR is None:
        return True
    start, end = SEESTAR_SECTOR
    end_adj = end if end > start else end + 360
    az_adj  = az if az >= start else az + 360
    return start <= az_adj <= end_adj


def _project_position(ac, seconds):
    """Dead-reckoning: project aircraft position forward by `seconds`.
    Returns (lat, lon) or None if track/speed unavailable."""
    if ac.get("track") is None or not ac.get("gs_kt"):
        return None
    track_r = math.radians(ac["track"])
    dist_m  = ac["gs_kt"] * 1852.0 / 3600.0 * seconds
    dlat = dist_m * math.cos(track_r) / 111_320.0
    dlon = dist_m * math.sin(track_r) / (111_320.0 * math.cos(math.radians(ac["lat"])))
    return ac["lat"] + dlat, ac["lon"] + dlon


def _sector_entry_seconds(ac):
    """Seconds until this aircraft enters the Seestar sector (0 = already inside),
    or None if it will not enter within LOOKAHEAD_S seconds.
    Checks in 5-second steps."""
    if _in_seestar_sector(azimuth_deg(ac["lat"], ac["lon"])):
        return 0
    step = 5
    for t in range(step, int(LOOKAHEAD_S) + 1, step):
        pos = _project_position(ac, t)
        if pos is None:
            return None
        if _in_seestar_sector(azimuth_deg(pos[0], pos[1])):
            return t
    return None

# ---------------------------------------------------------------------------
# Coordinate conversion: alt/az → RA/Dec
# ---------------------------------------------------------------------------

def _gmst_hours(utc_dt):
    """Greenwich Mean Sidereal Time in hours (IAU 1982 approximation, ±0.1 s)."""
    j2000 = datetime(2000, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    d = (utc_dt - j2000).total_seconds() / 86400.0
    return (18.697374558 + 24.06570982441908 * d) % 24


def altaz_to_radec(alt_deg, az_deg, utc_dt):
    """Convert altitude/azimuth to RA (hours) and Dec (degrees) for the
    configured observer location and given UTC time."""
    lat = math.radians(CENTER_LAT)
    alt = math.radians(alt_deg)
    az  = math.radians(az_deg)

    sin_dec = math.sin(alt) * math.sin(lat) + math.cos(alt) * math.cos(lat) * math.cos(az)
    sin_dec = max(-1.0, min(1.0, sin_dec))
    dec     = math.asin(sin_dec)

    cos_ha_num = math.sin(alt) - math.sin(dec) * math.sin(lat)
    cos_ha_den = math.cos(dec) * math.cos(lat)
    cos_ha = (cos_ha_num / cos_ha_den) if abs(cos_ha_den) > 1e-9 else 0.0
    cos_ha = max(-1.0, min(1.0, cos_ha))
    ha_rad = math.acos(cos_ha)
    if math.sin(az) > 0:                # object moving eastward → negative HA
        ha_rad = 2 * math.pi - ha_rad

    lst   = (_gmst_hours(utc_dt) + CENTER_LON / 15.0) % 24   # local sidereal time
    ha_h  = math.degrees(ha_rad) / 15.0
    ra    = (lst - ha_h) % 24

    return ra, math.degrees(dec)

# ---------------------------------------------------------------------------
# Sun position and exclusion zone  (SAFETY — do not remove)
# ---------------------------------------------------------------------------

def _julian_day(utc_dt):
    y, m = utc_dt.year, utc_dt.month
    d = utc_dt.day + (utc_dt.hour + utc_dt.minute / 60.0 + utc_dt.second / 3600.0) / 24.0
    if m <= 2:
        y -= 1
        m += 12
    A = int(y / 100)
    B = 2 - A + int(A / 4)
    return int(365.25 * (y + 4716)) + int(30.6001 * (m + 1)) + d + B - 1524.5


def _sun_radec(utc_dt):
    """Sun RA (hours) and Dec (degrees). Meeus ch.25/27, ~0.01° accuracy."""
    T     = (_julian_day(utc_dt) - 2451545.0) / 36525.0
    L0    = (280.46646 + 36000.76983 * T) % 360
    M     = (357.52911 + 35999.05029 * T - 0.0001537 * T * T) % 360
    Mr    = math.radians(M)
    C     = ((1.914602 - 0.004817 * T - 0.000014 * T * T) * math.sin(Mr)
           + (0.019993 - 0.000101 * T) * math.sin(2 * Mr)
           + 0.000289 * math.sin(3 * Mr))
    omega = math.radians(125.04 - 1934.136 * T)
    lam   = math.radians((L0 + C - 0.00569 - 0.00478 * math.sin(omega)) % 360)
    eps0  = 23.439291111 - 0.013004167 * T - 1.64e-7 * T * T + 5.04e-7 * T * T * T
    eps   = math.radians(eps0 + 0.00256 * math.cos(omega))
    ra    = math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam))
    dec   = math.asin(math.sin(eps) * math.sin(lam))
    return math.degrees(ra) / 15.0 % 24, math.degrees(dec)


def sun_altaz(utc_dt):
    """Sun altitude and azimuth (degrees) at the configured observer location."""
    ra_h, dec_d = _sun_radec(utc_dt)
    ra    = math.radians(ra_h * 15.0)
    dec   = math.radians(dec_d)
    lst_h = (_gmst_hours(utc_dt) + CENTER_LON / 15.0) % 24
    ha    = math.radians(lst_h * 15.0 - math.degrees(ra))
    lat   = math.radians(CENTER_LAT)
    sin_a = math.sin(lat) * math.sin(dec) + math.cos(lat) * math.cos(dec) * math.cos(ha)
    alt   = math.asin(max(-1.0, min(1.0, sin_a)))
    num   = math.sin(dec) - math.sin(alt) * math.sin(lat)
    den   = math.cos(alt) * math.cos(lat)
    cos_z = (num / den) if abs(den) > 1e-9 else 0.0
    az    = math.acos(max(-1.0, min(1.0, cos_z)))
    if math.sin(ha) > 0:
        az = 2 * math.pi - az
    return math.degrees(alt), math.degrees(az)


def _radec_sep_deg(ra1_h, dec1_d, ra2_h, dec2_d):
    """Angular separation between two equatorial coordinates (degrees)."""
    ra1, d1 = math.radians(ra1_h * 15.0), math.radians(dec1_d)
    ra2, d2 = math.radians(ra2_h * 15.0), math.radians(dec2_d)
    cos_d = (math.sin(d1) * math.sin(d2) +
             math.cos(d1) * math.cos(d2) * math.cos(ra1 - ra2))
    return math.degrees(math.acos(max(-1.0, min(1.0, cos_d))))


def _angular_sep_deg(az1, el1, az2, el2):
    """Great-circle angular separation between two (az, el) points in degrees."""
    a1, e1 = math.radians(az1), math.radians(el1)
    a2, e2 = math.radians(az2), math.radians(el2)
    cos_d  = (math.sin(e1) * math.sin(e2)
            + math.cos(e1) * math.cos(e2) * math.cos(a1 - a2))
    return math.degrees(math.acos(max(-1.0, min(1.0, cos_d))))


def check_sun_safe(az, el, sun_az, sun_el):
    """Return (is_safe, separation_deg).  Raises ValueError if separation < _MIN_SUN_EXCLUSION."""
    sep = _angular_sep_deg(az, el, sun_az, sun_el)
    if sep < _MIN_SUN_EXCLUSION:
        raise ValueError(
            f"HARD BLOCK: target is only {sep:.1f}° from sun "
            f"(absolute minimum {_MIN_SUN_EXCLUSION}°)"
        )
    return sep >= SUN_EXCLUSION_DEG, sep


# ---------------------------------------------------------------------------
# Target selection
# ---------------------------------------------------------------------------

def _angular_rate_deg_s(ac, prev_by_hex):
    """Estimate angular rate from two consecutive positions (deg/s).
    Returns a large value if no prior position is available."""
    prev = prev_by_hex.get(ac["hex"])
    if prev is None or prev.get("_ts") is None:
        return 999.0
    dt = time.monotonic() - prev["_ts"]
    if dt < 0.5:
        return 999.0
    az1 = azimuth_deg(prev["lat"], prev["lon"])
    az2 = azimuth_deg(ac["lat"],  ac["lon"])
    el1 = elevation_deg(prev["lat"], prev["lon"], prev.get("alt_ft")) or 0
    el2 = elevation_deg(ac["lat"],  ac["lon"],  ac.get("alt_ft"))    or 0
    daz = abs(az2 - az1)
    if daz > 180:
        daz = 360 - daz
    del_ = abs(el2 - el1)
    return math.hypot(daz, del_) / dt


def select_target(aircraft, prev_by_hex, sun_az, sun_el):
    """Return (ac, az, el, entry_t) for the best candidate:
      • entry_t = 0  → aircraft is already in the Seestar sector
      • entry_t > 0  → aircraft will enter sector in that many seconds
      • If target_callsign or target_hex is configured, require it.
      • In-sector aircraft are preferred over approaching ones; within each
        group the aircraft with the lowest angular rate wins.
      • Any aircraft within SUN_EXCLUSION_DEG of the sun is always rejected.
    """
    in_sector   = []
    approaching = []
    for ac in aircraft:
        if ac["alt_ft"] == 0:
            continue                          # skip ground traffic
        if TARGET_HEX and ac["hex"] != TARGET_HEX:
            continue
        if TARGET_CALLSIGN and (ac["callsign"] or "").upper() != TARGET_CALLSIGN:
            continue
        az = azimuth_deg(ac["lat"], ac["lon"])
        el = elevation_deg(ac["lat"], ac["lon"], ac["alt_ft"])
        if el is None or el < 1.0:
            continue                          # below effective horizon
        sep = _angular_sep_deg(az, el, sun_az, sun_el)
        if sep < SUN_EXCLUSION_DEG:
            continue                          # too close to sun — reject
        rate = _angular_rate_deg_s(ac, prev_by_hex)
        if _in_seestar_sector(az):
            in_sector.append((rate, ac, az, el))
        else:
            entry_t = _sector_entry_seconds(ac)
            if entry_t is not None:
                approaching.append((entry_t, rate, ac, az, el))

    if in_sector:
        in_sector.sort(key=lambda t: t[0])
        _rate, ac, az, el = in_sector[0]
        return ac, az, el, 0
    if approaching:
        approaching.sort(key=lambda t: (t[0], t[1]))   # entry_t first, then rate
        _entry_t, _rate, ac, az, el = approaching[0]
        return ac, az, el, _entry_t
    return None, None, None, None

# ---------------------------------------------------------------------------
# RSA-SHA1 signing — pure stdlib, no third-party packages
# Used for Seestar firmware 7.18+ challenge-response authentication.
# ---------------------------------------------------------------------------

# DER prefix for SHA1 DigestInfo (PKCS1v15): SEQUENCE { AlgorithmIdentifier, OCTET STRING }
_SHA1_DIGEST_INFO_PREFIX = bytes([
    0x30, 0x21,                          # SEQUENCE, 33 bytes
    0x30, 0x09,                          # SEQUENCE (AlgorithmIdentifier), 9 bytes
    0x06, 0x05, 0x2b, 0x0e, 0x03, 0x02, 0x1a,  # OID sha1 (1.3.14.3.2.26)
    0x05, 0x00,                          # NULL
    0x04, 0x14,                          # OCTET STRING, 20 bytes (SHA1 digest follows)
])


def _asn1_length(data, pos):
    b = data[pos]; pos += 1
    if b < 0x80:
        return b, pos
    n = b & 0x7f
    return int.from_bytes(data[pos:pos + n], "big"), pos + n


def _asn1_integer(data, pos):
    assert data[pos] == 0x02, f"Expected ASN.1 INTEGER at {pos}, got 0x{data[pos]:02x}"
    length, pos = _asn1_length(data, pos + 1)
    return int.from_bytes(data[pos:pos + length], "big"), pos + length


def _load_rsa_nd(pem_path):
    """Extract (n, d) from a PKCS8 RSA private key PEM file (pure stdlib)."""
    import base64 as _b64
    with open(pem_path) as f:
        b64 = "".join(l for l in f.read().splitlines() if not l.startswith("---"))
    der = _b64.b64decode(b64)

    # PKCS8: SEQUENCE { INTEGER(version), SEQUENCE(algId), OCTET STRING(RSAPrivateKey) }
    pos = 0
    assert der[pos] == 0x30
    _, pos = _asn1_length(der, pos + 1)
    _, pos = _asn1_integer(der, pos)            # version (skip)
    seq_len, seq_pos = _asn1_length(der, pos + 1)  # algorithmIdentifier SEQUENCE
    pos = seq_pos + seq_len
    pk_len, pk_pos = _asn1_length(der, pos + 1)    # privateKey OCTET STRING
    rsa = der[pk_pos:pk_pos + pk_len]

    # RSAPrivateKey: SEQUENCE { version, n, e, d, ... }
    assert rsa[0] == 0x30
    _, pos = _asn1_length(rsa, 1)
    _, pos = _asn1_integer(rsa, pos)            # version (skip)
    n, pos = _asn1_integer(rsa, pos)            # modulus
    _e, pos = _asn1_integer(rsa, pos)           # publicExponent (skip)
    d, _pos = _asn1_integer(rsa, pos)           # privateExponent
    return n, d


def _rsa_sha1_pkcs1v15_sign(message: str, pem_path: str) -> str:
    """Sign message string with RSA-SHA1-PKCS1v15. Returns base64. Pure stdlib."""
    import hashlib as _hl, base64 as _b64
    n, d = _load_rsa_nd(pem_path)
    key_len = (n.bit_length() + 7) // 8
    digest      = _hl.sha1(message.encode()).digest()
    digest_info = _SHA1_DIGEST_INFO_PREFIX + digest
    ps_len      = key_len - len(digest_info) - 3
    if ps_len < 8:
        raise ValueError("RSA key too short for PKCS1v15")
    padded = b"\x00\x01" + b"\xff" * ps_len + b"\x00" + digest_info
    sig = pow(int.from_bytes(padded, "big"), d, n).to_bytes(key_len, "big")
    return _b64.b64encode(sig).decode()


# ---------------------------------------------------------------------------
# Seestar TCP client
# ---------------------------------------------------------------------------

class SeestarClient:
    """JSON-over-TCP client for the Seestar native protocol (port 4700).

    Firmware 7.18+ requires challenge-response auth on connect.  The PEM
    private key is extracted from the official Seestar Mac app and stored at
    the path configured in config.toml [seestar] pem = "...".

    The connection is kept alive with a heartbeat on each goto call.
    On broken-pipe the client reconnects and re-authenticates transparently.
    """

    def __init__(self, host, port=4700, pem_path=None, timeout=5.0):
        self._host     = host
        self._port     = port
        self._pem_path = pem_path
        self._timeout  = timeout
        self._sock     = None
        self._buf      = b""
        self._id       = 0
        self._connect()

    def _connect(self):
        print(f"Connecting to Seestar at {self._host}:{self._port} …", flush=True)
        self._buf = b""
        try:
            self._sock = socket.create_connection(
                (self._host, self._port), timeout=self._timeout
            )
        except socket.gaierror as e:
            raise OSError(
                f"Cannot resolve '{self._host}': {e}. "
                "Check the hostname and that you are on the same network."
            ) from e
        self._sock.settimeout(self._timeout)
        if self._pem_path:
            self._authenticate()

    def _send(self, method, params=None):
        self._id += 1
        msg = {"id": self._id, "method": method}
        if params is not None:
            msg["params"] = params
        self._sock.sendall((json.dumps(msg) + "\r\n").encode())
        return self._id

    def _recv(self, timeout=3.0):
        self._sock.settimeout(timeout)
        try:
            while b"\r\n" not in self._buf:
                chunk = self._sock.recv(4096)
                if not chunk:
                    raise ConnectionError("Seestar closed connection")
                self._buf += chunk
            line, self._buf = self._buf.split(b"\r\n", 1)
            return json.loads(line)
        except socket.timeout:
            return None

    def _authenticate(self):
        """Firmware 7.18+ challenge-response: get_verify_str → sign → verify_client."""
        resp = self._recv_for_method("get_verify_str", send=True)
        if not resp:
            raise ConnectionError("No response to get_verify_str")
        challenge = resp.get("result", {}).get("str", "")
        if not challenge:
            raise ConnectionError(f"No challenge string in response: {resp}")
        signed = _rsa_sha1_pkcs1v15_sign(challenge, self._pem_path)
        resp2 = self._recv_for_method(
            "verify_client", params={"sign": signed, "data": challenge}, send=True
        )
        code = (resp2 or {}).get("code", -1)
        if code != 0:
            raise ConnectionError(f"Authentication failed: {resp2}")
        print("  Authenticated.", flush=True)

    def _recv_for_method(self, method, params=None, send=False):
        """Send a command and return the first response matching this method/id,
        skipping any unsolicited Event messages that arrive first."""
        msg_id = self._send(method, params) if send else None
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            resp = self._recv(timeout=max(0.1, deadline - time.monotonic()))
            if resp is None:
                break
            if "Event" in resp:
                continue                      # skip unsolicited events
            if resp.get("method") == method or (msg_id and resp.get("id") == msg_id):
                return resp
        return None

    def _send_recv(self, method, params=None):
        """Send a command, reconnecting once on broken pipe."""
        for attempt in range(2):
            try:
                msg_id = self._send(method, params)
                return msg_id, self._recv()
            except (BrokenPipeError, ConnectionError, OSError):
                if attempt:
                    raise
                print("  Connection lost — reconnecting …", flush=True)
                self._connect()

    def current_radec(self):
        """Return (ra_hours, dec_deg) of the current scope pointing, or None."""
        _, resp = self._send_recv("scope_get_equ_coord")
        if resp and isinstance(resp.get("result"), dict):
            r = resp["result"]
            return r.get("ra"), r.get("dec")
        return None, None

    def set_scenery_mode(self):
        """Switch camera to scenery (daytime) mode.  No goto, no autofocus."""
        return self._send_recv("iscope_start_view", {"mode": "scenery"})

    def goto_radec(self, ra_hours, dec_deg, label="Plane"):
        """Pure mount slew via scope_goto — no imaging pipeline, no autofocus.
        If the mount is busy (code 203) we cancel the current goto and retry once.
        """
        params = [round(ra_hours, 6), round(dec_deg, 4)]
        msg_id, resp = self._send_recv("scope_goto", params)
        if resp and resp.get("code") == 203:
            # Equipment moving — stop it and try once more
            self._send_recv("iscope_stop_view", {"stage": "AutoGoto"})
            time.sleep(0.4)
            msg_id, resp = self._send_recv("scope_goto", params)
        return msg_id, resp

    def heartbeat(self):
        """Keep the connection alive between goto calls."""
        try:
            self._id = 420
            self._send("scope_get_equ_coord")
        except OSError:
            pass

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass

# ---------------------------------------------------------------------------
# Main tracking loop
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Track a plane with the Seestar S30 Pro.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print goto commands without connecting to the telescope.")
    parser.add_argument("--goto-az", type=float, metavar="DEG",
                        help="Slew to this azimuth and exit (use with --goto-el). "
                             "Set az_offset_deg = 0 in config when measuring compass error.")
    parser.add_argument("--goto-el", type=float, metavar="DEG",
                        help="Elevation for --goto-az (degrees above horizon).")
    args = parser.parse_args()

    if args.goto_az is not None and args.goto_el is None:
        parser.error("--goto-az requires --goto-el")
    if args.goto_el is not None and args.goto_az is None:
        parser.error("--goto-el requires --goto-az")

    if not args.dry_run and not SEESTAR_HOST:
        print("Error: seestar.host is not set in config.toml. Use --dry-run to test without a telescope.",
              file=sys.stderr)
        sys.exit(1)

    client = None
    if not args.dry_run:
        try:
            client = SeestarClient(SEESTAR_HOST, SEESTAR_PORT, pem_path=SEESTAR_PEM_PATH)
            print("Connected.")
        except (OSError, ConnectionRefusedError) as e:
            print(f"Cannot connect to Seestar: {e}", file=sys.stderr)
            sys.exit(1)
        # Startup sun-safety check: refuse to run if scope is already pointing
        # at or near the sun, regardless of where it ended up last session.
        now0   = datetime.now(timezone.utc)
        ra, dec = client.current_radec()
        if ra is not None:
            sun_ra, sun_dec = _sun_radec(now0)
            sep = _radec_sep_deg(ra, dec, sun_ra, sun_dec)
            if sep < _MIN_SUN_EXCLUSION:
                print(
                    f"\n!! CRITICAL: scope is only {sep:.1f}° from the sun "
                    f"(hard minimum {_MIN_SUN_EXCLUSION}°).\n"
                    "   Physically point the Seestar away from the sun before running.",
                    file=sys.stderr,
                )
                client.close()
                sys.exit(1)
            elif sep < SUN_EXCLUSION_DEG:
                print(
                    f"!! WARNING: scope is {sep:.1f}° from the sun "
                    f"(exclusion zone {SUN_EXCLUSION_DEG}°) — please move it away."
                )
            else:
                print(f"Scope pointing: RA {ra:.4f}h  Dec {dec:+.3f}°  (sun {sep:.0f}° away — safe)")
        else:
            print("Scope pointing: unknown (could not query position)")

        _, resp = client.set_scenery_mode()
        mode_ok = isinstance(resp, dict) and ("Event" in resp or resp.get("code") == 0)
        print(f"Scenery mode: {'ok' if mode_ok else resp}")

    if args.goto_az is not None:
        now = datetime.now(timezone.utc)
        sun_el, sun_az = sun_altaz(now)
        try:
            safe, sep = check_sun_safe(args.goto_az, args.goto_el, sun_az, sun_el)
        except ValueError as e:
            print(f"!! {e}", file=sys.stderr)
            if client:
                client.close()
            sys.exit(1)
        if not safe:
            print(
                f"!! BLOCKED: az {args.goto_az:.1f}° el {args.goto_el:.1f}° is "
                f"only {sep:.1f}° from the sun (exclusion zone {SUN_EXCLUSION_DEG}°).",
                file=sys.stderr,
            )
            if client:
                client.close()
            sys.exit(1)
        ra, dec = altaz_to_radec(args.goto_el, args.goto_az, now)
        print(f"Slewing to az {args.goto_az:.1f}°  el {args.goto_el:+.1f}°  →  RA {ra:.4f}h  Dec {dec:+.3f}°  (sun {sep:.0f}° away)")
        if client:
            _, resp = client.goto_radec(ra, dec)
            if resp is None:
                print("Done. (no response from Seestar)")
            elif resp.get("code", 0) != 0 and "Event" not in resp:
                print(f"Seestar error code {resp.get('code')}: {resp.get('error', resp)}")
            else:
                print("Done.")
        else:
            print("(dry run — no connection)")
        if client:
            client.close()
        sys.exit(0)

    label   = "DRY RUN — " if args.dry_run else ""
    prev_by_hex: dict = {}

    print(f"{label}Starting tracking loop (Ctrl-C to stop). Poll every {POLL_INTERVAL}s.")
    print(f"Observer: {CENTER_LAT:.5f}, {CENTER_LON:.5f}  alt MSL {OBSERVER_ALT_MSL_M:.0f} m")

    sun_el0, sun_az0 = sun_altaz(datetime.now(timezone.utc))
    sun_vis = f"el {sun_el0:+.1f}°  az {sun_az0:.1f}°"
    if sun_el0 < 0:
        sun_vis += "  (below horizon)"
    print(f"☉ {sun_vis}  · exclusion zone: {SUN_EXCLUSION_DEG}°")

    if SEESTAR_SECTOR:
        print(f"Sector: {SEESTAR_SECTOR[0]:.0f}°–{SEESTAR_SECTOR[1]:.0f}°")
        sun_el_chk, sun_az_chk = sun_altaz(datetime.now(timezone.utc))
        if sun_el_chk > 0 and _in_seestar_sector(sun_az_chk):
            print(
                f"  Note: ☉ (az {sun_az_chk:.1f}°) is inside the sector — "
                f"aircraft within {SUN_EXCLUSION_DEG}° of it will be excluded."
            )
    else:
        print("Sector: 360° (no sector defined)")
    if TARGET_CALLSIGN:
        print(f"Locked on callsign: {TARGET_CALLSIGN}")
    elif TARGET_HEX:
        print(f"Locked on hex: {TARGET_HEX}")
    else:
        print("Auto-select: slowest in-sector aircraft.")

    try:
        while True:
            if client:
                client.heartbeat()

            source, aircraft = fetch_aircraft()
            if not aircraft:
                print("No aircraft data — retrying …")
                time.sleep(POLL_INTERVAL)
                continue

            now    = datetime.now(timezone.utc)
            sun_el, sun_az = sun_altaz(now)
            ac, az, el, entry_t = select_target(aircraft, prev_by_hex, sun_az, sun_el)

            # Stamp positions for angular-rate estimation on the next iteration.
            for p in aircraft:
                prev_by_hex[p["hex"]] = {**p, "_ts": time.monotonic()}

            if ac is None:
                tgt_desc = (TARGET_CALLSIGN or TARGET_HEX or "any in-sector")
                print(f"[{now:%H:%M:%S}] No suitable target ({tgt_desc})")
                time.sleep(POLL_INTERVAL)
                continue

            # ── Hard safety gate: second independent check before any goto ──
            # This fires even if select_target had a bug and returned a sun-unsafe target.
            try:
                safe, sep = check_sun_safe(az, el, sun_az, sun_el)
            except ValueError as e:
                print(f"[{now:%H:%M:%S}] !! {e}")
                time.sleep(POLL_INTERVAL)
                continue
            if not safe:
                ident_tmp = ac.get("callsign") or ac.get("hex") or "?"
                print(
                    f"[{now:%H:%M:%S}] !! BLOCKED {ident_tmp}: "
                    f"{sep:.1f}° from sun < {SUN_EXCLUSION_DEG}° exclusion zone"
                )
                time.sleep(POLL_INTERVAL)
                continue

            # Project the aircraft position forward by SLEW_TIME_S so the scope
            # is pointing where the plane will be when the slew completes.
            proj = _project_position(ac, SLEW_TIME_S)
            if proj is not None:
                goto_az = azimuth_deg(proj[0], proj[1])
                goto_el = elevation_deg(proj[0], proj[1], ac["alt_ft"]) or el
            else:
                goto_az, goto_el = az, el

            goto_az = (goto_az + AZ_OFFSET_DEG) % 360
            ra, dec = altaz_to_radec(goto_el, goto_az, now)
            ident    = ac.get("callsign") or ac.get("hex") or "?"
            dist_km  = math.hypot(_haversine_m(ac["lat"], ac["lon"]),
                                  (ac["alt_ft"] or 0) * 0.3048) / 1000.0
            proj_tag     = f"+{SLEW_TIME_S:.0f}s" if proj is not None else "now"
            approach_tag = f" in{entry_t}s" if entry_t else ""

            dist_ok      = dist_km <= PHOTO_MAX_KM
            el_ok        = el >= PHOTO_MIN_EL_DEG
            photo_ok     = dist_ok and el_ok
            ident_padded = f"{ident:7s}"
            ident_fmt    = f"{_GREEN}{ident_padded}{_RESET}" if photo_ok else ident_padded
            el_fmt       = f"{_RED}el{el:+5.1f}°{_RESET}" if not el_ok   else f"el{el:+5.1f}°"
            dist_fmt     = f"{_RED}{dist_km:4.0f}km{_RESET}" if not dist_ok else f"{dist_km:4.0f}km"

            print(
                f"[{now:%H:%M:%S}] {ident_fmt} "
                f"az{az:6.1f}° {el_fmt} {dist_fmt} "
                f"{proj_tag} ☉{sep:.0f}°{approach_tag}"
                + (f" Δ{AZ_OFFSET_DEG:+.1f}°" if AZ_OFFSET_DEG else "")
            )

            if client and dist_ok:
                msg_id, resp = client.goto_radec(ra, dec, label=ident)
                if resp is None:
                    print("  (no response from Seestar)")
                elif "Event" in resp:
                    pass                          # unsolicited event — not an error
                elif resp.get("code", 0) != 0:
                    print(f"  Seestar error code {resp.get('code')}: {resp.get('error', resp)}")

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        if client:
            client.close()


if __name__ == "__main__":
    main()
