"""Battery page data for the Matek I2C-INA-BM board (a TI INA228).

The chip measures bus voltage, shunt voltage, current, power, energy, charge
and its own temperature. It does not measure state of charge, health or cycle
count. Those three are estimated here and always marked as estimates.

Wiring assumed for the sign of the current: IN+ on the battery side, IN- on
the load side. Positive current is discharge. Charging is a negative current.

The charge and energy registers start again at 0 whenever the board loses
power. Cycle count and the learned capacity are kept in a small JSON file so
they survive that, and a backwards jump of the charge register is ignored.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("kiro.battery")

# Register map, INA228 datasheet (SBOSA20). Multi-byte registers are big-endian.
REG_CONFIG = 0x00
REG_ADC_CONFIG = 0x01
REG_SHUNT_CAL = 0x02
REG_VSHUNT = 0x04
REG_VBUS = 0x05
REG_DIETEMP = 0x06
REG_CURRENT = 0x07
REG_POWER = 0x08
REG_ENERGY = 0x09
REG_CHARGE = 0x0A
REG_DIAG_ALRT = 0x0B
REG_MANUFACTURER_ID = 0x3E
REG_DEVICE_ID = 0x3F

MANUFACTURER_TI = 0x5449
DEVICE_INA228 = 0x228  # DEVICE_ID bits 15:4. Bits 3:0 are the revision.

# Continuous bus, shunt and temperature. Each conversion is 1.052 ms, averaged 16 times.
# MODE 0xF, VBUSCT 5, VSHCT 5, VTCT 5, AVG 2.
ADC_CONFIG = 0xFB6A

# Full-scale shunt voltage. ADCRANGE 1 is ±40.96 mV, ADCRANGE 0 is ±163.84 mV.
SHUNT_FS_LOW_V = 0.04096
VSHUNT_LSB_LOW_V = 78.125e-9
VSHUNT_LSB_HIGH_V = 312.5e-9
VBUS_LSB_V = 195.3125e-6
DIETEMP_LSB_C = 7.8125e-3

# Fault bits in DIAG_ALRT. Conversion-ready and the summary ALERT bit are not faults.
DIAG_FAULTS = (
    (11, "energy_overflow"),
    (10, "charge_overflow"),
    (9, "math_overflow"),
    (8, "temperature_over_limit"),
    (7, "shunt_over_limit"),
    (6, "shunt_under_limit"),
    (5, "bus_over_limit"),
    (4, "bus_under_limit"),
    (3, "power_over_limit"),
    (1, "memory_checksum"),
)

# Rested open-circuit voltage per cell, percent. A rough lookup, not a gauge.
# LiPo includes 76% so the mock reading starts on an exact table point.
OCV_TABLES = {
    "lipo": (
        (3.40, 0), (3.55, 10), (3.65, 20), (3.72, 30), (3.77, 40), (3.81, 50),
        (3.84, 60), (3.87, 70), (3.90, 76), (3.93, 80), (4.00, 90), (4.10, 95), (4.20, 100),
    ),
    "li-ion": (
        (3.00, 0), (3.40, 5), (3.55, 10), (3.65, 20), (3.72, 30), (3.76, 40),
        (3.80, 50), (3.84, 60), (3.88, 70), (3.94, 80), (4.02, 90), (4.12, 95), (4.20, 100),
    ),
    "lifepo4": (
        (2.50, 0), (2.90, 5), (3.05, 10), (3.15, 20), (3.20, 30), (3.25, 40),
        (3.27, 50), (3.29, 60), (3.30, 70), (3.32, 80), (3.35, 90), (3.40, 95), (3.60, 100),
    ),
}

API_FIELDS = (
    "source", "updated_at", "voltage_v", "current_a", "power_w", "energy_wh", "charge_ah",
    "temperature_c", "percent", "charging", "time_remaining_min", "cell_voltage_v", "cells",
    "chemistry", "capacity_ah", "health_pct", "health_estimated", "cycles", "cycles_estimated",
    "alerts",
)

_CHEM_ALIASES = {
    "lipo": "lipo", "li-po": "lipo", "lipolymer": "lipo",
    "liion": "li-ion", "li-ion": "li-ion", "lion": "li-ion", "nmc": "li-ion",
    "lifepo4": "lifepo4", "life": "lifepo4", "lfp": "lifepo4",
}


def current_lsb(max_current_a: float) -> float:
    """Amps per bit of the 20-bit current result. CURRENT_LSB = Imax / 2^19."""
    return float(max_current_a) / float(1 << 19)


def choose_adcrange(max_current_a: float, shunt_ohm: float) -> int:
    """1 when the shunt drop at Imax fits in ±40.96 mV, else 0 (±163.84 mV).

    The Matek board is 204.8 A across 0.0002 ohm, which is exactly 40.96 mV,
    so the finer range is the one that matches the board.
    """
    return 1 if max_current_a * shunt_ohm <= SHUNT_FS_LOW_V + 1e-9 else 0


def shunt_cal(lsb: float, shunt_ohm: float, adcrange: int) -> int:
    """SHUNT_CAL register. Datasheet: 13107.2e6 * CURRENT_LSB * Rshunt, times 4 if ADCRANGE is 1."""
    scale = 4.0 if adcrange else 1.0
    cal = 13107.2e6 * lsb * shunt_ohm * scale
    if not math.isfinite(cal) or cal <= 0:
        raise ValueError("SHUNT_CAL must be a positive number")
    return int(min(65535, max(1, round(cal))))


def sign_extend(value: int, bits: int) -> int:
    value &= (1 << bits) - 1
    if value & (1 << (bits - 1)):
        value -= 1 << bits
    return value


def decode_vshunt_v(raw24: int, adcrange: int) -> float:
    """Shunt voltage in volts. The 20-bit signed result sits in bits 23:4."""
    counts = sign_extend(raw24, 24) >> 4
    lsb = VSHUNT_LSB_LOW_V if adcrange else VSHUNT_LSB_HIGH_V
    return counts * lsb


def decode_vbus_v(raw24: int) -> float:
    """Bus voltage in volts. Unsigned 20-bit result in bits 23:4, 195.3125 µV per bit."""
    return ((raw24 >> 4) & 0xFFFFF) * VBUS_LSB_V


def decode_dietemp_c(raw16: int) -> float:
    """Die temperature in °C. The 16-bit register LSB is 7.8125 m°C."""
    return sign_extend(raw16, 16) * DIETEMP_LSB_C


def decode_current_a(raw24: int, lsb: float) -> float:
    """Current in amps. Signed 20-bit result in bits 23:4, each bit CURRENT_LSB."""
    return (sign_extend(raw24, 24) >> 4) * lsb


def decode_power_w(raw24: int, lsb: float) -> float:
    """Power in watts. Unsigned 24-bit, LSB = 3.2 * CURRENT_LSB."""
    return (raw24 & 0xFFFFFF) * 3.2 * lsb


def decode_energy_j(raw40: int, lsb: float) -> float:
    """Energy in joules. Unsigned 40-bit, LSB = 16 * 3.2 * CURRENT_LSB."""
    return (raw40 & ((1 << 40) - 1)) * 16 * 3.2 * lsb


def decode_charge_c(raw40: int, lsb: float) -> float:
    """Charge in coulombs. Signed 40-bit, LSB = CURRENT_LSB."""
    return sign_extend(raw40, 40) * lsb


def decode_alerts(raw16: int) -> list[str]:
    """Fault names set in DIAG_ALRT, highest bit first. Empty when nothing is wrong."""
    return [name for bit, name in DIAG_FAULTS if raw16 & (1 << bit)]


def normalize_chemistry(name: str) -> str:
    key = (name or "").strip().lower().replace("_", "").replace(" ", "")
    # "li-ion" and "li-po" keep their hyphen; the replace above only drops "_" and spaces.
    key = key.replace("–", "-")
    if key in OCV_TABLES:
        return key
    return _CHEM_ALIASES.get(key, "lipo")


def ocv_percent(chemistry: str, cell_v: float) -> float:
    """Percent from a rested cell voltage. Flat outside the table."""
    table = OCV_TABLES[normalize_chemistry(chemistry)]
    if cell_v <= table[0][0]:
        return float(table[0][1])
    if cell_v >= table[-1][0]:
        return float(table[-1][1])
    for (v0, p0), (v1, p1) in zip(table, table[1:]):
        if v0 <= cell_v <= v1 and v1 != v0:
            return p0 + (cell_v - v0) / (v1 - v0) * (p1 - p0)
    return float(table[-1][1])


def ocv_volts(chemistry: str, percent: float) -> float:
    """Rested cell voltage for a percent. The inverse of ocv_percent on the same table."""
    table = OCV_TABLES[normalize_chemistry(chemistry)]
    p = max(0.0, min(100.0, float(percent)))
    if p <= table[0][1]:
        return float(table[0][0])
    if p >= table[-1][1]:
        return float(table[-1][0])
    for (v0, p0), (v1, p1) in zip(table, table[1:]):
        if p0 <= p <= p1 and p1 != p0:
            return v0 + (p - p0) / (p1 - p0) * (v1 - v0)
    return float(table[-1][0])


def health_word(pct: float | None) -> str:
    """The four words the header already uses: good, fair, poor, unknown."""
    if pct is None:
        return "unknown"
    if pct >= 80:
        return "good"
    if pct >= 60:
        return "fair"
    return "poor"


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _round(value: float | None, places: int) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(float(value), places)


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    raw = os.environ.get(name, "")
    if raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        log.warning("battery: %s=%r is not a number, using %s", name, raw, default)
        return default
    if not lo <= value <= hi:
        log.warning("battery: %s=%s is outside %s..%s, using %s", name, value, lo, hi, default)
        return default
    return value


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    return int(_env_float(name, float(default), float(lo), float(hi)))


def parse_addr(text: str) -> int:
    """I2C address as a decimal number (69) or hex (0x45)."""
    raw = (text or "").strip().lower()
    if not raw:
        return 69
    try:
        value = int(raw, 16) if raw.startswith("0x") else int(raw, 10)
    except ValueError:
        log.warning("battery: address %r is not a number, using 69", text)
        return 69
    if not 0x03 <= value <= 0x77:
        log.warning("battery: address %s is not a 7-bit device address, using 69", value)
        return 69
    return value


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class BatteryConfig:
    """The pack and the chip. Defaults match the Matek board and a stand-in 4S 10 Ah pack.

    The stand-in pack is not a measurement of this robot. Set the env vars to the real pack.
    """

    chemistry: str = "lipo"
    cells: int = 4
    capacity_ah: float = 10.0
    cycle_life: float | None = None
    shunt_ohm: float = 0.0002
    max_current_a: float = 204.8
    i2c_bus: int = 7
    i2c_addr: int = 69
    idle_a: float = 0.05       # |current| below this is neither charging nor a useful time estimate
    rest_a: float = 0.15       # |current| at or below this can anchor the percent from voltage
    rest_s: float = 20.0

    @classmethod
    def from_env(cls) -> "BatteryConfig":
        chem = normalize_chemistry(os.environ.get("KIRO_BATT_CHEMISTRY", "lipo"))
        given = (os.environ.get("KIRO_BATT_CHEMISTRY", "") or "").strip()
        if given and normalize_chemistry(given) == "lipo" and given.lower().replace("_", "").replace(" ", "") not in (
            "lipo", "li-po", "lipolymer", ""):
            # normalize falls back to lipo. Warn only when the text was not a known lipo alias.
            key = given.lower().replace("_", "").replace(" ", "").replace("–", "-")
            if key not in ("lipo", "li-po", "lipolymer") and key not in _CHEM_ALIASES:
                log.warning("battery: chemistry %r is unknown, using lipo", given)
        life_raw = os.environ.get("KIRO_BATT_CYCLE_LIFE", "").strip()
        life = None
        if life_raw:
            try:
                life = float(life_raw)
            except ValueError:
                log.warning("battery: KIRO_BATT_CYCLE_LIFE=%r is not a number", life_raw)
            else:
                if life <= 0:
                    life = None
        return cls(
            chemistry=chem,
            cells=_env_int("KIRO_BATT_CELLS", 4, 1, 24),
            capacity_ah=_env_float("KIRO_BATT_CAPACITY_AH", 10.0, 0.1, 1000.0),
            cycle_life=life,
            shunt_ohm=_env_float("KIRO_BATT_SHUNT_OHM", 0.0002, 1e-6, 1.0),
            max_current_a=_env_float("KIRO_BATT_MAX_A", 204.8, 0.1, 1000.0),
            i2c_bus=_env_int("KIRO_BATT_I2C_BUS", 7, 0, 30),
            i2c_addr=parse_addr(os.environ.get("KIRO_BATT_I2C_ADDR", "69")),
            idle_a=_env_float("KIRO_BATT_IDLE_A", 0.05, 0.0, 50.0),
            rest_a=_env_float("KIRO_BATT_REST_A", 0.15, 0.0, 50.0),
            rest_s=_env_float("KIRO_BATT_REST_S", 20.0, 0.0, 3600.0),
        )


def default_state_path(mock: bool) -> Path:
    """Where the learned capacity and the cycle count are kept.

    Mock mode uses a different file so a pretend run does not overwrite the robot's count.
    KIRO_BATT_STATE overrides both (the tests use that).
    """
    chosen = os.environ.get("KIRO_BATT_STATE", "").strip()
    if chosen:
        return Path(chosen).expanduser()
    name = "battery_state_mock.json" if mock else "battery_state.json"
    return Path("~/.config/kiro-webapp").expanduser() / name


class Ina228:
    """One INA228 on an SMBus. Pass bus_obj in tests; otherwise open() uses smbus2."""

    def __init__(self, bus: int, addr: int, shunt_ohm: float = 0.0002, max_current_a: float = 204.8, bus_obj=None):
        self.bus_num = int(bus)
        self.addr = int(addr)
        self.shunt_ohm = float(shunt_ohm)
        self.max_current_a = float(max_current_a)
        self.adcrange = choose_adcrange(self.max_current_a, self.shunt_ohm)
        self.lsb = current_lsb(self.max_current_a)
        self.cal = shunt_cal(self.lsb, self.shunt_ohm, self.adcrange)
        self._bus = bus_obj

    def open(self) -> None:
        if self._bus is None:
            try:
                import smbus2
            except ImportError as exc:
                raise OSError("smbus2 is not installed") from exc
            try:
                self._bus = smbus2.SMBus(self.bus_num)
            except OSError:
                raise
            except Exception as exc:  # smbus2 wraps some IO errors
                raise OSError(str(exc)) from exc
        maker = self._read16(REG_MANUFACTURER_ID)
        device = self._read16(REG_DEVICE_ID)
        if maker != MANUFACTURER_TI or (device >> 4) != DEVICE_INA228:
            raise OSError(f"not an INA228 (manufacturer {maker:#06x}, device {device:#06x})")
        # Calibration before the mode register, and no reset: energy and charge stay put
        # across a server restart as long as the board itself keeps its power.
        self._write16(REG_SHUNT_CAL, self.cal)
        self._write16(REG_CONFIG, 0x0010 if self.adcrange else 0x0000)
        self._write16(REG_ADC_CONFIG, ADC_CONFIG)

    def close(self) -> None:
        bus, self._bus = self._bus, None
        if bus is not None and hasattr(bus, "close"):
            try:
                bus.close()
            except Exception:  # noqa: BLE001 - closing a missing bus is not fatal
                pass

    def read(self) -> dict:
        if self._bus is None:
            raise OSError("INA228 is not open")
        lsb = self.lsb
        return {
            "voltage_v": decode_vbus_v(self._read24(REG_VBUS)),
            "shunt_v": decode_vshunt_v(self._read24(REG_VSHUNT), self.adcrange),
            "current_a": decode_current_a(self._read24(REG_CURRENT), lsb),
            "power_w": decode_power_w(self._read24(REG_POWER), lsb),
            "energy_wh": decode_energy_j(self._read40(REG_ENERGY), lsb) / 3600.0,
            "charge_ah": decode_charge_c(self._read40(REG_CHARGE), lsb) / 3600.0,
            "temperature_c": decode_dietemp_c(self._read16(REG_DIETEMP)),
            "alerts": decode_alerts(self._read16(REG_DIAG_ALRT)),
        }

    def _read_n(self, reg: int, n: int) -> list[int]:
        data = self._bus.read_i2c_block_data(self.addr, reg, n)
        if len(data) < n:
            raise OSError(f"short read of register {reg:#04x}")
        return list(data[:n])

    def _read16(self, reg: int) -> int:
        hi, lo = self._read_n(reg, 2)
        return (hi << 8) | lo

    def _read24(self, reg: int) -> int:
        b0, b1, b2 = self._read_n(reg, 3)
        return (b0 << 16) | (b1 << 8) | b2

    def _read40(self, reg: int) -> int:
        value = 0
        for byte in self._read_n(reg, 5):
            value = (value << 8) | byte
        return value

    def _write16(self, reg: int, value: int) -> None:
        self._bus.write_i2c_block_data(self.addr, reg, [(value >> 8) & 0xFF, value & 0xFF])


class Estimator:
    """Percent from rested voltage, then coulomb counting from the charge register.

    The first voltage is a starting guess (the pack may be under load). Once the
    current stays near zero for rest_s seconds, the percent is replaced by the
    rested-voltage lookup. Between those anchors the charge register moves it.

    Health is learned full capacity divided by the design capacity, only after a
    rest-to-rest swing of at least 30 points and 0.5 Ah in the same direction.
    Cycles are discharged amp-hours divided by the design capacity. Both are estimates.
    """

    def __init__(self, cfg: BatteryConfig, state: dict | None = None):
        self.cfg = cfg
        data = state or {}
        self.soc = _opt_float(data.get("soc"))
        if self.soc is not None:
            self.soc = _clamp(self.soc, 0.0, 100.0)
        self.discharged_ah = max(0.0, _opt_float(data.get("discharged_ah")) or 0.0)
        self.counted_ah = _opt_float(data.get("counted_ah")) or 0.0
        self.learned_ah = _opt_float(data.get("learned_ah"))
        self._anchor_soc = _opt_float(data.get("anchor_soc"))
        self._anchor_counted = _opt_float(data.get("anchor_counted"))
        self._last_charge_ah = _opt_float(data.get("last_charge_ah"))
        self._rest_since: float | None = None
        self._rest_done = False

    def capacity_for_count(self) -> float:
        """Amp-hours used to turn charge into a percent. The learned value once we have one."""
        if self.learned_ah and self.learned_ah > 0:
            return self.learned_ah
        return self.cfg.capacity_ah

    def update(self, sample: dict, now: float) -> None:
        current = _opt_float(sample.get("current_a"))
        voltage = _opt_float(sample.get("voltage_v"))
        charge = _opt_float(sample.get("charge_ah"))
        alerts = list(sample.get("alerts") or [])
        delta = self._charge_delta(charge, alerts)
        if delta:
            self.counted_ah += delta
            if delta > 0:
                self.discharged_ah += delta
        if self.soc is None and voltage is not None and self.cfg.cells > 0:
            self.soc = ocv_percent(self.cfg.chemistry, voltage / self.cfg.cells)
        if delta and self.soc is not None:
            cap = self.capacity_for_count()
            if cap > 0:
                self.soc = _clamp(self.soc - delta / cap * 100.0, 0.0, 100.0)
        self._note_rest(now, current, voltage)

    def _charge_delta(self, charge: float | None, alerts: list[str]) -> float:
        if charge is None:
            return 0.0
        previous = self._last_charge_ah
        self._last_charge_ah = charge
        if previous is None:
            return 0.0
        if "charge_overflow" in alerts or "math_overflow" in alerts:
            return 0.0
        delta = charge - previous
        if self._register_reset(previous, charge, delta):
            return 0.0
        return delta

    def _register_reset(self, previous: float, charge: float, delta: float) -> bool:
        """A power loss zeros the register. That step backwards is not a real charge."""
        if delta >= 0:
            return False
        drop = -delta
        return drop >= max(1.0, self.cfg.capacity_ah * 0.25) and charge <= max(0.2, previous * 0.25)

    def _note_rest(self, now: float, current: float | None, voltage: float | None) -> None:
        resting = current is not None and abs(current) <= self.cfg.rest_a and voltage is not None
        if not resting:
            self._rest_since = None
            self._rest_done = False
            return
        if self._rest_since is None:
            self._rest_since = now
        if self._rest_done or now - self._rest_since < self.cfg.rest_s:
            return
        self._rest_done = True
        new_soc = ocv_percent(self.cfg.chemistry, voltage / self.cfg.cells)
        self._learn(new_soc)
        self.soc = _clamp(new_soc, 0.0, 100.0)

    def _learn(self, new_soc: float) -> None:
        if self._anchor_soc is None or self._anchor_counted is None:
            self._anchor_soc = new_soc
            self._anchor_counted = self.counted_ah
            return
        dsoc = self._anchor_soc - new_soc
        used = self.counted_ah - self._anchor_counted
        if abs(dsoc) >= 30.0 and abs(used) >= 0.5 and used * dsoc > 0:
            learned = abs(used) / (abs(dsoc) / 100.0)
            cap = self.cfg.capacity_ah
            if cap * 0.3 <= learned <= cap * 1.3:
                if self.learned_ah is None:
                    self.learned_ah = learned
                else:
                    self.learned_ah = 0.7 * self.learned_ah + 0.3 * learned
        self._anchor_soc = new_soc
        self._anchor_counted = self.counted_ah

    def health_pct(self) -> float | None:
        if self.learned_ah is None or self.cfg.capacity_ah <= 0:
            return None
        return _clamp(self.learned_ah / self.cfg.capacity_ah * 100.0, 0.0, 100.0)

    def cycles(self) -> float:
        if self.cfg.capacity_ah <= 0:
            return 0.0
        return self.discharged_ah / self.cfg.capacity_ah

    def time_remaining_min(self, current: float | None) -> float | None:
        if self.soc is None or current is None:
            return None
        cap = self.capacity_for_count()
        if cap <= 0 or abs(current) <= self.cfg.idle_a:
            return None
        if current > 0:
            return (self.soc / 100.0) * cap / current * 60.0
        return ((100.0 - self.soc) / 100.0) * cap / abs(current) * 60.0

    def charging(self, current: float | None) -> bool | None:
        if current is None:
            return None
        return current < -self.cfg.idle_a

    def public(self, sample: dict | None) -> dict:
        """The API object. sample None means the chip is not being read: live numbers stay empty."""
        current = voltage = power = energy = charge = temp = None
        alerts: list[str] = []
        if sample is not None:
            current = _opt_float(sample.get("current_a"))
            voltage = _opt_float(sample.get("voltage_v"))
            power = _opt_float(sample.get("power_w"))
            energy = _opt_float(sample.get("energy_wh"))
            charge = _opt_float(sample.get("charge_ah"))
            temp = _opt_float(sample.get("temperature_c"))
            alerts = list(sample.get("alerts") or [])
        cell = None
        if voltage is not None and self.cfg.cells > 0:
            cell = voltage / self.cfg.cells
        percent = None if sample is None or self.soc is None else self.soc
        return {
            "voltage_v": _round(voltage, 3),
            "current_a": _round(current, 3),
            "power_w": _round(power, 2),
            "energy_wh": _round(energy, 3),
            "charge_ah": _round(charge, 4),
            "temperature_c": _round(temp, 2),
            "percent": _round(percent, 1),
            "charging": self.charging(current) if sample is not None else None,
            "time_remaining_min": _round(self.time_remaining_min(current) if sample is not None else None, 1),
            "cell_voltage_v": _round(cell, 3),
            "cells": int(self.cfg.cells),
            "chemistry": self.cfg.chemistry,
            "capacity_ah": _round(self.cfg.capacity_ah, 3),
            "health_pct": _round(self.health_pct(), 1),
            "health_estimated": True,
            "cycles": _round(self.cycles(), 2),
            "cycles_estimated": True,
            "alerts": alerts,
        }

    def dump(self) -> dict:
        return {
            "soc": self.soc,
            "discharged_ah": self.discharged_ah,
            "counted_ah": self.counted_ah,
            "learned_ah": self.learned_ah,
            "anchor_soc": self._anchor_soc,
            "anchor_counted": self._anchor_counted,
            "last_charge_ah": self._last_charge_ah,
            "chemistry": self.cfg.chemistry,
            "cells": self.cfg.cells,
            "capacity_ah": self.cfg.capacity_ah,
            "cycle_life": self.cfg.cycle_life,
        }


def _opt_float(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def mock_sample(t: float, cfg: BatteryConfig) -> dict:
    """A pretend pack. Voltage and current wander; the charge register is an absolute accumulator.

    t is seconds since this monitor was created. The numbers stay in a normal range for the
    configured pack, around the open-circuit voltage of 76%.
    """
    current = 1.8 + 0.7 * math.sin(t / 2.5)
    if math.sin(t / 9.0) > 0.97:
        current = -1.5
    cell = ocv_volts(cfg.chemistry, 76.0) + 0.015 * math.sin(t / 3.0)
    voltage = cell * cfg.cells
    # Offset plus the integral of the discharge wave, in amp-hours. The offset is so the
    # tile is not stuck at zero; only the change from one read to the next moves the percent.
    charge_ah = 1.25 + (1.8 * t - 1.75 * math.cos(t / 2.5) + 1.75) / 3600.0
    energy_wh = 18.4 + (1.8 * t) * max(voltage, 0.0) / 3600.0
    return {
        "voltage_v": voltage,
        "current_a": current,
        "power_w": abs(voltage * current),
        "energy_wh": energy_wh,
        "charge_ah": charge_ah,
        "temperature_c": 28.5 + 1.2 * math.sin(t / 5.0),
        "alerts": [],
    }


def _load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


class BatteryMonitor:
    """Mock readings, or a background read of the INA228. One object per server."""

    def __init__(self, mock: bool, state_path: Path | None = None, clock=None, config: BatteryConfig | None = None,
                 reader=None):
        self.mock = bool(mock)
        self.cfg = config or BatteryConfig.from_env()
        self.clock = clock or time.monotonic
        self._t0 = self.clock()
        self.path = Path(state_path) if state_path is not None else default_state_path(self.mock)
        self._estimator = Estimator(self.cfg, _load_state(self.path))
        if self.mock and self._estimator.soc is None:
            # A sample the header can show immediately. A real chip waits for a voltage.
            self._estimator.soc = 76.0
            self._estimator.learned_ah = self.cfg.capacity_ah * 94.0 / 100.0
            self._estimator.discharged_ah = self.cfg.capacity_ah * 12.4
        self._reader_factory = reader if reader is not None else self._open_reader
        self._ina: Ina228 | None = None
        self._hw: dict | None = None
        self._logged = False
        self._saved_at = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if not self.mock:
            self._thread = threading.Thread(target=self._loop, name="battery", daemon=True)
            self._thread.start()

    def set_mock(self, mock: bool) -> None:
        """Follow Robot.mock. Turning the pretend robot off does not open the I2C bus by itself."""
        with self._lock:
            self.mock = bool(mock)

    def detail(self) -> dict:
        with self._lock:
            body = self._detail()
        return body

    def summary(self) -> dict:
        """The four fields the header already shows on every page."""
        body = self.detail()
        if body["source"] == "unavailable":
            return {"percent": None, "charging": None, "health": "unknown", "source": "unavailable"}
        percent = None if body["percent"] is None else int(round(body["percent"]))
        return {
            "percent": percent,
            "charging": body["charging"],
            "health": health_word(body["health_pct"]),
            "source": body["source"],
        }

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        with self._lock:
            if self._ina is not None:
                self._ina.close()
                self._ina = None
            self._save(force=True)

    def _detail(self) -> dict:
        now = self.clock()
        if self.mock:
            sample = mock_sample(now - self._t0, self.cfg)
            self._estimator.update(sample, now)
            source = "mock"
        elif self._hw is None:
            sample = None
            source = "unavailable"
        else:
            sample = self._hw
            self._estimator.update(sample, now)
            source = "ina228"
        body = self._estimator.public(sample)
        body["source"] = source
        body["updated_at"] = iso_now()
        self._save(force=False, now=now)
        return body

    def _save(self, force: bool, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        if not force and now - self._saved_at < 5.0:
            return
        self._saved_at = now
        try:
            _write_state(self.path, self._estimator.dump())
        except OSError as exc:
            log.warning("battery state was not saved: %s", exc)

    def _open_reader(self) -> Ina228:
        ina = Ina228(self.cfg.i2c_bus, self.cfg.i2c_addr, self.cfg.shunt_ohm, self.cfg.max_current_a)
        ina.open()
        return ina

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._poll()
            if self._stop.wait(1.0):
                return

    def _poll(self) -> None:
        try:
            if self._ina is None:
                self._ina = self._reader_factory()
            sample = self._ina.read()
            with self._lock:
                self._hw = sample
            self._logged = False
        except Exception as exc:  # noqa: BLE001 - a missing board must not stop the server
            with self._lock:
                self._hw = None
                if self._ina is not None:
                    self._ina.close()
                    self._ina = None
            if not self._logged:
                log.warning("battery monitor unavailable: %s", exc)
                self._logged = True
