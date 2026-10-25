"""Log a JK BMS over Bluetooth LE and serve history graphs.

Reads the BMS every few seconds with aiobmsble, which only sends JK's read-only
"device info" and "cell info" requests. Samples go into SQLite
(data/bms.sqlite3) and the dashboard is served at http://127.0.0.1:8765
(see config.toml).

    uv run jkbms_logger.py                        console, log on screen
    .venv\\Scripts\\pythonw.exe jkbms_logger.py    hidden (what autostart runs)

The BMS accepts one Bluetooth client at a time, so the JK phone app can't
connect while this runs. "Free Bluetooth" on the dashboard pauses logging.
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import json
import logging
import logging.handlers
import socket
import sqlite3
import sys
import threading
import time
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from aiobmsble import TempSensor
from aiobmsble.bms.jikong_bms import BMS as JikongBMS
from bleak import BleakScanner

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
DB_PATH = ROOT / "data" / "bms.sqlite3"
LOG_PATH = ROOT / "logs" / "jkbms_logger.log"

log = logging.getLogger("jkbms")

# JK02_32S alarm bitmask, as aiobmsble reports it in "problem_code". Bit 4 was
# verified live (set while Cell OVP holds charging off); the other names follow
# JK's RS485 protocol document.
ALARM_BITS = (
    "Balance wire resistance",
    "MOSFET overtemperature",
    "Cell count mismatch",
    "Current sensor error",
    "Cell OVP",
    "Pack OVP",
    "Charge overcurrent",
    "Charge short circuit",
    "Charge overtemperature",
    "Charge undertemperature",
    "Coprocessor comm error",
    "Cell UVP",
    "Pack UVP",
    "Discharge overcurrent",
    "Discharge short circuit",
    "Discharge overtemperature",
)


def alarm_names(code: int | None) -> list[str]:
    if not code:
        return []
    names = [name for bit, name in enumerate(ALARM_BITS) if code >> bit & 1]
    if extra := code >> len(ALARM_BITS) << len(ALARM_BITS):
        names.append(f"Unknown alarm 0x{extra:X}")
    return names


# ---------------------------------------------------------------- BMS settings
#
# JK02_32S settings frame (type 0x01, 300 bytes) and its write registers, as in
# syssi/esphome-jk-bms (decode_jk02_settings_ and the JK02_32S column of its
# number/switch maps). Each 4-byte field at offset o is written through
# register (o - 6) / 4 + 1.
#
# What may be written is configuration: [limits] in config.toml sets the
# allowed range per setting, and a setting without one stays read-only.
# `bounds` here is only an outer sanity check on those ranges (the ranges
# esphome-jk-bms offers, i.e. what the BMS works with), so a typo like 36.5
# for 3.65 can't open up a nonsense write.


@dataclass(frozen=True)
class Setting:
    key: str
    label: str
    jk_name: str  # what the JK app calls it
    group: str
    offset: int
    register: int | None  # None: shown but never written
    factor: int = 1000  # raw = value * factor
    unit: str = "V"
    digits: int = 3
    bounds: tuple[float, float] | None = None  # outer limit for its [limits] range
    signed: bool = False
    switch: bool = False
    help: str = ""
    caution: str = ""  # repeated in the confirmation dialog


SETTING_GROUPS = (
    ("voltage", "Cell voltage protection"),
    ("balance", "Balancing"),
    ("current", "Current protection"),
    ("temperature", "Temperature protection"),
    ("battery", "Battery and switches"),
    ("inverter", "Inverter communication (CAN/RS485)"),
)

_CELL_V = {"bounds": (1.2, 4.35)}
_TEMP = {"factor": 10, "unit": "°C", "digits": 1, "signed": True}
_DELAY = {"factor": 1, "unit": "s", "digits": 0, "bounds": (2, 600)}
_AMPS = {"unit": "A", "digits": 1}
_INVERTER = "Only sent to inverters that talk to the BMS over CAN/RS485."

SETTINGS = (
    Setting("cell_ovp", "Overvoltage protection", "Cell OVP", "voltage", 18, 0x04, **_CELL_V,
            help="Charging stops when any cell reaches this."),
    Setting("cell_ovpr", "Overvoltage release", "Cell OVPR", "voltage", 22, 0x05, **_CELL_V,
            help="Charging is allowed again once the highest cell falls to this."),
    Setting("cell_uvp", "Undervoltage protection", "Cell UVP", "voltage", 10, 0x02, **_CELL_V,
            help="Discharging stops when any cell falls to this."),
    Setting("cell_uvpr", "Undervoltage release", "Cell UVPR", "voltage", 14, 0x03, **_CELL_V,
            help="Discharging is allowed again once the lowest cell recovers to this."),
    Setting("power_off_v", "Power-off voltage", "Power Off Vol.", "voltage", 46, 0x0B, **_CELL_V,
            help="The BMS switches itself off when a cell falls this low."),
    Setting("soc100_v", "100% charge voltage", "SOC-100% Volt.", "voltage", 30, 0x07, **_CELL_V,
            help="The charge counter resets to 100% when a cell reaches this."),
    Setting("soc0_v", "0% charge voltage", "SOC-0% Volt.", "voltage", 34, 0x08, **_CELL_V,
            help="The charge counter resets to 0% when a cell falls to this."),
    Setting("smart_sleep_v", "Smart sleep voltage", "Smart Sleep Volt.", "voltage", 6, None),
    Setting("balance_trigger_v", "Balance trigger difference", "Balance Trig. Volt.", "balance", 26, 0x06,
            bounds=(0.003, 1.0), help="The balancer runs when cells differ by at least this much..."),
    Setting("balance_start_v", "Balance start voltage", "Start Balance Volt.", "balance", 138, 0x22,
            bounds=(1.2, 4.25), help="...and a cell is above this voltage."),
    Setting("max_balance_a", "Balance current", "Max Balance Curr.", "balance", 78, 0x13, **_AMPS,
            bounds=(0.1, 15.0), help="Keep it within what your BMS model can do."),
    Setting("balancer", "Balancer", "Balance", "balance", 126, 0x1F, switch=True),
    Setting("max_charge_a", "Charge overcurrent protection", "Continued Charge Curr.", "current", 50, 0x0C,
            **_AMPS, bounds=(1, 600)),
    Setting("charge_ocp_delay", "Charge overcurrent delay", "Charge OCP Delay", "current", 54, 0x0D, **_DELAY),
    Setting("charge_ocpr_time", "Charge overcurrent recovery", "Charge OCPR Time", "current", 58, 0x0E, **_DELAY),
    Setting("max_discharge_a", "Discharge overcurrent protection", "Continued Discharge Curr.", "current",
            62, 0x0F, **_AMPS, bounds=(1, 1200)),
    Setting("discharge_ocp_delay", "Discharge overcurrent delay", "Discharge OCP Delay", "current", 66, 0x10,
            **_DELAY),
    Setting("discharge_ocpr_time", "Discharge overcurrent recovery", "Discharge OCPR Time", "current", 70,
            0x11, **_DELAY),
    Setting("scp_delay", "Short-circuit delay", "SCP Delay", "current", 134, None, factor=1, unit="µs", digits=0),
    Setting("scpr_time", "Short-circuit recovery", "SCPR Time", "current", 74, None, factor=1, unit="s", digits=0),
    Setting("charge_otp", "Charge overtemperature", "Charge OTP", "temperature", 82, 0x14, **_TEMP,
            bounds=(30, 80)),
    Setting("charge_otpr", "Charge overtemperature release", "Charge OTPR", "temperature", 86, 0x15, **_TEMP,
            bounds=(30, 80)),
    Setting("discharge_otp", "Discharge overtemperature", "Discharge OTP", "temperature", 90, 0x16, **_TEMP,
            bounds=(30, 80)),
    Setting("discharge_otpr", "Discharge overtemperature release", "Discharge OTPR", "temperature", 94, 0x17,
            **_TEMP, bounds=(30, 80)),
    Setting("charge_utp", "Charge undertemperature", "Charge UTP", "temperature", 98, 0x18, **_TEMP,
            bounds=(-45, 20), help="Lithium cells shouldn't be charged below 0 °C."),
    Setting("charge_utpr", "Charge undertemperature release", "Charge UTPR", "temperature", 102, 0x19,
            **_TEMP, bounds=(-45, 20)),
    Setting("mos_otp", "MOSFET overtemperature", "MOS OTP", "temperature", 106, 0x1A, **_TEMP, bounds=(50, 110)),
    Setting("mos_otpr", "MOSFET overtemperature release", "MOS OTPR", "temperature", 110, 0x1B, **_TEMP,
            bounds=(50, 110)),
    Setting("capacity_ah", "Battery capacity", "Battery Capacity", "battery", 130, 0x20, unit="Ah", digits=1,
            bounds=(2, 20000), help="Used for the state-of-charge counter."),
    Setting("cell_count", "Cell count", "Cell Count", "battery", 114, None, factor=1, unit="", digits=0),
    Setting("charging", "Charging allowed", "Charge", "battery", 118, 0x1D, switch=True,
            caution="With charging off, nothing can charge the battery."),
    Setting("discharging", "Discharging allowed", "Discharge", "battery", 122, 0x1E, switch=True,
            caution="With discharging off, the battery can't supply power, so a UPS loses its backup."),
    Setting("rcv", "Request charge voltage", "Vol. Cell RCV", "inverter", 38, 0x09, **_CELL_V, help=_INVERTER),
    Setting("rfv", "Request float voltage", "Vol. Cell RFV", "inverter", 42, 0x0A, **_CELL_V, help=_INVERTER),
)  # fmt: skip
SETTINGS_BY_KEY = {s.key: s for s in SETTINGS}
assert all(s.bounds for s in SETTINGS if s.register is not None and not s.switch), "writable numbers need bounds"

# bitmask at offset 282 (LE16); shown read-only
SETTING_FLAGS = (
    "Heating", "Temperature sensors disabled", "GPS heartbeat", "Port is RS485 (else CAN)",
    "Display always on", "Special charger", "Smart sleep", "PCL module disabled",
    "Timed stored data", "Charging float mode", "Emergency button", "Dry contact alarm intermittent",
    "Discharge OCP II", "Discharge OCP III", "GPS locked charging", "GPS locked discharging",
)  # fmt: skip

# a <= b + offset, checked on the values as they'd be after the change
SETTING_RULES = (
    ("cell_ovpr", "cell_ovp", -0.02, "Overvoltage release must be at least 0.02 V below overvoltage protection"),
    ("cell_uvp", "cell_uvpr", -0.02, "Undervoltage release must be at least 0.02 V above undervoltage protection"),
    ("power_off_v", "cell_uvp", -0.02, "Power-off voltage must be at least 0.02 V below undervoltage protection"),
    ("soc100_v", "cell_ovp", 0.0, "The 100% charge voltage can't be above overvoltage protection"),
    ("soc0_v", "soc100_v", -0.1, "The 0% charge voltage must be well below the 100% charge voltage"),
    ("balance_start_v", "cell_ovp", -0.02, "Balancing must start below overvoltage protection"),
    ("rcv", "cell_ovp", 0.0, "Request charge voltage can't be above overvoltage protection"),
    ("rfv", "rcv", 0.0, "Request float voltage can't be above request charge voltage"),
    ("charge_otpr", "charge_otp", -2.0, "Charge overtemperature release must be at least 2 °C below the limit"),
    ("discharge_otpr", "discharge_otp", -2.0, "Discharge overtemperature release must be at least 2 °C below the limit"),
    ("mos_otpr", "mos_otp", -2.0, "MOSFET overtemperature release must be at least 2 °C below the limit"),
    ("charge_utp", "charge_utpr", -2.0, "Charge undertemperature release must be at least 2 °C above the limit"),
)  # fmt: skip


def decode_settings(frame: bytes) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for s in SETTINGS:
        if s.switch:
            values[s.key] = bool(frame[s.offset])
        else:
            raw = int.from_bytes(frame[s.offset : s.offset + 4], "little", signed=s.signed)
            values[s.key] = round(raw / s.factor, 6)
    bits = int.from_bytes(frame[282:284], "little")
    flags = {name: bool(bits >> i & 1) for i, name in enumerate(SETTING_FLAGS)}
    return {"values": values, "flags": flags}


def raw_value(s: Setting, value: Any) -> int:
    # round, don't truncate: 3.53 * 1000 is 3529.9999...
    return int(bool(value)) if s.switch else round(float(value) * s.factor)


Limits = dict[str, tuple[float, float]]


def parse_limits(raw: dict[str, Any]) -> tuple[Limits, list[str]]:
    """[limits] from config.toml as {key: (min, max)}, plus problems to report.
    A bad entry is dropped, which leaves that setting read-only."""
    limits: Limits = {}
    problems: list[str] = []
    for key, value in raw.items():
        s = SETTINGS_BY_KEY.get(key)
        if s is not None and s.switch:
            problems.append(f"[limits] {key}: on/off settings don't take a range, ignored")
            continue
        if s is None or s.register is None:
            problems.append(f"[limits] {key}: not a setting that can be changed, ignored")
            continue
        numbers = isinstance(value, list) and len(value) == 2 and all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in value
        )
        if not numbers or value[0] >= value[1]:
            problems.append(f"[limits] {key}: needs [min, max] with min below max, ignored")
            continue
        lo, hi = float(value[0]), float(value[1])
        if lo < s.bounds[0] or hi > s.bounds[1]:
            problems.append(
                f"[limits] {key}: {lo:g}–{hi:g} {s.unit} is outside what the BMS works with "
                f"({s.bounds[0]:g}–{s.bounds[1]:g} {s.unit}), ignored"
            )
            continue
        limits[key] = (lo, hi)
    return limits, problems


def editable(s: Setting, limits: Limits) -> bool:
    return s.register is not None and (s.switch or s.key in limits)


def validate_changes(limits: Limits, current: dict[str, Any], changes: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for key, value in changes.items():
        s = SETTINGS_BY_KEY.get(key)
        if s is None or s.register is None:
            errors.append(f"{key}: can't be changed here")
        elif s.switch:
            if not isinstance(value, bool):
                errors.append(f"{s.label}: must be on or off")
        elif key not in limits:
            errors.append(f"{s.label}: no allowed range under [limits] in config.toml")
        elif isinstance(value, bool) or not isinstance(value, (int, float)):
            errors.append(f"{s.label}: not a number")
        elif not limits[key][0] <= value <= limits[key][1]:
            lo, hi = limits[key]
            errors.append(f"{s.label}: must be between {lo:g} and {hi:g} {s.unit}")
    if errors:
        return errors
    merged = {**current, **changes}
    for a, b, offset, message in SETTING_RULES:
        if (a in changes or b in changes) and merged[a] > merged[b] + offset + 1e-9:
            errors.append(message)
    return errors


# ---------------------------------------------------------------- config


@dataclass(frozen=True)
class Config:
    bms_name: str
    bms_address: str
    poll_seconds: float
    idle_store_seconds: float
    active_current_a: float
    thresholds: dict[str, Any]
    limits: Limits
    problems: tuple[str, ...]  # config mistakes, logged and shown on the settings tab
    host: str
    port: int

    @classmethod
    def load(cls, path: Path) -> Config:
        raw = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        bms, logging_, web = raw.get("bms", {}), raw.get("logging", {}), raw.get("web", {})
        limits, problems = parse_limits(raw.get("limits", {}))
        return cls(
            bms_name=str(bms.get("name", "")).strip(),
            bms_address=str(bms.get("address", "")).strip().upper(),
            poll_seconds=max(1.0, float(logging_.get("poll_seconds", 5))),
            idle_store_seconds=max(5.0, float(logging_.get("idle_store_seconds", 60))),
            active_current_a=abs(float(logging_.get("active_current_a", 0.25))),
            thresholds=dict(raw.get("thresholds", {})),
            limits=limits,
            problems=tuple(problems),
            host=str(web.get("host", "127.0.0.1")),
            port=int(web.get("port", 8765)),
        )

    @property
    def gap_seconds(self) -> float:
        """Silence longer than this means the logger wasn't running."""
        return max(3 * self.idle_store_seconds, 120.0)


# ---------------------------------------------------------------- samples


def _num(value: Any, digits: int) -> float | None:
    return None if value is None else round(float(value), digits)


def _flag(value: Any) -> int | None:
    return None if value is None else int(bool(value))


def temps_by_name(sensors: Iterable[TempSensor]) -> dict[str, float]:
    """Name sensors like the JK app: MOS for the MOSFETs, then T1, T2, ..."""
    temps: dict[str, float] = {}
    mos = probes = 0
    for sensor in sensors:
        if sensor.type == TempSensor.T.MOSFET:
            mos += 1
            name = "MOS" if mos == 1 else f"MOS{mos}"
        else:
            probes += 1
            name = f"T{probes}"
        temps[name] = round(float(sensor.value), 1)
    return temps


def sample_to_row(sample: dict[str, Any], ts: float) -> dict[str, Any] | None:
    """Flatten an aiobmsble sample into a database row; None if it's unusable."""
    cells = [round(v, 3) for v in sample.get("cell_voltages", [])]
    if not cells or not sample.get("voltage"):
        return None
    delta = sample.get("delta_voltage", max(cells) - min(cells))
    return {
        "ts": round(ts, 1),
        "voltage": _num(sample.get("voltage"), 3),
        "current": _num(sample.get("current"), 3),
        "power": _num(sample.get("power"), 1),
        "soc": _num(sample.get("battery_level"), 1),
        "remaining_ah": _num(sample.get("cycle_charge"), 3),
        "cell_min": min(cells),
        "cell_max": max(cells),
        "delta_mv": round(delta * 1000, 1),
        "cells": cells,
        "temps": temps_by_name(sample.get("temp_values", [])),
        "chg": _flag(sample.get("chrg_mosfet")),
        "dsg": _flag(sample.get("dischrg_mosfet")),
        "balancing": _flag(sample.get("balancer")),
        "balance_a": _num(sample.get("balance_current"), 3),
        "alarms": int(sample.get("problem_code") or 0),
        "cycles": sample.get("cycles"),
    }


class Recorder:
    """Decides which readings get a database row: every reading while current
    flows or a switch/alarm changes, otherwise one per idle_store_seconds."""

    WATCHED = ("chg", "dsg", "balancing", "alarms")

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.last: dict[str, Any] | None = None

    def should_store(self, row: dict[str, Any]) -> bool:
        last = self.last
        amps = row["current"] or 0.0
        keep = (
            last is None
            or row["ts"] - last["ts"] >= self.cfg.idle_store_seconds
            or abs(amps) >= self.cfg.active_current_a
            or abs(amps - (last["current"] or 0.0)) >= self.cfg.active_current_a
            or any(row[key] != last[key] for key in self.WATCHED)
        )
        if keep:
            self.last = row
        return keep


# ---------------------------------------------------------------- storage

COLUMNS = (
    "ts", "voltage", "current", "power", "soc", "remaining_ah",
    "cell_min", "cell_max", "delta_mv", "cells", "temps",
    "chg", "dsg", "balancing", "balance_a", "alarms", "cycles",
)  # fmt: skip
JSON_COLUMNS = ("cells", "temps")

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    ts REAL PRIMARY KEY,          -- unix time, seconds
    voltage REAL,                 -- V
    current REAL,                 -- A, + charging / - discharging
    power REAL,                   -- W
    soc REAL,                     -- %
    remaining_ah REAL,
    cell_min REAL, cell_max REAL, -- V
    delta_mv REAL,
    cells TEXT,                   -- JSON list of cell voltages, V
    temps TEXT,                   -- JSON object {"MOS": 19.1, "T1": ...}, degC
    chg INTEGER, dsg INTEGER,     -- charge / discharge MOSFET on
    balancing INTEGER, balance_a REAL,
    alarms INTEGER,               -- JK alarm bitmask, see ALARM_BITS
    cycles INTEGER
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS setting_changes (
    ts REAL,                      -- unix time, seconds
    key TEXT,                     -- SETTINGS key
    old_value REAL, new_value REAL, read_back REAL,
    ok INTEGER,
    note TEXT
);
"""
INSERT_SQL = (
    f"INSERT OR REPLACE INTO samples ({', '.join(COLUMNS)}) "
    f"VALUES ({', '.join('?' * len(COLUMNS))})"
)


def open_db(*, create: bool = False) -> sqlite3.Connection:
    if create:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    if create:
        conn.execute("PRAGMA journal_mode=WAL")  # readers never block the logger
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(SCHEMA)
    conn.row_factory = sqlite3.Row
    return conn


def insert_row(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    conn.execute(
        INSERT_SQL,
        [json.dumps(row[c], separators=(",", ":")) if c in JSON_COLUMNS else row[c] for c in COLUMNS],
    )
    conn.commit()


def load_rows(t0: float, t1: float) -> list[dict[str, Any]]:
    if not DB_PATH.exists():
        return []
    with contextlib.closing(open_db()) as conn:
        cursor = conn.execute(
            f"SELECT {', '.join(COLUMNS)} FROM samples WHERE ts BETWEEN ? AND ? ORDER BY ts",
            (t0, t1),
        )
        rows = [dict(r) for r in cursor]
    for row in rows:
        row["cells"] = json.loads(row["cells"] or "[]")
        row["temps"] = json.loads(row["temps"] or "{}")
    return rows


def db_stats() -> dict[str, Any]:
    if not DB_PATH.exists():
        return {"first_ts": None, "size_mb": 0.0}
    with contextlib.closing(open_db()) as conn:
        first = conn.execute("SELECT MIN(ts) FROM samples").fetchone()[0]
    size = sum(p.stat().st_size for p in DB_PATH.parent.glob(DB_PATH.name + "*"))
    return {"first_ts": first, "size_mb": round(size / 1e6, 2)}


# ---------------------------------------------------------------- history

SERIES_KEYS = (
    "voltage", "current", "power", "soc", "remaining_ah", "cell_min", "cell_max",
    "delta_mv", "chg", "dsg", "balancing", "balance_a", "alarms",
)  # fmt: skip


def _mean(values: Iterable[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _pick(values: Iterable[Any], fn: Callable[[list[Any]], Any]) -> Any:
    vals = [v for v in values if v is not None]
    return fn(vals) if vals else None


def _round(value: Any, digits: int = 4) -> Any:
    return round(value, digits) if isinstance(value, float) else value


def _temp_order(name: str) -> tuple[int, str]:
    return (0 if name.startswith("MOS") else 1, name)


def merge_rows(group: list[dict[str, Any]]) -> dict[str, Any]:
    """Collapse the rows of one time bucket into a single chart point."""
    n_cells = max(len(r["cells"]) for r in group)
    names = {name for r in group for name in r["temps"]}
    alarms = 0
    for r in group:
        alarms |= r["alarms"] or 0
    return {
        "ts": _mean(r["ts"] for r in group),
        **{
            key: _mean(r[key] for r in group)
            for key in ("voltage", "current", "power", "soc", "remaining_ah", "balance_a")
        },
        "cell_min": _pick((r["cell_min"] for r in group), min),
        "cell_max": _pick((r["cell_max"] for r in group), max),
        "delta_mv": _pick((r["delta_mv"] for r in group), max),
        # a switch shows as off for the whole bucket if it was off at any point
        "chg": _pick((r["chg"] for r in group), min),
        "dsg": _pick((r["dsg"] for r in group), min),
        "balancing": _pick((r["balancing"] for r in group), max),
        "alarms": alarms,
        "cells": [_mean(r["cells"][i] for r in group if len(r["cells"]) > i) for i in range(n_cells)],
        "temps": {name: _mean(r["temps"].get(name) for r in group) for name in names},
    }


def build_series(
    rows: list[dict[str, Any]], t0: float, t1: float, max_points: int, gap_s: float
) -> dict[str, Any]:
    """Column arrays for the charts, bucketed down to max_points, with a null
    point wherever the logger wasn't running so lines break instead of bridging."""
    points = rows
    if len(rows) > max_points:
        width = (t1 - t0) / max_points
        gap_s = max(gap_s, 2.5 * width)
        points, group, bucket = [], [], None
        for row in rows:
            key = int((row["ts"] - t0) // width)
            if group and key != bucket:
                points.append(merge_rows(group))
                group = []
            bucket = key
            group.append(row)
        if group:
            points.append(merge_rows(group))

    n_cells = max((len(p["cells"]) for p in points), default=0)
    temp_names = sorted({n for p in points for n in p["temps"]}, key=_temp_order)
    out: dict[str, Any] = {
        "ts": [],
        **{key: [] for key in SERIES_KEYS},
        "cells": [[] for _ in range(n_cells)],
        "temps": {name: [] for name in temp_names},
    }

    def append(point: dict[str, Any]) -> None:
        out["ts"].append(round(point["ts"], 1))
        for key in SERIES_KEYS:
            out[key].append(_round(point.get(key)))
        cells = point.get("cells", [])
        for i, col in enumerate(out["cells"]):
            col.append(_round(cells[i]) if i < len(cells) else None)
        temps = point.get("temps", {})
        for name, col in out["temps"].items():
            col.append(_round(temps.get(name), 1))

    prev_ts = None
    for point in points:
        if prev_ts is not None and point["ts"] - prev_ts > gap_s:
            append({"ts": (prev_ts + point["ts"]) / 2})
        append(point)
        prev_ts = point["ts"]
    return out


def _duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 90:
        return f"{minutes} min"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} h {minutes % 60:02d} min"
    return f"{hours // 24} d {hours % 24} h"


def find_sessions(rows: list[dict[str, Any]], cfg: Config) -> list[dict[str, Any]]:
    """Charge and discharge runs: stretches where |current| stays above the
    activity threshold, with the charge moved (trapezoid integration)."""
    threshold = cfg.active_current_a

    def start(kind: str, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "kind": kind, "start": row["ts"], "end": row["ts"], "open": False, "ah": 0.0,
            "peak_a": abs(row["current"] or 0.0), "soc_start": row["soc"], "v_start": row["voltage"],
            "cell_min": row["cell_min"], "cell_max": row["cell_max"],
        }  # fmt: skip

    def extend(s: dict[str, Any], a: dict[str, Any], b: dict[str, Any]) -> None:
        s["ah"] += ((a["current"] or 0.0) + (b["current"] or 0.0)) / 2 * (b["ts"] - a["ts"]) / 3600
        s["end"] = b["ts"]
        s["peak_a"] = max(s["peak_a"], abs(b["current"] or 0.0))
        s["cell_min"] = _pick((s["cell_min"], b["cell_min"]), min)
        s["cell_max"] = _pick((s["cell_max"], b["cell_max"]), max)

    def finish(s: dict[str, Any], last: dict[str, Any]) -> dict[str, Any]:
        s.update(soc_end=last["soc"], v_end=last["voltage"], ah=round(abs(s["ah"]), 2))
        return s

    sessions: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    prev: dict[str, Any] | None = None
    for row in rows:
        amps = row["current"] or 0.0
        kind = "charge" if amps >= threshold else "discharge" if amps <= -threshold else None
        broken = prev is not None and row["ts"] - prev["ts"] > cfg.gap_seconds
        if current is not None and prev is not None and (broken or kind != current["kind"]):
            if not broken:
                extend(current, prev, row)  # include the ramp down to this reading
            sessions.append(finish(current, prev if broken else row))
            current = None
        if kind is not None:
            if current is None:
                current = start(kind, row)
            elif prev is not None:
                extend(current, prev, row)
        prev = row
    if current is not None and prev is not None:
        current["open"] = True
        sessions.append(finish(current, prev))
    return [s for s in sessions if s["open"] or s["end"] - s["start"] >= 30]


def find_events(rows: list[dict[str, Any]], cfg: Config) -> list[dict[str, Any]]:
    """Switch changes, alarms raised/cleared and logger downtime."""
    expected = set(cfg.thresholds.get("expected_alarms", []))
    events: list[dict[str, Any]] = []
    prev: dict[str, Any] | None = None
    for row in rows:
        if prev is not None:
            ts = row["ts"]
            if ts - prev["ts"] > cfg.gap_seconds:
                events.append({
                    "ts": prev["ts"], "level": "muted",
                    "text": f"No data for {_duration(ts - prev['ts'])} (logger was not running)",
                })  # fmt: skip
            parts: list[str] = []
            level = "info"
            raised = (row["alarms"] or 0) & ~(prev["alarms"] or 0)
            cleared = (prev["alarms"] or 0) & ~(row["alarms"] or 0)
            if None not in (row["chg"], prev["chg"]) and row["chg"] != prev["chg"]:
                parts.append("Charging switched " + ("on" if row["chg"] else "off"))
                active = set(alarm_names(row["alarms"]))
                if not row["chg"] and not (active and active <= expected):
                    level = "warning"
            if None not in (row["dsg"], prev["dsg"]) and row["dsg"] != prev["dsg"]:
                parts.append("Discharging switched " + ("on" if row["dsg"] else "off"))
                if not row["dsg"]:
                    level = "critical"
            for name in alarm_names(raised):
                if name in expected:
                    parts.append(f"{name} (expected)")
                else:
                    parts.append(f"Alarm: {name}")
                    level = "critical"
            parts.extend(f"{name} cleared" for name in alarm_names(cleared))
            if parts:
                events.append({"ts": ts, "level": level, "text": " · ".join(parts)})
        prev = row
    return events


def history_payload(cfg: Config, t0: float, t1: float, points: int) -> dict[str, Any]:
    rows = load_rows(t0, t1)
    return {
        "from": t0,
        "to": t1,
        "count": len(rows),
        "bucket_seconds": (t1 - t0) / points if len(rows) > points else 0,
        "series": build_series(rows, t0, t1, points, cfg.gap_seconds),
        "sessions": find_sessions(rows, cfg),
        "events": find_events(rows, cfg),
    }


def export_csv(t0: float, t1: float) -> bytes:
    rows = load_rows(t0, t1)
    n_cells = max((len(r["cells"]) for r in rows), default=0)
    temp_names = sorted({n for r in rows for n in r["temps"]}, key=_temp_order)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "time", "unix_ts", "voltage_v", "current_a", "power_w", "soc_pct", "remaining_ah",
        *[f"cell{i + 1}_v" for i in range(n_cells)], "delta_mv",
        *[f"temp_{n}_c" for n in temp_names],
        "charge_switch", "discharge_switch", "balancing", "balance_current_a", "alarms",
    ])  # fmt: skip
    for r in rows:
        writer.writerow([
            datetime.fromtimestamp(r["ts"]).isoformat(sep=" ", timespec="seconds"), r["ts"],
            r["voltage"], r["current"], r["power"], r["soc"], r["remaining_ah"],
            *[r["cells"][i] if i < len(r["cells"]) else None for i in range(n_cells)], r["delta_mv"],
            *[r["temps"].get(n) for n in temp_names],
            r["chg"], r["dsg"], r["balancing"], r["balance_a"], "; ".join(alarm_names(r["alarms"])),
        ])  # fmt: skip
    return buf.getvalue().encode("utf-8-sig")  # BOM so Excel picks UTF-8


# ---------------------------------------------------------------- live state


@dataclass
class LiveState:
    """Shared between the Bluetooth loop and the web server threads."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    status: str = "starting"
    detail: str = ""
    pause_until: float = 0.0
    sample: dict[str, Any] | None = None
    device: dict[str, str] = field(default_factory=dict)
    wake: Callable[[], None] = lambda: None  # set by the Bluetooth loop
    logged: tuple[str, str] = ("", "")
    # settings: decoded from every settings frame the BMS sends
    settings: dict[str, Any] | None = None
    settings_at: float = 0.0
    # for running settings reads/writes on the Bluetooth loop from web threads
    loop: asyncio.AbstractEventLoop | None = None
    bms: JkBms | None = None
    settings_busy: threading.Lock = field(default_factory=threading.Lock)

    def set_settings(self, frame: bytes) -> None:
        decoded = decode_settings(frame)
        with self.lock:
            self.settings, self.settings_at = decoded, time.time()

    def settings_snapshot(self) -> tuple[dict[str, Any] | None, float]:
        with self.lock:
            return self.settings, self.settings_at

    def set_status(self, status: str, detail: str = "") -> None:
        with self.lock:
            self.status, self.detail = status, detail
        # "searching" alternates with "not found" every retry while the BMS is
        # out of reach; log only real changes so the file doesn't fill up
        if status != "searching" and (status, detail) != self.logged:
            self.logged = (status, detail)
            log.info("%s%s", status, f": {detail}" if detail else "")

    def set_sample(self, row: dict[str, Any]) -> None:
        with self.lock:
            self.sample = row

    def paused_for(self) -> float:
        with self.lock:
            return max(0.0, self.pause_until - time.time())

    def pause(self, seconds: float) -> None:
        with self.lock:
            self.pause_until = time.time() + seconds
        self.wake()

    def resume(self) -> None:
        with self.lock:
            self.pause_until = 0.0
        self.wake()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "status": self.status,
                "detail": self.detail,
                "pause_until": self.pause_until,
                "sample": self.sample,
                "device": dict(self.device),
            }


def status_payload(cfg: Config, state: LiveState) -> dict[str, Any]:
    snap = state.snapshot()
    sample = snap["sample"]
    if sample is not None:
        sample = {**sample, "alarm_names": alarm_names(sample["alarms"])}
    thresholds = dict(cfg.thresholds)
    settings, _ = state.settings_snapshot()
    if settings:  # the BMS's own values win over the config.toml copies
        v = settings["values"]
        thresholds.update(
            cell_ovp_v=v["cell_ovp"], cell_uvp_v=v["cell_uvp"], balance_start_v=v["balance_start_v"],
            balance_trigger_mv=round(v["balance_trigger_v"] * 1000),
        )
    return {
        **snap,
        "sample": sample,
        "now": time.time(),
        "poll_seconds": cfg.poll_seconds,
        "active_current_a": cfg.active_current_a,
        "thresholds": thresholds,
        "db": db_stats(),
    }


# ---------------------------------------------------------------- settings: read & write


class SettingsError(Exception):
    """A settings request that can't run right now; the message is for the user."""


def _same(s: Setting, a: Any, b: Any) -> bool:
    return raw_value(s, a) == raw_value(s, b)


def _write_order(current: dict[str, Any], changes: dict[str, Any]) -> list[str]:
    """Order writes so each intermediate state still passes the rules, e.g. raise
    OVP before raising OVPR to just under it (the BMS may refuse otherwise)."""
    state, pending, order = dict(current), dict(changes), []
    while pending:
        for key in list(pending):
            trial = {**state, key: pending[key]}
            if not any(key in (a, b) and trial[a] > trial[b] + off + 1e-9 for a, b, off, _ in SETTING_RULES):
                order.append(key)
                state[key] = pending.pop(key)
                break
        else:
            order.extend(pending)
            break
    return order


def settings_payload(cfg: Config, state: LiveState) -> dict[str, Any]:
    settings, read_at = state.settings_snapshot()
    history: list[dict[str, Any]] = []
    if DB_PATH.exists():
        with contextlib.closing(open_db()) as conn, contextlib.suppress(sqlite3.OperationalError):
            history = [dict(r) for r in conn.execute("SELECT * FROM setting_changes ORDER BY ts DESC LIMIT 30")]
    return {
        "connected": state.bms is not None,
        "paused": state.paused_for() > 0,
        "read_at": read_at,
        "values": settings["values"] if settings else None,
        "flags": settings["flags"] if settings else None,
        "groups": [{"key": k, "label": label} for k, label in SETTING_GROUPS],
        "settings": [
            {
                "key": s.key, "label": s.label, "jk_name": s.jk_name, "group": s.group, "unit": s.unit,
                "digits": s.digits, "step": None if s.switch else 10**-s.digits,
                "min": cfg.limits.get(s.key, (None, None))[0], "max": cfg.limits.get(s.key, (None, None))[1],
                "editable": editable(s, cfg.limits),
                # why it's read-only: "config" = no [limits] range, "fixed" = never written here
                "locked": None if editable(s, cfg.limits) else "config" if s.register is not None else "fixed",
                "switch": s.switch, "help": s.help, "caution": s.caution,
            }
            for s in SETTINGS
        ],  # fmt: skip
        "rules": [{"a": a, "b": b, "offset": off, "message": msg} for a, b, off, msg in SETTING_RULES],
        "config_problems": list(cfg.problems),
        "history": history,
    }


async def apply_settings(cfg: Config, state: LiveState, changes: dict[str, Any]) -> dict[str, Any]:
    """Write changes one register at a time. After each write the whole settings
    frame is read back: the target must hold the new value and nothing else may
    have moved, otherwise the remaining writes are skipped."""
    bms = state.bms
    if bms is None:
        raise SettingsError("Not connected to the BMS")
    await bms.read_settings()
    current = dict(state.settings_snapshot()[0]["values"])
    errors = validate_changes(cfg.limits, current, changes)
    if errors:
        return {"ok": False, "errors": errors, "results": []}

    results: list[dict[str, Any]] = []
    for key in _write_order(current, changes):
        s, new = SETTINGS_BY_KEY[key], changes[key]
        old = current[key]
        if _same(s, old, new):
            results.append({"key": key, "status": "unchanged", "old": old, "new": new, "read_back": old})
            continue
        log.warning("writing %s: %s -> %s (register 0x%02X)", key, old, new, s.register)
        # Like read requests, a write that lands while the BMS is busy sending is
        # silently dropped (seen live). If nothing changed at all, send it again;
        # repeating the same register write is harmless.
        for attempt in range(3):
            await bms.write_register(s.register, raw_value(s, new))
            await asyncio.sleep(0.5)
            await bms.read_settings()
            after = dict(state.settings_snapshot()[0]["values"])
            verified = _same(s, after[key], new)
            moved = [k for k in after if k != key and not _same(SETTINGS_BY_KEY[k], after[k], current[k])]
            if verified or moved or not _same(s, after[key], old):
                break
            log.info("%s: the BMS ignored the write, sending it again (%d)", key, attempt + 2)
        note = ""  # a value the BMS didn't take shows in read_back
        if moved:
            note = "also changed: " + ", ".join(f"{SETTINGS_BY_KEY[k].label} {current[k]} -> {after[k]}" for k in moved)
        ok = verified and not moved
        (log.info if ok else log.error)("%s %s: read back %s%s", "verified" if ok else "FAILED", key, after[key],
                                        f" ({note})" if note else "")  # fmt: skip
        with contextlib.closing(open_db()) as conn:
            conn.execute(
                "INSERT INTO setting_changes VALUES (?, ?, ?, ?, ?, ?, ?)",
                (time.time(), key, float(old), float(new), float(after[key]), int(ok), note),
            )
            conn.commit()
        results.append({"key": key, "status": "ok" if ok else "failed", "old": old, "new": new,
                        "read_back": after[key], "note": note})  # fmt: skip
        current = after
        if not ok:
            break  # stop at the first surprise
    done = {r["key"] for r in results}
    results += [{"key": k, "status": "skipped", "new": changes[k]} for k in changes if k not in done]
    return {"ok": all(r["status"] in ("ok", "unchanged") for r in results), "errors": [], "results": results}


def run_settings_op(state: LiveState, make_coro: Callable[[], Any], timeout: float) -> Any:
    """Run a settings coroutine on the Bluetooth loop from a web thread, one at a time."""
    if state.loop is None or state.bms is None:
        raise SettingsError(
            "Logging is paused, resume it to use settings" if state.paused_for() > 0 else "Not connected to the BMS yet"
        )
    if not state.settings_busy.acquire(blocking=False):
        raise SettingsError("Another settings change is still running")
    try:
        future = asyncio.run_coroutine_threadsafe(make_coro(), state.loop)
        try:
            return future.result(timeout)
        except TimeoutError:
            future.cancel()
            raise SettingsError("The BMS didn't answer in time") from None
    finally:
        state.settings_busy.release()


# ---------------------------------------------------------------- Bluetooth loop


def describe(exc: BaseException) -> str:
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}"[:200] if text else type(exc).__name__


class JkBms(JikongBMS):
    """aiobmsble's JK driver, plus the settings frame (type 0x01, which the base
    class drops) and register writes. Leans on aiobmsble internals (_op_lock,
    _send_msg, _cmd), hence the pinned minor version."""

    _HEAD = b"\x55\xaa\xeb\x90"
    _FRAME_LEN = 300

    def __init__(self, ble_device: Any, on_settings: Callable[[bytes], None]) -> None:
        super().__init__(ble_device)
        self._on_settings = on_settings
        self._settings_buf = bytearray()
        self._settings_seen = asyncio.Event()
        self._frame_done = asyncio.Event()

    def _notification_handler(self, sender: Any, data: bytearray) -> None:
        self._collect_settings(bytes(data))
        super()._notification_handler(sender, data)

    def _collect_settings(self, chunk: bytes) -> None:
        chunk = chunk.removeprefix(b"AT\r\n")  # sent by the BLE module itself
        if chunk.startswith(self._HEAD):
            self._settings_buf = bytearray(chunk)
        elif self._settings_buf:
            self._settings_buf += chunk
        if len(self._settings_buf) < self._FRAME_LEN:
            return
        frame = bytes(self._settings_buf[: self._FRAME_LEN])
        self._settings_buf = bytearray()
        if sum(frame[:-1]) & 0xFF != frame[-1]:
            return
        self._frame_done.set()
        if frame[4] == 0x01:
            self._on_settings(frame)
            self._settings_seen.set()

    async def _send(self, frame: bytes) -> None:
        # The BMS streams a frame every ~0.6 s and drops commands that arrive
        # while it's sending (seen live: writes right after a reply were lost),
        # so send just after a frame has finished, at the start of the quiet gap.
        self._frame_done.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._frame_done.wait(), 2.0)
        await asyncio.sleep(0.05)
        await self._send_msg(frame, 0, self._char_write_handle, 0, bool(self._inv_wr_mode))

    async def read_settings(self, attempts: int = 5) -> None:
        """Ask for a fresh settings frame: the BMS answers 0x96 with one 0x01 frame
        (measured: ~0.3 s), but drops a request that lands while it's still
        replying to the previous one, hence the retries."""
        await self._connect()
        async with self._op_lock:
            for _ in range(attempts):
                self._settings_seen.clear()
                await self._send(self._cmd(0x96))
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._settings_seen.wait(), 1.0)
                    return
        raise TimeoutError("the BMS didn't send its settings")

    async def write_register(self, register: int, raw: int) -> None:
        await self._connect()
        async with self._op_lock:
            await self._send(self._cmd(register, raw.to_bytes(4, "little", signed=True)))


async def find_device(cfg: Config):
    def matches(device, adv) -> bool:
        if cfg.bms_address:
            return device.address.upper() == cfg.bms_address
        return (adv.local_name or device.name or "").strip() == cfg.bms_name

    return await BleakScanner.find_device_by_filter(matches, timeout=20.0)


async def ble_loop(cfg: Config, state: LiveState) -> None:
    loop = asyncio.get_running_loop()
    wake = asyncio.Event()
    state.wake = lambda: loop.call_soon_threadsafe(wake.set)
    state.loop = loop

    async def nap(seconds: float) -> None:
        """Sleep, waking early on a pause/resume request from the dashboard."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(wake.wait(), timeout=max(0.0, seconds))
        wake.clear()

    db = open_db(create=True)
    recorder = Recorder(cfg)
    bms: JikongBMS | None = None
    fresh = False
    backoff = 5.0

    async def release() -> None:
        nonlocal bms
        state.bms = None
        if bms is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(bms.disconnect(), timeout=15)
            bms = None

    try:
        while True:
            if (paused := state.paused_for()) > 0:
                if bms is not None:
                    await release()
                state.set_status("paused", "Bluetooth is free for the phone app")
                await nap(paused)
                continue

            if bms is None:
                state.set_status("searching", f"looking for {cfg.bms_address or cfg.bms_name}")
                try:
                    device = await find_device(cfg)
                except Exception as exc:  # e.g. Bluetooth switched off
                    state.set_status("error", f"Bluetooth: {describe(exc)}")
                    device = None
                if device is None:
                    if state.status != "error":
                        state.set_status("not found", "BMS not advertising: phone app connected, or out of range")
                    await nap(backoff)
                    backoff = min(backoff * 2, 60.0)
                    continue
                state.set_status("connecting")
                bms, fresh = JkBms(device, state.set_settings), True

            try:
                sample = await asyncio.wait_for(bms.async_update(), timeout=120)
                if fresh:
                    fresh = False
                    state.bms = bms
                    with contextlib.suppress(Exception):
                        info = await asyncio.wait_for(bms.device_info(), timeout=30)
                        with state.lock:
                            state.device = {
                                k: str(info.get(k, "")) for k in ("name", "model", "hw_version", "sw_version")
                            }
            except Exception as exc:
                log.warning("BMS read failed: %s", describe(exc))
                await release()
                state.set_status("reconnecting", describe(exc))
                await nap(backoff)
                backoff = min(backoff * 2, 60.0)
                continue

            backoff = 5.0
            row = sample_to_row(sample, time.time())
            if row is None:
                log.warning("skipped an incomplete reading")
            else:
                state.set_sample(row)
                state.set_status("logging")
                if recorder.should_store(row):
                    try:
                        insert_row(db, row)
                    except sqlite3.Error as exc:
                        log.error("can't store reading: %s", describe(exc))
            await nap(cfg.poll_seconds)
    finally:
        await release()
        db.close()


# ---------------------------------------------------------------- web server

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
}
CSP = (
    "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR would let a second logger bind the same port; an
    # exclusive bind turns the port into a single-instance lock instead.
    allow_reuse_address = False

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def handle_error(self, request: Any, client_address: Any) -> None:
        if isinstance(sys.exc_info()[1], ConnectionError):
            return  # the browser went away mid-response
        log.exception("web request failed")


def _query_num(query: dict[str, list[str]], key: str, default: float) -> float:
    try:
        return float(query[key][0])
    except (KeyError, IndexError, ValueError):
        return default


def _time_range(query: dict[str, list[str]]) -> tuple[float, float]:
    t1 = _query_num(query, "to", time.time())
    t0 = _query_num(query, "from", t1 - 86400)
    return (t0, t1) if t0 < t1 else (t1 - 3600, t1)


def _stamp(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y%m%d-%H%M")


def make_handler(cfg: Config, state: LiveState) -> type[BaseHTTPRequestHandler]:
    local_hosts = {f"127.0.0.1:{cfg.port}", f"localhost:{cfg.port}", f"[::1]:{cfg.port}"}
    check_host = cfg.host in ("127.0.0.1", "localhost", "::1")

    class Handler(BaseHTTPRequestHandler):
        server_version = "jkbms-logger"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:
            log.debug("web: " + format, *args)  # the default writes to stderr, which pythonw lacks

        def do_GET(self) -> None:
            if not self._allowed():
                return
            url = urlparse(self.path)
            query = parse_qs(url.query)
            if url.path == "/api/status":
                self._json(status_payload(cfg, state))
            elif url.path == "/api/settings":
                self._json(settings_payload(cfg, state))
            elif url.path == "/api/history":
                t0, t1 = _time_range(query)
                points = int(min(max(_query_num(query, "points", 1500), 100), 5000))
                self._json(history_payload(cfg, t0, t1, points))
            elif url.path == "/api/export.csv":
                t0, t1 = _time_range(query)
                disposition = f'attachment; filename="battery_{_stamp(t0)}_{_stamp(t1)}.csv"'
                self._send(HTTPStatus.OK, export_csv(t0, t1), "text/csv; charset=utf-8",
                           {"Content-Disposition": disposition})  # fmt: skip
            else:
                self._static(url.path)

        def do_POST(self) -> None:
            if not self._allowed():
                return
            # Other web pages can't send a custom header here without a CORS
            # preflight, which this server never answers.
            if self.headers.get("X-JKBMS") != "1":
                self._send(HTTPStatus.FORBIDDEN, b"forbidden", "text/plain")
                return
            path = urlparse(self.path).path
            if path in ("/api/settings", "/api/settings/read"):
                self._settings_post(path)
                return
            if path == "/api/pause":
                try:
                    minutes = float(self._body_json().get("minutes", 10))
                except (TypeError, ValueError):
                    minutes = 10.0
                state.pause(min(max(minutes, 1.0), 240.0) * 60)
            elif path == "/api/resume":
                state.resume()
            else:
                self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
                return
            self._json(status_payload(cfg, state))

        def _settings_post(self, path: str) -> None:
            try:
                if path == "/api/settings/read":
                    run_settings_op(state, lambda: state.bms.read_settings(), timeout=20)
                    self._json(settings_payload(cfg, state))
                    return
                changes = self._body_json().get("changes")
                if not isinstance(changes, dict) or not changes:
                    raise SettingsError("No changes given")
                outcome = run_settings_op(
                    state, lambda: apply_settings(cfg, state, changes), timeout=30 + 10 * len(changes)
                )
                self._json({**outcome, "settings": settings_payload(cfg, state)})
            except SettingsError as exc:
                self._json({"error": str(exc)}, HTTPStatus.CONFLICT)
            except Exception as exc:  # Bluetooth trouble mid-operation
                log.exception("settings request failed")
                self._json({"error": f"Bluetooth error: {describe(exc)}"}, HTTPStatus.BAD_GATEWAY)

        def _allowed(self) -> bool:
            # blocks DNS rebinding: another name pointed at 127.0.0.1
            if check_host and self.headers.get("Host", "") not in local_hosts:
                self._send(HTTPStatus.FORBIDDEN, b"forbidden", "text/plain")
                return False
            return True

        def _body_json(self) -> dict[str, Any]:
            length = min(int(self.headers.get("Content-Length") or 0), 4096)
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return {}
            return data if isinstance(data, dict) else {}

        def _static(self, path: str) -> None:
            rel = "index.html" if path in ("", "/") else path.lstrip("/")
            target = (WEB_DIR / rel).resolve()
            if not (target.is_relative_to(WEB_DIR) and target.suffix in STATIC_TYPES and target.is_file()):
                self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
                return
            self._send(HTTPStatus.OK, target.read_bytes(), STATIC_TYPES[target.suffix])

        def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
            self._send(status, json.dumps(payload, separators=(",", ":")).encode(), "application/json")

        def _send(self, status: HTTPStatus, body: bytes, ctype: str, headers: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", CSP)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

    return Handler


# ---------------------------------------------------------------- main


def setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    ]
    if sys.stderr is not None:  # None under pythonw
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", handlers=handlers)
    for noisy in ("bleak", "bleak_retry_connector", "aiobmsble"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> int:
    setup_logging()
    cfg = Config.load(ROOT / "config.toml")
    if not (cfg.bms_name or cfg.bms_address):
        log.error("config.toml: set [bms] name or address")
        return 2
    for problem in cfg.problems:
        log.warning("config.toml: %s", problem)
    state = LiveState()
    try:
        server = DashboardServer((cfg.host, cfg.port), make_handler(cfg, state))
    except OSError as exc:
        log.error("can't listen on %s:%s, is the logger already running? (%s)", cfg.host, cfg.port, exc)
        return 1
    threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
    log.info("dashboard at http://%s:%s/", cfg.host, cfg.port)
    try:
        asyncio.run(ble_loop(cfg, state))
    except KeyboardInterrupt:
        pass
    except Exception:
        log.exception("logger crashed")
        return 1
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
