"""INA228 scaling, the state-of-charge estimate, and /api/battery."""
import re

import pytest
from fastapi.testclient import TestClient

from robot_server.base import MockBase
from robot_server.battery import (
    ADC_CONFIG,
    API_FIELDS,
    REG_ADC_CONFIG,
    REG_CHARGE,
    REG_CONFIG,
    REG_CURRENT,
    REG_DEVICE_ID,
    REG_DIAG_ALRT,
    REG_DIETEMP,
    REG_ENERGY,
    REG_MANUFACTURER_ID,
    REG_POWER,
    REG_SHUNT_CAL,
    REG_VBUS,
    REG_VSHUNT,
    BatteryConfig,
    BatteryMonitor,
    Estimator,
    Ina228,
    choose_adcrange,
    current_lsb,
    decode_alerts,
    decode_charge_c,
    decode_current_a,
    decode_dietemp_c,
    decode_energy_j,
    decode_power_w,
    decode_vbus_v,
    decode_vshunt_v,
    OCV_TABLES,
    ocv_percent,
    ocv_volts,
    parse_addr,
    shunt_cal,
)
from robot_server.config import Config
from robot_server.main import create_app

LSB = current_lsb(204.8)
_UPDATED = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _u24(counts_20: int) -> int:
    return (counts_20 << 4) & 0xFFFFFF


def _be(value: int, n: int) -> list[int]:
    return [(value >> (8 * i)) & 0xFF for i in range(n - 1, -1, -1)]


class FakeBus:
    def __init__(self, regs: dict[int, list[int]]):
        self.regs = regs
        self.writes: list[tuple[int, list[int]]] = []

    def read_i2c_block_data(self, addr, reg, n):
        assert addr == 0x45
        return list(self.regs[reg][:n])

    def write_i2c_block_data(self, addr, reg, data):
        assert addr == 0x45
        self.regs[reg] = list(data)
        self.writes.append((reg, list(data)))


def _chip_regs() -> dict[int, list[int]]:
    """Register bytes for a known pack: 16 V, 10 mV shunt, 25 °C, 1 A, 10 W, 1 Wh, 1 Ah."""
    return {
        REG_MANUFACTURER_ID: _be(0x5449, 2),
        REG_DEVICE_ID: _be(0x2281, 2),
        REG_VBUS: _be(_u24(81920), 3),             # 16 V
        REG_VSHUNT: _be(_u24(128000), 3),          # 10 mV at ADCRANGE 1
        REG_DIETEMP: _be(3200, 2),                 # 25 °C
        REG_CURRENT: _be(_u24(2560), 3),           # 1 A
        REG_POWER: _be(8000, 3),                   # 10 W
        REG_ENERGY: _be(180000, 5),                # 3600 J = 1 Wh
        REG_CHARGE: _be(3600 * 2560, 5),           # 3600 C = 1 Ah
        REG_DIAG_ALRT: _be((1 << 5) | (1 << 2) | (1 << 1), 2),
    }


def test_shunt_cal_matches_the_matek_board():
    assert choose_adcrange(204.8, 0.0002) == 1
    assert choose_adcrange(500, 0.0002) == 0  # 100 mV needs the wider range
    assert shunt_cal(LSB, 0.0002, 1) == 4096
    assert shunt_cal(LSB, 0.0002, 0) == 1024
    assert LSB == pytest.approx(204.8 / 2**19)


def test_register_scaling_with_fake_values():
    assert decode_vbus_v(_u24(81920)) == pytest.approx(16.0, abs=1e-6)
    assert decode_vshunt_v(_u24(128000), 1) == pytest.approx(0.010, abs=1e-8)
    assert decode_vshunt_v(_u24(128000), 0) == pytest.approx(0.040, abs=1e-8)
    assert decode_dietemp_c(3200) == pytest.approx(25.0, abs=1e-6)
    assert decode_dietemp_c((-1280) & 0xFFFF) == pytest.approx(-10.0, abs=1e-6)
    assert decode_current_a(_u24(2560), LSB) == pytest.approx(1.0, abs=1e-6)
    assert decode_current_a(_u24(-2560), LSB) == pytest.approx(-1.0, abs=1e-6)
    assert decode_power_w(8000, LSB) == pytest.approx(10.0, abs=1e-4)
    assert decode_energy_j(180000, LSB) / 3600 == pytest.approx(1.0, abs=1e-6)
    assert decode_charge_c(3600 * 2560, LSB) / 3600 == pytest.approx(1.0, abs=1e-6)
    negative = (-3600 * 2560) & ((1 << 40) - 1)
    assert decode_charge_c(negative, LSB) / 3600 == pytest.approx(-1.0, abs=1e-6)
    # Bit 2 is conversion-ready, not a fault. The fault names stay in bit order.
    assert decode_alerts((1 << 5) | (1 << 2) | (1 << 1)) == ["bus_over_limit", "memory_checksum"]


def test_ina228_reads_a_fake_bus_and_programs_calibration():
    bus = FakeBus(_chip_regs())
    ina = Ina228(7, 0x45, 0.0002, 204.8, bus_obj=bus)
    ina.open()
    written = dict(bus.writes)
    assert written[REG_SHUNT_CAL] == [0x10, 0x00]  # 4096
    assert written[REG_CONFIG] == [0x00, 0x10]     # ADCRANGE = 1
    assert written[REG_ADC_CONFIG] == [(ADC_CONFIG >> 8) & 0xFF, ADC_CONFIG & 0xFF]
    reading = ina.read()
    assert reading["voltage_v"] == pytest.approx(16.0, abs=1e-4)
    assert reading["shunt_v"] == pytest.approx(0.010, abs=1e-6)
    assert reading["current_a"] == pytest.approx(1.0, abs=1e-4)
    assert reading["power_w"] == pytest.approx(10.0, abs=1e-3)
    assert reading["energy_wh"] == pytest.approx(1.0, abs=1e-4)
    assert reading["charge_ah"] == pytest.approx(1.0, abs=1e-4)
    assert reading["temperature_c"] == pytest.approx(25.0, abs=1e-4)
    assert reading["alerts"] == ["bus_over_limit", "memory_checksum"]
    bus.regs[REG_DEVICE_ID] = _be(0x2380, 2)
    with pytest.raises(OSError, match="not an INA228"):
        Ina228(7, 0x45, bus_obj=bus).open()


def test_address_accepts_decimal_and_hex():
    assert parse_addr("69") == 69
    assert parse_addr("0x45") == 69
    assert parse_addr("68") == 68
    assert parse_addr("0x44") == 68
    assert parse_addr("65") == 65
    assert parse_addr("0x41") == 65
    assert parse_addr("nope") == 69
    assert parse_addr("") == 69


def test_ocv_table_round_trip_and_chemistry_names():
    for chem in ("lipo", "li-ion", "lifepo4"):
        for _volts, percent in OCV_TABLES[chem]:
            volts = ocv_volts(chem, percent)
            assert ocv_percent(chem, volts) == pytest.approx(percent, abs=0.05)
    assert ocv_percent("lipo", 3.0) == 0
    assert ocv_percent("lipo", 4.5) == 100
    assert ocv_volts("LiPo", 50) == pytest.approx(ocv_volts("lipo", 50))
    assert ocv_volts("LiFePO4", 50) < ocv_volts("lipo", 50)
    # Midway between 70% (3.87 V) and 76% (3.90 V) on the LiPo table.
    assert ocv_percent("lipo", 3.885) == pytest.approx(73.0, abs=0.2)


def _cfg(**kw) -> BatteryConfig:
    base = dict(chemistry="lipo", cells=4, capacity_ah=10.0, rest_s=0.0, rest_a=0.15, idle_a=0.05)
    base.update(kw)
    return BatteryConfig(**base)


def _reading(**kw) -> dict:
    row = {"voltage_v": ocv_volts("lipo", 76) * 4, "current_a": 0.0, "charge_ah": 0.0,
           "power_w": 0.0, "energy_wh": 0.0, "temperature_c": 25.0, "alerts": []}
    row.update(kw)
    return row


def test_coulomb_count_anchored_by_resting_voltage():
    est = Estimator(_cfg())
    est.update(_reading(current_a=0.0, charge_ah=0.0), now=0)
    assert est.soc == pytest.approx(76.0, abs=0.1)
    # 1 Ah out of a 10 Ah pack, while the current is too high to re-anchor.
    est.update(_reading(current_a=5.0, charge_ah=1.0, voltage_v=15.0), now=10)
    body = est.public(_reading(current_a=5.0, charge_ah=1.0, voltage_v=15.0))
    assert body["percent"] == pytest.approx(66.0, abs=0.2)
    assert body["charging"] is False
    assert body["time_remaining_min"] == pytest.approx(79.2, abs=0.3)
    assert body["cell_voltage_v"] == pytest.approx(15.0 / 4, abs=1e-3)
    # Charging puts amp-hours back and the sign of the current flips.
    est.update(_reading(current_a=-2.0, charge_ah=0.5, voltage_v=15.2), now=20)
    body = est.public(_reading(current_a=-2.0, charge_ah=0.5, voltage_v=15.2))
    assert body["percent"] == pytest.approx(71.0, abs=0.2)
    assert body["charging"] is True
    assert body["time_remaining_min"] == pytest.approx(87.0, abs=0.5)
    # Sitting still replaces the count with the rested voltage (76% at 3.90 V/cell).
    est.update(_reading(current_a=0.0, charge_ah=0.5), now=40)
    assert est.soc == pytest.approx(76.0, abs=0.1)
    # A tiny current is not called charging, and there is no time estimate.
    idle = est.public(_reading(current_a=0.02, charge_ah=0.5))
    assert idle["charging"] is False
    assert idle["time_remaining_min"] is None


def test_register_reset_and_overflow_do_not_jump_the_percent():
    est = Estimator(_cfg(rest_s=100.0))  # no resting anchor during this test
    est.update(_reading(current_a=2.0, charge_ah=0.0, voltage_v=ocv_volts("lipo", 80) * 4), now=0)
    est.update(_reading(current_a=2.0, charge_ah=5.0, voltage_v=15.0), now=5)
    after = est.soc
    assert after == pytest.approx(30.0, abs=0.5)
    est.update(_reading(current_a=2.0, charge_ah=0.05, voltage_v=15.0), now=6)
    assert est.soc == pytest.approx(after, abs=0.01)
    est.update(_reading(current_a=2.0, charge_ah=3.0, voltage_v=15.0, alerts=["charge_overflow"]), now=7)
    assert est.soc == pytest.approx(after, abs=0.01)


def test_health_is_learned_full_capacity_and_cycles_count_discharged_ah():
    est = Estimator(_cfg())  # rest_s 0, so a quiet sample anchors immediately
    est.update(_reading(current_a=0.0, charge_ah=0.0, voltage_v=ocv_volts("lipo", 90) * 4), now=0)
    assert est.soc == pytest.approx(90.0, abs=0.1)
    assert est.health_pct() is None
    est.update(_reading(current_a=5.0, charge_ah=4.0, voltage_v=14.5), now=10)
    assert est.soc == pytest.approx(50.0, abs=0.5)
    est.update(_reading(current_a=0.0, charge_ah=4.0, voltage_v=ocv_volts("lipo", 50) * 4), now=30)
    assert est.soc == pytest.approx(50.0, abs=0.1)
    assert est.learned_ah == pytest.approx(10.0, abs=0.1)
    assert est.health_pct() == pytest.approx(100.0, abs=0.5)
    assert est.cycles() == pytest.approx(0.4, abs=0.02)
    body = est.public(_reading(current_a=0.0, charge_ah=4.0))
    assert body["health_estimated"] is True
    assert body["cycles_estimated"] is True


def test_mock_varies_and_a_missing_chip_is_unavailable(tmp_path):
    clock = {"t": 0.0}
    mock = BatteryMonitor(True, state_path=tmp_path / "m.json", clock=lambda: clock["t"])
    try:
        first = mock.detail()
        assert first["source"] == "mock"
        assert first["percent"] == pytest.approx(76.0, abs=0.2)
        assert first["charging"] is False
        assert first["health_pct"] == pytest.approx(94.0, abs=0.2)
        assert first["health_estimated"] is True
        assert first["cycles"] == pytest.approx(12.4, abs=0.05)
        assert first["cycles_estimated"] is True
        assert first["alerts"] == []
        assert _UPDATED.match(first["updated_at"])
        clock["t"] = 3.0
        second = mock.detail()
        assert second["current_a"] != first["current_a"]
        assert second["voltage_v"] != first["voltage_v"]
        assert second["temperature_c"] != first["temperature_c"]
    finally:
        mock.close()

    def missing():
        raise OSError("no device")

    bare = BatteryMonitor(False, state_path=tmp_path / "u.json", reader=missing)
    try:
        empty = bare.detail()
        assert empty["source"] == "unavailable"
        assert empty["voltage_v"] is None
        assert empty["current_a"] is None
        assert empty["percent"] is None
        assert empty["charging"] is None
        assert empty["health_estimated"] is True
        assert empty["cycles_estimated"] is True
        assert empty["alerts"] == []
        assert set(empty) == set(API_FIELDS)
    finally:
        bare.close()


def test_saved_cycles_come_back(tmp_path):
    path = tmp_path / "saved.json"
    cfg = _cfg()
    est = Estimator(cfg)
    est.soc = 40.0
    est.discharged_ah = 30.0
    est.learned_ah = 8.0
    from robot_server.battery import _write_state
    _write_state(path, est.dump())
    again = BatteryMonitor(True, state_path=path, clock=lambda: 0.0, config=cfg)
    try:
        body = again.detail()
        assert body["percent"] == pytest.approx(40.0, abs=0.2)
        assert body["cycles"] == pytest.approx(3.0, abs=0.02)
        assert body["health_pct"] == pytest.approx(80.0, abs=0.2)
    finally:
        again.close()


def _login(client, pin="4321"):
    r = client.post("/api/login", json={"pin": pin}, headers={"origin": "http://testserver"})
    assert r.status_code == 200, r.text


def test_api_battery_shape_and_pages_link_to_it(monkeypatch):
    monkeypatch.setenv("KIRO_BATT_CHEMISTRY", "li-ion")
    monkeypatch.setenv("KIRO_BATT_CELLS", "6")
    monkeypatch.setenv("KIRO_BATT_CAPACITY_AH", "8")
    with TestClient(create_app(Config(pin="4321", backend="mock"), MockBase())) as client:
        assert client.get("/api/battery").status_code == 401
        _login(client)
        body = client.get("/api/battery").json()
        assert set(body) == set(API_FIELDS)
        assert body["source"] == "mock"
        assert body["chemistry"] == "li-ion"
        assert body["cells"] == 6
        assert body["capacity_ah"] == 8
        assert body["health_estimated"] is True
        assert body["cycles_estimated"] is True
        assert isinstance(body["alerts"], list)
        assert isinstance(body["percent"], float)
        assert 0 <= body["percent"] <= 100
        assert body["voltage_v"] > 0
        assert _UPDATED.match(body["updated_at"])
        with client.websocket_connect("/ws?name=Battery") as ws:
            for _ in range(30):
                msg = ws.receive_json()
                if msg.get("t") == "robot":
                    break
            else:
                raise AssertionError("robot snapshot not received")
        icon = msg["battery"]
        assert set(icon) == {"percent", "charging", "health", "source"}
        assert icon["source"] == "mock"
        assert isinstance(icon["percent"], int)
        for path in ("/", "/dashboard.html", "/studio.html", "/battery.html"):
            page = client.get(path).text
            assert 'href="battery.html"' in page
        assert 'data-i18n="bat.manage"' in client.get("/battery.html").text
