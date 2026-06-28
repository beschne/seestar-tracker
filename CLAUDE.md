# Seestar Plane Tracker — CLAUDE.md

## Project summary
Single-script ADS-B plane tracker for the Seestar S30 Pro smart telescope.
`seestar_track.py` polls keyless ADS-B APIs, picks the best in-sector aircraft,
converts its position to RA/Dec, and steers the mount via JSON-over-TCP.

GitHub repo: `beschne/seestar-tracker`

## Git / commits
Ask before committing and before pushing to GitHub.

## Stack
- Python 3.11+ stdlib only — no third-party packages, no pip installs.
- `tomllib` is built-in since 3.11; no virtual environment needed.

## Running
```
python3 seestar_track.py                          # live telescope tracking
python3 seestar_track.py --dry-run                # prints goto commands, no connection
python3 seestar_track.py --goto-az 245 --goto-el 5   # slew to az/el once and exit
```

## Configuration
`config.toml` (gitignored — copy from `config.sample.toml`).
Two sections: `[observer]` for location/radius, `[seestar]` for telescope settings.

## Seestar protocol
- TCP port 4700, `\r\n`-delimited JSON.
- **Firmware 7.18+ auth**: `get_verify_str` → RSA-SHA1-PKCS1v15 sign → `verify_client`.
  Signing is pure stdlib (`hashlib` + `pow(m,d,n)`).
  Private key: `/Applications/Seestar.app/Wrapper/Seestar.app/my_private.pem`.
- **Scenery mode**: `iscope_start_view {"mode": "scenery"}` — 100 ms daytime exposure, no autofocus.
  Set once at startup; do not change.
- **Mount slew**: `scope_goto [ra_hours, dec_deg]` — pure equatorial slew, no imaging pipeline.
  Do not use `iscope_start_view {"mode": "star"}` — it triggers AutoFocus which fails in daylight.
- **Heartbeat**: `scope_get_equ_coord` (id=420) sent every loop iteration to prevent
  inactivity shutdown (~60 s idle timeout observed).

## Sun safety (never remove either layer)
1. `select_target()` filters out aircraft within `SUN_EXCLUSION_DEG` (default 30°).
2. `check_sun_safe()` hard-blocks any goto within `_MIN_SUN_EXCLUSION` (15°, unconfigurable).
A startup check queries the current scope position and refuses to run if already too close.

## Target selection and predictive positioning
- Auto-selects the in-sector aircraft with the lowest angular rate.
- Looks ahead `LOOKAHEAD_S` (default 90 s) for aircraft about to enter the sector.
- Every goto aims at `_project_position(ac, SLEW_TIME_S)` — dead-reckoning from ADS-B track + speed.

## Photo opportunity indicator
Callsign turns green (ANSI) when slant range ≤ `PHOTO_MAX_KM` (default 20 km)
and elevation ≥ `PHOTO_MIN_EL_DEG` (default 15°). Both are configurable via
`photo_max_km` / `photo_min_el_deg` in `[seestar]`. Color is suppressed when
stdout is not a TTY.

## Pointing accuracy and compass correction
The Seestar's internal magnetometer is the dominant error source in alt/az mode:
typical accuracy is ±5–15° in azimuth. The accelerometer (leveling) is much more
accurate (±0.5–1°) and does not need correction.

`az_offset_deg` in `[seestar]` applies a fixed azimuth correction before every
`altaz_to_radec` call. Sign convention: positive = scope was pointing left of target.
When non-zero, the log shows `az_off +X.X°` on each line.

The offset is stable for a given setup location and does not drift during a session.
Re-measure after moving the Seestar or restarting the app.

To measure: use `--goto-az AZ --goto-el EL` to slew to a landmark with a known compass
bearing (set az_offset_deg = 0 first), then switch to widefield in the Seestar app.
Widefield FOV is ~9°×5°. Offset ≈ (pixel distance from center / half image width) × 4.5°.
